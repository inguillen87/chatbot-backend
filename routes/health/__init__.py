from flask import Blueprint, jsonify
from extensions import db
from sqlalchemy import text

health_bp = Blueprint('health', __name__, url_prefix='/api/health')

@health_bp.route('/', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    status = {"status": "ok", "db": "unknown"}
    try:
        db.session.execute(text("SELECT 1"))
        status["db"] = "connected"
    except Exception as e:
        status["db"] = "error"
        status["error"] = str(e)
        return jsonify(status), 500

    return jsonify(status), 200
