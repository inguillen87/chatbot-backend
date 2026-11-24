"""Superadmin switch for an organization's public brand identity."""
import re
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from cutover_writer_fence import cutover_writer_fence_enabled
from models import AuditEvent, TenantProfile, db
from services.brand_identity import KEY, BrandIdentityError, normalize, public_brand_identity, stored_brand_identity
from utils.auth_helpers import auth_sin_escrituras_implicitas, token_requerido
from utils.roles import is_authorized_superadmin_user

brand_identity_bp = Blueprint("brand_identity", __name__)
PATH = "/api/admin/tenants/<slug>/brand-identity"
CONTRACT = "organization.brand_identity_admin.v1"


def _response(value, status=200):
    response = jsonify({"contract_version": CONTRACT, **value})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _tenant(slug, *, lock=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug or ""):
        return None
    query = select(TenantProfile).where(TenantProfile.slug == slug)
    if lock:
        query = query.with_for_update(of=TenantProfile).execution_options(populate_existing=True)
    return db.session.execute(query).scalar_one_or_none()


def _state(tenant):
    return {"tenant": {"id": tenant.id, "slug": tenant.slug},
            "brand_identity": stored_brand_identity(tenant),
            "published": public_brand_identity(tenant) is not None}


@brand_identity_bp.get(PATH)
@token_requerido
@auth_sin_escrituras_implicitas
def read_brand_identity(current_user, slug):
    if not is_authorized_superadmin_user(current_user):
        return _response({"reason_code": "brand_identity_superadmin_required"}, 403)
    tenant = _tenant(slug)
    if tenant is None:
        return _response({"reason_code": "brand_identity_tenant_missing"}, 404)
    return _response(_state(tenant))


@brand_identity_bp.put(PATH)
@token_requerido
@auth_sin_escrituras_implicitas
def write_brand_identity(current_user, slug):
    """Body ``{"brand_identity": {...}}`` publishes it; ``{"brand_identity": null}`` removes it."""
    if not is_authorized_superadmin_user(current_user):
        return _response({"reason_code": "brand_identity_superadmin_required"}, 403)
    if cutover_writer_fence_enabled(current_app.config):
        return _response({"reason_code": "brand_identity_writer_fenced"}, 503)
    command = request.get_json(silent=True)
    if not isinstance(command, dict) or set(command) != {"brand_identity"}:
        return _response({"reason_code": "brand_identity_invalid"}, 400)
    try:
        value = None if command["brand_identity"] is None else normalize(command["brand_identity"])
    except BrandIdentityError as error:
        return _response({"reason_code": error.code}, 400)
    try:
        tenant = _tenant(slug, lock=True)
        if tenant is None:
            db.session.rollback()
            return _response({"reason_code": "brand_identity_tenant_missing"}, 404)
        config = dict(tenant.configuracion or {}) if isinstance(tenant.configuracion, dict) else {}
        if value is None:
            config.pop(KEY, None)
        else:
            config[KEY] = value
        tenant.configuracion = config
        db.session.add(AuditEvent(tenant_id=tenant.id, actor_user_id=current_user.id,
            event_type="brand_identity.updated" if value is not None else "brand_identity.removed",
            resource_type=CONTRACT, resource_id=uuid4().hex,
            details={"fields": sorted(value) if value else []}))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return _response({"reason_code": "brand_identity_storage_unavailable"}, 503)
    return _response(_state(tenant))
