"""Endpoints for generating and publishing cryptographic anchors."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_anchor_service import (
    build_snapshot,
    generate_merkle_proof,
    publish_snapshot,
)
from services.encuestas_service import EncuestaError
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


encuestas_anchor_bp = Blueprint("encuestas_anchor_bp", __name__, url_prefix="/api/encuestas/<int:encuesta_id>/anchor")


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


@encuestas_anchor_bp.before_request
def _check_feature():
    guard = _feature_guard()
    if guard:
        return guard
    return None


@encuestas_anchor_bp.route("/snapshot", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def snapshot(current_user, encuesta_id: int):
    data = request.get_json(force=True)
    desde = data.get("desde")
    hasta = data.get("hasta")
    try:
        snap = build_snapshot(encuesta_id, desde, hasta, current_user)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify({
        "snapshot_id": snap.id,
        "root_hash": snap.root_hash,
        "total_respuestas": snap.total_respuestas,
        "desde_at": snap.desde_at.isoformat(),
        "hasta_at": snap.hasta_at.isoformat(),
    })


@encuestas_anchor_bp.route("/publish/<int:snapshot_id>", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def publish(current_user, encuesta_id: int, snapshot_id: int):
    chain = (request.get_json(silent=True) or {}).get("chain", "polygon")
    try:
        snap = publish_snapshot(snapshot_id, chain=chain)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify({"ok": True, "tx_id": snap.tx_id, "anchor_status": snap.anchor_status})


@encuestas_anchor_bp.route("/<int:snapshot_id>/verify", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin", "empleado")
def verify(current_user, encuesta_id: int, snapshot_id: int):
    respuesta_id = request.args.get("respuesta_id", type=int)
    if not respuesta_id:
        return jsonify({"error": "respuesta_id requerido"}), 400
    try:
        data = generate_merkle_proof(snapshot_id, respuesta_id)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify({"ok": True, **data})
