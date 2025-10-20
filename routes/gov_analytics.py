"""Endpoints that expose the civic analytics pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import Blueprint, g, jsonify, request

from services.government_pipeline import (
    build_heatmap,
    build_scorecards,
    cluster_incidents,
    demand_forecast,
    load_incidents_for_municipio,
    plan_routes,
)
from routes.auth import token_requerido
from utils.permissions import require_role
from utils.time_utils import get_local_now


gov_analytics_bp = Blueprint("gov_analytics", __name__, url_prefix="/gov/analytics")


def _resolve_municipio_id(user) -> Optional[int]:
    municipio_id = getattr(user, "municipio_id", None)
    if municipio_id is not None:
        return municipio_id

    owner_user = getattr(g, "owner_user", None)
    if owner_user is not None:
        owner_municipio_id = getattr(owner_user, "municipio_id", None)
        if owner_municipio_id is not None:
            return owner_municipio_id
        if getattr(owner_user, "tipo_chat", None) == "municipio":
            owner_id = getattr(owner_user, "id", None)
            if owner_id is not None:
                return owner_id

    empresa_id = getattr(user, "empresa_id", None)
    if empresa_id is not None:
        return empresa_id

    if getattr(user, "tipo_chat", None) == "municipio":
        fallback_id = getattr(user, "id", None)
        if fallback_id is not None:
            return fallback_id

    return None


def _resolve_dates(args) -> Tuple[Optional[datetime], Optional[datetime]]:
    dias = args.get("dias", type=int)
    if dias and dias > 0:
        date_to = get_local_now()
        date_from = date_to - timedelta(days=dias)
        return date_from, date_to

    raw_from = args.get("desde")
    raw_to = args.get("hasta")
    date_from = _parse_date(raw_from)
    date_to = _parse_date(raw_to)
    return date_from, date_to


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _load_records(current_user):
    municipio_id = _resolve_municipio_id(current_user)
    if municipio_id is None:
        return None, jsonify({"error": "El usuario no posee un municipio asociado."}), 404

    date_from, date_to = _resolve_dates(request.args)
    records = load_incidents_for_municipio(municipio_id, date_from=date_from, date_to=date_to)
    return records, None, None


@gov_analytics_bp.route("/scorecards", methods=["GET", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado", "visor")
def civic_scorecards(current_user):
    if request.method == "OPTIONS":
        return "", 204

    records, error_response, status = _load_records(current_user)
    if error_response is not None:
        return error_response, status

    payload = build_scorecards(records)
    return jsonify(payload)


@gov_analytics_bp.route("/heatmap", methods=["GET", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado", "visor")
def civic_heatmap(current_user):
    if request.method == "OPTIONS":
        return "", 204

    records, error_response, status = _load_records(current_user)
    if error_response is not None:
        return error_response, status

    resolution = request.args.get("resolution", type=int) or 7
    min_count = request.args.get("min_count", type=int) or 3
    payload = build_heatmap(records, resolution=resolution, min_count=min_count)
    return jsonify(payload)


@gov_analytics_bp.route("/demand", methods=["GET", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado", "visor")
def civic_demand(current_user):
    if request.method == "OPTIONS":
        return "", 204

    records, error_response, status = _load_records(current_user)
    if error_response is not None:
        return error_response, status

    periods = request.args.get("periods", type=int) or 14
    payload = demand_forecast(records, periods=periods)
    return jsonify(payload)


@gov_analytics_bp.route("/clusters", methods=["GET", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado", "visor")
def civic_clusters(current_user):
    if request.method == "OPTIONS":
        return "", 204

    records, error_response, status = _load_records(current_user)
    if error_response is not None:
        return error_response, status

    resolution = request.args.get("resolution", type=int) or 8
    min_count = request.args.get("min_count", type=int) or 5
    payload = cluster_incidents(records, resolution=resolution, min_count=min_count)
    return jsonify(payload)


@gov_analytics_bp.route("/routes", methods=["GET", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado", "visor")
def civic_routes(current_user):
    if request.method == "OPTIONS":
        return "", 204

    records, error_response, status = _load_records(current_user)
    if error_response is not None:
        return error_response, status

    depot_lat = request.args.get("depot_lat", type=float)
    depot_lng = request.args.get("depot_lng", type=float)
    if depot_lat is None or depot_lng is None:
        return jsonify({"error": "Debe especificar depot_lat y depot_lng"}), 400

    max_stops = request.args.get("max_stops", type=int) or 25
    payload = plan_routes(records, depot_lat=depot_lat, depot_lng=depot_lng, max_stops=max_stops)
    return jsonify(payload)


__all__ = ["gov_analytics_bp"]
