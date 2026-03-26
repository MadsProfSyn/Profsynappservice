"""
Lightweight Route Optimizer for Pre-Assigned Inspections

This module solves ONLY the routing problem (TSP) when inspections have already
been assigned to inspectors by the user via drag & drop UI.

UPDATED: Now works directly with monday_items_selected table (not inspection_queue)
UPDATED: Uses cached Mapbox distance_km for accurate route totals
UPDATED: Supports existing_ids - inspections that are already scheduled and should keep their times
UPDATED: Calls Mapbox Directions API on cache miss (no more Haversine fallback)
UPDATED: Supports fixed_stops - booked shifts passed by coordinates (Option B)
UPDATED: Supports DYMO (+X min, IF/PS only) and Cylinderskift (+X min, IF/PS/FF) duration adjustments
UPDATED: Modifier values read from inspection_duration_modifiers table (not hardcoded)
UPDATED: Default start time changed to 08:30
UPDATED: Manual duration_minutes override - if set on monday_items_selected, always wins
UPDATED: Note field passed through from monday_items_selected to route stop output
UPDATED: Cylinderskift now applies to FF inspections (previously IF/PS only)
UPDATED: fixed_order flag - when True, skips TSP and uses inspection_ids order exactly as provided

Expected performance: <2 seconds for typical workloads (2-5 inspectors, 3-7 inspections each)
"""

import os
import math
import uuid
from datetime import datetime, timedelta
from itertools import permutations
from typing import List, Dict, Tuple, Optional
import pytz
import requests
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
MAPBOX_TOKEN = os.getenv("MAPBOX_TOKEN")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY environment variables")

if not MAPBOX_TOKEN:
    print("⚠️ WARNING: MAPBOX_TOKEN not set - will use Haversine estimates on cache miss")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance in km between two coordinates (straight line)"""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def estimate_travel_minutes(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Estimate travel time in minutes based on distance.
    Uses speed tiers: urban (<8km) = 25 km/h, suburban (8-20km) = 35 km/h, highway (20+km) = 65 km/h
    NOTE: This is only used as last-resort fallback if Mapbox API fails.
    """
    if lat1 == lat2 and lng1 == lng2:
        return 0.0

    km = haversine_km(lat1, lng1, lat2, lng2)

    if km <= 1.0:
        speed_kmh = 25.0
    elif km <= 8.0:
        speed_kmh = 25.0
    elif km <= 20.0:
        speed_kmh = 35.0
    else:
        speed_kmh = 65.0

    minutes = (km / speed_kmh) * 60.0
    return max(5.0, minutes)


def make_cache_key(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> str:
    """Create standardized cache key with 5 decimal precision"""
    from_lng_r = round(from_lng, 5)
    from_lat_r = round(from_lat, 5)
    to_lng_r = round(to_lng, 5)
    to_lat_r = round(to_lat, 5)
    return f"{from_lng_r:.5f},{from_lat_r:.5f}->{to_lng_r:.5f},{to_lat_r:.5f}"


def fetch_mapbox_directions(from_lat: float, from_lng: float,
                            to_lat: float, to_lng: float) -> Tuple[Optional[float], Optional[float]]:
    """
    Call Mapbox Directions API to get real driving time and distance.
    Returns (minutes, km) tuple, or (None, None) if API call fails.
    """
    if not MAPBOX_TOKEN:
        return None, None

    url = f"https://api.mapbox.com/directions/v5/mapbox/driving/{from_lng},{from_lat};{to_lng},{to_lat}"
    params = {
        'access_token': MAPBOX_TOKEN,
        'overview': 'false'
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get('routes') and len(data['routes']) > 0:
            route = data['routes'][0]
            minutes = route['duration'] / 60.0
            km = route['distance'] / 1000.0
            print(f"  🗺️ Mapbox API: {minutes:.1f} min, {km:.1f} km")
            return minutes, km
        else:
            print(f"  ⚠️ Mapbox API returned no routes")
            return None, None

    except requests.exceptions.Timeout:
        print(f"  ⚠️ Mapbox API timeout")
        return None, None
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Mapbox API error: {e}")
        return None, None
    except Exception as e:
        print(f"  ❌ Mapbox API unexpected error: {e}")
        return None, None


def get_cached_travel_data(from_lat: float, from_lng: float,
                           to_lat: float, to_lng: float) -> Tuple[float, float]:
    """
    Get cached travel time (minutes) and distance (km) from Mapbox cache.
    On cache miss, calls Mapbox Directions API directly and caches the result.
    Only falls back to Haversine estimates if Mapbox API also fails.
    """
    if from_lat == to_lat and from_lng == to_lng:
        return 0.0, 0.0

    key = make_cache_key(from_lat, from_lng, to_lat, to_lng)

    # Step 1: Check cache
    try:
        result = supabase.table('mapbox_travel_cache')\
            .select('minutes, distance_km')\
            .eq('key', key)\
            .execute()

        if result.data and len(result.data) > 0:
            row = result.data[0]
            cached_minutes = float(row['minutes']) if row.get('minutes') is not None else None
            cached_km = float(row['distance_km']) if row.get('distance_km') is not None else None

            if cached_minutes is not None:
                if cached_km is None:
                    cached_km = haversine_km(from_lat, from_lng, to_lat, to_lng) * 1.3
                print(f"  ✅ Cache HIT: {key} = {cached_minutes} min, {cached_km:.1f} km")
                return max(5.0, cached_minutes), cached_km

        print(f"  ⚠️ Cache MISS: {key}")
    except Exception as e:
        print(f"  ❌ Cache error: {e}")

    # Step 2: Cache miss - call Mapbox API
    mapbox_minutes, mapbox_km = fetch_mapbox_directions(from_lat, from_lng, to_lat, to_lng)

    if mapbox_minutes is not None and mapbox_km is not None:
        try:
            supabase.table('mapbox_travel_cache').upsert({
                'key': key,
                'minutes': mapbox_minutes,
                'distance_km': mapbox_km,
                'updated_at': datetime.utcnow().isoformat()
            }, on_conflict='key').execute()
            print(f"  💾 Cached new route: {key} = {mapbox_minutes:.1f} min, {mapbox_km:.1f} km")
        except Exception as e:
            print(f"  ⚠️ Failed to cache: {e}")

        return max(5.0, mapbox_minutes), mapbox_km

    # Step 3: Last resort fallback - Haversine estimates
    print(f"  ⚠️ Using Haversine fallback for: {key}")
    est_minutes = estimate_travel_minutes(from_lat, from_lng, to_lat, to_lng)
    est_km = haversine_km(from_lat, from_lng, to_lat, to_lng) * 1.3
    return est_minutes, est_km


def get_cached_travel_time(from_lat: float, from_lng: float,
                           to_lat: float, to_lng: float) -> float:
    """Get cached travel time or return estimate (minutes). For backwards compatibility."""
    minutes, _ = get_cached_travel_data(from_lat, from_lng, to_lat, to_lng)
    return minutes


def get_cached_distance_km(from_lat: float, from_lng: float,
                           to_lat: float, to_lng: float) -> float:
    """Get cached distance or return estimate (km)."""
    _, km = get_cached_travel_data(from_lat, from_lng, to_lat, to_lng)
    return km


def round_to_nearest_5_min(dt: datetime) -> datetime:
    """Round datetime UP to nearest 5 minutes."""
    discard = timedelta(minutes=dt.minute % 5, seconds=dt.second, microseconds=dt.microsecond)
    if discard:
        dt += timedelta(minutes=5) - discard
    return dt.replace(second=0, microsecond=0)


def time_str_to_minutes(time_str: str) -> int:
    """Convert time string (HH:MM or HH:MM:SS) to minutes from midnight"""
    try:
        parts = time_str.split(':')
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        return hours * 60 + minutes
    except (ValueError, IndexError):
        return 8 * 60 + 30  # Default 08:30


# ============================================================================
# MODIFIER LOOKUP
# ============================================================================

def get_modifier_minutes(modifier_name: str, fallback: int) -> int:
    """
    Read a bonus modifier value from the inspection_duration_modifiers table.
    Falls back to the hardcoded default if the table is unavailable or the
    row does not exist.

    Expected rows: name='DYMO' and name='Cylinderskift'
    Verify with: SELECT name, minutes FROM inspection_duration_modifiers;
    """
    try:
        result = supabase.table('inspection_duration_modifiers')\
            .select('minutes')\
            .eq('name', modifier_name)\
            .execute()
        if result.data and len(result.data) > 0:
            return int(result.data[0]['minutes'])
    except Exception as e:
        print(f"  ⚠️ Could not fetch modifier '{modifier_name}': {e}")
    print(f"  ⚠️ Using fallback modifier: {modifier_name} = {fallback} min")
    return fallback


# ============================================================================
# INSPECTION TYPE TO DURATION MAPPING
# ============================================================================

def get_inspection_duration(inspection_type: str, rooms: int,
                            has_dymo: bool = False, has_cylinderskift: bool = False) -> int:
    """
    Get inspection duration in minutes based on type, room count, and extras.

    Modifier rules:
    - DYMO:          +X min — applies to IF and PS only
    - Cylinderskift: +X min — applies to IF, PS, and FF

    Modifier values are read live from inspection_duration_modifiers table.
    Fallbacks: DYMO=5, Cylinderskift=10.

    NOTE: This function is only called when there is NO manual duration_minutes
    override on the monday_items_selected row. Manual overrides bypass this
    entirely and are used as-is with no extras added on top.
    """
    type_mapping = {
        'Proforma': 'PA',
        'Projektsyn': 'PS',
        'Indflytningssyn': 'IF',
        'Fraflytningssyn': 'FF'
    }

    abbrev = type_mapping.get(inspection_type)
    if not abbrev:
        print(f"  ⚠️ Unknown inspection type: {inspection_type}, using default 45 min")
        return 45

    # Get base duration from database
    duration = None
    try:
        result = supabase.table('inspection_durations')\
            .select('minutes')\
            .eq('inspection_type', abbrev)\
            .eq('rooms', rooms)\
            .execute()

        if result.data and len(result.data) > 0:
            duration = result.data[0]['minutes']
    except Exception as e:
        print(f"  ⚠️ Error fetching duration: {e}")

    # Fallback defaults if not found in database
    if duration is None:
        defaults = {'PA': 30, 'PS': 45, 'IF': 45, 'FF': 60}
        duration = defaults.get(abbrev, 45)
        print(f"  ⚠️ Using fallback duration: {duration} min for {abbrev}")

    # ── Modifier: DYMO — IF and PS only ─────────────────────────────────────
    if abbrev in ['IF', 'PS'] and has_dymo:
        dymo_mins = get_modifier_minutes('DYMO', 5)
        duration += dymo_mins
        print(f"  📌 +{dymo_mins} min for DYMO")

    # ── Modifier: Cylinderskift — IF, PS, and FF ─────────────────────────────
    if abbrev in ['IF', 'PS', 'FF'] and has_cylinderskift:
        cyl_mins = get_modifier_minutes('Cylinderskift', 10)
        duration += cyl_mins
        print(f"  🔧 +{cyl_mins} min for Cylinderskift")

    return duration


# ============================================================================
# TSP SOLVER - Finds optimal route order
# ============================================================================

def solve_tsp_bruteforce(
    home_coords: Tuple[float, float],
    stop_coords: List[Tuple[float, float]],
    stop_ids: List[int]
) -> Tuple[List[int], float]:
    """Solve TSP via brute force for small number of stops (≤7)."""
    if len(stop_coords) == 0:
        return [], 0.0

    if len(stop_coords) == 1:
        km_to = get_cached_distance_km(home_coords[0], home_coords[1], stop_coords[0][0], stop_coords[0][1])
        km_back = get_cached_distance_km(stop_coords[0][0], stop_coords[0][1], home_coords[0], home_coords[1])
        return [stop_ids[0]], km_to + km_back

    best_order = None
    best_distance = float('inf')

    for perm in permutations(range(len(stop_coords))):
        total_km = 0.0

        first_idx = perm[0]
        total_km += get_cached_distance_km(
            home_coords[0], home_coords[1],
            stop_coords[first_idx][0], stop_coords[first_idx][1]
        )

        for i in range(len(perm) - 1):
            from_idx = perm[i]
            to_idx = perm[i + 1]
            total_km += get_cached_distance_km(
                stop_coords[from_idx][0], stop_coords[from_idx][1],
                stop_coords[to_idx][0], stop_coords[to_idx][1]
            )

        last_idx = perm[-1]
        total_km += get_cached_distance_km(
            stop_coords[last_idx][0], stop_coords[last_idx][1],
            home_coords[0], home_coords[1]
        )

        if total_km < best_distance:
            best_distance = total_km
            best_order = [stop_ids[idx] for idx in perm]

    return best_order, best_distance


def solve_tsp_nearest_neighbor(
    home_coords: Tuple[float, float],
    stop_coords: List[Tuple[float, float]],
    stop_ids: List[int]
) -> Tuple[List[int], float]:
    """Solve TSP via nearest neighbor heuristic for larger stop counts."""
    if len(stop_coords) == 0:
        return [], 0.0

    remaining = list(range(len(stop_coords)))
    route_indices = []
    total_km = 0.0
    current_lat, current_lng = home_coords

    while remaining:
        best_idx = None
        best_dist = float('inf')

        for idx in remaining:
            dist = get_cached_distance_km(current_lat, current_lng,
                                          stop_coords[idx][0], stop_coords[idx][1])
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

        route_indices.append(best_idx)
        total_km += best_dist
        current_lat, current_lng = stop_coords[best_idx]
        remaining.remove(best_idx)

    total_km += get_cached_distance_km(current_lat, current_lng, home_coords[0], home_coords[1])
    return [stop_ids[idx] for idx in route_indices], total_km


def solve_tsp(
    home_coords: Tuple[float, float],
    stop_coords: List[Tuple[float, float]],
    stop_ids: List[int]
) -> Tuple[List[int], float]:
    """Solve TSP - picks algorithm based on stop count."""
    if len(stop_coords) <= 7:
        return solve_tsp_bruteforce(home_coords, stop_coords, stop_ids)
    else:
        return solve_tsp_nearest_neighbor(home_coords, stop_coords, stop_ids)


# ============================================================================
# OPTION B: TSP with custom starting point (for fixed_stops support)
# ============================================================================

def solve_tsp_from_point(
    start_coords: Tuple[float, float],
    home_coords: Tuple[float, float],
    stop_coords: List[Tuple[float, float]],
    stop_ids: List[int]
) -> Tuple[List[int], float]:
    """
    Solve TSP starting from a specific point (not home).
    Route: start_coords → stops (in optimal order) → home
    Used when there are fixed_stops — we start from the last fixed stop.
    """
    if len(stop_coords) == 0:
        return [], 0.0

    if len(stop_coords) == 1:
        km_to = get_cached_distance_km(start_coords[0], start_coords[1], stop_coords[0][0], stop_coords[0][1])
        km_back = get_cached_distance_km(stop_coords[0][0], stop_coords[0][1], home_coords[0], home_coords[1])
        return [stop_ids[0]], km_to + km_back

    if len(stop_coords) <= 7:
        best_order = None
        best_distance = float('inf')

        for perm in permutations(range(len(stop_coords))):
            total_km = 0.0

            first_idx = perm[0]
            total_km += get_cached_distance_km(
                start_coords[0], start_coords[1],
                stop_coords[first_idx][0], stop_coords[first_idx][1]
            )

            for i in range(len(perm) - 1):
                from_idx = perm[i]
                to_idx = perm[i + 1]
                total_km += get_cached_distance_km(
                    stop_coords[from_idx][0], stop_coords[from_idx][1],
                    stop_coords[to_idx][0], stop_coords[to_idx][1]
                )

            last_idx = perm[-1]
            total_km += get_cached_distance_km(
                stop_coords[last_idx][0], stop_coords[last_idx][1],
                home_coords[0], home_coords[1]
            )

            if total_km < best_distance:
                best_distance = total_km
                best_order = [stop_ids[idx] for idx in perm]

        return best_order, best_distance
    else:
        remaining = list(range(len(stop_coords)))
        route_indices = []
        total_km = 0.0
        current_lat, current_lng = start_coords

        while remaining:
            best_idx = None
            best_dist = float('inf')

            for idx in remaining:
                dist = get_cached_distance_km(current_lat, current_lng,
                                              stop_coords[idx][0], stop_coords[idx][1])
                if dist < best_dist:
                    best_dist = dist
                    best_idx = idx

            route_indices.append(best_idx)
            total_km += best_dist
            current_lat, current_lng = stop_coords[best_idx]
            remaining.remove(best_idx)

        total_km += get_cached_distance_km(current_lat, current_lng, home_coords[0], home_coords[1])
        return [stop_ids[idx] for idx in route_indices], total_km


# ============================================================================
# DATA FETCHING
# ============================================================================

def fetch_inspector_data(inspector_id: str, date: str) -> Optional[Dict]:
    """Fetch inspector's home location and availability for date"""

    result = supabase.table('inspectors')\
        .select('id, full_name, address, lat, lng')\
        .eq('id', inspector_id)\
        .execute()

    if not result.data or len(result.data) == 0:
        return None

    inspector = result.data[0]

    if not inspector.get('lat') or not inspector.get('lng'):
        return None

    avail_result = supabase.table('supabase_availability')\
        .select('start_time_local, end_time_local')\
        .eq('inspector_id', inspector_id)\
        .eq('date_local', date)\
        .eq('is_available', True)\
        .execute()

    start_time = '08:30:00'
    end_time = '17:00:00'

    if avail_result.data and len(avail_result.data) > 0:
        avail = avail_result.data[0]
        if avail.get('start_time_local') and str(avail['start_time_local']).lower() != 'none':
            start_time = avail['start_time_local']
        if avail.get('end_time_local') and str(avail['end_time_local']).lower() != 'none':
            end_time = avail['end_time_local']

    capacity_result = supabase.table('inspector_capacity_view')\
        .select('shift_details, booked_minutes, remaining_minutes')\
        .eq('inspector_id', inspector_id)\
        .eq('date_local', date)\
        .execute()

    existing_shifts = []
    latest_shift_end_min = 0

    if capacity_result.data and len(capacity_result.data) > 0:
        capacity = capacity_result.data[0]
        if capacity.get('shift_details'):
            existing_shifts = capacity['shift_details']

            for shift in existing_shifts:
                if shift.get('end_time'):
                    try:
                        shift_end = datetime.strptime(shift['end_time'], '%H:%M:%S').time()
                        shift_end_min = shift_end.hour * 60 + shift_end.minute
                        latest_shift_end_min = max(latest_shift_end_min, shift_end_min)
                    except (ValueError, TypeError):
                        pass

    try:
        st = datetime.strptime(start_time, '%H:%M:%S').time()
        start_min = st.hour * 60 + st.minute
    except (ValueError, TypeError):
        start_min = 8 * 60 + 30

    if latest_shift_end_min > start_min:
        start_min = latest_shift_end_min + 15

    start_min = max(8 * 60 + 30, start_min)

    return {
        'id': inspector['id'],
        'full_name': inspector['full_name'],
        'home_address': inspector.get('address', ''),
        'home_lat': inspector['lat'],
        'home_lng': inspector['lng'],
        'available_start_min': start_min,
        'available_end_time': end_time,
        'existing_shifts': existing_shifts
    }


def fetch_monday_items(item_ids: List[int], include_scheduled: bool = False) -> List[Dict]:
    """
    Fetch inspection details from monday_items_selected table.

    Duration priority (highest to lowest):
      1. duration_minutes column (manual override) — always wins, no modifiers added on top
      2. get_inspection_duration() — derived from synstype + antal_vaerelser + modifier flags

    Note field is passed through as-is for display in the frontend.

    IMPORTANT: Returns rows in the same order as item_ids. This is critical when
    fixed_order=True so that the caller's intended sequence is preserved.
    """
    if not item_ids:
        return []

    print(f"  📋 Fetching {len(item_ids)} items from monday_items_selected...")

    select_fields = (
        'id, adresse, synstype, antal_vaerelser, lat, lng, dato_tid, '
        'has_dymo, has_cylinderskift, duration_minutes, note'
    )
    if include_scheduled:
        select_fields += ', scheduled_start_time, scheduled_end_time'

    result = supabase.table('monday_items_selected')\
        .select(select_fields)\
        .in_('id', item_ids)\
        .execute()

    # Build a lookup so we can re-order to match item_ids
    items_by_id = {}
    for item in (result.data or []):
        inspection_type = item.get('synstype', 'Indflytningssyn')
        rooms = item.get('antal_vaerelser') or 3
        has_dymo = item.get('has_dymo', False) or False
        has_cylinderskift = item.get('has_cylinderskift', False) or False

        # ── Duration resolution ──────────────────────────────────────────────
        manual_duration = item.get('duration_minutes')
        if manual_duration and int(manual_duration) > 0:
            duration = int(manual_duration)
            duration_is_manual = True
            print(f"  ✏️ Manual duration override: {duration} min for item {item['id']} ({item.get('adresse', '')[:30]})")
        else:
            duration = get_inspection_duration(inspection_type, rooms, has_dymo, has_cylinderskift)
            duration_is_manual = False
        # ────────────────────────────────────────────────────────────────────

        if not item.get('lat') or not item.get('lng'):
            print(f"  ⚠️ Skipping item {item['id']} — missing coordinates ({item.get('adresse', '')})")
            continue

        ins_data = {
            'id': item['id'],
            'address': item.get('adresse', 'Ukendt adresse'),
            'inspection_type': inspection_type,
            'rooms': rooms,
            'lat': item['lat'],
            'lng': item['lng'],
            'duration_minutes': duration,
            'duration_is_manual': duration_is_manual,
            'preferred_date': item.get('dato_tid'),
            'has_dymo': has_dymo,
            'has_cylinderskift': has_cylinderskift,
            'note': item.get('note') or None,
        }

        if include_scheduled:
            ins_data['scheduled_start_time'] = item.get('scheduled_start_time')
            ins_data['scheduled_end_time'] = item.get('scheduled_end_time')

        items_by_id[item['id']] = ins_data

    # Return in the same order as item_ids (critical for fixed_order mode)
    inspections = []
    missing_coords = []
    for id_ in item_ids:
        if id_ in items_by_id:
            inspections.append(items_by_id[id_])
        else:
            missing_coords.append(f"ID: {id_}")

    if missing_coords:
        print(f"  ⚠️ Skipping {len(missing_coords)} items without coordinates:")
        for addr in missing_coords[:5]:
            print(f"      - {addr}")
        if len(missing_coords) > 5:
            print(f"      ... and {len(missing_coords) - 5} more")

    print(f"  ✅ Loaded {len(inspections)} items with coordinates")
    return inspections


# ============================================================================
# OPTION B: Parse and validate fixed_stops
# ============================================================================

def parse_fixed_stops(fixed_stops_raw: List[Dict]) -> List[Dict]:
    """
    Parse and validate fixed_stops from the request.
    Fixed stops are booked shifts passed by coordinates instead of monday_item_id.
    """
    if not fixed_stops_raw:
        return []

    valid_stops = []
    for i, stop in enumerate(fixed_stops_raw):
        lat = stop.get('lat')
        lng = stop.get('lng')
        start_time = stop.get('start_time', '')
        end_time = stop.get('end_time', '')

        if lat is None or lng is None:
            print(f"  ⚠️ Fixed stop {i} missing coordinates, skipping")
            continue

        if not start_time or not end_time:
            print(f"  ⚠️ Fixed stop {i} missing times, skipping")
            continue

        valid_stops.append({
            'lat': float(lat),
            'lng': float(lng),
            'start_time': start_time[:5] if len(start_time) > 5 else start_time,
            'end_time': end_time[:5] if len(end_time) > 5 else end_time,
            'address': stop.get('address', 'Booked shift'),
            'type': stop.get('type', 'B'),
            'is_fixed': True
        })

    valid_stops.sort(key=lambda x: time_str_to_minutes(x['start_time']))
    return valid_stops


# ============================================================================
# OPTION B: Schedule new inspections after fixed stops
# ============================================================================

def schedule_after_fixed_stops(
    inspector: Dict,
    fixed_stops: List[Dict],
    new_inspections: List[Dict],
    home_coords: Tuple[float, float],
    day_midnight: datetime
) -> Tuple[List[Dict], float]:
    """
    Schedule new inspections starting after the last fixed stop.
    Returns: (route_stops, total_km)
    """
    route_stops = []
    total_km = 0.0

    prev_coords = home_coords
    for seq, stop in enumerate(fixed_stops, start=1):
        stop_coords = (stop['lat'], stop['lng'])

        leg_km = get_cached_distance_km(prev_coords[0], prev_coords[1], stop_coords[0], stop_coords[1])
        total_km += leg_km

        travel_min = 0 if seq == 1 else int(round(get_cached_travel_time(
            prev_coords[0], prev_coords[1], stop_coords[0], stop_coords[1]
        )))

        route_stops.append({
            'sequence': seq,
            'monday_item_id': None,
            'address': stop['address'],
            'inspection_type': stop.get('type', 'B'),
            'rooms': None,
            'start_time': stop['start_time'],
            'end_time': stop['end_time'],
            'duration_minutes': time_str_to_minutes(stop['end_time']) - time_str_to_minutes(stop['start_time']),
            'duration_is_manual': False,
            'travel_from_previous_mins': travel_min,
            'distance_from_previous_km': round(leg_km, 1),
            'is_existing': True,
            'is_fixed': True,
            'note': None,
        })

        prev_coords = stop_coords

    if not new_inspections:
        if fixed_stops:
            last_coords = (fixed_stops[-1]['lat'], fixed_stops[-1]['lng'])
            total_km += get_cached_distance_km(last_coords[0], last_coords[1], home_coords[0], home_coords[1])
        return route_stops, total_km

    if fixed_stops:
        last_fixed = fixed_stops[-1]
        start_coords = (last_fixed['lat'], last_fixed['lng'])
        current_min = time_str_to_minutes(last_fixed['end_time'])
        print(f"  📍 Starting new inspections from last fixed stop: {last_fixed['address'][:30]} at {last_fixed['end_time']}")
    else:
        start_coords = home_coords
        current_min = inspector['available_start_min']

    stop_coords = [(ins['lat'], ins['lng']) for ins in new_inspections]
    stop_ids = [ins['id'] for ins in new_inspections]
    inspection_by_id = {ins['id']: ins for ins in new_inspections}

    optimal_order, new_route_km = solve_tsp_from_point(start_coords, home_coords, stop_coords, stop_ids)
    total_km += new_route_km

    prev_coords = start_coords
    base_seq = len(route_stops)

    for seq_offset, inspection_id in enumerate(optimal_order, start=1):
        ins = inspection_by_id[inspection_id]
        ins_coords = (ins['lat'], ins['lng'])

        travel_min, leg_km = get_cached_travel_data(
            prev_coords[0], prev_coords[1],
            ins_coords[0], ins_coords[1]
        )
        travel_min = int(round(travel_min))
        current_min += travel_min

        start_dt = day_midnight + timedelta(minutes=current_min)
        start_dt = round_to_nearest_5_min(start_dt)
        current_min = (start_dt - day_midnight).seconds // 60

        duration = ins['duration_minutes']
        end_min = current_min + duration
        end_dt = day_midnight + timedelta(minutes=end_min)

        route_stops.append({
            'sequence': base_seq + seq_offset,
            'monday_item_id': ins['id'],
            'address': ins['address'],
            'inspection_type': ins['inspection_type'],
            'rooms': ins['rooms'],
            'start_time': start_dt.strftime('%H:%M'),
            'end_time': end_dt.strftime('%H:%M'),
            'duration_minutes': duration,
            'duration_is_manual': ins.get('duration_is_manual', False),
            'travel_from_previous_mins': travel_min,
            'distance_from_previous_km': round(leg_km, 1),
            'is_existing': False,
            'is_fixed': False,
            'has_dymo': ins.get('has_dymo', False),
            'has_cylinderskift': ins.get('has_cylinderskift', False),
            'note': ins.get('note'),
        })

        current_min = end_min
        prev_coords = ins_coords

    return route_stops, total_km


# ============================================================================
# MAIN OPTIMIZATION FUNCTION
# ============================================================================

def optimize_inspector_routes(
    date: str,
    assignments: List[Dict],
    save_to_db: bool = True
) -> Dict:
    """
    Optimize routes for pre-assigned inspections.

    Args:
        date: Target date (YYYY-MM-DD format)
        assignments: List of dicts:
            [
                {
                    "inspector_id": "uuid",
                    "inspection_ids": [123, 456, 789],
                    "existing_ids": [123],
                    "fixed_order": false,   # When true, inspection_ids order is used exactly
                    "fixed_stops": [...]
                },
                ...
            ]

    fixed_order behaviour:
        False (default) — Railway runs TSP and finds the optimal sequence
        True            — Railway skips TSP entirely and uses inspection_ids
                          in the exact order provided. Use when the user has
                          manually reordered stops on the frontend.
    """

    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"🚀 Route Optimizer - {date}")
    print(f"   Inspectors: {len(assignments)}")
    print(f"   Total inspections: {sum(len(a.get('inspection_ids', [])) for a in assignments)}")
    print(f"{'='*60}")

    tz = pytz.timezone('Europe/Copenhagen')
    base_date = datetime.strptime(date, '%Y-%m-%d').date()
    day_midnight = tz.localize(datetime.combine(base_date, datetime.min.time()))

    all_routes = []
    errors = []
    total_km = 0.0
    total_travel_minutes = 0
    total_scheduled = 0

    for assignment in assignments:
        inspector_id = assignment.get('inspector_id')
        inspection_ids = assignment.get('inspection_ids', [])
        existing_ids = set(int(id) for id in assignment.get('existing_ids', []))
        fixed_stops_raw = assignment.get('fixed_stops', [])

        # ── fixed_order: skip TSP, respect the provided inspection_ids order ──
        fixed_order = bool(assignment.get('fixed_order', False))
        if fixed_order:
            print(f"  📌 fixed_order=True — using provided inspection sequence as-is")

        if not inspector_id:
            errors.append("Missing inspector_id in assignment")
            continue

        fixed_stops = parse_fixed_stops(fixed_stops_raw)

        if not inspection_ids and not fixed_stops:
            print(f"  ⚠️ No inspections or fixed stops for inspector {inspector_id}")
            continue

        try:
            inspection_ids = [int(id) for id in inspection_ids]
        except (ValueError, TypeError) as e:
            errors.append(f"Invalid inspection_ids format: {e}")
            continue

        inspector = fetch_inspector_data(inspector_id, date)
        if not inspector:
            errors.append(f"Inspector {inspector_id} not found or missing coordinates")
            continue

        print(f"\n📍 {inspector['full_name']}")
        print(f"   Home: {inspector['home_address']}")
        print(f"   Available from: {inspector['available_start_min'] // 60:02d}:{inspector['available_start_min'] % 60:02d}")
        print(f"   Fixed stops (booked): {len(fixed_stops)} | Existing (locked): {len(existing_ids)} | New: {len(inspection_ids) - len(existing_ids)} | Fixed order: {fixed_order}")

        home_coords = (inspector['home_lat'], inspector['home_lng'])

        if fixed_stops:
            new_inspection_ids = [id for id in inspection_ids if id not in existing_ids]
            new_inspections = fetch_monday_items(new_inspection_ids) if new_inspection_ids else []

            print(f"   Using fixed_stops mode: {len(fixed_stops)} fixed + {len(new_inspections)} new")

            route_stops, route_km = schedule_after_fixed_stops(
                inspector, fixed_stops, new_inspections, home_coords, day_midnight
            )
        else:
            # fetch_monday_items now preserves inspection_ids order
            inspections = fetch_monday_items(inspection_ids, include_scheduled=True)
            if not inspections:
                errors.append(f"No valid inspections found for {inspector['full_name']}")
                continue

            existing_inspections = [ins for ins in inspections if ins['id'] in existing_ids]
            new_inspections = [ins for ins in inspections if ins['id'] not in existing_ids]

            print(f"   Loaded: {len(existing_inspections)} existing, {len(new_inspections)} new")

            if existing_inspections and new_inspections:
                route_stops, route_km = schedule_mixed_route(
                    inspector, existing_inspections, new_inspections,
                    home_coords, day_midnight, tz,
                    fixed_order=fixed_order
                )
            elif existing_inspections:
                route_stops, route_km = build_existing_only_route(
                    inspector, existing_inspections, home_coords, day_midnight
                )
            else:
                route_stops, route_km = schedule_new_only_route(
                    inspector, new_inspections, home_coords, day_midnight,
                    fixed_order=fixed_order
                )

        print(f"   Optimal route: {route_km:.1f} km (including return home)")
        total_km += route_km

        for stop in route_stops:
            total_travel_minutes += stop.get('travel_from_previous_mins', 0)
            total_scheduled += 1

            is_fixed = stop.get('is_fixed', False)
            is_existing = stop.get('is_existing', False)
            has_dymo = stop.get('has_dymo', False)
            has_cylinderskift = stop.get('has_cylinderskift', False)
            duration_is_manual = stop.get('duration_is_manual', False)

            if is_fixed:
                lock_status = "🔒 FIXED"
            elif is_existing:
                lock_status = "🔒 LOCKED"
            else:
                lock_status = "🆕 NEW"

            extras = []
            if duration_is_manual:
                extras.append("MANUAL DUR")
            if has_dymo:
                extras.append("DYMO")
            if has_cylinderskift:
                extras.append("CYL")
            extras_str = f" [{'+'.join(extras)}]" if extras else ""

            addr_display = (stop.get('address') or 'Unknown')[:35]
            print(f"      {stop['sequence']}. {addr_display} | {stop['start_time']}-{stop['end_time']} | {lock_status}{extras_str}")

        if route_stops:
            last_stop = route_stops[-1]
            if fixed_stops and last_stop.get('is_fixed'):
                last_fixed = [s for s in fixed_stops if s['start_time'] == last_stop['start_time']]
                if last_fixed:
                    return_home_km = get_cached_distance_km(
                        last_fixed[0]['lat'], last_fixed[0]['lng'],
                        home_coords[0], home_coords[1]
                    )
                    print(f"      → Return home: {return_home_km:.1f} km")
            elif last_stop.get('monday_item_id'):
                last_items = fetch_monday_items([last_stop['monday_item_id']])
                if last_items:
                    return_home_km = get_cached_distance_km(
                        last_items[0]['lat'], last_items[0]['lng'],
                        home_coords[0], home_coords[1]
                    )
                    print(f"      → Return home: {return_home_km:.1f} km")

        route_summary = {
            'inspector_id': inspector_id,
            'inspector_name': inspector['full_name'],
            'home_address': inspector['home_address'],
            'total_inspections': len(route_stops),
            'fixed_count': len(fixed_stops),
            'existing_count': len([s for s in route_stops if s.get('is_existing') and not s.get('is_fixed')]),
            'new_count': len([s for s in route_stops if not s.get('is_existing')]),
            'fixed_order': fixed_order,
            'total_km': round(route_km, 1),
            'total_travel_minutes': sum(s.get('travel_from_previous_mins', 0) for s in route_stops),
            'start_time': route_stops[0]['start_time'] if route_stops else None,
            'end_time': route_stops[-1]['end_time'] if route_stops else None,
            'stops': route_stops
        }
        all_routes.append(route_summary)

    execution_seconds = (datetime.now() - start_time).total_seconds()

    metrics = {
        'total_scheduled': total_scheduled,
        'total_inspectors': len(all_routes),
        'total_travel_km': round(total_km, 1),
        'total_travel_minutes': total_travel_minutes,
        'execution_seconds': round(execution_seconds, 3)
    }

    print(f"\n{'='*60}")
    print(f"✅ Route optimization complete")
    print(f"   Scheduled: {total_scheduled} inspections")
    print(f"   Inspectors: {len(all_routes)}")
    print(f"   Total km: {total_km:.1f} (including return home)")
    print(f"   Execution time: {execution_seconds:.3f}s")
    print(f"{'='*60}")

    return {
        'status': 'success' if not errors else 'partial',
        'routes': all_routes,
        'metrics': metrics,
        'errors': errors if errors else None
    }


# ============================================================================
# ROUTE BUILDER HELPERS
# ============================================================================

def build_existing_only_route(
    inspector: Dict,
    existing_inspections: List[Dict],
    home_coords: Tuple[float, float],
    day_midnight: datetime
) -> Tuple[List[Dict], float]:
    """Build route for inspector with ONLY existing (already scheduled) inspections."""
    existing_inspections.sort(key=lambda x: time_str_to_minutes(x.get('scheduled_start_time', '08:30')))

    route_stops = []
    total_km = 0.0
    prev_coords = home_coords

    for seq, ins in enumerate(existing_inspections, start=1):
        ins_coords = (ins['lat'], ins['lng'])

        start_time = ins.get('scheduled_start_time', '08:30')
        end_time = ins.get('scheduled_end_time', '09:45')

        leg_km = get_cached_distance_km(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])
        total_km += leg_km

        travel_min = 0 if seq == 1 else int(round(get_cached_travel_time(
            prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1]
        )))

        route_stops.append({
            'sequence': seq,
            'monday_item_id': ins['id'],
            'address': ins['address'],
            'inspection_type': ins['inspection_type'],
            'rooms': ins['rooms'],
            'start_time': start_time[:5] if len(start_time) > 5 else start_time,
            'end_time': end_time[:5] if len(end_time) > 5 else end_time,
            'duration_minutes': ins['duration_minutes'],
            'duration_is_manual': ins.get('duration_is_manual', False),
            'travel_from_previous_mins': travel_min,
            'distance_from_previous_km': round(leg_km, 1),
            'is_existing': True,
            'has_dymo': ins.get('has_dymo', False),
            'has_cylinderskift': ins.get('has_cylinderskift', False),
            'note': ins.get('note'),
        })

        prev_coords = ins_coords

    if existing_inspections:
        last_coords = (existing_inspections[-1]['lat'], existing_inspections[-1]['lng'])
        total_km += get_cached_distance_km(last_coords[0], last_coords[1], home_coords[0], home_coords[1])

    return route_stops, total_km


def schedule_new_only_route(
    inspector: Dict,
    new_inspections: List[Dict],
    home_coords: Tuple[float, float],
    day_midnight: datetime,
    fixed_order: bool = False
) -> Tuple[List[Dict], float]:
    """
    Schedule route for inspector with ONLY new inspections.

    fixed_order=False (default): TSP finds the optimal sequence.
    fixed_order=True:            new_inspections order is used exactly as provided.
                                 Since fetch_monday_items preserves inspection_ids
                                 order, this reflects the user's manual reordering.
    """
    inspection_by_id = {ins['id']: ins for ins in new_inspections}
    stop_coords = [(ins['lat'], ins['lng']) for ins in new_inspections]
    stop_ids = [ins['id'] for ins in new_inspections]

    if fixed_order:
        # Respect the exact order provided — no TSP
        optimal_order = stop_ids
        print(f"  📌 Skipping TSP — using fixed order: {optimal_order}")
        # Calculate route_km sequentially in the fixed order
        route_km = 0.0
        coords_in_order = [home_coords] + [(inspection_by_id[id_]['lat'], inspection_by_id[id_]['lng']) for id_ in optimal_order] + [home_coords]
        for i in range(len(coords_in_order) - 1):
            route_km += get_cached_distance_km(
                coords_in_order[i][0], coords_in_order[i][1],
                coords_in_order[i+1][0], coords_in_order[i+1][1]
            )
    else:
        optimal_order, route_km = solve_tsp(home_coords, stop_coords, stop_ids)

    current_min = inspector['available_start_min']
    route_stops = []
    prev_coords = home_coords

    for seq, inspection_id in enumerate(optimal_order, start=1):
        ins = inspection_by_id[inspection_id]
        ins_coords = (ins['lat'], ins['lng'])

        if seq == 1:
            travel_min = 0
            leg_km = get_cached_distance_km(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])
        else:
            travel_min, leg_km = get_cached_travel_data(
                prev_coords[0], prev_coords[1],
                ins_coords[0], ins_coords[1]
            )
            travel_min = int(round(travel_min))
            current_min += travel_min

        start_dt = day_midnight + timedelta(minutes=current_min)
        start_dt = round_to_nearest_5_min(start_dt)
        current_min = (start_dt - day_midnight).seconds // 60

        duration = ins['duration_minutes']
        end_min = current_min + duration
        end_dt = day_midnight + timedelta(minutes=end_min)

        route_stops.append({
            'sequence': seq,
            'monday_item_id': ins['id'],
            'address': ins['address'],
            'inspection_type': ins['inspection_type'],
            'rooms': ins['rooms'],
            'start_time': start_dt.strftime('%H:%M'),
            'end_time': end_dt.strftime('%H:%M'),
            'duration_minutes': duration,
            'duration_is_manual': ins.get('duration_is_manual', False),
            'travel_from_previous_mins': travel_min,
            'distance_from_previous_km': round(leg_km, 1),
            'is_existing': False,
            'has_dymo': ins.get('has_dymo', False),
            'has_cylinderskift': ins.get('has_cylinderskift', False),
            'note': ins.get('note'),
        })

        current_min = end_min
        prev_coords = ins_coords

    return route_stops, route_km


def schedule_mixed_route(
    inspector: Dict,
    existing_inspections: List[Dict],
    new_inspections: List[Dict],
    home_coords: Tuple[float, float],
    day_midnight: datetime,
    tz,
    fixed_order: bool = False
) -> Tuple[List[Dict], float]:
    """
    Schedule route with BOTH existing (locked) and new inspections.

    fixed_order=True: new inspections are placed in the order provided,
    fitting them sequentially into available gaps rather than optimising.
    """
    existing_inspections.sort(key=lambda x: time_str_to_minutes(x.get('scheduled_start_time', '08:30')))

    existing_slots = []
    for ins in existing_inspections:
        start_min = time_str_to_minutes(ins.get('scheduled_start_time', '08:30'))
        end_min = time_str_to_minutes(ins.get('scheduled_end_time', '09:45'))
        existing_slots.append({
            'inspection': ins,
            'start_min': start_min,
            'end_min': end_min,
            'coords': (ins['lat'], ins['lng'])
        })

    gaps = []
    day_start = inspector['available_start_min']
    day_end = 17 * 60

    if existing_slots:
        first_start = existing_slots[0]['start_min']
        if first_start > day_start:
            gaps.append({
                'start_min': day_start,
                'end_min': first_start,
                'prev_coords': home_coords,
                'next_coords': existing_slots[0]['coords']
            })

    for i in range(len(existing_slots) - 1):
        gap_start = existing_slots[i]['end_min']
        gap_end = existing_slots[i + 1]['start_min']
        if gap_end > gap_start + 15:
            gaps.append({
                'start_min': gap_start,
                'end_min': gap_end,
                'prev_coords': existing_slots[i]['coords'],
                'next_coords': existing_slots[i + 1]['coords']
            })

    if existing_slots:
        last_end = existing_slots[-1]['end_min']
        if day_end > last_end:
            gaps.append({
                'start_min': last_end,
                'end_min': day_end,
                'prev_coords': existing_slots[-1]['coords'],
                'next_coords': home_coords
            })
    else:
        gaps.append({
            'start_min': day_start,
            'end_min': day_end,
            'prev_coords': home_coords,
            'next_coords': home_coords
        })

    assigned_new = []
    remaining_new = list(new_inspections)  # order preserved from fetch_monday_items

    for gap in gaps:
        current_min = gap['start_min']
        prev_coords = gap['prev_coords']

        while remaining_new and current_min < gap['end_min']:
            if fixed_order:
                # Take the next inspection in the provided order if it fits
                best_ins = None
                for ins in remaining_new:
                    ins_coords = (ins['lat'], ins['lng'])
                    travel_to = get_cached_travel_time(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])
                    if current_min + travel_to + ins['duration_minutes'] <= gap['end_min']:
                        best_ins = ins
                        break  # take first fitting one in order, don't search further
            else:
                # Original: pick best-scoring inspection for this gap
                best_ins = None
                best_score = float('inf')
                for ins in remaining_new:
                    ins_coords = (ins['lat'], ins['lng'])
                    travel_to = get_cached_travel_time(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])
                    travel_out = get_cached_travel_time(ins_coords[0], ins_coords[1], gap['next_coords'][0], gap['next_coords'][1])
                    if current_min + travel_to + ins['duration_minutes'] <= gap['end_min']:
                        score = travel_to + travel_out
                        if score < best_score:
                            best_score = score
                            best_ins = ins

            if best_ins:
                ins_coords = (best_ins['lat'], best_ins['lng'])
                travel_min = int(round(get_cached_travel_time(
                    prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1]
                )))

                current_min += travel_min

                start_dt = day_midnight + timedelta(minutes=current_min)
                start_dt = round_to_nearest_5_min(start_dt)
                current_min = (start_dt - day_midnight).seconds // 60

                end_min = current_min + best_ins['duration_minutes']
                end_dt = day_midnight + timedelta(minutes=end_min)

                assigned_new.append({
                    'inspection': best_ins,
                    'start_min': current_min,
                    'end_min': end_min,
                    'start_time': start_dt.strftime('%H:%M'),
                    'end_time': end_dt.strftime('%H:%M'),
                    'travel_min': travel_min,
                    'coords': ins_coords
                })

                remaining_new.remove(best_ins)
                prev_coords = ins_coords
                current_min = end_min
            else:
                break

    all_stops = []

    for slot in existing_slots:
        ins = slot['inspection']
        all_stops.append({
            'start_min': slot['start_min'],
            'inspection': ins,
            'start_time': ins.get('scheduled_start_time', '08:30')[:5],
            'end_time': ins.get('scheduled_end_time', '09:45')[:5],
            'is_existing': True
        })

    for assigned in assigned_new:
        all_stops.append({
            'start_min': assigned['start_min'],
            'inspection': assigned['inspection'],
            'start_time': assigned['start_time'],
            'end_time': assigned['end_time'],
            'travel_min': assigned['travel_min'],
            'is_existing': False
        })

    all_stops.sort(key=lambda x: x['start_min'])

    route_stops = []
    total_km = 0.0
    prev_coords = home_coords

    for seq, stop in enumerate(all_stops, start=1):
        ins = stop['inspection']
        ins_coords = (ins['lat'], ins['lng'])

        leg_km = get_cached_distance_km(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])
        total_km += leg_km

        if seq == 1:
            travel_min = 0
        elif stop['is_existing']:
            travel_min = int(round(get_cached_travel_time(
                prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1]
            )))
        else:
            travel_min = stop.get('travel_min', 0)

        route_stops.append({
            'sequence': seq,
            'monday_item_id': ins['id'],
            'address': ins['address'],
            'inspection_type': ins['inspection_type'],
            'rooms': ins['rooms'],
            'start_time': stop['start_time'],
            'end_time': stop['end_time'],
            'duration_minutes': ins['duration_minutes'],
            'duration_is_manual': ins.get('duration_is_manual', False),
            'travel_from_previous_mins': travel_min,
            'distance_from_previous_km': round(leg_km, 1),
            'is_existing': stop['is_existing'],
            'has_dymo': ins.get('has_dymo', False),
            'has_cylinderskift': ins.get('has_cylinderskift', False),
            'note': ins.get('note'),
        })

        prev_coords = ins_coords

    if all_stops:
        last_ins = all_stops[-1]['inspection']
        last_coords = (last_ins['lat'], last_ins['lng'])
        total_km += get_cached_distance_km(last_coords[0], last_coords[1], home_coords[0], home_coords[1])

    if remaining_new:
        print(f"  ⚠️ Could not fit {len(remaining_new)} new inspections in available gaps")
        for ins in remaining_new:
            print(f"      - {ins['address'][:40]}")

    return route_stops, total_km


# ============================================================================
# CONVENIENCE FUNCTION
# ============================================================================

def preview_routes(date: str, assignments: List[Dict]) -> Dict:
    """Preview optimized routes without saving to database."""
    return optimize_inspector_routes(date, assignments, save_to_db=False)
