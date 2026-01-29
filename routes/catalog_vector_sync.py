"""Endpoints to report the catalog vector synchronization status."""

from __future__ import annotations

from flask import Blueprint, jsonify

from routes.auth import token_requerido
from services.catalog_vector_sync_service import catalog_vector_sync_service


catalog_vector_sync_bp = Blueprint(
    "catalog_vector_sync_bp",
    __name__,
    url_prefix="/pymes/<int:pyme_id>/catalog-vector-sync",
)


def _user_can_access_pyme(current_user, pyme_id: int) -> bool:
    """Basic permission check for PyME scoped endpoints."""

    if current_user is None:
        return False

    if getattr(current_user, "id", None) == pyme_id:
        return True

    if getattr(current_user, "empresa_id", None) == pyme_id:
        return True

    # Allow top-level admins (empresa_id is None and rol == 'admin') to introspect their own entity.
    if (
        getattr(current_user, "rol", None) == "admin"
        and getattr(current_user, "empresa_id", None) in (None, current_user.id)
        and getattr(current_user, "id", None) == pyme_id
    ):
        return True

    return False


@catalog_vector_sync_bp.route("/status", methods=["GET", "OPTIONS"])
@token_requerido
def get_catalog_vector_status(current_user, pyme_id: int):
    """Return a summarized status of the catalog vector sync for a PyME."""

    if not _user_can_access_pyme(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para consultar esta PYME."}), 403

    status = catalog_vector_sync_service.get_status(pyme_id)
    return jsonify(status.to_dict()), 200


@catalog_vector_sync_bp.route("", methods=["POST", "OPTIONS"])
@token_requerido
def trigger_catalog_vector_sync(current_user, pyme_id: int):
    """Acknowledge catalog vector sync triggers from the frontend."""

    if not _user_can_access_pyme(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para consultar esta PYME."}), 403

    return jsonify({"status": "accepted"}), 202
