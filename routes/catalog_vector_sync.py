"""Endpoints to report the catalog vector synchronization status."""

from __future__ import annotations

from flask import Blueprint, jsonify

from models import TenantProfile
from routes.auth import token_requerido
from services.catalog_vector_sync_service import catalog_vector_sync_service
from services.plan_access import (
    integration_access_payload,
    integration_plan_required_payload,
    plan_allows_integration_feature,
)


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


def _tenant_for_pyme(pyme_id: int):
    return TenantProfile.query.filter_by(pyme_id=pyme_id).first()


def _catalog_plan_required_response(pyme_id: int):
    tenant = _tenant_for_pyme(pyme_id)
    response = jsonify(
        integration_plan_required_payload(
            tenant,
            "catalog_management",
            render_as="integration_locked",
        )
    )
    response.status_code = 403
    return response


@catalog_vector_sync_bp.route("/status", methods=["GET", "OPTIONS"])
@token_requerido
def get_catalog_vector_status(current_user, pyme_id: int):
    """Return a summarized status of the catalog vector sync for a PyME."""

    if not _user_can_access_pyme(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para consultar esta PYME."}), 403

    status = catalog_vector_sync_service.get_status(pyme_id)
    payload = status.to_dict()
    payload["access"] = integration_access_payload(_tenant_for_pyme(pyme_id))
    return jsonify(payload), 200


@catalog_vector_sync_bp.route("", methods=["POST", "OPTIONS"])
@token_requerido
def trigger_catalog_vector_sync(current_user, pyme_id: int):
    """Acknowledge catalog vector sync triggers from the frontend."""

    if not _user_can_access_pyme(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para consultar esta PYME."}), 403

    if not plan_allows_integration_feature(_tenant_for_pyme(pyme_id), "catalog_management"):
        return _catalog_plan_required_response(pyme_id)

    return jsonify({"status": "accepted"}), 202
