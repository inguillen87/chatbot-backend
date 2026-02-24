"""Analytics API endpoints for surveys (REST + legacy aliases)."""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_analytics_service import (
    export_csv as export_csv_stream,
    get_alerts,
    get_anomaly_report,
    get_dashboard_bundle,
    get_executive_brief,
    get_forecast,
    get_heatmap,
    get_segment_compare,
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
    for key in ("desde", "hasta", "canal", "utm_source", "utm_campaign", "bbox"):
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
    def forecast(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        window_minutes = request.args.get("window_minutes", default=10, type=int) or 10
        horizon_minutes = request.args.get("horizon_minutes", default=60, type=int) or 60
        try:
            data = get_forecast(
                encuesta_id,
                filtros=filtros,
                window_minutes=window_minutes,
                horizon_minutes=horizon_minutes,
            )
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/forecast", view_func=forecast, methods=["GET"])

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def alerts(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        window_minutes = request.args.get("window_minutes", default=10, type=int) or 10
        min_activity = request.args.get("min_activity", default=5, type=int) or 5
        try:
            data = get_alerts(
                encuesta_id,
                filtros=filtros,
                window_minutes=window_minutes,
                min_activity_threshold=min_activity,
            )
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/alerts", view_func=alerts, methods=["GET"])

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def brief(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        try:
            data = get_executive_brief(encuesta_id, filtros)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/brief", view_func=brief, methods=["GET"])



    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def dashboard(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        granularity = request.args.get("granularity", "day")
        use_envelope = str(request.args.get("envelope") or "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            data = get_dashboard_bundle(encuesta_id, filtros, granularity=granularity)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        if use_envelope:
            return jsonify({
                "ok": True,
                "data": data,
                "meta": {
                    "encuesta_id": encuesta_id,
                    "filters": filtros,
                    "granularity": granularity,
                },
                "errors": [],
            })

        return jsonify(data)

    bp.add_url_rule("/dashboard", view_func=dashboard, methods=["GET"])
    if spanish_aliases:
        bp.add_url_rule("/tablero", view_func=dashboard, methods=["GET"], endpoint="dashboard_tablero")

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def segment_compare(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        segment_a = {
            key: request.args.get(f"a_{key}")
            for key in ("canal", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais")
            if request.args.get(f"a_{key}")
        }
        segment_b = {
            key: request.args.get(f"b_{key}")
            for key in ("canal", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais")
            if request.args.get(f"b_{key}")
        }
        try:
            data = get_segment_compare(
                encuesta_id,
                filtros=filtros,
                segment_a=segment_a,
                segment_b=segment_b,
            )
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/segments/compare", view_func=segment_compare, methods=["GET"])

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def anomalies(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        burst_window = request.args.get("burst_window_minutes", default=5, type=int) or 5
        burst_threshold = request.args.get("burst_threshold", default=10, type=int) or 10
        try:
            data = get_anomaly_report(
                encuesta_id,
                filtros=filtros,
                burst_window_minutes=burst_window,
                burst_threshold=burst_threshold,
            )
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/anomalies", view_func=anomalies, methods=["GET"])

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
