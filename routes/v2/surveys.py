from __future__ import annotations

from flask import Blueprint, jsonify, request

from extensions import db
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.encuestas_service import (
    EncuestaError,
    get_encuesta,
    get_public_encuesta,
    list_encuestas,
    save_respuesta,
    serialize_encuesta,
    serialize_public_encuesta,
)

v2_surveys_bp = Blueprint("v2_surveys", __name__, url_prefix="/api/v2")
v2_public_surveys_bp = Blueprint("v2_public_surveys", __name__, url_prefix="/api/v2/public/surveys")


def _resolve_tenant_or_error(*, required: bool = True):
    explicit_slug = (request.headers.get("X-Tenant-Slug") or request.args.get("tenant_slug") or "").strip()
    if required and not explicit_slug:
        return None, (jsonify({"error": "X-Tenant-Slug es obligatorio en surveys v2"}), 400)
    try:
        return resolve_tenant_v2(required=required, explicit_slug=explicit_slug or None), None
    except V2TenantResolutionError as exc:
        return None, (jsonify({"error": exc.message}), exc.status_code)


@v2_surveys_bp.route("/surveys", methods=["GET"])
def list_surveys_v2():
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error

    estado = (request.args.get("estado") or request.args.get("status") or "").strip() or None
    encuestas = list_encuestas(tenant_id=tenant.id, estado=estado)
    return jsonify({"items": [serialize_encuesta(encuesta) for encuesta in encuestas], "total": len(encuestas)})


@v2_surveys_bp.route("/surveys/<int:survey_id>", methods=["GET"])
def survey_detail_v2(survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error

    try:
        encuesta = get_encuesta(survey_id, tenant_id=tenant.id)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(serialize_encuesta(encuesta))


@v2_surveys_bp.route("/surveys/public/<string:token>", methods=["GET"])
def survey_public_by_token_v2(token: str):
    tenant, error = _resolve_tenant_or_error(required=False)
    if error:
        return error

    preferred_tenant_id = tenant.id if tenant is not None else None
    try:
        encuesta = get_public_encuesta(token, preferred_tenant_id=preferred_tenant_id)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(serialize_public_encuesta(encuesta, slug_publico=token))


@v2_public_surveys_bp.route("/<string:token>/respond", methods=["POST"])
def respond_public_survey_v2(token: str):
    tenant, error = _resolve_tenant_or_error(required=False)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    request_ctx = {
        "ip": request.remote_addr,
        "user_agent": request.headers.get("User-Agent"),
        "referer": request.headers.get("Referer"),
        "anon_id": payload.get("anon_id") or payload.get("anonId") or request.cookies.get("anon_id"),
        "canal": payload.get("canal") or "web",
    }
    preferred_tenant_id = tenant.id if tenant is not None else None

    try:
        respuesta = save_respuesta(token, payload, request_ctx, preferred_tenant_id=preferred_tenant_id)
        db.session.commit()
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code
    except Exception:
        db.session.rollback()
        raise

    return jsonify({"ok": True, "respuesta_id": respuesta.id}), 201
