"""Analytics API endpoints for surveys (REST + legacy aliases)."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, jsonify, request

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
    get_segment_suggestions,
    get_summary,
    get_timeseries,
)
from services.encuestas_service import EncuestaError, get_encuesta
from utils.auth_helpers import token_requerido
from utils.permissions import require_role


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


def _parse_filtros() -> dict:
    filtros = {}
    for key in ("desde", "hasta", "canal", "utm_source", "utm_campaign", "bbox", "include_demo", "exclude_demo"):
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


def _authorize_encuesta(current_user, encuesta_id: int) -> None:
    get_encuesta(encuesta_id, user=current_user)



def _dashboard_etag(payload: dict) -> str:
    """Build a stable ETag for dashboard payloads (ignoring volatile timestamps)."""

    canonical = dict(payload or {})
    canonical.pop("updated_at", None)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _json_with_etag(payload: dict):
    etag = _dashboard_etag(payload)
    if request.if_none_match.contains(etag):
        response = Response(status=304)
        response.set_etag(etag)
        return response

    response = jsonify(payload)
    response.set_etag(etag)
    response.headers.setdefault("Cache-Control", "private, max-age=30")
    return response


def _pdf_escape(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_simple_pdf(lines: list[str]) -> bytes:
    chunks = ["BT /F1 10 Tf"]
    y = 790
    for idx, line in enumerate(lines):
        safe = _pdf_escape(line)
        if idx == 0:
            chunks.append(f"50 {y} Td ({safe}) Tj")
        else:
            chunks.append("0 -14 Td (" + safe + ") Tj")
        y -= 14
        if y < 40:
            break
    chunks.append("ET")
    stream = "\n".join(chunks).encode("latin-1", errors="replace")

    objects = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n",
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n",
        f"5 0 obj<</Length {len(stream)}>>stream\n".encode("ascii") + stream + b"\nendstream endobj\n",
    ]

    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(body))
        body.extend(obj)

    xref_start = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(f"trailer<</Root 1 0 R/Size {len(offsets)}>>\n".encode("ascii"))
    body.extend(f"startxref\n{xref_start}\n%%EOF".encode("ascii"))
    return bytes(body)


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
            _authorize_encuesta(current_user, encuesta_id)
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
            _authorize_encuesta(current_user, encuesta_id)
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
        request_started = time.perf_counter()
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        filtros = _parse_filtros()
        resolution = request.args.get("resolution", type=int)
        try:
            _authorize_encuesta(current_user, encuesta_id)
            data = get_heatmap(encuesta_id, filtros, resolution=resolution)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        response = jsonify(data)
        elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault("Server-Timing", f"encuestas_heatmap;dur={elapsed_ms}")
        current_app.logger.info(
            "[encuestas.analytics.heatmap] request_id=%s encuesta_id=%s points=%s cells=%s state=%s total_ms=%s",
            request_id,
            encuesta_id,
            len(data.get("points") or []),
            len(data.get("cells") or []),
            (data.get("render_contract") or {}).get("state"),
            elapsed_ms,
        )
        return response

    bp.add_url_rule("/heatmap", view_func=heatmap, methods=["GET"])


    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def forecast(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        window_minutes = request.args.get("window_minutes", default=10, type=int) or 10
        horizon_minutes = request.args.get("horizon_minutes", default=60, type=int) or 60
        try:
            _authorize_encuesta(current_user, encuesta_id)
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
            _authorize_encuesta(current_user, encuesta_id)
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
            _authorize_encuesta(current_user, encuesta_id)
            data = get_executive_brief(encuesta_id, filtros)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/brief", view_func=brief, methods=["GET"])



    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def dashboard(current_user, encuesta_id: int):
        request_started = time.perf_counter()
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        filtros = _parse_filtros()
        granularity = request.args.get("granularity", "day")
        use_envelope = str(request.args.get("envelope") or "").strip().lower() in {"1", "true", "yes", "on"}
        fast_mode = str(request.args.get("fast") or request.args.get("lite") or "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            _authorize_encuesta(current_user, encuesta_id)
            data = get_dashboard_bundle(encuesta_id, filtros, granularity=granularity, fast_mode=fast_mode)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        if use_envelope:
            envelope = {
                "ok": True,
                "data": data,
                "meta": {
                    "encuesta_id": encuesta_id,
                    "filters": filtros,
                    "granularity": granularity,
                    "fast_mode": fast_mode,
                },
                "errors": [],
            }
            response = _json_with_etag(envelope)
            elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
            response.headers.setdefault("X-Request-Id", request_id)
            response.headers.setdefault("Server-Timing", f"encuestas_dashboard;dur={elapsed_ms}")
            current_app.logger.info(
                "[encuestas.analytics.dashboard] request_id=%s encuesta_id=%s fast_mode=%s heatmap=%s latest_responses=%s total_ms=%s",
                request_id,
                encuesta_id,
                fast_mode,
                ((data.get("meta") or {}).get("module_state") or {}).get("heatmap"),
                ((data.get("meta") or {}).get("module_state") or {}).get("latest_responses"),
                elapsed_ms,
            )
            return response

        response = _json_with_etag(data)
        elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault("Server-Timing", f"encuestas_dashboard;dur={elapsed_ms}")
        current_app.logger.info(
            "[encuestas.analytics.dashboard] request_id=%s encuesta_id=%s fast_mode=%s heatmap=%s latest_responses=%s total_ms=%s",
            request_id,
            encuesta_id,
            fast_mode,
            ((data.get("meta") or {}).get("module_state") or {}).get("heatmap"),
            ((data.get("meta") or {}).get("module_state") or {}).get("latest_responses"),
            elapsed_ms,
        )
        return response

    bp.add_url_rule("/dashboard", view_func=dashboard, methods=["GET"])
    if spanish_aliases:
        bp.add_url_rule("/tablero", view_func=dashboard, methods=["GET"], endpoint="dashboard_tablero")

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def segment_compare(current_user, encuesta_id: int):
        filtros = _parse_filtros()

        def _parse_segment_value(raw_value: str | None):
            if not raw_value:
                return None
            parts = [part.strip() for part in str(raw_value).split(",") if part.strip()]
            if not parts:
                return None
            return parts if len(parts) > 1 else parts[0]

        segment_a = {
            key: _parse_segment_value(request.args.get(f"a_{key}"))
            for key in ("canal", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais")
            if request.args.get(f"a_{key}")
        }
        segment_b = {
            key: _parse_segment_value(request.args.get(f"b_{key}"))
            for key in ("canal", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais")
            if request.args.get(f"b_{key}")
        }
        try:
            _authorize_encuesta(current_user, encuesta_id)
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
    def segment_suggestions(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        limit = request.args.get("limit", default=5, type=int) or 5
        try:
            _authorize_encuesta(current_user, encuesta_id)
            data = get_segment_suggestions(encuesta_id, filtros=filtros, limit=limit)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(data)

    bp.add_url_rule("/segments/suggestions", view_func=segment_suggestions, methods=["GET"])

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def anomalies(current_user, encuesta_id: int):
        filtros = _parse_filtros()
        burst_window = request.args.get("burst_window_minutes", default=5, type=int) or 5
        burst_threshold = request.args.get("burst_threshold", default=10, type=int) or 10
        try:
            _authorize_encuesta(current_user, encuesta_id)
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
        try:
            _authorize_encuesta(current_user, encuesta_id)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        def generate():
            try:
                for chunk in export_csv_stream(encuesta_id, filtros):
                    yield chunk
            except EncuestaError as err:
                yield "error,{}\n".format(err.message)

        headers = {"Content-Disposition": f"attachment; filename=encuesta-{encuesta_id}.csv"}
        return Response(generate(), mimetype="text/csv", headers=headers)

    bp.add_url_rule("/export.csv", view_func=export_view, methods=["GET"])

    @token_requerido
    @require_role("admin", "empleado", "super_admin")
    def export_pdf_view(current_user, encuesta_id: int):
        request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex}"
        filtros = _parse_filtros()
        try:
            _authorize_encuesta(current_user, encuesta_id)
            summary = get_summary(encuesta_id, filtros)
            heatmap = get_heatmap(encuesta_id, filtros)
            brief_data = get_executive_brief(encuesta_id, filtros)
        except EncuestaError as err:
            payload = err.to_dict()
            if isinstance(payload, dict):
                payload.setdefault("ok", False)
                payload.setdefault("request_id", request_id)
            response = jsonify(payload)
            response.status_code = err.status_code
            response.headers.setdefault("X-Request-Id", request_id)
            return response

        categorias = (summary.get("preguntas") or [{}])[0].get("opciones") or []
        top_categorias = categorias[:5]
        category_layers = ((heatmap.get("metadata") or {}).get("category_layers") or {})
        category_geo = (category_layers.get("categories") or [])[:5]

        lines = [
            "Reporte analytics encuestas",
            f"Encuesta: {encuesta_id}",
            f"Emitido: {datetime.now(timezone.utc).isoformat()}",
            "",
            "Resumen:",
            f"- total_respuestas: {int(summary.get('total_respuestas') or 0)}",
            f"- participantes_unicos: {int(summary.get('participantes_unicos') or 0)}",
            f"- tasa_completitud: {summary.get('tasa_completitud')}",
            "",
            "Categorias (estadisticas):",
        ]
        if top_categorias:
            for item in top_categorias:
                lines.append(f"- {item.get('label')}: {item.get('value')}")
        else:
            lines.append("- Sin categorias detectadas")

        lines.append("")
        lines.append("Mapa de calor por categorias:")
        if category_geo:
            for item in category_geo:
                lines.append(f"- {item.get('categoria')} | peso={item.get('total_weight')} | eventos={item.get('event_count')}")
        else:
            lines.append("- Sin capas geograficas por categoria")

        lines.append("")
        lines.append("Analisis IA:")
        lines.append(f"- headline: {brief_data.get('headline') or 'N/D'}")
        for insight in (brief_data.get("insights") or [])[:5]:
            lines.append(f"- insight: {insight}")

        pdf = _build_simple_pdf(lines)
        return Response(
            pdf,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=encuesta-{encuesta_id}-analytics.pdf",
                "X-Request-Id": request_id,
            },
        )

    bp.add_url_rule("/export.pdf", view_func=export_pdf_view, methods=["GET"])

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
