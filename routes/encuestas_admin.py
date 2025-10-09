"""Administrative endpoints for managing surveys."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    update_encuesta,
    publicar_encuesta,
    cerrar_encuesta,
    list_encuestas,
    get_encuesta,
    serialize_encuesta,
)
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


encuestas_admin_bp = Blueprint("encuestas_admin_bp", __name__, url_prefix="/api/encuestas")


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


@encuestas_admin_bp.before_request
def _check_feature():
    guard = _feature_guard()
    if guard:
        return guard
    return None


@encuestas_admin_bp.route("", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def crear_encuesta(current_user):
    try:
        encuesta = create_encuesta(request.get_json(force=True), current_user)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify({"id": encuesta.id, "estado": encuesta.estado}), 201


@encuestas_admin_bp.route("/<int:encuesta_id>", methods=["PUT"])
@token_requerido
@require_role("admin", "super_admin")
def editar_encuesta(current_user, encuesta_id: int):
    try:
        encuesta = update_encuesta(encuesta_id, request.get_json(force=True), current_user)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify(serialize_encuesta(encuesta)), 200


@encuestas_admin_bp.route("/<int:encuesta_id>/publicar", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def publicar(current_user, encuesta_id: int):
    try:
        encuesta, link = publicar_encuesta(encuesta_id, current_user)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code

    base_url = request.host_url.rstrip("/")
    url_publica = f"{base_url}/e/{link.slug_publico}"
    return jsonify({"ok": True, "slug_publico": link.slug_publico, "url_publica": url_publica}), 200


@encuestas_admin_bp.route("/<int:encuesta_id>/cerrar", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def cerrar(current_user, encuesta_id: int):
    try:
        encuesta = cerrar_encuesta(encuesta_id, current_user)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify({"ok": True, "estado": encuesta.estado}), 200


@encuestas_admin_bp.route("", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin")
def listar_encuestas(current_user):
    estado = request.args.get("estado")
    try:
        tenant_id = getattr(current_user, "municipio_id", None) or getattr(current_user, "empresa_id", None) or current_user.id
        encuestas = list_encuestas(tenant_id, estado)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify([serialize_encuesta(e) for e in encuestas]), 200


@encuestas_admin_bp.route("/<int:encuesta_id>", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin")
def detalle_encuesta(current_user, encuesta_id: int):
    try:
        encuesta = get_encuesta(encuesta_id, user=current_user)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify(serialize_encuesta(encuesta)), 200
