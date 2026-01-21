"""Analytics API endpoints for surveys (REST + legacy aliases)."""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_analytics_service import (
    export_csv as export_csv_stream,
    get_heatmap,
    get_summary,
    get_timeseries,
)
from services.encuestas_service import EncuestaError
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


def _parse_filtros() -> dict:
    filtros = {}
    for key in ("desde", "hasta", "canal", "utm_source", "utm_campaign"):
        value = request.args.get(key)
        if value:
            filtros[key] = value

    for key in ("genero", "rango_etario", "barrio", "ciudad", "provincia", "pais"):
        raw_value = request.args.get(key)
        if not raw_value:
            continue
        parts = [part.strip() for part in raw_value.split(",") if part.strip()]
        if not parts:
            continue
        filtros[key] = parts if len(parts) > 1 else parts[0]
    return filtros


def _create_blueprint(name: str, url_prefix: str, *, spanish_aliases: bool) -> Blueprint:
    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    @bp.before_request
    def _check_feature():
        guard = _feature_guard()
        if guard:
            return guard
        return None

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def summary(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        try:
            data = get_summary(encuesta_id, filtros)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/summary", view_func=summary, methods=["GET"])
    if spanish_aliases:
        bp.add_url_rule("/resumen", view_func=summary, methods=["GET"], endpoint="summary_resumen")

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

    bp.add_url_rule("/timeseries", view_func=timeseries, methods=["GET"])
    if spanish_aliases:
        bp.add_url_rule("/series", view_func=timeseries, methods=["GET"], endpoint="timeseries_series")

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def heatmap(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        resolution = request.args.get("resolution", type=int)
        try:
            data = get_heatmap(encuesta_id, filtros, resolution=resolution)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/heatmap", view_func=heatmap, methods=["GET"])

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def export_view(current_user, encuesta_id: int):
        filtros = _parse_filtros()

        def generate():
            try:
                for chunk in export_csv_stream(encuesta_id, filtros):
                    yield chunk
            except EncuestaError as err:
                yield "error,{}\n".format(err.message)

        headers = {"Content-Disposition": f"attachment; filename=encuesta-{encuesta_id}.csv"}
        return Response(generate(), mimetype="text/csv", headers=headers)

    bp.add_url_rule("/export.csv", view_func=export_view, methods=["GET"])

    return bp


encuestas_analytics_bp = _create_blueprint(
    "encuestas_analytics_bp",
    "/api/encuestas/<int:encuesta_id>/analytics",
    spanish_aliases=True,
)
encuestas_analytics_legacy_bp = _create_blueprint(
    "encuestas_analytics_legacy_bp",
    "/admin/encuestas/<int:encuesta_id>/analytics",
    spanish_aliases=True,
)
encuestas_analytics_admin_bp = _create_blueprint(
    "encuestas_analytics_admin_bp",
    "/api/admin/encuestas/<int:encuesta_id>/analytics",
    spanish_aliases=True,
)
encuestas_analytics_municipal_bp = _create_blueprint(
    "encuestas_analytics_municipal_bp",
    "/api/municipal/encuestas/<int:encuesta_id>/analytics",
    spanish_aliases=True,
)
