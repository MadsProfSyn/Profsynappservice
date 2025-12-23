# Route Optimizer Service

Lightweight VRP route optimizer for pre-assigned inspections. Solves only the routing problem (TSP) when inspections have already been assigned to inspectors via drag & drop UI.

## Performance

| Inspections/Inspector | Algorithm | Time |
|----------------------|-----------|------|
| ≤7 | Brute force (optimal) | <10ms |
| 8+ | Nearest neighbor | <100ms |

**Typical total: <1 second** for 2-5 inspectors with 3-7 inspections each.

## Endpoints

### `GET /health`
Health check for Railway.

### `POST /preview-routes`
Preview optimized routes **without saving** to database. Use for real-time UI updates.

### `POST /optimize-routes`
Optimize routes **and save** to `proposed_assignments` table.

### Request Format

```json
{
  "date": "2026-01-02",
  "assignments": [
    {
      "inspector_id": "uuid-of-inspector",
      "inspection_ids": ["uuid-1", "uuid-2", "uuid-3"]
    },
    {
      "inspector_id": "uuid-of-another-inspector", 
      "inspection_ids": ["uuid-4", "uuid-5"]
    }
  ]
}
```

### Response Format

```json
{
  "status": "success",
  "vrp_run_id": "uuid-of-run",
  "routes": [
    {
      "inspector_id": "uuid",
      "inspector_name": "Mohamed Lazaar Chrifi",
      "home_address": "Solrød Strand, Danmark",
      "total_inspections": 3,
      "total_km": 45.2,
      "total_travel_minutes": 52,
      "start_time": "09:00",
      "end_time": "12:15",
      "stops": [
        {
          "sequence": 1,
          "inspection_id": "uuid",
          "address": "Ejboparken 61, 2. tv, 4000 Roskilde",
          "inspection_type": "Projektsyn",
          "rooms": 3,
          "start_time": "09:00",
          "end_time": "09:45",
          "duration_minutes": 45,
          "travel_from_previous_mins": 0
        }
      ]
    }
  ],
  "metrics": {
    "total_scheduled": 7,
    "total_inspectors": 2,
    "total_travel_km": 89.4,
    "total_travel_minutes": 98,
    "execution_seconds": 0.234
  },
  "errors": null
}
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | ✅ | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | ✅ | Supabase service role key |
| `PORT` | ❌ | Port to run on (default: 8080) |

## Deployment to Railway

1. Create new Railway service
2. Connect to your GitHub repo (or this subfolder)
3. Set environment variables:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY`
4. Railway will auto-detect the Procfile and deploy

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SUPABASE_URL="your-url"
export SUPABASE_SERVICE_KEY="your-key"

# Run
python route_optimizer_api.py
```

## Database Tables Used

**Read:**
- `inspectors` - Inspector home locations
- `supabase_availability` - Availability for date
- `inspector_capacity_view` - Existing shifts
- `inspection_queue` - Inspection details
- `inspection_type_mappings` - Type → abbreviation
- `inspection_durations` - Duration lookup
- `mapbox_travel_cache` - Cached travel times

**Write:**
- `vrp_runs` - Run metadata
- `proposed_assignments` - Scheduled assignments
