from __future__ import annotations

from typing import Any

from flask import Blueprint, g, jsonify, request

from extensions import db
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.encuestas_analytics_service import get_dashboard_bundle
from services.encuestas_service import (
    EncuestaError,
    cerrar_encuesta,
    create_encuesta,
    get_encuesta,
    get_public_encuesta,
    list_encuestas,
    publicar_encuesta,
    save_respuesta,
    serialize_encuesta,
    serialize_public_encuesta,
    update_encuesta,
)
from utils.auth_helpers import token_requerido
from utils.permissions import require_role

v2_surveys_bp = Blueprint("v2_surveys", __name__, url_prefix="/api/v2")
v2_public_surveys_bp = Blueprint("v2_public_surveys", __name__, url_prefix="/api/v2/public/surveys")


_TYPE_MAP = {
    "single": "opcion_unica",
    "multi": "opcion_multiple",
    "rating": "rating_emoji",
    "text": "abierta",
    "nps": "rating_emoji",
    "ranking": "opcion_multiple",
    "location": "abierta",
}


def _resolve_tenant_or_error(*, required: bool = True):
    explicit_slug = (request.headers.get("X-Tenant-Slug") or request.args.get("tenant_slug") or "").strip()
    if required and not explicit_slug:
        return None, (jsonify({"error": "X-Tenant-Slug es obligatorio en surveys v2"}), 400)
    try:
        return resolve_tenant_v2(required=required, explicit_slug=explicit_slug or None), None
    except V2TenantResolutionError as exc:
        return None, (jsonify({"error": exc.message}), exc.status_code)


def _user_tenant_candidates(current_user) -> set[int]:
    values = {
        getattr(current_user, "tenant_id", None),
        getattr(current_user, "municipio_id", None),
        getattr(current_user, "empresa_id", None),
        getattr(current_user, "pyme_id", None),
    }
    normalized: set[int] = set()
    for value in values:
        if value in (None, ""):
            continue
        try:
            normalized.add(int(value))
        except (TypeError, ValueError):
            continue
    return normalized


def _enforce_tenant_access(current_user, tenant) -> tuple[bool, tuple | None]:
    role = str(getattr(current_user, "rol", "") or "").lower()
    if role == "super_admin":
        # super_admin is allowed only when tenant was explicitly resolved by v2 resolver
        return True, None

    candidates = _user_tenant_candidates(current_user)
    if tenant.id in candidates:
        return True, None

    if (getattr(current_user, "tenant_slug", None) or "").strip().lower() == (tenant.slug or "").strip().lower():
        return True, None

    return False, (jsonify({"error": "Permisos insuficientes para este tenant"}), 403)


def _normalize_question(question: dict[str, Any], index: int) -> dict[str, Any]:
    q_type = str(question.get("type") or question.get("tipo") or "single").strip().lower()
    internal_type = _TYPE_MAP.get(q_type, q_type)

    return {
        "tipo": internal_type,
        "texto": question.get("label") or question.get("texto") or question.get("title") or "",
        "obligatoria": bool(question.get("required") if "required" in question else question.get("obligatoria", False)),
        "orden": question.get("order_index") if question.get("order_index") is not None else question.get("orden", index),
        "opciones": question.get("options") or question.get("opciones") or [],
        "logica_condicional": question.get("conditional_logic") or question.get("logica_condicional"),
    }


def _normalize_admin_payload(payload: dict[str, Any]) -> dict[str, Any]:
    questions = payload.get("questions") if isinstance(payload.get("questions"), list) else payload.get("preguntas")
    normalized_questions = []
    for index, question in enumerate(questions or [], start=1):
        if isinstance(question, dict):
            normalized_questions.append(_normalize_question(question, index))

    channel = str(payload.get("channel") or payload.get("canal") or "web").strip().lower()

    return {
        "titulo": payload.get("title") or payload.get("titulo"),
        "descripcion": payload.get("description") or payload.get("descripcion"),
        "tipo": payload.get("survey_type") or payload.get("tipo") or "opinion",
        "inicio_at": payload.get("opens_at") or payload.get("inicio_at"),
        "fin_at": payload.get("closes_at") or payload.get("fin_at"),
        "preguntas": normalized_questions,
        "tags": payload.get("tags") or [f"channel:{channel}"],
        "anonimo_permitido": bool(payload.get("allow_anonymous", True)),
        "politica_unicidad": payload.get("uniqueness_policy") or payload.get("politica_unicidad") or "anon_id",
    }


@v2_surveys_bp.route("/surveys", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def list_surveys_v2(current_user):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    estado = (request.args.get("estado") or request.args.get("status") or "").strip() or None
    encuestas = list_encuestas(tenant_id=tenant.id, estado=estado)
    return jsonify({"items": [serialize_encuesta(encuesta) for encuesta in encuestas], "total": len(encuestas)})


@v2_surveys_bp.route("/surveys", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def create_survey_v2(current_user):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    payload = _normalize_admin_payload(request.get_json(silent=True) or {})
    g.tenant_profile = tenant
    try:
        encuesta = create_encuesta(payload, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(serialize_encuesta(encuesta)), 201


@v2_surveys_bp.route("/surveys/<int:survey_id>", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def survey_detail_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    try:
        encuesta = get_encuesta(survey_id, tenant_id=tenant.id)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(serialize_encuesta(encuesta))


@v2_surveys_bp.route("/surveys/<int:survey_id>", methods=["PATCH"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def update_survey_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    payload = _normalize_admin_payload(request.get_json(silent=True) or {})
    g.tenant_profile = tenant
    try:
        encuesta = update_encuesta(survey_id, payload, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(serialize_encuesta(encuesta))


@v2_surveys_bp.route("/surveys/<int:survey_id>/publish", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def publish_survey_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    g.tenant_profile = tenant
    try:
        encuesta, link = publicar_encuesta(survey_id, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code

    payload = serialize_encuesta(encuesta)
    payload["public_token"] = link.slug_publico
    payload["public_url"] = f"/api/v2/public/surveys/{link.slug_publico}"
    return jsonify(payload)


@v2_surveys_bp.route("/surveys/<int:survey_id>/close", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def close_survey_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    g.tenant_profile = tenant
    try:
        encuesta = cerrar_encuesta(survey_id, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(serialize_encuesta(encuesta))


@v2_surveys_bp.route("/surveys/<int:survey_id>/analytics", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def survey_analytics_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    try:
        encuesta = get_encuesta(survey_id, tenant_id=tenant.id)
        data = get_dashboard_bundle(encuesta.id, filtros={}, granularity=request.args.get("granularity", "day"))
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(data)


@v2_public_surveys_bp.route("/<string:token>", methods=["GET"])
def survey_public_by_token_v2(token: str):
    tenant, error = _resolve_tenant_or_error(required=False)
    if error:
        return error

    preferred_tenant_id = tenant.id if tenant is not None else None
    try:
        encuesta = get_public_encuesta(token, preferred_tenant_id=preferred_tenant_id)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    payload = serialize_public_encuesta(encuesta, slug_publico=token)
    payload.pop("tenant_id", None)
    return jsonify(payload)


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
        "canal": payload.get("source") or payload.get("channel") or payload.get("canal") or "public_link",
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
