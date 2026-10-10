"""Fail-closed compatibility endpoints for the retired catalog wizard API."""

from flask import Blueprint, jsonify

from middleware.tenant_context import require_tenant
from routes.catalog_import import _exact_catalog_tenant
from utils.auth_helpers import token_requerido


catalog_bp = Blueprint("catalog_bp", __name__)


def _legacy_catalog_endpoint_disabled(current_user):
    tenant, tenant_error = _exact_catalog_tenant(current_user)
    if tenant_error is not None:
        return tenant_error
    response = jsonify(
        {
            "codigo": "catalog_legacy_endpoint_disabled",
            "mensaje": "Este endpoint fue reemplazado por la importacion administrada del tenant.",
            "tenant_slug": tenant.slug,
            "replacement": {
                "create": "/api/admin/catalog/import",
                "commit": "/api/admin/catalog/import/{upload_id}/commit",
            },
        }
    )
    response.status_code = 410
    return response


@catalog_bp.route("/api/catalog/upload", methods=["POST"])
@token_requerido
@require_tenant
def upload_catalog_preview(current_user):
    return _legacy_catalog_endpoint_disabled(current_user)


@catalog_bp.route("/api/catalog/confirm", methods=["POST"])
@token_requerido
@require_tenant
def confirm_catalog_upload(current_user):
    return _legacy_catalog_endpoint_disabled(current_user)
