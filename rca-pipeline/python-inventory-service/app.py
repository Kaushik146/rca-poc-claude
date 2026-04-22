"""
Python Inventory Service — Flask, port 5003

API (what Java's HttpInventoryClient expects):
  POST /reserve  { "product_id": str, "quantity": int } → { "reserved": bool }
  POST /release  { "product_id": str, "quantity": int } → { "released": bool }
  GET  /stock/<product_id>                              → { "available": int }

Cross-platform bug #4 (boundary):
  BUG:   stock > quantity   (last unit can never be reserved — off-by-one)
  FIX:   stock >= quantity

NOTE: This service uses an in-memory dict for stock data. All state is lost on
restart. For production use, replace _stock with a persistent store (e.g. Redis,
PostgreSQL, or SQLite).

PRODUCTION: Use gunicorn to run this app in production:
  gunicorn -w 4 -b 0.0.0.0:5003 app:app
This file keeps app.run() for local development only.
"""

import logging
import signal
import sys
import threading
import re
import uuid
from functools import wraps
from datetime import datetime
from flask import Flask, request, jsonify, g

# Configure logging with correlation ID
class CorrelationIdFilter(logging.Filter):
    def filter(self, record):
        record.correlation_id = getattr(logging.LogRecord, 'correlation_id', 'unknown')
        return True

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(correlation_id)s] %(message)s'
)
logger = logging.getLogger(__name__)
logger.addFilter(CorrelationIdFilter())

app = Flask(__name__)

# Security: Limit request payload to 1MB
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024

# ── Correlation ID propagation ──
@app.before_request
def before_request():
    """Read X-Correlation-ID from request header or generate a UUID."""
    correlation_id = request.headers.get('X-Correlation-ID', str(uuid.uuid4()))
    g.correlation_id = correlation_id
    # Add correlation ID to all log records for this request
    logging.LogRecord.correlation_id = correlation_id

@app.after_request
def after_request(response):
    """Add X-Correlation-ID to response header."""
    response.headers['X-Correlation-ID'] = g.get('correlation_id', 'unknown')
    return response

# In-memory stock store  { sku: int }
_stock = {
    "SKU-001": 100,
    "SKU-002": 5,
    "SKU-003": 1,    # Only 1 in stock — exposes off-by-one bug perfectly
    "SKU-NOSTOCK": 0,
}

# Thread safety
_stock_lock = threading.Lock()

# Track startup time for health check uptime
_startup_time = datetime.utcnow()


def validate_product_id(product_id):
    """
    Validate product_id: must be string, max 50 chars, alphanumeric + hyphen.
    Returns (is_valid, error_message)
    """
    if not isinstance(product_id, str):
        return False, "product_id must be a string"
    if len(product_id) == 0 or len(product_id) > 50:
        return False, "product_id must be 1-50 characters"
    if not re.match(r'^[a-zA-Z0-9\-]+$', product_id):
        return False, "product_id must contain only alphanumeric characters and hyphens"
    return True, None


def validate_quantity(quantity):
    """
    Validate quantity: must be positive integer, max 10000.
    Returns (is_valid, error_message)
    """
    if not isinstance(quantity, int):
        return False, "quantity must be an integer"
    if quantity <= 0 or quantity > 10000:
        return False, "quantity must be between 1 and 10000"
    return True, None


def cors_headers(f):
    """Decorator to add basic CORS headers to responses."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        response = f(*args, **kwargs)
        # Handle both jsonify responses and tuple responses
        if isinstance(response, tuple):
            body, status_code = response[0], response[1]
            headers = {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
            }
            return body, status_code, headers
        return response
    return decorated_function


def require_json_content_type(f):
    """Decorator to verify Content-Type is application/json for POST/PUT requests."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method in ['POST', 'PUT']:
            if not request.is_json:
                logger.warning(f"Invalid Content-Type for {request.method} {request.path}: {request.content_type}")
                return jsonify({"error": "Content-Type must be application/json"}), 415
        return f(*args, **kwargs)
    return decorated_function


@app.route("/stock/<product_id>", methods=["GET"])
@cors_headers
def get_stock(product_id):
    """Get current stock level for a product."""
    is_valid, error = validate_product_id(product_id)
    if not is_valid:
        logger.warning(f"Invalid product_id in GET /stock: {error}")
        return jsonify({"error": error}), 400

    with _stock_lock:
        available = _stock.get(product_id, 0)

    logger.info(f"GET /stock/{product_id} -> available={available}")
    return jsonify({"product_id": product_id, "available": available}), 200


@app.route("/reserve", methods=["POST"])
@cors_headers
@require_json_content_type
def reserve():
    """Reserve stock for a product. Returns 200 with reserved: true/false."""
    try:
        data = request.get_json()
        if data is None:
            logger.warning("POST /reserve: No JSON data provided")
            return jsonify({"error": "Request body must contain valid JSON"}), 400
    except Exception as e:
        logger.warning(f"POST /reserve: JSON parse error: {e}")
        return jsonify({"error": "Invalid JSON in request body"}), 400

    # Python expects snake_case field names from Java client
    product_id = data.get("product_id")
    quantity = data.get("quantity")

    # Validate inputs
    if product_id is None:
        logger.warning("POST /reserve: Missing product_id")
        return jsonify({"error": "product_id is required"}), 400

    is_valid, error = validate_product_id(product_id)
    if not is_valid:
        logger.warning(f"POST /reserve: Invalid product_id: {error}")
        return jsonify({"error": error}), 400

    if quantity is None:
        logger.warning("POST /reserve: Missing quantity")
        return jsonify({"error": "quantity is required"}), 400

    is_valid, error = validate_quantity(quantity)
    if not is_valid:
        logger.warning(f"POST /reserve: Invalid quantity for {product_id}: {error}")
        return jsonify({"error": error}), 400

    # Perform reservation with thread safety
    with _stock_lock:
        stock = _stock.get(product_id, 0)

        # ── CORRECT: stock >= quantity (can reserve if exactly enough) ──
        if stock >= quantity:
            _stock[product_id] = stock - quantity
            remaining = _stock[product_id]
            logger.info(f"POST /reserve: {product_id} x{quantity} SUCCESS (remaining={remaining})")
            return jsonify({"reserved": True, "remaining": remaining}), 200
        else:
            logger.info(f"POST /reserve: {product_id} x{quantity} FAILED (available={stock})")
            return jsonify({"reserved": False, "available": stock}), 409


@app.route("/release", methods=["POST"])
@cors_headers
@require_json_content_type
def release():
    """Release (add back) stock for a product."""
    try:
        data = request.get_json()
        if data is None:
            logger.warning("POST /release: No JSON data provided")
            return jsonify({"error": "Request body must contain valid JSON"}), 400
    except Exception as e:
        logger.warning(f"POST /release: JSON parse error: {e}")
        return jsonify({"error": "Invalid JSON in request body"}), 400

    product_id = data.get("product_id")
    quantity = data.get("quantity")

    # Validate inputs
    if product_id is None:
        logger.warning("POST /release: Missing product_id")
        return jsonify({"error": "product_id is required"}), 400

    is_valid, error = validate_product_id(product_id)
    if not is_valid:
        logger.warning(f"POST /release: Invalid product_id: {error}")
        return jsonify({"error": error}), 400

    if quantity is None:
        logger.warning("POST /release: Missing quantity")
        return jsonify({"error": "quantity is required"}), 400

    is_valid, error = validate_quantity(quantity)
    if not is_valid:
        logger.warning(f"POST /release: Invalid quantity for {product_id}: {error}")
        return jsonify({"error": error}), 400

    # Perform release with thread safety
    with _stock_lock:
        current_stock = _stock.get(product_id, 0)
        new_stock = current_stock + quantity
        _stock[product_id] = new_stock
        logger.info(f"POST /release: {product_id} x{quantity} (new_stock={new_stock})")
        return jsonify({"released": True, "stock": new_stock}), 200


@app.route("/health", methods=["GET"])
@cors_headers
def health():
    """Health check endpoint with uptime and item count."""
    uptime_seconds = (datetime.utcnow() - _startup_time).total_seconds()

    with _stock_lock:
        item_count = len(_stock)

    logger.info(f"GET /health: uptime={uptime_seconds}s, items={item_count}")
    return jsonify({
        "status": "ok",
        "uptime_seconds": uptime_seconds,
        "item_count": item_count
    }), 200


def handle_shutdown(signum, frame):
    """Graceful shutdown handler."""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    sys.exit(0)


# Register signal handlers for graceful shutdown
signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


if __name__ == "__main__":
    logger.info("Starting Python Inventory Service on port 5003")
    logger.info("For production, use: gunicorn -w 4 -b 0.0.0.0:5003 app:app")
    app.run(port=5003, debug=False)
