from flask import Blueprint, jsonify
from services.qdrant_service import get_qdrant_client
import logging

health_bp = Blueprint('health_bp', __name__)
logger = logging.getLogger(__name__)

@health_bp.route('/health', methods=['GET'])
def health_check():
    """Health check for backend services."""
    status = {"status": "ok", "services": {}}

    # Check Qdrant
    try:
        qdrant_cli = get_qdrant_client()
        if qdrant_cli:
            qdrant_cli.get_collections()
            status["services"]["qdrant"] = "connected"
        else:
            status["services"]["qdrant"] = "not_configured"
    except Exception as e:
        logger.error(f"Health check failed for Qdrant: {e}")
        status["services"]["qdrant"] = "error"
        status["status"] = "degraded"

    return jsonify(status)
