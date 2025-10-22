"""Public endpoints for the progressive web app multi-tenant experience."""

from __future__ import annotations

from typing import List, Tuple

from flask import Blueprint, abort, g, jsonify, request

from models import MunicipioPost, TenantProfile
from services.encuestas_service import (
    EncuestaError,
    get_public_encuesta,
    list_public_encuestas_for_tenant,
    save_respuesta,
    serialize_public_encuesta,
)


pwa_public_bp = Blueprint("pwa_public", __name__, url_prefix="/api/pwa/public")


def _require_tenant() -> TenantProfile:
    tenant = getattr(g, "tenant_profile", None)
    if tenant is None:
        abort(404, description="Tenant no encontrado")
    return tenant


def _resolve_encuestas_tenant_id(tenant: TenantProfile) -> int | None:
    if tenant.encuestas_tenant_id:
        return tenant.encuestas_tenant_id
    if tenant.municipio_id:
        return tenant.municipio_id
    if tenant.pyme_id:
        return tenant.pyme_id
    return None


def _posts_query_for_tenant(tenant: TenantProfile):
    owner_id = tenant.municipio_id or tenant.pyme_id
    if not owner_id:
        return MunicipioPost.query.filter(False)
    return MunicipioPost.query.filter(MunicipioPost.municipio_id == owner_id)


@pwa_public_bp.get("/tenant")
def tenant_info():
    tenant = _require_tenant()
    return jsonify(tenant.to_public_dict())


@pwa_public_bp.get("/surveys")
def list_surveys():
    tenant = _require_tenant()
    tenant_id = _resolve_encuestas_tenant_id(tenant)
    if not tenant_id:
        return jsonify([])

    encuestas: List[Tuple[object, str]] = list_public_encuestas_for_tenant(tenant_id, limit=25)
    payload = [
        serialize_public_encuesta(encuesta, slug_publico=slug)
        for encuesta, slug in encuestas
    ]
    return jsonify(payload)


@pwa_public_bp.get("/surveys/<slug>")
def get_survey(slug: str):
    tenant = _require_tenant()
    tenant_id = _resolve_encuestas_tenant_id(tenant)
    try:
        encuesta = get_public_encuesta(slug)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    if tenant_id and encuesta.tenant_id != tenant_id:
        abort(404, description="Encuesta no encontrada")

    return jsonify(serialize_public_encuesta(encuesta, slug_publico=slug))


@pwa_public_bp.post("/surveys/<slug>/respond")
def respond_survey(slug: str):
    tenant = _require_tenant()
    tenant_id = _resolve_encuestas_tenant_id(tenant)
    try:
        encuesta = get_public_encuesta(slug)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    if tenant_id and encuesta.tenant_id != tenant_id:
        abort(404, description="Encuesta no encontrada")

    payload = request.get_json(silent=True) or {}
    request_ctx = {
        "ip": request.headers.get("X-Forwarded-For") or request.remote_addr,
        "anon_id": request.cookies.get("Anon-Id") or request.cookies.get("anon_id"),
        "canal": "pwa",
    }
    try:
        respuesta = save_respuesta(slug, payload, request_ctx)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify({"id": respuesta.id})


@pwa_public_bp.get("/news")
def list_news():
    tenant = _require_tenant()
    query = _posts_query_for_tenant(tenant).filter(MunicipioPost.tipo_post != "evento")
    items = (
        query.order_by(MunicipioPost.fecha_publicacion.desc())
        .limit(50)
        .all()
    )
    return jsonify([item.to_dict() for item in items])


@pwa_public_bp.get("/events")
def list_events():
    tenant = _require_tenant()
    query = _posts_query_for_tenant(tenant).filter(MunicipioPost.tipo_post == "evento")
    items = (
        query.order_by(MunicipioPost.fecha_evento_inicio.asc().nullslast())
        .limit(100)
        .all()
    )
    return jsonify([item.to_dict() for item in items])
