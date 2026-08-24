import logging
import re
import uuid

from flask import Blueprint, current_app, g, jsonify, request
from extensions import db
from sqlalchemy import text

from services.runtime_readiness import get_cached_runtime_readiness
from utils.runtime_environment import is_production_runtime

health_bp = Blueprint('health', __name__, url_prefix='/api/health')
runtime_readiness_bp = Blueprint('runtime_readiness', __name__)
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
logger = logging.getLogger(__name__)


@health_bp.route('/', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    status = {"status": "ok", "db": "unknown"}
    try:
        db.session.execute(text("SELECT 1"))
        status["db"] = "connected"
    except Exception as exc:
        logger.warning(
            "Legacy database health probe failed error_type=%s",
            type(exc).__name__,
        )
        status["status"] = "degraded"
        status["db"] = "error"
        return jsonify(status), 500

    return jsonify(status), 200


def _request_id() -> str:
    candidate = str(
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or getattr(g, "request_id", None)
        or ""
    ).strip()
    request_id = (
        candidate
        if _REQUEST_ID_PATTERN.fullmatch(candidate)
        else uuid.uuid4().hex
    )
    g.request_id = request_id
    return request_id


@runtime_readiness_bp.route('/health/ready', methods=['GET', 'HEAD'])
def readiness_check():
    """Report whether required runtime dependencies can serve traffic."""

    request_id = _request_id()
    payload = get_cached_runtime_readiness(
        engine=db.engine,
        redis_uri=current_app.config.get("RATELIMIT_STORAGE_URI"),
        production_like=is_production_runtime(
            config_env=current_app.config.get("ENV")
        ),
        database_timeout_seconds=current_app.config.get(
            "READINESS_DATABASE_TIMEOUT_SECONDS",
            1.5,
        ),
        redis_timeout_seconds=current_app.config.get(
            "READINESS_REDIS_TIMEOUT_SECONDS",
            1.0,
        ),
        cache_ttl_seconds=current_app.config.get(
            "READINESS_CACHE_TTL_SECONDS",
            1.0,
        ),
    )
    payload["request_id"] = request_id
    response = jsonify(payload)
    response.status_code = 200 if payload["ready"] else 503
    response.headers["X-Request-Id"] = request_id
    response.headers["Cache-Control"] = "no-store"
    return response
