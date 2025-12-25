"""
Lightweight Route Optimizer for Pre-Assigned Inspections

This module solves ONLY the routing problem (TSP) when inspections have already
been assigned to inspectors by the user via drag & drop UI.

UPDATED: Now works directly with monday_items_selected table (not inspection_queue)

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
    """Calculate distance in km between two coordinates"""
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


def get_cached_travel_time(from_lat: float, from_lng: float, 
                           to_lat: float, to_lng: float) -> float:
    """Get cached travel time or return estimate (minutes)."""
    if from_lat == to_lat and from_lng == to_lng:
        return 0.0
    
    # Round to 5 decimal places to match Edge Function cache keys
    from_lng_r = round(from_lng, 5)
    from_lat_r = round(from_lat, 5)
    to_lng_r = round(to_lng, 5)
    to_lat_r = round(to_lat, 5)
    
    # Format key with exactly 5 decimal places (matching Edge Function format)
    key = f"{from_lng_r:.5f},{from_lat_r:.5f}->{to_lng_r:.5f},{to_lat_r:.5f}"
    
    try:
        result = supabase.table('mapbox_travel_cache').select('minutes').eq('key', key).execute()
        if result.data and len(result.data) > 0 and result.data[0].get('minutes') is not None:
            cached_minutes = float(result.data[0]['minutes'])
            print(f"  ✅ Cache HIT: {key} = {cached_minutes} min")
            return max(5.0, cached_minutes)
        else:
            print(f"  ⚠️ Cache MISS: {key}")
    except Exception as e:
        print(f"  ❌ Cache error: {e}")
    
    return estimate_travel_minutes(from_lat, from_lng, to_lat, to_lng)


def round_to_nearest_5_min(dt: datetime) -> datetime:
    """Round datetime UP to nearest 5 minutes."""
    discard = timedelta(minutes=dt.minute % 5, seconds=dt.second, microseconds=dt.microsecond)
    if discard:
        dt += timedelta(minutes=5) - discard
    return dt.replace(second=0, microsecond=0)


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
    """
    if len(stop_coords) == 0:
        return [], 0.0
    
    if len(stop_coords) == 1:
        km = haversine_km(home_coords[0], home_coords[1], stop_coords[0][0], stop_coords[0][1])
        km += haversine_km(stop_coords[0][0], stop_coords[0][1], home_coords[0], home_coords[1])
        return [stop_ids[0]], km
    
    best_order = None
    best_distance = float('inf')
    
    for perm in permutations(range(len(stop_coords))):
        total_km = 0.0
        
        # Home to first stop
        first_idx = perm[0]
        total_km += haversine_km(
            home_coords[0], home_coords[1],
            stop_coords[first_idx][0], stop_coords[first_idx][1]
        )
        
        # Between stops
        for i in range(len(perm) - 1):
            from_idx = perm[i]
            to_idx = perm[i + 1]
            total_km += haversine_km(
                stop_coords[from_idx][0], stop_coords[from_idx][1],
                stop_coords[to_idx][0], stop_coords[to_idx][1]
            )
        
        # Last stop to home
        last_idx = perm[-1]
        total_km += haversine_km(
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
            dist = haversine_km(current_lat, current_lng, 
                               stop_coords[idx][0], stop_coords[idx][1])
            if dist < best_dist:
                best_dist = dist
                best_idx = idx
        
        route_indices.append(best_idx)
        total_km += best_dist
        current_lat, current_lng = stop_coords[best_idx]
        remaining.remove(best_idx)
    
    # Return to home
    total_km += haversine_km(current_lat, current_lng, home_coords[0], home_coords[1])
    
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


def fetch_monday_items(item_ids: List[int]) -> List[Dict]:
    """
    Fetch inspection details from monday_items_selected table.
    
    Args:
        item_ids: List of monday_items_selected.id values (bigint)
    
    Returns:
        List of inspection dicts with coordinates and durations
    """
    if not item_ids:
        return []
    
    print(f"  📋 Fetching {len(item_ids)} items from monday_items_selected...")
    
    result = supabase.table('monday_items_selected')\
        .select('id, adresse, synstype, antal_vaerelser, lat, lng, dato_tid')\
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
        
        inspections.append({
            'id': item['id'],  # Keep as integer for monday_items_selected
            'address': item.get('adresse', 'Ukendt adresse'),
            'inspection_type': inspection_type,
            'rooms': rooms,
            'lat': item['lat'],
            'lng': item['lng'],
            'duration_minutes': duration,
            'preferred_date': item.get('dato_tid')
        })
    
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
    
    Args:
        date: Target date (YYYY-MM-DD format)
        assignments: List of dicts with format:
            [
                {"inspector_id": "uuid", "inspection_ids": [123, 456, ...]},  # monday_items_selected.id
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
    all_db_assignments = []  # For saving to proposed_assignments
    errors = []
    
    total_km = 0.0
    total_travel_minutes = 0
    total_scheduled = 0
    
    for assignment in assignments:
        inspector_id = assignment.get('inspector_id')
        inspection_ids = assignment.get('inspection_ids', [])
        
        if not inspector_id:
            errors.append("Missing inspector_id in assignment")
            continue
        
        if not inspection_ids:
            print(f"  ⚠️ No inspections for inspector {inspector_id}")
            continue
        
        # Convert inspection_ids to integers (they come from monday_items_selected.id)
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
        
        # Fetch inspection data from monday_items_selected
        inspections = fetch_monday_items(inspection_ids)
        if not inspections:
            errors.append(f"No valid inspections found for {inspector['full_name']}")
            continue
        
        print(f"   Inspections to route: {len(inspections)}")
        
        # Build coordinates for TSP
        home_coords = (inspector['home_lat'], inspector['home_lng'])
        stop_coords = [(ins['lat'], ins['lng']) for ins in inspections]
        stop_ids = [ins['id'] for ins in inspections]
        
        # Create lookup by ID
        inspection_by_id = {ins['id']: ins for ins in inspections}
        
        # Solve TSP
        optimal_order, route_km = solve_tsp(home_coords, stop_coords, stop_ids)
        
        print(f"   Optimal route: {route_km:.1f} km")
        total_km += route_km
        
        # Build schedule with times
        current_min = inspector['available_start_min']
        route_stops = []
        prev_coords = home_coords
        
        for seq, inspection_id in enumerate(optimal_order, start=1):
            ins = inspection_by_id[inspection_id]
            ins_coords = (ins['lat'], ins['lng'])
            
            # Calculate travel time from previous location
            if seq == 1:
                # First stop: no travel time consumed (starts at available time)
                travel_min = 0
            else:
                travel_min = int(round(get_cached_travel_time(
                    prev_coords[0], prev_coords[1],
                    ins_coords[0], ins_coords[1]
                )))
                current_min += travel_min
            
            # Round start time to nearest 5 min
            start_dt = day_midnight + timedelta(minutes=current_min)
            start_dt = round_to_nearest_5_min(start_dt)
            current_min = (start_dt - day_midnight).seconds // 60
            
            # Calculate end time
            duration = ins['duration_minutes']
            end_min = current_min + duration
            end_dt = day_midnight + timedelta(minutes=end_min)
            
            # Build route stop for response
            route_stop = {
                'sequence': seq,
                'monday_item_id': ins['id'],  # Return the monday_items_selected.id
                'address': ins['address'],
                'inspection_type': ins['inspection_type'],
                'rooms': ins['rooms'],
                'start_time': start_dt.strftime('%H:%M'),
                'end_time': end_dt.strftime('%H:%M'),
                'duration_minutes': duration,
                'travel_from_previous_mins': travel_min
            }
            route_stops.append(route_stop)
            
            print(f"      {seq}. {ins['address'][:40]}")
            print(f"         {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')} ({duration}m) | Travel: {travel_min}m")
            
            total_travel_minutes += travel_min
            total_scheduled += 1
            
            # Move time forward
            current_min = end_min
            prev_coords = ins_coords
        
        # Build route summary
        route_summary = {
            'inspector_id': inspector_id,
            'inspector_name': inspector['full_name'],
            'home_address': inspector['home_address'],
            'total_inspections': len(route_stops),
            'total_km': round(route_km, 1),
            'total_travel_minutes': sum(s['travel_from_previous_mins'] for s in route_stops),
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
    print(f"   Total km: {total_km:.1f}")
    print(f"   Execution time: {execution_seconds:.3f}s")
    print(f"{'='*60}")
    
    # Note: We're NOT saving to proposed_assignments anymore since that table
    # expects inspection_queue UUIDs. The frontend should update monday_items_selected directly.
    
    return {
        'status': 'success' if not errors else 'partial',
        'routes': all_routes,
        'metrics': metrics,
        'errors': errors if errors else None
    }


# ============================================================================
# CONVENIENCE FUNCTION - Get optimal times without saving
# ============================================================================

def preview_routes(date: str, assignments: List[Dict]) -> Dict:
    """
    Preview optimized routes without saving to database.
    Use this for UI preview before final confirmation.
    """
    return optimize_inspector_routes(date, assignments, save_to_db=False)
