"""Analytics API endpoints for surveys."""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_analytics_service import (
    export_csv,
    get_heatmap,
    get_summary,
    get_timeseries,
)
from services.encuestas_service import EncuestaError
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


encuestas_analytics_bp = Blueprint(
    "encuestas_analytics_bp", __name__, url_prefix="/api/encuestas/<int:encuesta_id>/analytics"
)


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


@encuestas_analytics_bp.before_request
def _check_feature():
    guard = _feature_guard()
    if guard:
        return guard
    return None


def _parse_filtros() -> dict:
    filtros = {}
    for key in ("desde", "hasta", "canal", "utm_source", "utm_campaign"):
        value = request.args.get(key)
        if value:
            filtros[key] = value
    return filtros


@encuestas_analytics_bp.route("/summary", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def summary(current_user, encuesta_id: int):
    filtros = _parse_filtros()
    try:
        data = get_summary(encuesta_id, filtros)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify(data)


@encuestas_analytics_bp.route("/timeseries", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def timeseries(current_user, encuesta_id: int):
    filtros = _parse_filtros()
    granularity = request.args.get("granularity", "day")
    try:
        data = get_timeseries(encuesta_id, granularity, filtros)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify(data)


@encuestas_analytics_bp.route("/heatmap", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def heatmap(current_user, encuesta_id: int):
    filtros = _parse_filtros()
    try:
        data = get_heatmap(encuesta_id, filtros)
    except EncuestaError as err:
        return jsonify(err.to_dict()), err.status_code
    return jsonify(data)


@encuestas_analytics_bp.route("/export.csv", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def export_csv(current_user, encuesta_id: int):
    filtros = _parse_filtros()

    def generate():
        try:
            for chunk in export_csv(encuesta_id, filtros):
                yield chunk
        except EncuestaError as err:
            yield "error,{}\n".format(err.message)

    headers = {"Content-Disposition": f"attachment; filename=encuesta-{encuesta_id}.csv"}
    return Response(generate(), mimetype="text/csv", headers=headers)
