"""
Route Optimizer API Service

Flask server exposing the lightweight route optimizer for pre-assigned inspections.
Deployed as a separate Railway service.

Endpoints:
    POST /optimize-routes     - Optimize routes and save to DB
    POST /preview-routes      - Preview routes without saving (for UI)
    GET  /health              - Health check
"""

import os
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from vrp_route_optimizer import optimize_inspector_routes, preview_routes
import traceback

app = Flask(__name__)

# Configure CORS - update with your actual frontend domains
CORS(app, origins=[
    "http://localhost:3000",
    "http://localhost:5173",
    "https://*.lovable.app",
    "https://*.vercel.app",
    # Add your production domain here
])

# Track service status
service_status = {
    'started_at': datetime.utcnow().isoformat(),
    'requests_handled': 0,
    'last_request': None
}


@app.route('/', methods=['GET'])
def root():
    """Root endpoint - redirect to health"""
    return health()


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint for Railway"""
    return jsonify({
        'status': 'healthy',
        'service': 'route-optimizer',
        'version': '1.0.0',
        'started_at': service_status['started_at'],
        'requests_handled': service_status['requests_handled'],
        'last_request': service_status['last_request'],
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/optimize-routes', methods=['POST'])
def optimize_routes_endpoint():
    """
    Optimize routes for pre-assigned inspections and save to database.
    
    Request body:
    {
        "date": "2026-01-02",
        "assignments": [
            {"inspector_id": "uuid", "inspection_ids": ["uuid1", "uuid2"]},
            {"inspector_id": "uuid", "inspection_ids": ["uuid3", "uuid4"]}
        ]
    }
    
    Response:
    {
        "status": "success",
        "vrp_run_id": "uuid",
        "routes": [...],
        "metrics": {...}
    }
    """
    service_status['requests_handled'] += 1
    service_status['last_request'] = datetime.utcnow().isoformat()
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No JSON body provided'}), 400
        
        date = data.get('date')
        assignments = data.get('assignments')
        
        # Validation
        validation_error = validate_request(date, assignments)
        if validation_error:
            return jsonify({'error': validation_error}), 400
        
        # Run optimization with save
        result = optimize_inspector_routes(date, assignments, save_to_db=True)
        
        return jsonify(result)
    
    except Exception as e:
        print(f"❌ Error in /optimize-routes: {e}")
        print(traceback.format_exc())
        return jsonify({
            'error': str(e),
            'status': 'error'
        }), 500


@app.route('/preview-routes', methods=['POST'])
def preview_routes_endpoint():
    """
    Preview optimized routes WITHOUT saving to database.
    Use this for real-time UI preview as user drags inspections.
    
    Same request/response format as /optimize-routes
    """
    service_status['requests_handled'] += 1
    service_status['last_request'] = datetime.utcnow().isoformat()
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No JSON body provided'}), 400
        
        date = data.get('date')
        assignments = data.get('assignments')
        
        # Validation
        validation_error = validate_request(date, assignments)
        if validation_error:
            return jsonify({'error': validation_error}), 400
        
        # Run optimization without save
        result = preview_routes(date, assignments)
        
        return jsonify(result)
    
    except Exception as e:
        print(f"❌ Error in /preview-routes: {e}")
        print(traceback.format_exc())
        return jsonify({
            'error': str(e),
            'status': 'error'
        }), 500


def validate_request(date: str, assignments: list) -> str | None:
    """Validate request data, returns error message or None if valid"""
    
    if not date:
        return 'Missing required field: date'
    
    # Validate date format
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return 'Invalid date format. Use YYYY-MM-DD'
    
    if not assignments:
        return 'Missing required field: assignments'
    
    if not isinstance(assignments, list):
        return 'Field assignments must be an array'
    
    if len(assignments) == 0:
        return 'Assignments array is empty'
    
    # Validate each assignment
    for i, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            return f'Assignment {i} must be an object'
        
        if not assignment.get('inspector_id'):
            return f'Assignment {i} missing inspector_id'
        
        inspection_ids = assignment.get('inspection_ids')
        if not inspection_ids:
            return f'Assignment {i} missing inspection_ids'
        
        if not isinstance(inspection_ids, list):
            return f'Assignment {i} inspection_ids must be an array'
    
    return None


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({
        'error': 'Endpoint not found',
        'available_endpoints': [
            'GET /health',
            'POST /optimize-routes',
            'POST /preview-routes'
        ]
    }), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({
        'error': 'Internal server error',
        'message': str(e)
    }), 500


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    
    print(f"{'='*60}")
    print(f"🚀 Route Optimizer API")
    print(f"   Port: {port}")
    print(f"   Debug: {debug}")
    print(f"   Time: {datetime.utcnow().isoformat()}")
    print(f"{'='*60}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)
