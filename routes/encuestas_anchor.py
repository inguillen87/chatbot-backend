"""Endpoints for generating and publishing cryptographic anchors."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_anchor_service import (
    build_snapshot,
    generate_merkle_proof,
    list_snapshots,
    publish_snapshot,
)
from services.encuestas_service import EncuestaError
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


encuestas_anchor_bp = Blueprint("encuestas_anchor_bp", __name__, url_prefix="/api/encuestas/<int:encuesta_id>/anchor")
encuestas_anchor_legacy_bp = Blueprint(
    "encuestas_anchor_legacy_bp", __name__, url_prefix="/admin/encuestas/<int:encuesta_id>"
)
encuestas_anchor_admin_bp = Blueprint(
    "encuestas_anchor_admin_bp", __name__, url_prefix="/api/admin/encuestas/<int:encuesta_id>"
)
encuestas_anchor_municipal_bp = Blueprint(
    "encuestas_anchor_municipal_bp", __name__, url_prefix="/api/municipal/encuestas/<int:encuesta_id>"
)
encuestas_anchor_admin_surveys_bp = Blueprint(
    "encuestas_anchor_admin_surveys_bp", __name__, url_prefix="/api/admin/surveys/<int:encuesta_id>"
)
encuestas_anchor_legacy_surveys_bp = Blueprint(
    "encuestas_anchor_legacy_surveys_bp", __name__, url_prefix="/admin/surveys/<int:encuesta_id>"
)
encuestas_anchor_municipal_surveys_bp = Blueprint(
    "encuestas_anchor_municipal_surveys_bp", __name__, url_prefix="/api/municipal/surveys/<int:encuesta_id>"
)


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


@encuestas_anchor_legacy_bp.before_request
@encuestas_anchor_admin_bp.before_request
@encuestas_anchor_municipal_bp.before_request
@encuestas_anchor_admin_surveys_bp.before_request
@encuestas_anchor_legacy_surveys_bp.before_request
@encuestas_anchor_municipal_surveys_bp.before_request
def _check_feature_legacy():
    guard = _feature_guard()
    if guard:
        return guard
    return None


def _snapshots_response(encuesta_id: int, current_user):
    try:
        data = list_snapshots(encuesta_id, current_user)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify(data), 200


def _handle_snapshot_creation(encuesta_id: int, current_user):
    data = request.get_json(force=True) or {}
    desde = data.get("desde")
    hasta = data.get("hasta")
    try:
        snap = build_snapshot(encuesta_id, desde, hasta, current_user)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return (
        jsonify(
            {
                "snapshot_id": snap.id,
                "root_hash": snap.root_hash,
                "total_respuestas": snap.total_respuestas,
                "desde_at": snap.desde_at.isoformat(),
                "hasta_at": snap.hasta_at.isoformat(),
            }
        ),
        200,
    )


def _handle_snapshot_publish(snapshot_id: int):
    chain = (request.get_json(silent=True) or {}).get("chain", "polygon")
    try:
        snap = publish_snapshot(snapshot_id, chain=chain)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify({"ok": True, "tx_id": snap.tx_id, "anchor_status": snap.anchor_status}), 200


def _handle_snapshot_verify(snapshot_id: int):
    respuesta_id = request.args.get("respuesta_id", type=int)
    if not respuesta_id:
        return jsonify({"error": "respuesta_id requerido"}), 400
    try:
        data = generate_merkle_proof(snapshot_id, respuesta_id)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify({"ok": True, **data}), 200


@encuestas_anchor_bp.route("/snapshot", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def snapshot(current_user, encuesta_id: int):
    return _handle_snapshot_creation(encuesta_id, current_user)


@encuestas_anchor_legacy_bp.route("/snapshot", methods=["POST"])
@encuestas_anchor_admin_bp.route("/snapshot", methods=["POST"])
@encuestas_anchor_municipal_bp.route("/snapshot", methods=["POST"])
@encuestas_anchor_admin_surveys_bp.route("/snapshot", methods=["POST"])
@encuestas_anchor_legacy_surveys_bp.route("/snapshot", methods=["POST"])
@encuestas_anchor_municipal_surveys_bp.route("/snapshot", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def snapshot_legacy(current_user, encuesta_id: int):
    return _handle_snapshot_creation(encuesta_id, current_user)


@encuestas_anchor_bp.route("/publish/<int:snapshot_id>", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def publish(current_user, encuesta_id: int, snapshot_id: int):
    return _handle_snapshot_publish(snapshot_id)


@encuestas_anchor_legacy_bp.route("/publish/<int:snapshot_id>", methods=["POST"])
@encuestas_anchor_admin_bp.route("/publish/<int:snapshot_id>", methods=["POST"])
@encuestas_anchor_municipal_bp.route("/publish/<int:snapshot_id>", methods=["POST"])
@encuestas_anchor_admin_surveys_bp.route("/publish/<int:snapshot_id>", methods=["POST"])
@encuestas_anchor_legacy_surveys_bp.route("/publish/<int:snapshot_id>", methods=["POST"])
@encuestas_anchor_municipal_surveys_bp.route("/publish/<int:snapshot_id>", methods=["POST"])
@token_requerido
@require_role("admin", "super_admin")
def publish_legacy(current_user, encuesta_id: int, snapshot_id: int):
    return _handle_snapshot_publish(snapshot_id)


@encuestas_anchor_bp.route("/<int:snapshot_id>/verify", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin", "empleado")
def verify(current_user, encuesta_id: int, snapshot_id: int):
    return _handle_snapshot_verify(snapshot_id)


@encuestas_anchor_legacy_bp.route("/<int:snapshot_id>/verify", methods=["GET"])
@encuestas_anchor_admin_bp.route("/<int:snapshot_id>/verify", methods=["GET"])
@encuestas_anchor_municipal_bp.route("/<int:snapshot_id>/verify", methods=["GET"])
@encuestas_anchor_admin_surveys_bp.route("/<int:snapshot_id>/verify", methods=["GET"])
@encuestas_anchor_legacy_surveys_bp.route("/<int:snapshot_id>/verify", methods=["GET"])
@encuestas_anchor_municipal_surveys_bp.route("/<int:snapshot_id>/verify", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin", "empleado")
def verify_legacy(current_user, encuesta_id: int, snapshot_id: int):
    return _handle_snapshot_verify(snapshot_id)


@encuestas_anchor_bp.route("/snapshots", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin", "empleado")
def list_snapshots_api(current_user, encuesta_id: int):
    return _snapshots_response(encuesta_id, current_user)


@encuestas_anchor_legacy_bp.route("/snapshots", methods=["GET"])
@encuestas_anchor_admin_bp.route("/snapshots", methods=["GET"])
@encuestas_anchor_municipal_bp.route("/snapshots", methods=["GET"])
@encuestas_anchor_admin_surveys_bp.route("/snapshots", methods=["GET"])
@encuestas_anchor_legacy_surveys_bp.route("/snapshots", methods=["GET"])
@encuestas_anchor_municipal_surveys_bp.route("/snapshots", methods=["GET"])
@token_requerido
@require_role("admin", "super_admin", "empleado")
def list_snapshots_legacy(current_user, encuesta_id: int):
    return _snapshots_response(encuesta_id, current_user)
