"""
Lightweight Route Optimizer for Pre-Assigned Inspections

This module solves ONLY the routing problem (TSP) when inspections have already
been assigned to inspectors by the user via drag & drop UI.

UPDATED: Now works directly with monday_items_selected table (not inspection_queue)
UPDATED: Uses cached Mapbox distance_km for accurate route totals
UPDATED: Supports existing_ids - inspections that are already scheduled and should keep their times

Expected performance: <2 seconds for typical workloads (2-5 inspectors, 3-7 inspections each)
"""

import os
import math
import uuid
from datetime import datetime, timedelta
from itertools import permutations
from typing import List, Dict, Tuple, Optional
import pytz
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY environment variables")

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
    return max(5.0, minutes)  # Minimum 5 minutes


def make_cache_key(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> str:
    """Create standardized cache key with 5 decimal precision"""
    from_lng_r = round(from_lng, 5)
    from_lat_r = round(from_lat, 5)
    to_lng_r = round(to_lng, 5)
    to_lat_r = round(to_lat, 5)
    return f"{from_lng_r:.5f},{from_lat_r:.5f}->{to_lng_r:.5f},{to_lat_r:.5f}"


def get_cached_travel_data(from_lat: float, from_lng: float, 
                           to_lat: float, to_lng: float) -> Tuple[float, float]:
    """
    Get cached travel time (minutes) and distance (km) from Mapbox cache.
    Returns (minutes, km) tuple. Falls back to estimates if cache miss.
    """
    if from_lat == to_lat and from_lng == to_lng:
        return 0.0, 0.0
    
    key = make_cache_key(from_lat, from_lng, to_lat, to_lng)
    
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
                # If we have minutes but no km, estimate km from Haversine * 1.3 (road factor)
                if cached_km is None:
                    cached_km = haversine_km(from_lat, from_lng, to_lat, to_lng) * 1.3
                
                print(f"  ✅ Cache HIT: {key} = {cached_minutes} min, {cached_km:.1f} km")
                return max(5.0, cached_minutes), cached_km
        
        print(f"  ⚠️ Cache MISS: {key}")
    except Exception as e:
        print(f"  ❌ Cache error: {e}")
    
    # Fallback to estimates
    est_minutes = estimate_travel_minutes(from_lat, from_lng, to_lat, to_lng)
    est_km = haversine_km(from_lat, from_lng, to_lat, to_lng) * 1.3  # Road factor
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
        return 9 * 60  # Default 09:00


# ============================================================================
# INSPECTION TYPE TO DURATION MAPPING
# ============================================================================

def get_inspection_duration(inspection_type: str, rooms: int) -> int:
    """
    Get inspection duration in minutes based on type and room count.
    Falls back to default if not found.
    """
    # Map Danish inspection types to abbreviations
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
    
    try:
        result = supabase.table('inspection_durations')\
            .select('minutes')\
            .eq('inspection_type', abbrev)\
            .eq('rooms', rooms)\
            .execute()
        
        if result.data and len(result.data) > 0:
            return result.data[0]['minutes']
    except Exception as e:
        print(f"  ⚠️ Error fetching duration: {e}")
    
    # Default durations by type
    defaults = {'PA': 30, 'PS': 45, 'IF': 45, 'FF': 60}
    return defaults.get(abbrev, 45)


# ============================================================================
# TSP SOLVER - Finds optimal route order
# ============================================================================

def solve_tsp_bruteforce(
    home_coords: Tuple[float, float],
    stop_coords: List[Tuple[float, float]],
    stop_ids: List[int]
) -> Tuple[List[int], float]:
    """
    Solve TSP via brute force for small number of stops (≤7).
    Returns optimal order of stop_ids and total distance in km.
    
    Route: home → stops (in optimal order) → home
    Uses cached Mapbox distances when available.
    """
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
        
        # Home to first stop
        first_idx = perm[0]
        total_km += get_cached_distance_km(
            home_coords[0], home_coords[1],
            stop_coords[first_idx][0], stop_coords[first_idx][1]
        )
        
        # Between stops
        for i in range(len(perm) - 1):
            from_idx = perm[i]
            to_idx = perm[i + 1]
            total_km += get_cached_distance_km(
                stop_coords[from_idx][0], stop_coords[from_idx][1],
                stop_coords[to_idx][0], stop_coords[to_idx][1]
            )
        
        # Last stop to home
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
    """
    Solve TSP via nearest neighbor heuristic for larger stop counts.
    Fast but not always optimal - good enough for 8+ stops.
    Uses cached Mapbox distances when available.
    """
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
    
    # Return to home
    total_km += get_cached_distance_km(current_lat, current_lng, home_coords[0], home_coords[1])
    
    return [stop_ids[idx] for idx in route_indices], total_km


def solve_tsp(
    home_coords: Tuple[float, float],
    stop_coords: List[Tuple[float, float]],
    stop_ids: List[int]
) -> Tuple[List[int], float]:
    """
    Solve TSP - picks algorithm based on stop count.
    ≤7 stops: brute force (optimal)
    >7 stops: nearest neighbor (fast heuristic)
    """
    if len(stop_coords) <= 7:
        return solve_tsp_bruteforce(home_coords, stop_coords, stop_ids)
    else:
        return solve_tsp_nearest_neighbor(home_coords, stop_coords, stop_ids)


# ============================================================================
# DATA FETCHING
# ============================================================================

def fetch_inspector_data(inspector_id: str, date: str) -> Optional[Dict]:
    """Fetch inspector's home location and availability for date"""
    
    # Get inspector base info
    result = supabase.table('inspectors')\
        .select('id, full_name, address, lat, lng')\
        .eq('id', inspector_id)\
        .execute()
    
    if not result.data or len(result.data) == 0:
        return None
    
    inspector = result.data[0]
    
    if not inspector.get('lat') or not inspector.get('lng'):
        return None
    
    # Get availability times for this date
    avail_result = supabase.table('supabase_availability')\
        .select('start_time_local, end_time_local')\
        .eq('inspector_id', inspector_id)\
        .eq('date_local', date)\
        .eq('is_available', True)\
        .execute()
    
    start_time = '09:00:00'
    end_time = '17:00:00'
    
    if avail_result.data and len(avail_result.data) > 0:
        avail = avail_result.data[0]
        if avail.get('start_time_local') and str(avail['start_time_local']).lower() != 'none':
            start_time = avail['start_time_local']
        if avail.get('end_time_local') and str(avail['end_time_local']).lower() != 'none':
            end_time = avail['end_time_local']
    
    # Check for existing shifts (from capacity view)
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
    
    # Calculate actual available start time
    try:
        st = datetime.strptime(start_time, '%H:%M:%S').time()
        start_min = st.hour * 60 + st.minute
    except (ValueError, TypeError):
        start_min = 9 * 60
    
    # Adjust for existing shifts (+15 min buffer)
    if latest_shift_end_min > start_min:
        start_min = latest_shift_end_min + 15
    
    # Ensure minimum 09:00 start
    start_min = max(9 * 60, start_min)
    
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
    
    Args:
        item_ids: List of monday_items_selected.id values (bigint)
        include_scheduled: If True, also fetch scheduled_start_time and scheduled_end_time
    
    Returns:
        List of inspection dicts with coordinates and durations
    """
    if not item_ids:
        return []
    
    print(f"  📋 Fetching {len(item_ids)} items from monday_items_selected...")
    
    select_fields = 'id, adresse, synstype, antal_vaerelser, lat, lng, dato_tid'
    if include_scheduled:
        select_fields += ', scheduled_start_time, scheduled_end_time'
    
    result = supabase.table('monday_items_selected')\
        .select(select_fields)\
        .in_('id', item_ids)\
        .execute()
    
    inspections = []
    missing_coords = []
    
    for item in (result.data or []):
        # Check for coordinates
        if not item.get('lat') or not item.get('lng'):
            missing_coords.append(item.get('adresse', f"ID: {item['id']}"))
            continue
        
        # Get duration based on type and rooms
        inspection_type = item.get('synstype', 'Indflytningssyn')
        rooms = item.get('antal_vaerelser', 3)
        duration = get_inspection_duration(inspection_type, rooms)
        
        ins_data = {
            'id': item['id'],  # Keep as integer for monday_items_selected
            'address': item.get('adresse', 'Ukendt adresse'),
            'inspection_type': inspection_type,
            'rooms': rooms,
            'lat': item['lat'],
            'lng': item['lng'],
            'duration_minutes': duration,
            'preferred_date': item.get('dato_tid')
        }
        
        # Include scheduled times if requested
        if include_scheduled:
            ins_data['scheduled_start_time'] = item.get('scheduled_start_time')
            ins_data['scheduled_end_time'] = item.get('scheduled_end_time')
        
        inspections.append(ins_data)
    
    if missing_coords:
        print(f"  ⚠️ Skipping {len(missing_coords)} items without coordinates:")
        for addr in missing_coords[:5]:  # Show first 5
            print(f"      - {addr}")
        if len(missing_coords) > 5:
            print(f"      ... and {len(missing_coords) - 5} more")
    
    print(f"  ✅ Loaded {len(inspections)} items with coordinates")
    return inspections


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
    
    This solves ONLY the routing/sequencing problem (TSP) - the assignment
    of inspections to inspectors has already been done by the user.
    
    NEW: Supports existing_ids - inspections that are already scheduled.
    These keep their fixed times and new inspections are scheduled around them.
    
    Args:
        date: Target date (YYYY-MM-DD format)
        assignments: List of dicts with format:
            [
                {
                    "inspector_id": "uuid",
                    "inspection_ids": [123, 456, ...],  # All inspection IDs
                    "existing_ids": [123]  # Optional: IDs that are already scheduled (keep times)
                },
                ...
            ]
        save_to_db: Whether to save results to proposed_assignments table
    
    Returns:
        Dict with routes, metrics, and any errors
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
        
        if not inspector_id:
            errors.append("Missing inspector_id in assignment")
            continue
        
        if not inspection_ids:
            print(f"  ⚠️ No inspections for inspector {inspector_id}")
            continue
        
        # Convert inspection_ids to integers
        try:
            inspection_ids = [int(id) for id in inspection_ids]
        except (ValueError, TypeError) as e:
            errors.append(f"Invalid inspection_ids format: {e}")
            continue
        
        # Fetch inspector data
        inspector = fetch_inspector_data(inspector_id, date)
        if not inspector:
            errors.append(f"Inspector {inspector_id} not found or missing coordinates")
            continue
        
        print(f"\n📍 {inspector['full_name']}")
        print(f"   Home: {inspector['home_address']}")
        print(f"   Available from: {inspector['available_start_min'] // 60:02d}:{inspector['available_start_min'] % 60:02d}")
        print(f"   Existing (locked): {len(existing_ids)} | New: {len(inspection_ids) - len(existing_ids)}")
        
        # Fetch inspection data (include scheduled times for existing)
        inspections = fetch_monday_items(inspection_ids, include_scheduled=True)
        if not inspections:
            errors.append(f"No valid inspections found for {inspector['full_name']}")
            continue
        
        # Separate existing vs new inspections
        existing_inspections = [ins for ins in inspections if ins['id'] in existing_ids]
        new_inspections = [ins for ins in inspections if ins['id'] not in existing_ids]
        
        print(f"   Loaded: {len(existing_inspections)} existing, {len(new_inspections)} new")
        
        # Build coordinates for TSP (only for new inspections)
        home_coords = (inspector['home_lat'], inspector['home_lng'])
        
        if existing_inspections and new_inspections:
            # MIXED CASE: Existing + New inspections
            # Strategy: Keep existing times fixed, schedule new ones in gaps
            route_stops, route_km = schedule_mixed_route(
                inspector, existing_inspections, new_inspections, 
                home_coords, day_midnight, tz
            )
        elif existing_inspections:
            # ONLY EXISTING: Just return their scheduled times
            route_stops, route_km = build_existing_only_route(
                inspector, existing_inspections, home_coords, day_midnight
            )
        else:
            # ONLY NEW: Standard TSP optimization
            route_stops, route_km = schedule_new_only_route(
                inspector, new_inspections, home_coords, day_midnight
            )
        
        print(f"   Optimal route: {route_km:.1f} km (including return home)")
        total_km += route_km
        
        # Calculate totals
        for stop in route_stops:
            total_travel_minutes += stop.get('travel_from_previous_mins', 0)
            total_scheduled += 1
            
            is_existing = stop['monday_item_id'] in existing_ids
            lock_status = "🔒 LOCKED" if is_existing else "🆕 NEW"
            print(f"      {stop['sequence']}. {stop['address'][:35]} | {stop['start_time']}-{stop['end_time']} | {lock_status}")
        
        # Calculate return home
        if route_stops:
            last_stop = route_stops[-1]
            last_ins = next((ins for ins in inspections if ins['id'] == last_stop['monday_item_id']), None)
            if last_ins:
                return_home_km = get_cached_distance_km(
                    last_ins['lat'], last_ins['lng'],
                    home_coords[0], home_coords[1]
                )
                print(f"      → Return home: {return_home_km:.1f} km")
        
        # Build route summary
        route_summary = {
            'inspector_id': inspector_id,
            'inspector_name': inspector['full_name'],
            'home_address': inspector['home_address'],
            'total_inspections': len(route_stops),
            'existing_count': len(existing_inspections),
            'new_count': len(new_inspections),
            'total_km': round(route_km, 1),
            'total_travel_minutes': sum(s.get('travel_from_previous_mins', 0) for s in route_stops),
            'start_time': route_stops[0]['start_time'] if route_stops else None,
            'end_time': route_stops[-1]['end_time'] if route_stops else None,
            'stops': route_stops
        }
        all_routes.append(route_summary)
    
    # Calculate execution time
    execution_seconds = (datetime.now() - start_time).total_seconds()
    
    # Build metrics
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


def build_existing_only_route(
    inspector: Dict,
    existing_inspections: List[Dict],
    home_coords: Tuple[float, float],
    day_midnight: datetime
) -> Tuple[List[Dict], float]:
    """
    Build route for inspector with ONLY existing (already scheduled) inspections.
    Just returns their existing times in order.
    """
    # Sort by scheduled start time
    existing_inspections.sort(key=lambda x: time_str_to_minutes(x.get('scheduled_start_time', '09:00')))
    
    route_stops = []
    total_km = 0.0
    prev_coords = home_coords
    
    for seq, ins in enumerate(existing_inspections, start=1):
        ins_coords = (ins['lat'], ins['lng'])
        
        # Get times from scheduled values
        start_time = ins.get('scheduled_start_time', '09:00')
        end_time = ins.get('scheduled_end_time', '10:00')
        
        # Calculate distance from previous
        leg_km = get_cached_distance_km(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])
        total_km += leg_km
        
        # Calculate travel time
        travel_min = 0 if seq == 1 else int(round(get_cached_travel_time(
            prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1]
        )))
        
        route_stops.append({
            'sequence': seq,
            'monday_item_id': ins['id'],
            'address': ins['address'],
            'inspection_type': ins['inspection_type'],
            'rooms': ins['rooms'],
            'start_time': start_time[:5] if len(start_time) > 5 else start_time,  # HH:MM
            'end_time': end_time[:5] if len(end_time) > 5 else end_time,
            'duration_minutes': ins['duration_minutes'],
            'travel_from_previous_mins': travel_min,
            'distance_from_previous_km': round(leg_km, 1),
            'is_existing': True
        })
        
        prev_coords = ins_coords
    
    # Add return home distance
    if existing_inspections:
        last_coords = (existing_inspections[-1]['lat'], existing_inspections[-1]['lng'])
        total_km += get_cached_distance_km(last_coords[0], last_coords[1], home_coords[0], home_coords[1])
    
    return route_stops, total_km


def schedule_new_only_route(
    inspector: Dict,
    new_inspections: List[Dict],
    home_coords: Tuple[float, float],
    day_midnight: datetime
) -> Tuple[List[Dict], float]:
    """
    Schedule route for inspector with ONLY new inspections.
    Standard TSP optimization starting at inspector's available time.
    """
    # Build coordinates for TSP
    stop_coords = [(ins['lat'], ins['lng']) for ins in new_inspections]
    stop_ids = [ins['id'] for ins in new_inspections]
    
    # Create lookup by ID
    inspection_by_id = {ins['id']: ins for ins in new_inspections}
    
    # Solve TSP
    optimal_order, route_km = solve_tsp(home_coords, stop_coords, stop_ids)
    
    # Build schedule with times
    current_min = inspector['available_start_min']
    route_stops = []
    prev_coords = home_coords
    
    for seq, inspection_id in enumerate(optimal_order, start=1):
        ins = inspection_by_id[inspection_id]
        ins_coords = (ins['lat'], ins['lng'])
        
        # Calculate travel time and distance from previous location
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
        
        # Round start time to nearest 5 min
        start_dt = day_midnight + timedelta(minutes=current_min)
        start_dt = round_to_nearest_5_min(start_dt)
        current_min = (start_dt - day_midnight).seconds // 60
        
        # Calculate end time
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
            'travel_from_previous_mins': travel_min,
            'distance_from_previous_km': round(leg_km, 1),
            'is_existing': False
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
    tz
) -> Tuple[List[Dict], float]:
    """
    Schedule route with BOTH existing (locked) and new inspections.
    
    Strategy:
    1. Keep existing inspections at their fixed times
    2. Find gaps between existing inspections
    3. Assign new inspections to optimal gaps based on location
    """
    # Sort existing by start time
    existing_inspections.sort(key=lambda x: time_str_to_minutes(x.get('scheduled_start_time', '09:00')))
    
    # Build timeline of existing slots
    existing_slots = []
    for ins in existing_inspections:
        start_min = time_str_to_minutes(ins.get('scheduled_start_time', '09:00'))
        end_min = time_str_to_minutes(ins.get('scheduled_end_time', '10:00'))
        existing_slots.append({
            'inspection': ins,
            'start_min': start_min,
            'end_min': end_min,
            'coords': (ins['lat'], ins['lng'])
        })
    
    # Find gaps for new inspections
    # Gap 1: Before first existing inspection
    # Gap 2-N: Between existing inspections
    # Gap N+1: After last existing inspection
    
    gaps = []
    day_start = inspector['available_start_min']
    day_end = 17 * 60  # 17:00
    
    # Gap before first
    if existing_slots:
        first_start = existing_slots[0]['start_min']
        if first_start > day_start:
            gaps.append({
                'start_min': day_start,
                'end_min': first_start,
                'prev_coords': home_coords,
                'next_coords': existing_slots[0]['coords']
            })
    
    # Gaps between existing
    for i in range(len(existing_slots) - 1):
        gap_start = existing_slots[i]['end_min']
        gap_end = existing_slots[i + 1]['start_min']
        if gap_end > gap_start + 15:  # At least 15 min gap
            gaps.append({
                'start_min': gap_start,
                'end_min': gap_end,
                'prev_coords': existing_slots[i]['coords'],
                'next_coords': existing_slots[i + 1]['coords']
            })
    
    # Gap after last
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
        # No existing, entire day is a gap
        gaps.append({
            'start_min': day_start,
            'end_min': day_end,
            'prev_coords': home_coords,
            'next_coords': home_coords
        })
    
    # Assign new inspections to gaps (greedy by travel efficiency)
    assigned_new = []
    remaining_new = list(new_inspections)
    
    for gap in gaps:
        gap_duration = gap['end_min'] - gap['start_min']
        current_min = gap['start_min']
        prev_coords = gap['prev_coords']
        
        while remaining_new and current_min < gap['end_min']:
            # Find best inspection for this gap (closest to prev_coords)
            best_ins = None
            best_score = float('inf')
            
            for ins in remaining_new:
                ins_coords = (ins['lat'], ins['lng'])
                travel_to = get_cached_travel_time(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])
                travel_out = get_cached_travel_time(ins_coords[0], ins_coords[1], gap['next_coords'][0], gap['next_coords'][1])
                total_time = travel_to + ins['duration_minutes']
                
                # Check if it fits in remaining gap
                if current_min + travel_to + ins['duration_minutes'] <= gap['end_min']:
                    score = travel_to + travel_out  # Prefer lower total detour
                    if score < best_score:
                        best_score = score
                        best_ins = ins
            
            if best_ins:
                ins_coords = (best_ins['lat'], best_ins['lng'])
                travel_min = int(round(get_cached_travel_time(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])))
                
                # Add travel time
                current_min += travel_min
                
                # Round to nearest 5 min
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
                break  # No more inspections fit in this gap
    
    # Build combined route (existing + assigned new, sorted by start time)
    all_stops = []
    
    for slot in existing_slots:
        ins = slot['inspection']
        all_stops.append({
            'start_min': slot['start_min'],
            'inspection': ins,
            'start_time': ins.get('scheduled_start_time', '09:00')[:5],
            'end_time': ins.get('scheduled_end_time', '10:00')[:5],
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
    
    # Sort by start time
    all_stops.sort(key=lambda x: x['start_min'])
    
    # Build final route_stops with distances
    route_stops = []
    total_km = 0.0
    prev_coords = home_coords
    
    for seq, stop in enumerate(all_stops, start=1):
        ins = stop['inspection']
        ins_coords = (ins['lat'], ins['lng'])
        
        # Calculate distance
        leg_km = get_cached_distance_km(prev_coords[0], prev_coords[1], ins_coords[0], ins_coords[1])
        total_km += leg_km
        
        # Calculate travel time
        if seq == 1:
            travel_min = 0
        elif stop['is_existing']:
            # For existing, recalculate travel time
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
            'travel_from_previous_mins': travel_min,
            'distance_from_previous_km': round(leg_km, 1),
            'is_existing': stop['is_existing']
        })
        
        prev_coords = ins_coords
    
    # Add return home distance
    if all_stops:
        last_ins = all_stops[-1]['inspection']
        last_coords = (last_ins['lat'], last_ins['lng'])
        total_km += get_cached_distance_km(last_coords[0], last_coords[1], home_coords[0], home_coords[1])
    
    # Note any unassigned new inspections
    if remaining_new:
        print(f"  ⚠️ Could not fit {len(remaining_new)} new inspections in available gaps")
        for ins in remaining_new:
            print(f"      - {ins['address'][:40]}")
    
    return route_stops, total_km


# ============================================================================
# CONVENIENCE FUNCTION - Get optimal times without saving
# ============================================================================

def preview_routes(date: str, assignments: List[Dict]) -> Dict:
    """
    Preview optimized routes without saving to database.
    Use this for UI preview before final confirmation.
    """
    return optimize_inspector_routes(date, assignments, save_to_db=False)
