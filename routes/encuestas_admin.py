"""Administrative endpoints for managing surveys."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    update_encuesta,
    publicar_encuesta,
    cerrar_encuesta,
    list_encuestas,
    get_encuesta,
    list_respuestas,
    serialize_encuesta,
    serialize_respuesta,
)
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


def _create_admin_blueprint(name: str, url_prefix: str) -> Blueprint:
    """Return a blueprint that exposes the admin survey endpoints."""

    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    @bp.before_request
    def _check_feature():  # pragma: no cover - simple guard
        guard = _feature_guard()
        if guard:
            return guard
        return None

    @bp.route("", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def crear_encuesta_endpoint(current_user):
        try:
            encuesta = create_encuesta(request.get_json(force=True), current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify({"id": encuesta.id, "estado": encuesta.estado}), 201

    @bp.route("/<int:encuesta_id>", methods=["PUT"])
    @token_requerido
    @require_role("admin", "super_admin")
    def editar_encuesta_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = update_encuesta(encuesta_id, request.get_json(force=True), current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_encuesta(encuesta)), 200

    @bp.route("/<int:encuesta_id>/publicar", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def publicar_endpoint(current_user, encuesta_id: int):
        try:
            encuesta, link = publicar_encuesta(encuesta_id, current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        base_url = (
            current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
            or request.host_url.rstrip("/")
        )
        url_publica = f"{base_url}/e/{link.slug_publico}"
        return (
            jsonify({"ok": True, "slug_publico": link.slug_publico, "url_publica": url_publica}),
            200,
        )

    @bp.route("/<int:encuesta_id>/cerrar", methods=["POST"])
    @token_requerido
    @require_role("admin", "super_admin")
    def cerrar_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = cerrar_encuesta(encuesta_id, current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify({"ok": True, "estado": encuesta.estado}), 200

    @bp.route("", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def listar_encuestas_endpoint(current_user):
        estado = request.args.get("estado")
        try:
            tenant_id = (
                getattr(current_user, "municipio_id", None)
                or getattr(current_user, "empresa_id", None)
                or current_user.id
            )
            encuestas = list_encuestas(tenant_id, estado)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify([serialize_encuesta(e) for e in encuestas]), 200

    @bp.route("/<int:encuesta_id>", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def detalle_encuesta_endpoint(current_user, encuesta_id: int):
        try:
            encuesta = get_encuesta(encuesta_id, user=current_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_encuesta(encuesta)), 200

    @bp.route("/<int:encuesta_id>/respuestas", methods=["GET"])
    @token_requerido
    @require_role("admin", "super_admin")
    def listar_respuestas_endpoint(current_user, encuesta_id: int):
        limit = request.args.get("limit", default=None, type=int)
        offset = request.args.get("offset", default=None, type=int)
        try:
            encuesta, respuestas, total, limit_value, offset_value = list_respuestas(
                encuesta_id,
                current_user,
                limit=limit,
                offset=offset,
            )
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        payload = {
            "encuesta_id": encuesta.id,
            "total": total,
            "limit": limit_value,
            "offset": offset_value,
            "respuestas": [serialize_respuesta(resp) for resp in respuestas],
        }
        return jsonify(payload), 200

    return bp


encuestas_admin_bp = _create_admin_blueprint("encuestas_admin_bp", "/api/encuestas")
encuestas_admin_legacy_bp = _create_admin_blueprint(
    "encuestas_admin_legacy_bp", "/admin/encuestas"
)
