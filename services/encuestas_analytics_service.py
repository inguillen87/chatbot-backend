"""Analytics helpers for surveys."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from statistics import mean, median
from threading import Lock
from time import monotonic
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Numeric, String, and_, case, cast, literal, or_
from sqlalchemy.orm import selectinload

from database import db
from models import EncEncuesta, EncRespuesta, EncPregunta, EncRespuestaDetalle, EncLink, TenantProfile
from services.openai_bridge import client as openai_client
from services.encuestas_service import (
    EncuestaError,
    compile_survey_visibility,
    get_encuesta,
    get_public_encuesta,
    _parse_datetime,
    _resolve_geo_metadata_for_tenant,
)
from services.huggingface_ai_insights import build_collection_ai_insights, build_map_ai_layers
from services.openai_model_defaults import (
    DEFAULT_OPENAI_SOL_MODEL,
    chat_completion_compatibility_options,
    resolve_openai_model,
)
from services.survey_response_provenance import (
    SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
    SURVEY_RESPONSE_ORIGIN_REAL,
    SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
    build_survey_response_provenance,
    filter_survey_response_query_by_origin,
    is_trusted_demo_seed_response,
)
from services.survey_jurisdiction import (
    tenant_requires_government_survey_evidence,
)
from services.territorial_evidence import (
    coordinate_jurisdiction_status,
    resolve_tenant_jurisdiction,
)
from utils.heatmap import (
    build_feature_collection,
    compute_heatmap_cell_id,
    compute_heatmap_centroid,
    enrich_heatmap_cells,
    enrich_heatmap_points,
)
from utils.map_config import get_map_config


_SINGLE_CHOICE_TYPES = {
    "opcion_unica",
    "single_choice",
    "single-choice",
    "singlechoice",
    "single",
    "radio",
}

_MULTIPLE_CHOICE_TYPES = {
    "opcion_multiple",
    "multiple_choice",
    "multiple-choice",
    "multiple",
    "checkbox",
    "check",
    "multi_select",
    "multi-select",
    "multiselect",
}



logger = logging.getLogger(__name__)
_MAP_CONTRACT_VERSION = "2026.04-maplibre-v1"
SURVEY_AI_BRIEF_CONTRACT_VERSION = "encuestas.ai_executive_brief.v1"
SURVEY_AI_ADVISORY_POLICY = {
    "advisory_only": True,
    "mutates_operational_state": False,
    "state_mutation_allowed": False,
    "python_handlers_remain_authority": True,
    "requires_operator_confirmation": True,
}
PUBLIC_SMALL_CELL_CONTRACT_VERSION = "surveys.public_small_cell.v1"
PUBLIC_SMALL_CELL_DEFAULT_MINIMUM = 5
SURVEY_ANALYTICS_SAMPLE_CONTRACT_VERSION = "surveys.analytics_sample.v1"
SURVEY_ANALYTICS_DEFAULT_SAMPLE_LIMIT = 500
SURVEY_ANALYTICS_MAX_SAMPLE_LIMIT = 1_000
SURVEY_ANALYTICS_MAX_TIMESERIES_BUCKETS = 10_080
SURVEY_ANALYTICS_CHANNEL_TOP_LIMIT = 20
PUBLIC_LIVE_RESULTS_CACHE_TTL_SECONDS = 5.0
PUBLIC_LIVE_RESULTS_CACHE_MAX_ENTRIES = 256

_ANALYTICS_SNAPSHOT_CACHE: ContextVar[Optional[Dict[str, Dict[str, Any]]]] = (
    ContextVar("survey_analytics_snapshot_cache", default=None)
)
_PUBLIC_LIVE_RESULTS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_PUBLIC_LIVE_RESULTS_CACHE_LOCK = Lock()


def _public_small_cell_minimum() -> int:
    """Return a bounded k-anonymity floor for public source-anonymous results."""

    raw = os.environ.get("SURVEY_PUBLIC_MIN_CELL_SIZE")
    try:
        value = int(raw or PUBLIC_SMALL_CELL_DEFAULT_MINIMUM)
    except (TypeError, ValueError):
        value = PUBLIC_SMALL_CELL_DEFAULT_MINIMUM
    # Five is a hard public floor. Deploy-time configuration may strengthen
    # the threshold but can never weaken the privacy boundary.
    return max(PUBLIC_SMALL_CELL_DEFAULT_MINIMUM, min(value, 50))


def _bounded_public_small_cell_minimum(raw: Any = None) -> int:
    try:
        value = int(raw if raw is not None else _public_small_cell_minimum())
    except (TypeError, ValueError, OverflowError):
        value = _public_small_cell_minimum()
    return max(PUBLIC_SMALL_CELL_DEFAULT_MINIMUM, min(value, 50))


def _has_positive_small_cell(values: Iterable[Any], minimum: int) -> bool:
    for raw in values:
        try:
            value = int(raw or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 < value < minimum:
            return True
    return False


def _public_small_cell_ai_requires_suppression(
    *,
    privacy_mode: str,
    total_responses: Any,
    questions: Any,
    timeline: Any,
    heatmap_cells: Any,
    minimum_cell_size: Optional[int] = None,
) -> bool:
    """Decide before any AI call whether exact public aggregates are private."""

    if str(privacy_mode or "legacy").strip().lower() != "source_anonymous":
        return False
    minimum = _bounded_public_small_cell_minimum(minimum_cell_size)
    if _has_positive_small_cell([total_responses], minimum):
        return True
    if isinstance(questions, list):
        for question in questions:
            if not isinstance(question, Mapping):
                continue
            options = question.get("opciones")
            if isinstance(options, list) and _has_positive_small_cell(
                (
                    option.get("votos", option.get("value"))
                    for option in options
                    if isinstance(option, Mapping)
                ),
                minimum,
            ):
                return True
    if isinstance(timeline, list) and _has_positive_small_cell(
        (
            item.get("total", item.get("respuestas", item.get("value")))
            for item in timeline
            if isinstance(item, Mapping)
        ),
        minimum,
    ):
        return True
    return isinstance(heatmap_cells, list) and _has_positive_small_cell(
        (
            cell.get("count", cell.get("weight", cell.get("w")))
            for cell in heatmap_cells
            if isinstance(cell, Mapping)
        ),
        minimum,
    )


def _apply_public_small_cell_policy(
    payload: Dict[str, Any],
    *,
    privacy_mode: str,
    minimum_cell_size: Optional[int] = None,
    results_final: bool = False,
) -> Dict[str, Any]:
    """Redact public aggregates that could isolate source-anonymous people.

    The policy is deliberately conservative. If one option, minute bucket or
    map cell is below ``k``, the whole corresponding surface is hidden so the
    omitted value cannot be reconstructed by subtraction. Administrative PII
    exports remain governed separately by RBAC and audit.
    """

    normalized_mode = str(privacy_mode or "legacy").strip().lower()
    source_anonymous = normalized_mode == "source_anonymous"
    results_final = bool(results_final)
    active_source_anonymous = source_anonymous and not results_final
    minimum = _bounded_public_small_cell_minimum(minimum_cell_size)

    heatmap = payload.get("heatmap")
    heatmap_metadata = heatmap.get("metadata") if isinstance(heatmap, dict) else None
    heatmap_metadata = heatmap_metadata if isinstance(heatmap_metadata, dict) else {}
    geo_aggregation = heatmap_metadata.get("aggregation")
    geo_aggregation = geo_aggregation if isinstance(geo_aggregation, dict) else {}
    try:
        sql_suppressed_cells = int(
            geo_aggregation.get("suppressed_cell_count") or 0
        )
    except (TypeError, ValueError, OverflowError):
        sql_suppressed_cells = 0
    try:
        sql_safe_cells = int(geo_aggregation.get("safe_cell_count") or 0)
    except (TypeError, ValueError, OverflowError):
        sql_safe_cells = 0
    direct_geo_small_cell = False
    if source_anonymous and isinstance(heatmap, dict):
        cells = heatmap.get("cells")
        direct_geo_small_cell = isinstance(cells, list) and _has_positive_small_cell(
            (
                cell.get("count", cell.get("weight", cell.get("w")))
                for cell in cells
                if isinstance(cell, Mapping)
            ),
            minimum,
        )
    # Any SQL-suppressed geographic cell makes the heatmap surface sensitive,
    # even when no safe cell remains to display: suppression metadata would
    # otherwise reveal the exact size of an all-sub-k cohort.  A hidden
    # remainder additionally makes the overall total subtractable when at
    # least one publishable cell is shown beside it.
    geo_cells_suppressed = source_anonymous and (
        sql_suppressed_cells > 0 or direct_geo_small_cell
    )
    geo_remainder_suppressed = (
        sql_suppressed_cells > 0 and sql_safe_cells > 0
    ) or direct_geo_small_cell
    policy_enabled = source_anonymous or geo_remainder_suppressed
    privacy_contract: Dict[str, Any] = {
        "contract_version": PUBLIC_SMALL_CELL_CONTRACT_VERSION,
        "privacy_mode": normalized_mode,
        "results_final": results_final,
        "enabled": policy_enabled,
        "minimum_cell_size": minimum if policy_enabled else None,
        "suppressed_surfaces": [],
        "reason_code": None,
    }
    payload["privacy"] = privacy_contract
    if not policy_enabled:
        return payload

    try:
        exact_total = int(payload.get("total_respuestas") or 0)
    except (TypeError, ValueError, OverflowError):
        exact_total = 0
    # Active source-anonymous results are withheld as one stable public cohort.
    # Releasing stateless k-safe snapshots would still allow longitudinal
    # differencing (for example 7-0 followed by 7-1). Final aggregates become
    # eligible only after an explicit persisted close.
    cohort_suppressed = (
        source_anonymous and results_final and 0 <= exact_total < minimum
    )
    aggregate_total_suppressed = (
        active_source_anonymous
        or cohort_suppressed
        or geo_remainder_suppressed
    )
    suppressed_surfaces: list[str] = []

    if aggregate_total_suppressed:
        if active_source_anonymous:
            privacy_state = "source_anonymous_active"
        elif cohort_suppressed:
            privacy_state = "cohort_below_minimum"
        else:
            privacy_state = "geo_remainder"
        stable_seed = (
            f"survey:{payload.get('encuesta_id')}:privacy:{privacy_state}:k:{minimum}"
        )
        payload["result_version"] = None
        payload["snapshot_version"] = (
            "private:" + hashlib.sha256(stable_seed.encode("utf-8")).hexdigest()[:16]
        )
        payload["total_respuestas"] = None
        payload["total_respuestas_bucket"] = (
            "withheld_until_close"
            if active_source_anonymous
            else f"<{minimum}"
            if cohort_suppressed
            else f">={minimum}"
        )
        if not active_source_anonymous and not cohort_suppressed:
            payload["total_respuestas_lower_bound"] = minimum
        suppressed_surfaces.append("cohort_total")

        empty_state = payload.get("empty_state")
        if isinstance(empty_state, dict):
            empty_state.update(
                {
                    "is_empty": None,
                    "title": "Resultados protegidos",
                    "message": (
                        "Los resultados detallados se habilitan cuando cada "
                        f"cohorte publicada alcanza al menos {minimum} respuestas."
                    ),
                    "action_hint": "wait_for_minimum_cell_size",
                }
            )

        provenance = payload.get("data_provenance")
        if isinstance(provenance, dict):
            for key in (
                "real_responses_included",
                "synthetic_responses_included",
                "synthetic_responses_excluded",
                "unverified_responses_included",
                "unverified_responses_excluded",
                "population_size",
                "sample_size",
                "sample_limit",
                "raw_responses_materialized",
            ):
                provenance[key] = None
            provenance.update(
                {
                    "contains_synthetic": None,
                    "sampled": None,
                    "partial": None,
                    "exact_aggregates": False,
                    "privacy_redacted": True,
                    "population_bucket": (
                        "withheld_until_close"
                        if active_source_anonymous
                        else f"<{minimum}"
                        if cohort_suppressed
                        else f">={minimum}"
                    ),
                }
            )

        timeline_metadata = payload.get("timeline_metadata")
        if isinstance(timeline_metadata, dict):
            timeline_metadata.update(
                {
                    "bucket_count": None,
                    "partial": None,
                    "privacy_redacted": True,
                }
            )

    questions = payload.get("preguntas")
    any_question_suppressed = False
    if isinstance(questions, list):
        for question in questions:
            if not isinstance(question, dict):
                continue
            options = question.get("opciones")
            options = options if isinstance(options, list) else []
            question_has_small_cell = (
                aggregate_total_suppressed
                or _has_positive_small_cell(
                    (
                        option.get("votos", option.get("value"))
                        for option in options
                        if isinstance(option, Mapping)
                    ),
                    minimum,
                )
            )
            if not question_has_small_cell:
                continue
            any_question_suppressed = True
            question["total_votos"] = None
            question["suppressed"] = True
            question["suppression_reason"] = "minimum_cell_size_not_met"
            for option in options:
                if not isinstance(option, dict):
                    continue
                option["value"] = None
                option["votos"] = None
                option["porcentaje"] = None
                option["suppressed"] = True
        if any_question_suppressed:
            suppressed_surfaces.append("question_results")

    timeline = payload.get("timeline_minute")
    timeline_has_small_cell = aggregate_total_suppressed or (
        isinstance(timeline, list)
        and _has_positive_small_cell(
            (
                item.get("total", item.get("respuestas", item.get("value")))
                for item in timeline
                if isinstance(item, Mapping)
            ),
            minimum,
        )
    )
    if timeline_has_small_cell:
        payload["timeline_minute"] = []
        momentum = payload.get("momentum")
        if isinstance(momentum, dict):
            for key in (
                "last_window",
                "previous_window",
                "delta",
                "last_10m",
                "previous_10m",
            ):
                momentum[key] = None
            momentum["trend"] = "suppressed"
            momentum["suppressed"] = True
        suppressed_surfaces.append("timeline")

    heatmap_has_small_cell = aggregate_total_suppressed or geo_cells_suppressed
    if isinstance(heatmap, dict):
        cells = heatmap.get("cells")
        heatmap_has_small_cell = heatmap_has_small_cell or (
            isinstance(cells, list)
            and _has_positive_small_cell(
                (
                    cell.get("count", cell.get("weight", cell.get("w")))
                    for cell in cells
                    if isinstance(cell, Mapping)
                ),
                minimum,
            )
        )
        if heatmap_has_small_cell:
            heatmap["points"] = []
            heatmap["cells"] = []
            metadata = heatmap.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                heatmap["metadata"] = metadata
            metadata.pop("raw_points_count", None)
            metadata.update(
                {
                    "points_count": None,
                    "cells_count": None,
                    "truncated_points": None,
                    "truncated_cells": None,
                    "suppressed": True,
                    "suppression_reason": "minimum_cell_size_not_met",
                    "minimum_cell_size": minimum,
                }
            )
            aggregation = metadata.get("aggregation")
            if isinstance(aggregation, dict):
                for key in (
                    "cell_count",
                    "total_cell_count",
                    "geo_response_count",
                    "safe_cell_count",
                    "safe_response_count",
                    "suppressed_cell_count",
                    "suppressed_response_count",
                ):
                    aggregation[key] = None
                aggregation.update(
                    {
                        "has_suppressed_cells": None,
                        "partial": None,
                        "privacy_redacted": True,
                    }
                )
            suppressed_surfaces.append("heatmap")

    telemetry = payload.get("live_telemetry")
    if isinstance(telemetry, dict):
        recent = telemetry.get("responses_last_hour")
        recent_is_small = _has_positive_small_cell([recent], minimum)
        if aggregate_total_suppressed:
            telemetry["has_responses"] = None
            telemetry["responses_total"] = None
            telemetry["responses_bucket"] = payload.get("total_respuestas_bucket")
            telemetry["polling_interval_ms"] = 5000
        if timeline_has_small_cell or recent_is_small:
            telemetry["responses_last_hour"] = None
            telemetry["participation_per_minute"] = None
            telemetry["trend"] = "suppressed"

    kpis = payload.get("kpis")
    if isinstance(kpis, dict):
        if aggregate_total_suppressed or timeline_has_small_cell or _has_positive_small_cell(
            [kpis.get("responses_last_hour")], minimum
        ):
            kpis["responses_last_hour"] = None
            kpis["participation_per_minute"] = None
        if any_question_suppressed:
            kpis["leader"] = None
            kpis["leader_label"] = None
        if "heatmap" in suppressed_surfaces:
            kpis["heatmap_coverage_cells"] = None

    if suppressed_surfaces:
        payload["ai_summary"] = (
            "Los resultados anonimos se publican cuando la encuesta queda cerrada."
            if active_source_anonymous
            else "Los resultados detallados estan protegidos por el umbral minimo "
            f"de {minimum} participantes por celda."
        )
        payload["ai_insights"] = []
        payload["ai_layers"] = {}
        payload["operator_recommendations"] = []
        payload["ai_signal"] = {
            "contract_version": "surveys.live_ai_signal.v1",
            "provider_family": "none",
            "mode": "privacy_suppressed",
            "hf_status": {
                "enabled": False,
                "reason_code": "minimum_cell_size_not_met",
            },
            "summary": {
                "text": payload["ai_summary"],
                "contains_exact_counts": False,
            },
            "collection": {"item_count": None, "privacy_redacted": True},
            "recommended_actions": [],
            "advisory_policy": dict(SURVEY_AI_ADVISORY_POLICY),
        }

        render_contract = payload.get("render_contract")
        if isinstance(render_contract, dict):
            render_contract["privacy_state"] = (
                "source_anonymous_results_withheld_until_close"
                if active_source_anonymous
                else "minimum_cell_size_not_met"
            )
            if aggregate_total_suppressed:
                render_contract["polling_interval_ms"] = 5000
            supports = render_contract.get("supports")
            if isinstance(supports, list):
                render_contract["supports"] = [
                    item for item in supports if item != "csv_export"
                ]
        ui_actions = payload.get("ui_actions")
        if isinstance(ui_actions, list):
            payload["ui_actions"] = [
                action
                for action in ui_actions
                if not (
                    isinstance(action, Mapping)
                    and action.get("id") == "export_live_csv"
                )
            ]

    privacy_contract["suppressed_surfaces"] = list(
        dict.fromkeys(suppressed_surfaces)
    )
    privacy_contract["reason_code"] = (
        "source_anonymous_results_withheld_until_close"
        if active_source_anonymous and suppressed_surfaces
        else "minimum_cell_size_not_met"
        if suppressed_surfaces
        else None
    )
    privacy_contract["detailed_results_suppressed"] = bool(suppressed_surfaces)
    privacy_contract["cohort_size_disclosed"] = not aggregate_total_suppressed
    privacy_contract["geo_remainder_protected"] = (
        None
        if active_source_anonymous or cohort_suppressed
        else geo_remainder_suppressed
    )
    return payload


def _survey_analytics_model() -> str:
    return resolve_openai_model(
        "OPENAI_SURVEY_ANALYTICS_MODEL",
        DEFAULT_OPENAI_SOL_MODEL,
    )
LIVE_ANALYTICS_RANGE_CONTRACT_VERSION = "surveys.analytics_range.v1"
LIVE_ANALYTICS_RANGE_PRESETS = {
    "last_60m": "Últimos 60 minutos",
    "last_24h": "Últimas 24 horas",
    "today": "Hoy (desde las 00:00)",
}
_TEXT_TYPES = {
    "abierta",
    "text",
    "texto",
    "open_text",
    "open-text",
    "open",
}


def _normalize_question_type(raw: Optional[str]) -> str:
    if raw is None:
        return "single_choice"

    text = str(raw).strip().lower()
    if not text:
        return "single_choice"
    if text in _SINGLE_CHOICE_TYPES:
        return "single_choice"
    if text in _MULTIPLE_CHOICE_TYPES:
        return "multiple_choice"
    if text in _TEXT_TYPES:
        return "text"
    return text


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "si"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_bbox_filter(value: Any) -> Optional[Tuple[float, float, float, float]]:
    if not value:
        return None

    raw_values: List[Any]
    if isinstance(value, Mapping):
        raw_values = [
            value.get("min_lng", value.get("min_lon", value.get("west", value.get("lng_min", value.get("lon_min"))))),
            value.get("min_lat", value.get("south", value.get("lat_min"))),
            value.get("max_lng", value.get("max_lon", value.get("east", value.get("lng_max", value.get("lon_max"))))),
            value.get("max_lat", value.get("north", value.get("lat_max"))),
        ]
    elif isinstance(value, str):
        raw_values = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    else:
        return None

    if len(raw_values) != 4:
        return None

    try:
        min_lng, min_lat, max_lng, max_lat = (float(item) for item in raw_values)
    except (TypeError, ValueError):
        return None

    values = (min_lng, min_lat, max_lng, max_lat)
    if not all(math.isfinite(item) for item in values):
        return None
    if min_lng > max_lng or min_lat > max_lat:
        return None
    if min_lng < -180 or max_lng > 180 or min_lat < -90 or max_lat > 90:
        return None
    return values


def _is_demo_respuesta(respuesta: EncRespuesta) -> bool:
    return is_trusted_demo_seed_response(respuesta)


def _response_data_mode(filtros: Optional[Dict[str, Any]]) -> str:
    """Resolve explicit admin provenance controls; default to real data."""

    if not filtros:
        return "real"
    if _as_bool(filtros.get("exclude_demo")) is True:
        return "real"
    requested_mode = str(filtros.get("data_mode") or "").strip().lower()
    if requested_mode in {"real", "synthetic"}:
        return requested_mode
    return "real"


def _apply_filters(query, filtros: Optional[Dict[str, Any]]):
    if not filtros:
        return query
    if filtros.get("desde"):
        desde = _parse_datetime(filtros["desde"])
        if desde:
            query = query.filter(EncRespuesta.submitted_at >= desde)
    if filtros.get("hasta"):
        hasta = _parse_datetime(filtros["hasta"])
        if hasta:
            query = query.filter(EncRespuesta.submitted_at <= hasta)
    if filtros.get("canal"):
        query = query.filter(EncRespuesta.canal == filtros["canal"])
    if filtros.get("utm_source"):
        query = query.filter(EncRespuesta.utm_source == filtros["utm_source"])
    if filtros.get("utm_campaign"):
        query = query.filter(EncRespuesta.utm_campaign == filtros["utm_campaign"])
    bbox = _parse_bbox_filter(filtros.get("bbox"))
    if bbox:
        min_lng, min_lat, max_lng, max_lat = bbox
        query = query.filter(
            EncRespuesta.lng >= min_lng,
            EncRespuesta.lng <= max_lng,
            EncRespuesta.lat >= min_lat,
            EncRespuesta.lat <= max_lat,
        )

    def _apply_text_filter(column, key: str):
        values = filtros.get(key)
        if not values:
            return
        if isinstance(values, str):
            query_local = query.filter(column == values)
        else:
            query_local = query.filter(column.in_(list(values)))
        return query_local

    for column, key in (
        (EncRespuesta.genero, "genero"),
        (EncRespuesta.rango_etario, "rango_etario"),
        (EncRespuesta.barrio, "barrio"),
        (EncRespuesta.ciudad, "ciudad"),
        (EncRespuesta.provincia, "provincia"),
        (EncRespuesta.pais, "pais"),
    ):
        filtered = _apply_text_filter(column, key)
        if filtered is not None:
            query = filtered
    return query


def _analytics_sample_limit(value: Any = None) -> int:
    raw = value if value is not None else os.environ.get(
        "SURVEY_ANALYTICS_SAMPLE_LIMIT",
        SURVEY_ANALYTICS_DEFAULT_SAMPLE_LIMIT,
    )
    try:
        normalized = int(raw)
    except (TypeError, ValueError, OverflowError):
        normalized = SURVEY_ANALYTICS_DEFAULT_SAMPLE_LIMIT
    return max(0, min(normalized, SURVEY_ANALYTICS_MAX_SAMPLE_LIMIT))


def _analytics_bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _canonical_filter_key(filtros: Optional[Mapping[str, Any]]) -> str:
    return json.dumps(
        dict(filtros or {}),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@contextmanager
def _analytics_snapshot_scope():
    """Reuse one bounded response snapshot across a composite dashboard call."""

    existing = _ANALYTICS_SNAPSHOT_CACHE.get()
    if existing is not None:
        yield existing
        return
    cache: Dict[str, Dict[str, Any]] = {}
    token = _ANALYTICS_SNAPSHOT_CACHE.set(cache)
    try:
        yield cache
    finally:
        _ANALYTICS_SNAPSHOT_CACHE.reset(token)


def _response_queries(
    encuesta: EncEncuesta,
    filtros: Optional[Dict[str, Any]],
) -> Tuple[Any, Any, str]:
    base_query = EncRespuesta.query.filter(EncRespuesta.encuesta_id == encuesta.id)
    base_query = _apply_filters(base_query, filtros)
    mode = _response_data_mode(filtros)
    expected_origin = (
        SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO
        if mode == "synthetic"
        else SURVEY_RESPONSE_ORIGIN_REAL
    )
    selected_query = base_query.filter(
        EncRespuesta.response_origin == expected_origin
    )
    return base_query, selected_query, mode


def _build_response_snapshot(
    encuesta: EncEncuesta,
    filtros: Optional[Dict[str, Any]],
    *,
    sample_limit: Optional[int] = None,
) -> Dict[str, Any]:
    effective_sample_limit = _analytics_sample_limit(sample_limit)
    base_query, selected_query, mode = _response_queries(encuesta, filtros)

    origin_row = (
        base_query.with_entities(
            db.func.coalesce(
                db.func.sum(
                    case(
                        (EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("real_count"),
            db.func.coalesce(
                db.func.sum(
                    case(
                        (
                            EncRespuesta.response_origin
                            == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("synthetic_count"),
            db.func.coalesce(
                db.func.sum(
                    case(
                        (
                            EncRespuesta.response_origin
                            == SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("unverified_count"),
            db.func.max(
                case(
                    (
                        EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL,
                        EncRespuesta.id,
                    )
                )
            ).label("real_max_id"),
            db.func.max(
                case(
                    (
                        EncRespuesta.response_origin
                        == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
                        EncRespuesta.id,
                    )
                )
            ).label("synthetic_max_id"),
        )
        .order_by(None)
        .one()
    )
    real_count = int(origin_row.real_count or 0)
    synthetic_count = int(origin_row.synthetic_count or 0)
    unverified_count = int(origin_row.unverified_count or 0)
    population_size = synthetic_count if mode == "synthetic" else real_count
    result_version = int(
        (
            origin_row.synthetic_max_id
            if mode == "synthetic"
            else origin_row.real_max_id
        )
        or 0
    )

    sample: List[EncRespuesta] = []
    if effective_sample_limit > 0 and population_size > 0:
        sample = (
            selected_query.options(
                selectinload(EncRespuesta.detalles).joinedload(
                    EncRespuestaDetalle.opcion
                )
            )
            .order_by(
                EncRespuesta.submitted_at.desc(),
                EncRespuesta.id.desc(),
            )
            .limit(effective_sample_limit)
            .all()
        )
        sample.reverse()

    sample_size = len(sample)
    partial = sample_size < population_size
    provenance = build_survey_response_provenance(
        real_count=real_count,
        synthetic_count=synthetic_count,
        unverified_count=unverified_count,
        mode=mode,
        synthetic_excluded=synthetic_count if mode == "real" else 0,
        unverified_excluded=unverified_count,
    )
    provenance.update(
        {
            "sample_contract_version": SURVEY_ANALYTICS_SAMPLE_CONTRACT_VERSION,
            "population_size": population_size,
            "sample_size": sample_size,
            "sample_limit": effective_sample_limit,
            "sampled": partial,
            "partial": partial,
            "sample_order": "latest",
        }
    )
    return {
        "encuesta": encuesta,
        "filters": dict(filtros or {}),
        "mode": mode,
        "base_query": base_query,
        "selected_query": selected_query,
        "sample": sample,
        "population_size": population_size,
        "real_count": real_count,
        "synthetic_count": synthetic_count,
        "unverified_count": unverified_count,
        "result_version": result_version,
        "provenance": provenance,
        "derived": {},
    }


def _get_response_snapshot(
    encuesta: EncEncuesta,
    filtros: Optional[Dict[str, Any]],
    *,
    sample_limit: Optional[int] = None,
) -> Dict[str, Any]:
    effective_sample_limit = _analytics_sample_limit(sample_limit)
    key = (
        f"{int(encuesta.id)}:{_response_data_mode(filtros)}:"
        f"{effective_sample_limit}:{_canonical_filter_key(filtros)}"
    )
    cache = _ANALYTICS_SNAPSHOT_CACHE.get()
    if cache is not None and key in cache:
        return cache[key]
    snapshot = _build_response_snapshot(
        encuesta,
        filtros,
        sample_limit=effective_sample_limit,
    )
    if cache is not None:
        cache[key] = snapshot
    return snapshot


def _snapshot_value(
    snapshot: Dict[str, Any],
    key: str,
    factory,
):
    derived = snapshot.setdefault("derived", {})
    if key not in derived:
        derived[key] = factory()
    return derived[key]


def _collect_respuestas_with_provenance(
    encuesta: EncEncuesta,
    filtros: Optional[Dict[str, Any]],
):
    snapshot = _get_response_snapshot(encuesta, filtros)
    return list(snapshot["sample"]), dict(snapshot["provenance"])


def _collect_respuestas(encuesta: EncEncuesta, filtros: Optional[Dict[str, Any]]):
    respuestas, _provenance = _collect_respuestas_with_provenance(
        encuesta,
        filtros,
    )
    return respuestas


def _selected_response_ids_subquery(snapshot: Mapping[str, Any]):
    return (
        snapshot["selected_query"]
        .with_entities(EncRespuesta.id.label("response_id"))
        .order_by(None)
        .subquery()
    )


def _exact_option_statistics(
    snapshot: Dict[str, Any],
) -> Tuple[Dict[int, Counter], Dict[int, Counter], Counter]:
    def _load():
        response_ids = _selected_response_ids_subquery(snapshot)
        option_rows = (
            db.session.query(
                EncRespuestaDetalle.pregunta_id,
                EncRespuestaDetalle.opcion_id,
                db.func.count(EncRespuestaDetalle.id).label("detail_count"),
                db.func.count(
                    db.func.distinct(EncRespuestaDetalle.respuesta_id)
                ).label("response_count"),
            )
            .join(
                response_ids,
                response_ids.c.response_id
                == EncRespuestaDetalle.respuesta_id,
            )
            .filter(EncRespuestaDetalle.opcion_id.isnot(None))
            .group_by(
                EncRespuestaDetalle.pregunta_id,
                EncRespuestaDetalle.opcion_id,
            )
            .all()
        )
        option_counts: Dict[int, Counter] = defaultdict(Counter)
        unique_counts: Dict[int, Counter] = defaultdict(Counter)
        for question_id, option_id, detail_count, response_count in option_rows:
            option_counts[int(question_id)][int(option_id)] = int(
                detail_count or 0
            )
            unique_counts[int(question_id)][int(option_id)] = int(
                response_count or 0
            )

        answered_rows = (
            db.session.query(
                EncRespuestaDetalle.pregunta_id,
                db.func.count(
                    db.func.distinct(EncRespuestaDetalle.respuesta_id)
                ).label("response_count"),
            )
            .join(
                response_ids,
                response_ids.c.response_id
                == EncRespuestaDetalle.respuesta_id,
            )
            .filter(
                or_(
                    EncRespuestaDetalle.opcion_id.isnot(None),
                    db.func.length(
                        db.func.trim(
                            db.func.coalesce(
                                EncRespuestaDetalle.texto_libre,
                                "",
                            )
                        )
                    )
                    > 0,
                )
            )
            .group_by(EncRespuestaDetalle.pregunta_id)
            .all()
        )
        answered_counts = Counter(
            {
                int(question_id): int(response_count or 0)
                for question_id, response_count in answered_rows
            }
        )
        return option_counts, unique_counts, answered_counts

    return _snapshot_value(snapshot, "exact_option_statistics", _load)


def _exact_summary_frequencies(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    def _load():
        selected_query = snapshot["selected_query"]
        # Channel names are tenant-controlled legacy data. Normalize casing in
        # SQL and materialize only a bounded top set; the exact remainder is
        # represented by ``otros`` so totals remain auditable without an
        # unbounded GROUP BY result in Python.
        channel_expr = db.func.coalesce(
            db.func.nullif(
                db.func.lower(db.func.trim(cast(EncRespuesta.canal, String))),
                "",
            ),
            "sin_canal",
        )
        channel_distinct_total = int(
            selected_query.with_entities(
                db.func.count(db.func.distinct(channel_expr))
            )
            .order_by(None)
            .scalar()
            or 0
        )
        channel_rows = (
            selected_query.with_entities(
                channel_expr.label("label"),
                db.func.count(EncRespuesta.id).label("total"),
            )
            .group_by(channel_expr)
            .order_by(
                db.func.count(EncRespuesta.id).desc(),
                channel_expr.asc(),
            )
            .limit(SURVEY_ANALYTICS_CHANNEL_TOP_LIMIT)
            .all()
        )
        channels = Counter(
            {str(label or "sin_canal"): int(total or 0) for label, total in channel_rows}
        )
        channel_top_total = sum(channels.values())
        channel_other_count = max(
            0,
            int(snapshot.get("population_size") or 0) - channel_top_total,
        )
        if channel_other_count:
            channels["otros"] += channel_other_count
        channel_metadata = {
            "top_limit": SURVEY_ANALYTICS_CHANNEL_TOP_LIMIT,
            "distinct_total": channel_distinct_total,
            "returned_distinct": len(channel_rows),
            "truncated": channel_distinct_total > len(channel_rows),
            "other_count": channel_other_count,
        }

        source_expr = db.func.coalesce(EncRespuesta.utm_source, "n/a")
        campaign_expr = db.func.coalesce(EncRespuesta.utm_campaign, "n/a")
        utm_rows = (
            selected_query.with_entities(
                source_expr.label("source"),
                campaign_expr.label("campaign"),
                db.func.count(EncRespuesta.id).label("total"),
            )
            .group_by(source_expr, campaign_expr)
            .order_by(db.func.count(EncRespuesta.id).desc())
            .limit(500)
            .all()
        )
        utm = Counter(
            {
                f"{str(source or 'n/a')}|{str(campaign or 'n/a')}": int(total or 0)
                for source, campaign, total in utm_rows
            }
        )

        dimensions: Dict[str, Counter] = {}
        for key, column in (
            ("genero", EncRespuesta.genero),
            ("rango_etario", EncRespuesta.rango_etario),
            ("barrio", EncRespuesta.barrio),
            ("ciudad", EncRespuesta.ciudad),
            ("provincia", EncRespuesta.provincia),
            ("pais", EncRespuesta.pais),
        ):
            rows = (
                selected_query.with_entities(
                    column.label("label"),
                    db.func.count(EncRespuesta.id).label("total"),
                )
                .filter(
                    column.isnot(None),
                    db.func.length(db.func.trim(cast(column, String))) > 0,
                )
                .group_by(column)
                .order_by(db.func.count(EncRespuesta.id).desc())
                .limit(50)
                .all()
            )
            dimensions[key] = Counter(
                {str(label): int(total or 0) for label, total in rows}
            )

        identity_expr = case(
            (
                EncRespuesta.huella_unica.isnot(None),
                literal("fingerprint:") + cast(EncRespuesta.huella_unica, String),
            ),
            (
                EncRespuesta.user_id.isnot(None),
                literal("user:") + cast(EncRespuesta.user_id, String),
            ),
            (
                EncRespuesta.dni.isnot(None),
                literal("dni:") + cast(EncRespuesta.dni, String),
            ),
            (
                EncRespuesta.phone.isnot(None),
                literal("phone:") + cast(EncRespuesta.phone, String),
            ),
            (
                EncRespuesta.ip.isnot(None),
                literal("ip:") + cast(EncRespuesta.ip, String),
            ),
            else_=literal("anon:") + cast(EncRespuesta.id, String),
        )
        unique_participants = int(
            selected_query.with_entities(
                db.func.count(db.func.distinct(identity_expr))
            )
            .order_by(None)
            .scalar()
            or 0
        )
        return {
            "channels": channels,
            "channel_metadata": channel_metadata,
            "utm": utm,
            "dimensions": dimensions,
            "unique_participants": unique_participants,
            "utm_top_limit": 500,
            "dimension_top_limit": 50,
        }

    return _snapshot_value(snapshot, "exact_summary_frequencies", _load)


def _time_bucket_expression(granularity: str):
    normalized = str(granularity or "day").strip().lower()
    dialect = str(getattr(db.session.get_bind().dialect, "name", "") or "")
    if dialect == "postgresql":
        unit = "minute" if normalized == "minute" else "hour" if normalized == "hour" else "day"
        return db.func.date_trunc(unit, EncRespuesta.submitted_at)
    if normalized == "minute":
        return db.func.strftime("%Y-%m-%dT%H:%M:00", EncRespuesta.submitted_at)
    if normalized == "hour":
        return db.func.strftime("%Y-%m-%dT%H:00:00", EncRespuesta.submitted_at)
    return db.func.strftime("%Y-%m-%d", EncRespuesta.submitted_at)


def _bucket_iso(value: Any, granularity: str) -> str:
    normalized = str(granularity or "day").strip().lower()
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if normalized == "day":
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _exact_timeseries(
    snapshot: Dict[str, Any],
    granularity: str,
    *,
    max_buckets: int = SURVEY_ANALYTICS_MAX_TIMESERIES_BUCKETS,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    normalized = str(granularity or "day").strip().lower()
    if normalized not in {"minute", "hour", "day"}:
        normalized = "day"
    effective_limit = max(1, min(int(max_buckets), SURVEY_ANALYTICS_MAX_TIMESERIES_BUCKETS))
    cache_key = f"exact_timeseries:{normalized}:{effective_limit}"

    def _load():
        bucket_expr = _time_bucket_expression(normalized)
        rows = (
            snapshot["selected_query"]
            .with_entities(
                bucket_expr.label("bucket"),
                db.func.count(EncRespuesta.id).label("total"),
            )
            .filter(EncRespuesta.submitted_at.isnot(None))
            .group_by(bucket_expr)
            .order_by(bucket_expr.desc())
            .limit(effective_limit + 1)
            .all()
        )
        partial = len(rows) > effective_limit
        rows = rows[:effective_limit]
        rows.reverse()
        series = [
            {
                "fecha": _bucket_iso(bucket, normalized),
                "total": int(total or 0),
            }
            for bucket, total in rows
        ]
        return series, {
            "contract_version": SURVEY_ANALYTICS_SAMPLE_CONTRACT_VERSION,
            "granularity": normalized,
            "bucket_limit": effective_limit,
            "bucket_count": len(series),
            "partial": partial,
        }

    return _snapshot_value(snapshot, cache_key, _load)


def _exact_recent_windows(
    snapshot: Dict[str, Any],
    *,
    now: datetime,
    window_minutes: int,
) -> Tuple[int, int, int]:
    window = max(1, min(int(window_minutes or 10), 60))
    normalized_now = _as_utc_datetime(now)
    current_start = normalized_now - timedelta(minutes=window)
    previous_start = normalized_now - timedelta(minutes=window * 2)
    row = (
        snapshot["selected_query"]
        .with_entities(
            db.func.coalesce(
                db.func.sum(
                    case(
                        (
                            and_(
                                EncRespuesta.submitted_at >= current_start,
                                EncRespuesta.submitted_at <= normalized_now,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("current_count"),
            db.func.coalesce(
                db.func.sum(
                    case(
                        (
                            and_(
                                EncRespuesta.submitted_at >= previous_start,
                                EncRespuesta.submitted_at < current_start,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("previous_count"),
            db.func.coalesce(
                db.func.sum(
                    case(
                        (
                            and_(
                                EncRespuesta.submitted_at
                                >= normalized_now - timedelta(hours=1),
                                EncRespuesta.submitted_at <= normalized_now,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("last_hour_count"),
        )
        .order_by(None)
        .one()
    )
    return (
        int(row.current_count or 0),
        int(row.previous_count or 0),
        int(row.last_hour_count or 0),
    )


def _exact_geo_cells(
    snapshot: Dict[str, Any],
    *,
    max_cells: int,
    minimum_count: int = 1,
    precision: int = 3,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    effective_limit = max(1, min(int(max_cells or 1), 5_000))
    effective_minimum = max(1, int(minimum_count or 1))
    cache_key = f"geo_cells:{precision}:{effective_minimum}:{effective_limit}"

    def _load():
        lat_cell = db.func.round(cast(EncRespuesta.lat, Numeric), precision)
        lng_cell = db.func.round(cast(EncRespuesta.lng, Numeric), precision)
        total_expr = db.func.count(EncRespuesta.id)
        grouped_cells = (
            snapshot["selected_query"]
            .with_entities(
                lat_cell.label("lat_cell"),
                lng_cell.label("lng_cell"),
                db.func.avg(EncRespuesta.lat).label("centroid_lat"),
                db.func.avg(EncRespuesta.lng).label("centroid_lng"),
                total_expr.label("total"),
            )
            .filter(
                EncRespuesta.lat.isnot(None),
                EncRespuesta.lng.isnot(None),
                EncRespuesta.lat.between(-90, 90),
                EncRespuesta.lng.between(-180, 180),
            )
            .group_by(lat_cell, lng_cell)
            .order_by(None)
            .subquery()
        )
        (
            total_cell_count,
            geo_response_count,
            suppressed_cell_count,
            suppressed_response_count,
        ) = (
            db.session.query(
                db.func.count(grouped_cells.c.total),
                db.func.coalesce(db.func.sum(grouped_cells.c.total), 0),
                db.func.coalesce(
                    db.func.sum(
                        case(
                            (grouped_cells.c.total < effective_minimum, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                db.func.coalesce(
                    db.func.sum(
                        case(
                            (
                                grouped_cells.c.total < effective_minimum,
                                grouped_cells.c.total,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .one()
        )
        rows = (
            db.session.query(
                grouped_cells.c.lat_cell,
                grouped_cells.c.lng_cell,
                grouped_cells.c.centroid_lat,
                grouped_cells.c.centroid_lng,
                grouped_cells.c.total,
            )
            .filter(grouped_cells.c.total >= effective_minimum)
            .order_by(
                grouped_cells.c.total.desc(),
                grouped_cells.c.lat_cell.asc(),
                grouped_cells.c.lng_cell.asc(),
            )
            .limit(effective_limit + 1)
            .all()
        )
        partial = len(rows) > effective_limit
        rows = rows[:effective_limit]
        cells: List[Dict[str, Any]] = []
        for lat_cell_value, lng_cell_value, centroid_lat, centroid_lng, total in rows:
            lat_value = float(centroid_lat if centroid_lat is not None else lat_cell_value)
            lng_value = float(centroid_lng if centroid_lng is not None else lng_cell_value)
            count = int(total or 0)
            cells.append(
                {
                    "cell_id": f"grid_{round(float(lat_cell_value), precision)}_{round(float(lng_cell_value), precision)}_{precision}",
                    "count": count,
                    "centroid_lat": round(lat_value, 6),
                    "centroid_lon": round(lng_value, 6),
                }
            )
        enrich_heatmap_cells(cells)
        return cells, {
            "contract_version": SURVEY_ANALYTICS_SAMPLE_CONTRACT_VERSION,
            "aggregation": f"sql_grid_{precision}_decimals",
            "minimum_cell_size": effective_minimum,
            "cell_limit": effective_limit,
            "cell_count": len(cells),
            "total_cell_count": int(total_cell_count or 0),
            "geo_response_count": int(geo_response_count or 0),
            "safe_cell_count": max(
                0,
                int(total_cell_count or 0) - int(suppressed_cell_count or 0),
            ),
            "safe_response_count": max(
                0,
                int(geo_response_count or 0) - int(suppressed_response_count or 0),
            ),
            "suppressed_cell_count": int(suppressed_cell_count or 0),
            "suppressed_response_count": int(suppressed_response_count or 0),
            "has_suppressed_cells": bool(suppressed_cell_count),
            "partial": partial,
        }

    return _snapshot_value(snapshot, cache_key, _load)


def _bounded_geo_points(
    snapshot: Dict[str, Any],
    *,
    limit: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    effective_limit = max(0, min(int(limit or 0), 5_000))
    cache_key = f"geo_points:{effective_limit}"

    def _load():
        if effective_limit <= 0:
            return [], {
                "sample_limit": 0,
                "sample_size": 0,
                "sampled": False,
                "partial": False,
            }
        rows = (
            snapshot["selected_query"]
            .with_entities(
                EncRespuesta.id,
                EncRespuesta.lat,
                EncRespuesta.lng,
                EncRespuesta.barrio,
                EncRespuesta.ciudad,
                EncRespuesta.provincia,
                EncRespuesta.pais,
                EncRespuesta.canal,
                EncRespuesta.submitted_at,
            )
            .filter(
                EncRespuesta.lat.isnot(None),
                EncRespuesta.lng.isnot(None),
                EncRespuesta.lat.between(-90, 90),
                EncRespuesta.lng.between(-180, 180),
            )
            .order_by(
                EncRespuesta.submitted_at.desc(),
                EncRespuesta.id.desc(),
            )
            .limit(effective_limit + 1)
            .all()
        )
        partial = len(rows) > effective_limit
        rows = rows[:effective_limit]
        points = [
            {
                "response_id": int(response_id),
                "lat": float(lat),
                "lng": float(lng),
                "weight": 1,
                "barrio": barrio,
                "ciudad": ciudad,
                "provincia": provincia,
                "pais": pais,
                "canal": canal,
                "submitted_at": submitted_at.isoformat()
                if submitted_at
                else None,
            }
            for (
                response_id,
                lat,
                lng,
                barrio,
                ciudad,
                provincia,
                pais,
                canal,
                submitted_at,
            ) in rows
        ]
        enrich_heatmap_points(
            points,
            property_keys=("barrio", "ciudad", "provincia", "pais", "canal"),
        )
        return points, {
            "sample_limit": effective_limit,
            "sample_size": len(points),
            "sampled": partial,
            "partial": partial,
        }

    return _snapshot_value(snapshot, cache_key, _load)


def _public_live_cache_get(key: str) -> Optional[Dict[str, Any]]:
    now = monotonic()
    with _PUBLIC_LIVE_RESULTS_CACHE_LOCK:
        cached = _PUBLIC_LIVE_RESULTS_CACHE.get(key)
        if cached is None:
            return None
        expires_at, payload = cached
        if expires_at <= now:
            _PUBLIC_LIVE_RESULTS_CACHE.pop(key, None)
            return None
        return deepcopy(payload)


def _public_live_cache_put(key: str, payload: Dict[str, Any]) -> None:
    now = monotonic()
    with _PUBLIC_LIVE_RESULTS_CACHE_LOCK:
        expired = [
            cache_key
            for cache_key, (expires_at, _payload) in _PUBLIC_LIVE_RESULTS_CACHE.items()
            if expires_at <= now
        ]
        for cache_key in expired:
            _PUBLIC_LIVE_RESULTS_CACHE.pop(cache_key, None)
        while len(_PUBLIC_LIVE_RESULTS_CACHE) >= PUBLIC_LIVE_RESULTS_CACHE_MAX_ENTRIES:
            oldest_key = next(iter(_PUBLIC_LIVE_RESULTS_CACHE), None)
            if oldest_key is None:
                break
            _PUBLIC_LIVE_RESULTS_CACHE.pop(oldest_key, None)
        _PUBLIC_LIVE_RESULTS_CACHE[key] = (
            now + PUBLIC_LIVE_RESULTS_CACHE_TTL_SECONDS,
            deepcopy(payload),
        )


def live_results_http_etag(payload: Mapping[str, Any]) -> str:
    # Hash every stable representation field.  Counts alone are insufficient:
    # an operator can rename a question/option without adding a response, and
    # clients must not receive a false 304 for the previous presentation.
    material = {
        key: value
        for key, value in payload.items()
        if key not in {
            "cache_etag",
            "cache_control",
            "request_id",
            "updated_at",
        }
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _top_counter(counter: Counter, limit: int = 10) -> List[Dict[str, Any]]:
    return [
        {"label": label, "value": count}
        for label, count in counter.most_common(limit)
    ]


def _counter_to_list(counter: Counter) -> List[Dict[str, Any]]:
    """Return a stable list representation for chart-friendly payloads."""

    # ``Counter`` preserves insertion order starting from Python 3.7, but we
    # still sort descending to match the behaviour of ``most_common`` which the
    # frontend was already using for other widgets.
    return [
        {"label": label, "value": counter[label]}
        for label in sorted(counter.keys(), key=lambda key: counter[key], reverse=True)
    ]


DEFAULT_HEATMAP_RESOLUTION = 8
NOETHER_ANALYTICS_MAPS_SURFACE = {
    "name": "Noether Analytics Maps",
    "scope": "surveys_live_heatmap",
    "supports": [
        "live_vote_heatmaps",
        "privacy_safe_geo_aggregation",
        "maplibre_layers",
        "socket_polling_sync",
        "operator_actions",
    ],
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _resolve_live_analytics_range(
    filtros: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    requested_filters = dict(filtros or {})
    effective_filters = {
        key: value
        for key, value in requested_filters.items()
        if key not in {"range_preset", "range_timezone"}
    }
    preset = str(requested_filters.get("range_preset") or "").strip().lower() or None
    timezone_name = str(requested_filters.get("range_timezone") or "UTC").strip() or "UTC"
    custom_desde = requested_filters.get("desde")
    custom_hasta = requested_filters.get("hasta")

    try:
        range_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EncuestaError(
            f"Zona horaria invalida: {timezone_name}",
            status_code=400,
            payload={"reason_code": "invalid_analytics_range_timezone"},
        ) from exc

    if preset and preset not in LIVE_ANALYTICS_RANGE_PRESETS:
        raise EncuestaError(
            f"Preset de rango analitico invalido: {preset}",
            status_code=400,
            payload={
                "reason_code": "invalid_analytics_range_preset",
                "allowed_values": sorted(LIVE_ANALYTICS_RANGE_PRESETS),
            },
        )
    if preset and (custom_desde or custom_hasta):
        raise EncuestaError(
            "Usa range_preset o desde/hasta, no ambos.",
            status_code=400,
            payload={"reason_code": "ambiguous_analytics_range"},
        )
    if bool(custom_desde) != bool(custom_hasta):
        raise EncuestaError(
            "El rango personalizado requiere desde y hasta.",
            status_code=400,
            payload={"reason_code": "incomplete_analytics_range"},
        )

    now_utc = _as_utc_datetime(now or _utc_now())
    if preset:
        # Rolling presets must share the same cohort and cache representation
        # throughout one live-cache window. Microsecond-level boundaries turn
        # every poll into a miss and change the ETag without any new response.
        bucket_seconds = max(1, int(PUBLIC_LIVE_RESULTS_CACHE_TTL_SECONDS))
        bucket_epoch = (
            int(now_utc.timestamp()) // bucket_seconds
        ) * bucket_seconds
        now_utc = datetime.fromtimestamp(bucket_epoch, timezone.utc)
    desde: Optional[datetime] = None
    hasta: Optional[datetime] = None
    mode = "all_time"
    label = "Todo el histórico"

    if preset:
        mode = "preset"
        hasta = now_utc
        label = LIVE_ANALYTICS_RANGE_PRESETS[preset]
        if preset == "last_60m":
            desde = now_utc - timedelta(minutes=60)
        elif preset == "last_24h":
            desde = now_utc - timedelta(hours=24)
        else:
            local_now = now_utc.astimezone(range_timezone)
            desde = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    elif custom_desde and custom_hasta:
        mode = "custom"
        label = "Rango personalizado"
        desde = _parse_datetime(str(custom_desde))
        hasta = _parse_datetime(str(custom_hasta))
        if desde is None or hasta is None:
            raise EncuestaError(
                "El rango personalizado requiere fechas validas.",
                status_code=400,
                payload={"reason_code": "invalid_analytics_range"},
            )

    if desde is not None and hasta is not None:
        if desde > hasta:
            raise EncuestaError(
                "El inicio del rango analitico no puede ser posterior al fin.",
                status_code=400,
                payload={"reason_code": "invalid_analytics_range_order"},
            )
        effective_filters["desde"] = desde.isoformat()
        effective_filters["hasta"] = hasta.isoformat()

    duration_minutes = None
    if desde is not None and hasta is not None:
        duration_minutes = max(0, int((hasta - desde).total_seconds() // 60))

    analytics_range = {
        "contract_version": LIVE_ANALYTICS_RANGE_CONTRACT_VERSION,
        "mode": mode,
        "preset": preset,
        "label": label,
        "timezone": timezone_name,
        "desde": desde.isoformat() if desde is not None else None,
        "hasta": hasta.isoformat() if hasta is not None else None,
        "duration_minutes": duration_minutes,
    }
    return effective_filters, analytics_range


def _build_synthetic_heatmap_points(
    encuesta: EncEncuesta,
    respuestas: Sequence[EncRespuesta],
) -> List[Dict[str, Any]]:
    """Build fallback heatmap points when responses lack explicit coordinates."""

    tenant_geo = _resolve_geo_metadata_for_tenant(encuesta.tenant_id)
    center = (tenant_geo or {}).get("center") if isinstance(tenant_geo, dict) else None
    if not center or len(center) < 2:
        center = [-58.3816, -34.6037]

    base_lng = float(center[0])
    base_lat = float(center[1])
    synthetic_points: List[Dict[str, Any]] = []

    for idx, respuesta in enumerate(respuestas):
        if respuesta.lat is not None and respuesta.lng is not None:
            continue

        # deterministic spread around tenant center so repeated requests are stable
        lat = base_lat + (((idx % 7) - 3) * 0.0025)
        lng = base_lng + (((idx % 11) - 5) * 0.0025)
        submitted_at = respuesta.submitted_at
        synthetic_points.append(
            {
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "w": 0.5,
                "barrio": respuesta.barrio,
                "ciudad": respuesta.ciudad,
                "provincia": respuesta.provincia,
                "pais": respuesta.pais,
                "canal": respuesta.canal,
                "synthetic": True,
                "submitted_at": submitted_at.isoformat() if submitted_at else None,
            }
        )

    return synthetic_points


def _aggregate_heatmap_cells(
    respuestas: Sequence[EncRespuesta],
    *,
    resolution: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    points: List[Dict[str, Any]] = []
    cells: Dict[str, Dict[str, Any]] = {}
    effective_resolution = resolution or DEFAULT_HEATMAP_RESOLUTION

    for respuesta in respuestas:
        if respuesta.lat is None or respuesta.lng is None:
            continue

        lat = float(respuesta.lat)
        lng = float(respuesta.lng)
        submitted_at = respuesta.submitted_at
        points.append(
            {
                "lat": lat,
                "lng": lng,
                "w": 1.0,
                "categoria": _extract_response_category(respuesta),
                "barrio": respuesta.barrio,
                "ciudad": respuesta.ciudad,
                "provincia": respuesta.provincia,
                "pais": respuesta.pais,
                "canal": respuesta.canal,
                "submitted_at": submitted_at.isoformat() if submitted_at else None,
            }
        )

        cell_id = compute_heatmap_cell_id(lat, lng, effective_resolution)
        cell = cells.setdefault(
            cell_id,
            {
                "count": 0,
                "lat_sum": 0.0,
                "lng_sum": 0.0,
                "barrios": defaultdict(int),
                "canales": defaultdict(int),
            },
        )
        cell["count"] += 1
        cell["lat_sum"] += lat
        cell["lng_sum"] += lng
        if respuesta.barrio:
            cell["barrios"][respuesta.barrio] += 1
        if respuesta.canal:
            cell["canales"][respuesta.canal] += 1

    cells_payload: List[Dict[str, Any]] = []
    for cell_id, data in cells.items():
        centroid_lat, centroid_lng = compute_heatmap_centroid(
            cell_id,
            lat_sum=data["lat_sum"],
            lng_sum=data["lng_sum"],
            count=data["count"],
        )
        cells_payload.append(
            {
                "cell_id": cell_id,
                "count": data["count"],
                "centroid_lat": round(centroid_lat, 6) if centroid_lat is not None else None,
                "centroid_lon": round(centroid_lng, 6) if centroid_lng is not None else None,
                "barrios": dict(
                    sorted(data["barrios"].items(), key=lambda item: item[1], reverse=True)
                ),
                "canales": dict(
                    sorted(data["canales"].items(), key=lambda item: item[1], reverse=True)
                ),
            }
        )

    cells_payload.sort(key=lambda cell: cell["count"], reverse=True)
    enrich_heatmap_points(
        points,
        property_keys=("barrio", "ciudad", "provincia", "pais", "canal"),
    )
    enrich_heatmap_cells(
        cells_payload,
        property_keys=("barrios", "canales"),
    )
    return points, cells_payload


def _normalize_live_geo_privacy(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"raw", "admin_raw", "exact"}:
        return "raw"
    return "public_aggregated"


def _public_heatmap_point_from_cell(cell: Mapping[str, Any]) -> Dict[str, Any] | None:
    lat = cell.get("centroid_lat")
    lng = cell.get("centroid_lon")
    try:
        lat_value = float(lat)
        lng_value = float(lng)
    except (TypeError, ValueError):
        return None

    count = int(cell.get("count") or 0)
    if count < _public_small_cell_minimum():
        return None

    return {
        "cell_id": cell.get("cell_id"),
        "lat": round(lat_value, 3),
        "lng": round(lng_value, 3),
        "w": float(count or 1),
        "weight": float(count or 1),
        "count": count,
        "source": "survey_heatmap_cell",
        "privacy_mode": "public_aggregated",
    }


def _prepare_live_heatmap_payload(
    points: Sequence[Mapping[str, Any]],
    cells: Sequence[Mapping[str, Any]],
    *,
    geo_privacy: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    privacy_mode = _normalize_live_geo_privacy(geo_privacy)
    if privacy_mode == "raw":
        return (
            [dict(point) for point in points],
            [dict(cell) for cell in cells],
            {
                "privacy_mode": "raw",
                "raw_points_redacted": False,
                "coordinate_precision": "exact",
            },
        )

    minimum_cell_size = _public_small_cell_minimum()
    publishable_cells = [
        cell
        for cell in cells
        if isinstance(cell, Mapping)
        and int(cell.get("count") or 0) >= minimum_cell_size
    ]
    public_points = [
        point
        for point in (
            _public_heatmap_point_from_cell(cell) for cell in publishable_cells
        )
        if point is not None
    ]
    public_cells: List[Dict[str, Any]] = []
    for cell in publishable_cells:
        next_cell = {
            "cell_id": cell.get("cell_id"),
            "count": int(cell.get("count") or 0),
            "weight": int(cell.get("count") or 0),
            "privacy_mode": "public_aggregated",
        }
        if cell.get("centroid_lat") is not None:
            next_cell["centroid_lat"] = round(float(cell["centroid_lat"]), 3)
        if cell.get("centroid_lon") is not None:
            next_cell["centroid_lon"] = round(float(cell["centroid_lon"]), 3)
        public_cells.append(next_cell)

    return (
        public_points,
        public_cells,
        {
            "privacy_mode": "public_aggregated",
            "raw_points_redacted": True,
            "coordinate_precision": "rounded_3_decimals",
            "aggregation_mode": "one_point_per_heatmap_cell",
            "minimum_cell_size": minimum_cell_size,
            "small_cells_suppressed": True,
        },
    )


def _build_map_filter(points: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Return filter metadata for the heatmap payload.

    The modern admin dashboard can expose dynamic filters for map layers and it
    expects the backend to provide the available values with usage counts so it
    can render the controls without extra round-trips.  We derive the
    statistics from the already-normalised points list to avoid additional
    database queries.
    """

    filter_counters: Dict[str, Counter] = {
        "canal": Counter(),
        "barrio": Counter(),
        "ciudad": Counter(),
        "provincia": Counter(),
        "pais": Counter(),
    }

    for point in points:
        if not isinstance(point, Mapping):  # type: ignore[arg-type]
            continue
        for key, counter in filter_counters.items():
            raw_value = point.get(key)
            if raw_value is None:
                continue
            values: Iterable[Any]
            if isinstance(raw_value, (list, tuple, set)):
                values = raw_value
            else:
                values = (raw_value,)
            for value in values:
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    counter[text] += 1

    options: Dict[str, List[Dict[str, Any]]] = {}
    for key, counter in filter_counters.items():
        if not counter:
            continue
        options[key] = [
            {"label": label, "value": label, "count": count}
            for label, count in counter.most_common()
        ]

    keys = sorted(options.keys())
    return {
        "available": bool(options),
        "keys": keys,
        "options": options,
    }


def _extract_response_category(respuesta: EncRespuesta) -> str:
    """Return a category-like label for map segmentation from response details."""

    for detalle in (respuesta.detalles or []):
        opcion = getattr(detalle, "opcion", None)
        texto_opcion = (getattr(opcion, "texto", None) or "").strip() if opcion else ""
        if texto_opcion:
            return texto_opcion
        texto_libre = (getattr(detalle, "texto_libre", None) or "").strip()
        if texto_libre:
            return texto_libre[:80]
    return "sin_categoria"


def _build_category_heatmap_layers(points: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    palette = ["#EF4444", "#F97316", "#EAB308", "#22C55E", "#06B6D4", "#3B82F6", "#8B5CF6", "#EC4899"]
    grouped: Dict[str, Dict[str, Any]] = {}

    for point in points:
        if not isinstance(point, Mapping):
            continue
        lat = point.get("lat")
        lng = point.get("lng")
        if lat is None or lng is None:
            continue
        categoria = str(point.get("categoria") or "sin_categoria").strip().lower() or "sin_categoria"
        weight = float(point.get("weight") or point.get("w") or point.get("count") or 1.0)
        ts = point.get("ts") or point.get("submitted_at")
        bucket = grouped.setdefault(categoria, {"count": 0, "weight": 0.0, "points": []})
        bucket["count"] += 1
        bucket["weight"] += max(weight, 0.0)
        mapped_point = {"lat": float(lat), "lng": float(lng), "weight": round(max(weight, 0.0), 4)}
        if ts:
            mapped_point["ts"] = ts
        bucket["points"].append(mapped_point)

    ranked = sorted(grouped.items(), key=lambda item: item[1]["weight"], reverse=True)
    feature_collection: Dict[str, Any] = {"type": "FeatureCollection", "features": []}
    for name, data in ranked:
        for point in data.get("points") or []:
            feature_collection["features"].append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [point["lng"], point["lat"]]},
                    "properties": {
                        "categoria": name,
                        "weight": point.get("weight", 1.0),
                        "ts": point.get("ts"),
                    },
                }
            )

    map_config = get_map_config() or {}
    style_url = map_config.get("style_url") or "https://demotiles.maplibre.org/style.json"

    if not ranked:
        return {
            "provider": "maplibre",
            "engine": "maplibre-gl-js",
            "style_url": style_url,
            "contract_version": _MAP_CONTRACT_VERSION,
            "categories": [],
            "legend": {"mode": "category_weight", "min_weight": 0, "max_weight": 0},
            "source": feature_collection,
            "source_options": {"cluster": True, "clusterMaxZoom": 14, "clusterRadius": 45},
            "layers": {
                "heatmap": {"id": "encuestas-heat", "type": "heatmap", "source": "encuestas"},
                "clusters": {"id": "encuestas-clusters", "type": "circle", "source": "encuestas"},
                "points": {"id": "encuestas-points", "type": "circle", "source": "encuestas"},
            },
            "interactions": {"hover": True, "time_slider": {"enabled": False, "field": "ts"}},
            "source_meta": {"total_input_points": len(points)},
            "telemetry": {
                "event_endpoint": "/api/analytics/event",
                "events": ["map_loaded", "layer_toggle", "time_slider_changed", "cluster_click"],
            },
        }

    max_weight = max(float(item[1]["weight"]) for item in ranked) or 1.0
    has_time_values = any(bool(point.get("ts")) for _, data in ranked for point in (data.get("points") or []))
    categories = []
    for index, (name, data) in enumerate(ranked):
        categories.append(
            {
                "categoria": name,
                "color": palette[index % len(palette)],
                "event_count": int(data["count"]),
                "total_weight": round(float(data["weight"]), 4),
                "intensity": round(float(data["weight"]) / max_weight, 4),
                "points": data["points"],
            }
        )

    return {
        "provider": "maplibre",
        "engine": "maplibre-gl-js",
        "style_url": style_url,
        "contract_version": _MAP_CONTRACT_VERSION,
        "source": feature_collection,
        "source_meta": {"total_input_points": len(points)},
        "source_options": {"cluster": True, "clusterRadius": 45, "clusterMaxZoom": 14},
        "layers": {
            "heatmap": {"id": "encuestas-heat", "type": "heatmap", "source": "encuestas"},
            "clusters": {"id": "encuestas-clusters", "type": "circle", "source": "encuestas", "filter": ["has", "point_count"]},
            "points": {"id": "encuestas-points", "type": "circle", "source": "encuestas", "filter": ["!", ["has", "point_count"]]},
        },
        "interactions": {"hover": True, "time_slider": {"enabled": bool(has_time_values), "field": "ts"}},
        "categories": categories,
        "legend": {"mode": "category_weight", "min_weight": 0, "max_weight": round(max_weight, 4)},
        "telemetry": {
            "event_endpoint": "/api/analytics/event",
            "events": ["map_loaded", "layer_toggle", "time_slider_changed", "cluster_click"],
        },
    }


def _build_survey_ai_items(
    encuesta: EncEncuesta,
    points: Sequence[Dict[str, Any]],
    cells: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    title = getattr(encuesta, "titulo", None) or getattr(encuesta, "nombre", None) or "encuesta"
    for point in points:
        if not isinstance(point, Mapping):
            continue
        items.append(
            {
                "source": "survey",
                "text": " ".join(
                    str(value)
                    for value in (
                        title,
                        point.get("categoria"),
                        point.get("barrio"),
                        point.get("ciudad"),
                        point.get("provincia"),
                        point.get("canal"),
                    )
                    if value
                ),
                "category": point.get("categoria"),
                "channel": point.get("canal"),
                "lat": point.get("lat"),
                "lng": point.get("lng"),
            }
        )
    for cell in cells:
        if not isinstance(cell, Mapping):
            continue
        barrios = cell.get("barrios") if isinstance(cell.get("barrios"), Mapping) else {}
        canales = cell.get("canales") if isinstance(cell.get("canales"), Mapping) else {}
        items.append(
            {
                "source": "survey_cell",
                "text": " ".join(
                    str(value)
                    for value in (
                        title,
                        " ".join(list(barrios.keys())[:3]),
                        " ".join(list(canales.keys())[:3]),
                        cell.get("count"),
                    )
                    if value
                ),
                "category": "encuesta",
                "channel": next(iter(canales.keys()), None) if canales else None,
            }
        )
    return items


def _build_heatmap_metadata(
    encuesta: EncEncuesta,
    points: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    tenant_geo = _resolve_geo_metadata_for_tenant(encuesta.tenant_id)
    bounds = None
    if points:
        min_lat = min(point["lat"] for point in points)
        max_lat = max(point["lat"] for point in points)
        min_lng = min(point["lng"] for point in points)
        max_lng = max(point["lng"] for point in points)
        bounds = [min_lng, min_lat, max_lng, max_lat]

    metadata = {
        "encuesta_id": encuesta.id,
        "total_points": len(points),
        "tenant_id": encuesta.tenant_id,
        "bounds": bounds,
        "tenant_bounds": tenant_geo.get("bounds") if tenant_geo else None,
        "tenant_center": tenant_geo.get("center") if tenant_geo else None,
    }
    return metadata


def get_summary(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    encuesta = get_encuesta(encuesta_id)
    visibility_plan = compile_survey_visibility(encuesta)
    snapshot = _get_response_snapshot(encuesta, filtros)
    respuestas = list(snapshot["sample"])
    data_provenance = dict(snapshot["provenance"])
    total = int(snapshot["population_size"])
    sample_total = len(respuestas)

    opciones_por_pregunta = defaultdict(Counter)
    selecciones_unicas_por_pregunta = defaultdict(Counter)
    textos_abiertos: Dict[int, List[str]] = defaultdict(list)
    elegibles_por_pregunta: Counter = Counter()
    respuestas_por_pregunta: Counter = Counter()
    preguntas_por_id = {pregunta.id: pregunta for pregunta in encuesta.preguntas}
    tipos_respuesta_opcion = frozenset(
        {"opcion_unica", "opcion_multiple", "rating_emoji"}
    )
    opciones_validas_por_pregunta = {
        pregunta.id: {opcion.id for opcion in pregunta.opciones}
        for pregunta in encuesta.preguntas
    }
    canales = Counter()
    utm = Counter()
    participantes_unicos: set[str] = set()
    generos = Counter()
    rangos_etarios = Counter()
    barrios = Counter()
    ciudades = Counter()
    provincias = Counter()
    paises = Counter()
    edades: List[int] = []

    respuestas_completas = 0

    for respuesta in respuestas:
        canales[respuesta.canal or "sin_canal"] += 1
        utm_key = f"{respuesta.utm_source or 'n/a'}|{respuesta.utm_campaign or 'n/a'}"
        utm[utm_key] += 1

        fingerprint = (
            respuesta.huella_unica
            or (respuesta.user_id and f"user:{respuesta.user_id}")
            or (respuesta.dni and f"dni:{respuesta.dni.strip()}")
            or (respuesta.phone and f"phone:{respuesta.phone.strip()}")
            or (respuesta.ip and f"ip:{respuesta.ip}")
        )
        participantes_unicos.add(str(fingerprint or f"anon:{respuesta.id}"))

        if respuesta.genero:
            generos[respuesta.genero] += 1
        if respuesta.rango_etario:
            rangos_etarios[respuesta.rango_etario] += 1
        if respuesta.barrio:
            barrios[respuesta.barrio] += 1
        if respuesta.ciudad:
            ciudades[respuesta.ciudad] += 1
        if respuesta.provincia:
            provincias[respuesta.provincia] += 1
        if respuesta.pais:
            paises[respuesta.pais] += 1
        if isinstance(respuesta.edad, int):
            edades.append(respuesta.edad)

        selected_option_ids_by_question_id: Dict[int, set[int]] = defaultdict(set)
        preguntas_con_respuesta_sustantiva: set[int] = set()
        for detalle in respuesta.detalles:
            pregunta = preguntas_por_id.get(detalle.pregunta_id)
            if detalle.opcion_id:
                opciones_por_pregunta[detalle.pregunta_id][detalle.opcion_id] += 1
                selected_option_ids_by_question_id[detalle.pregunta_id].add(
                    detalle.opcion_id
                )
                opcion_es_valida = (
                    detalle.opcion_id
                    in opciones_validas_por_pregunta.get(
                        detalle.pregunta_id, set()
                    )
                )
                if (
                    pregunta is not None
                    and pregunta.tipo in tipos_respuesta_opcion
                    and opcion_es_valida
                ):
                    preguntas_con_respuesta_sustantiva.add(detalle.pregunta_id)
            if detalle.texto_libre:
                textos_abiertos[detalle.pregunta_id].append(detalle.texto_libre)
                if (
                    pregunta is not None
                    and pregunta.tipo == "abierta"
                    and str(detalle.texto_libre).strip()
                ):
                    preguntas_con_respuesta_sustantiva.add(detalle.pregunta_id)

        preguntas_visibles = visibility_plan.evaluate(
            selected_option_ids_by_question_id=(
                selected_option_ids_by_question_id
            )
        )
        preguntas_visibles_ids = {
            pregunta.id for pregunta in preguntas_visibles
        }
        preguntas_respondidas_ids = preguntas_con_respuesta_sustantiva.intersection(
            preguntas_visibles_ids
        )
        elegibles_por_pregunta.update(preguntas_visibles_ids)
        respuestas_por_pregunta.update(preguntas_respondidas_ids)
        for pregunta_id in preguntas_visibles_ids:
            for opcion_id in selected_option_ids_by_question_id.get(
                pregunta_id, set()
            ).intersection(opciones_validas_por_pregunta.get(pregunta_id, set())):
                # One response contributes at most once to an option's new
                # respondent-based rates, even if historical data contains
                # duplicate detail rows. Legacy counters above remain intact.
                selecciones_unicas_por_pregunta[pregunta_id][opcion_id] += 1

        preguntas_obligatorias_visibles = {
            pregunta.id
            for pregunta in preguntas_visibles
            if getattr(pregunta, "obligatoria", False)
        }
        if preguntas_obligatorias_visibles:
            if all(
                pid in preguntas_con_respuesta_sustantiva
                for pid in preguntas_obligatorias_visibles
            ):
                respuestas_completas += 1
        else:
            respuestas_completas += 1

    # Frequencies and participant totals are exact SQL aggregates.  Only the
    # visibility/completion estimates and free-text examples below use the
    # explicitly bounded response sample.
    (
        opciones_por_pregunta,
        selecciones_unicas_por_pregunta,
        exact_answered_by_question,
    ) = _exact_option_statistics(snapshot)
    respuestas_por_pregunta = exact_answered_by_question
    exact_frequencies = _exact_summary_frequencies(snapshot)
    canales = exact_frequencies["channels"]
    utm = exact_frequencies["utm"]
    generos = exact_frequencies["dimensions"]["genero"]
    rangos_etarios = exact_frequencies["dimensions"]["rango_etario"]
    barrios = exact_frequencies["dimensions"]["barrio"]
    ciudades = exact_frequencies["dimensions"]["ciudad"]
    provincias = exact_frequencies["dimensions"]["provincia"]
    paises = exact_frequencies["dimensions"]["pais"]
    participantes_unicos_exactos = int(exact_frequencies["unique_participants"])

    partial_sample = bool(data_provenance.get("partial"))
    if partial_sample and sample_total:
        for pregunta in encuesta.preguntas:
            sampled_eligible = int(elegibles_por_pregunta[pregunta.id])
            if sampled_eligible >= sample_total:
                elegibles_por_pregunta[pregunta.id] = total
            else:
                elegibles_por_pregunta[pregunta.id] = min(
                    total,
                    int(round(sampled_eligible / sample_total * total)),
                )
        respuestas_completas = min(
            total,
            int(round(respuestas_completas / sample_total * total)),
        )
    data_provenance.update(
        {
            "exact_aggregates": True,
            "text_examples_sampled": partial_sample,
            "eligibility_estimated_from_sample": partial_sample,
            "completion_estimated_from_sample": partial_sample,
            "utm_top_limit": exact_frequencies["utm_top_limit"],
            "dimension_top_limit": exact_frequencies["dimension_top_limit"],
            "channel_top_limit": exact_frequencies["channel_metadata"]["top_limit"],
            "channel_distinct_total": exact_frequencies["channel_metadata"]["distinct_total"],
            "channel_truncated": exact_frequencies["channel_metadata"]["truncated"],
        }
    )

    preguntas_summary = []
    for pregunta in encuesta.preguntas:
        normalized_tipo = _normalize_question_type(pregunta.tipo)
        respuestas_elegibles = elegibles_por_pregunta[pregunta.id]
        respuestas_respondidas = respuestas_por_pregunta[pregunta.id]
        tasa_respuesta_elegible = (
            respuestas_respondidas / respuestas_elegibles * 100
            if respuestas_elegibles
            else 0
        )
        pregunta_data = {
            "pregunta_id": pregunta.id,
            "texto": pregunta.texto,
            "tipo": normalized_tipo,
            "tipo_interno": pregunta.tipo,
            "total_respuestas": total,
            "respuestas_elegibles": respuestas_elegibles,
            "respuestas_respondidas": respuestas_respondidas,
            "tasa_respuesta_elegible": round(tasa_respuesta_elegible, 2),
        }
        if normalized_tipo in {"single_choice", "multiple_choice"}:
            opciones = []
            for opcion in pregunta.opciones:
                conteo = opciones_por_pregunta[pregunta.id][opcion.id]
                respuestas_seleccionaron = selecciones_unicas_por_pregunta[
                    pregunta.id
                ][opcion.id]
                porcentaje = (conteo / total * 100) if total else 0
                porcentaje_total_encuesta = (
                    respuestas_seleccionaron / total * 100
                    if total
                    else 0
                )
                porcentaje_elegibles = (
                    respuestas_seleccionaron / respuestas_elegibles * 100
                    if respuestas_elegibles
                    else 0
                )
                porcentaje_respuestas_pregunta = (
                    respuestas_seleccionaron / respuestas_respondidas * 100
                    if respuestas_respondidas
                    else 0
                )
                # These are per-option selection rates. For multiple-choice
                # questions their sum may legitimately exceed 100 percent.
                opciones.append(
                    {
                        "opcion_id": opcion.id,
                        "texto": opcion.texto,
                        "conteo": conteo,
                        "value": conteo,
                        "porcentaje": round(porcentaje, 2),
                        "respuestas_seleccionaron": respuestas_seleccionaron,
                        "porcentaje_total_encuesta": round(
                            porcentaje_total_encuesta, 2
                        ),
                        "porcentaje_elegibles": round(
                            porcentaje_elegibles, 2
                        ),
                        "porcentaje_respuestas_pregunta": round(
                            porcentaje_respuestas_pregunta, 2
                        ),
                    }
                )
            pregunta_data["opciones"] = opciones
            pregunta_data["series"] = [
                {"label": opcion["texto"], "value": opcion["conteo"]}
                for opcion in opciones
            ]
        elif normalized_tipo == "text":
            muestras = textos_abiertos.get(pregunta.id, [])[:20]
            pregunta_data["muestras_texto"] = muestras
            # Frontend widgets expect ``opciones`` to exist so they can iterate
            # without special casing preguntas de texto libre.
            pregunta_data["opciones"] = []
            pregunta_data["series"] = []
        else:
            # Preserve backwards compatibility for unexpected question types by
            # exposing aggregated option counts when available.
            opciones = []
            for opcion in pregunta.opciones:
                conteo = opciones_por_pregunta[pregunta.id][opcion.id]
                respuestas_seleccionaron = selecciones_unicas_por_pregunta[
                    pregunta.id
                ][opcion.id]
                porcentaje = (conteo / total * 100) if total else 0
                porcentaje_total_encuesta = (
                    respuestas_seleccionaron / total * 100
                    if total
                    else 0
                )
                porcentaje_elegibles = (
                    respuestas_seleccionaron / respuestas_elegibles * 100
                    if respuestas_elegibles
                    else 0
                )
                porcentaje_respuestas_pregunta = (
                    respuestas_seleccionaron / respuestas_respondidas * 100
                    if respuestas_respondidas
                    else 0
                )
                opciones.append(
                    {
                        "opcion_id": opcion.id,
                        "texto": opcion.texto,
                        "conteo": conteo,
                        "value": conteo,
                        "porcentaje": round(porcentaje, 2),
                        "respuestas_seleccionaron": respuestas_seleccionaron,
                        "porcentaje_total_encuesta": round(
                            porcentaje_total_encuesta, 2
                        ),
                        "porcentaje_elegibles": round(
                            porcentaje_elegibles, 2
                        ),
                        "porcentaje_respuestas_pregunta": round(
                            porcentaje_respuestas_pregunta, 2
                        ),
                    }
                )
            pregunta_data["opciones"] = opciones
            pregunta_data["series"] = [
                {"label": opcion["texto"], "value": opcion["conteo"]}
                for opcion in opciones
            ]
        preguntas_summary.append(pregunta_data)

    canales_list = [
        {
            "canal": canal,
            "label": canal,
            "conteo": count,
            "value": count,
        }
        for canal, count in sorted(canales.items(), key=lambda item: item[1], reverse=True)
    ]
    canales_map = {canal: count for canal, count in canales.items()}
    utm_data = []
    for key, count in utm.items():
        source, campaign = key.split("|", 1)
        utm_data.append({"utm_source": source, "utm_campaign": campaign, "conteo": count})

    tasa_completitud = (respuestas_completas / total * 100) if total else 0.0

    edades_ordenadas = sorted(edades)
    edad_promedio = round(mean(edades_ordenadas), 2) if edades_ordenadas else None
    edad_mediana = median(edades_ordenadas) if edades_ordenadas else None

    def _percentile(values: List[int], pct: float) -> Optional[float]:
        if not values:
            return None
        if len(values) == 1:
            return float(values[0])
        index = (len(values) - 1) * pct / 100.0
        lower = int(index)
        upper = min(lower + 1, len(values) - 1)
        fraction = index - lower
        return round(values[lower] + (values[upper] - values[lower]) * fraction, 2)

    edad_p90 = _percentile(edades_ordenadas, 90.0)

    territorio_breakdown = {
        "barrios": _top_counter(barrios),
        "ciudades": _top_counter(ciudades),
        "provincias": _top_counter(provincias),
        "paises": _top_counter(paises),
    }

    territorio_sections = [
        {
            "key": key,
            "label": key.capitalize(),
            "series": values,
        }
        for key, values in territorio_breakdown.items()
    ]

    demografia = {
        # ``genero`` and ``rango_etario`` now expose array payloads to align
        # with the modern admin dashboard, while the ``*_map`` aliases keep the
        # dictionary structure for legacy consumers and regression tests.
        "genero": _counter_to_list(generos),
        "genero_series": _counter_to_list(generos),
        "genero_map": dict(generos),
        "rango_etario": _counter_to_list(rangos_etarios),
        "rango_etario_series": _counter_to_list(rangos_etarios),
        "rango_etario_map": dict(rangos_etarios),
        "edad": {
            "promedio": edad_promedio,
            "mediana": edad_mediana,
            "p90": edad_p90,
            "muestra": len(edades_ordenadas),
        },
        # ``territorio`` now follows the array-first contract expected by the
        # modern admin dashboard (each entry already exposes ``series`` so the
        # frontend can map safely), while ``territorio_map`` keeps backwards
        # compatibility for legacy consumers and regression tests.
        "territorio": territorio_sections,
        "territorio_map": territorio_breakdown,
    }

    return {
        "encuesta_id": encuesta.id,
        "data_provenance": data_provenance,
        "total_respuestas": total,
        "participantes_unicos": participantes_unicos_exactos,
        "respuestas_completas": respuestas_completas,
        "respuestas_incompletas": max(total - respuestas_completas, 0),
        "tasa_completitud": round(tasa_completitud, 2),
        "preguntas": preguntas_summary,
        "canales": canales_list,
        "canales_map": canales_map,
        "canales_metadata": exact_frequencies["channel_metadata"],
        "utm": utm_data,
        "demografia": demografia,
    }


def get_timeseries(encuesta_id: int, granularity: str = "day", filtros: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    encuesta = get_encuesta(encuesta_id)
    snapshot = _get_response_snapshot(encuesta, filtros)
    series, _metadata = _exact_timeseries(snapshot, granularity)
    return series




def get_forecast(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    window_minutes: int = 10,
    horizon_minutes: int = 60,
) -> Dict[str, Any]:
    """Build a lightweight short-term projection from minute-level activity."""

    encuesta = get_encuesta(encuesta_id)
    snapshot = _get_response_snapshot(encuesta, filtros)
    now = _utc_now()
    window = max(5, min(int(window_minutes or 10), 60))
    horizon = max(15, min(int(horizon_minutes or 60), 240))
    current_count, _previous_count, _last_hour_count = _exact_recent_windows(
        snapshot,
        now=now,
        window_minutes=window,
    )
    baseline_total = int(snapshot["population_size"])
    moving_avg = round(current_count / window, 3)
    projected_additional = int(round(moving_avg * horizon))
    projected_total = baseline_total + projected_additional

    confidence = "media"
    if baseline_total < 20:
        confidence = "baja"
    elif baseline_total > 200:
        confidence = "alta"

    return {
        "encuesta_id": encuesta.id,
        "window_minutes": window,
        "horizon_minutes": horizon,
        "baseline_total": baseline_total,
        "current_rate_per_minute": moving_avg,
        "projected_additional": projected_additional,
        "projected_total": projected_total,
        "confidence": confidence,
        "data_provenance": dict(snapshot["provenance"]),
        "updated_at": now.isoformat(),
    }


def get_alerts(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    window_minutes: int = 10,
    min_activity_threshold: int = 5,
) -> Dict[str, Any]:
    """Evaluate alert rules for campaign operations dashboards."""

    encuesta = get_encuesta(encuesta_id)
    window = max(5, min(int(window_minutes or 10), 30))
    snapshot = _get_response_snapshot(encuesta, filtros)
    now = _utc_now()
    last_window, previous_window, _last_hour = _exact_recent_windows(
        snapshot,
        now=now,
        window_minutes=window,
    )

    delta = last_window - previous_window
    trend = "estable"
    if delta > 0:
        trend = "subiendo"
    elif delta < 0:
        trend = "bajando"

    alerts: List[Dict[str, Any]] = []
    if trend == "bajando" and previous_window >= min_activity_threshold:
        alerts.append(
            {
                "code": "participacion_en_caida",
                "severity": "high" if delta <= -max(3, min_activity_threshold // 2) else "medium",
                "message": "La participación cayó en la ventana reciente. Recomendada activación de recordatorios.",
                "delta": delta,
            }
        )
    if trend == "subiendo" and last_window >= min_activity_threshold:
        alerts.append(
            {
                "code": "momento_favorable",
                "severity": "info",
                "message": "La participación está acelerando. Buen momento para ampliar difusión.",
                "delta": delta,
            }
        )

    summary = get_summary(encuesta_id, filtros)
    leader_payload = None
    if summary.get("preguntas"):
        candidate_options: List[Dict[str, Any]] = []
        for pregunta in summary["preguntas"]:
            opciones = pregunta.get("opciones") or []
            if opciones:
                sorted_options = sorted(opciones, key=lambda item: item.get("porcentaje", 0), reverse=True)
                candidate_options.append(sorted_options[0])
        if candidate_options:
            leader_payload = sorted(candidate_options, key=lambda item: item.get("porcentaje", 0), reverse=True)[0]
    if leader_payload and float(leader_payload.get("porcentaje") or 0) >= 60:
        alerts.append(
            {
                "code": "liderazgo_marcado",
                "severity": "info",
                "message": "Se detecta un liderazgo fuerte en una opción de respuesta.",
                "value": leader_payload.get("porcentaje"),
            }
        )

    return {
        "encuesta_id": encuesta.id,
        "window_minutes": max(5, min(int(window_minutes or 10), 30)),
        "threshold": max(1, int(min_activity_threshold or 5)),
        "alerts": alerts,
        "has_alerts": bool(alerts),
        "data_provenance": dict(snapshot["provenance"]),
        "evaluated_at": now.isoformat(),
    }


def _generate_openai_executive_brief(
    encuesta: EncEncuesta,
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Optionally enrich executive brief with OpenAI when credentials are configured."""

    if not openai_client:
        return None

    payload = {
        "encuesta_id": encuesta.id,
        "titulo": encuesta.titulo,
        "summary": {
            "total_respuestas": summary.get("total_respuestas", 0),
            "participantes_unicos": summary.get("participantes_unicos", 0),
            "tasa_completitud": summary.get("tasa_completitud", 0),
        },
        "forecast": {
            "projected_total": forecast.get("projected_total", 0),
            "horizon_minutes": forecast.get("horizon_minutes", 0),
            "momentum": forecast.get("momentum", "stable"),
        },
        "alerts": alerts.get("alerts", []),
    }

    system_prompt = (
        "Eres un consultor senior de analítica cívica y experiencia ciudadana. "
        "Devuelve SOLO JSON con campos: headline (string <= 35 palabras), "
        "insights (array de 2 strings accionables), risk_level (low|medium|high)."
    )

    model = _survey_analytics_model()
    request_kwargs = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    request_kwargs.update(
        chat_completion_compatibility_options(model, legacy_temperature=0.2)
    )

    try:
        response = openai_client.chat.completions.create(
            **request_kwargs,
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        headline = str(parsed.get("headline") or "").strip()
        insights = parsed.get("insights") if isinstance(parsed.get("insights"), list) else []
        insights = [str(item).strip() for item in insights if str(item).strip()][:2]
        risk_level = str(parsed.get("risk_level") or "").strip().lower()

        if not headline:
            return None

        if risk_level not in {"low", "medium", "high"}:
            risk_level = "medium"

        return {"headline": headline, "insights": insights, "risk_level": risk_level}
    except Exception as exc:
        logger.warning(
            "[encuestas_analytics] OpenAI brief enrichment failed model=%s error_type=%s",
            model,
            type(exc).__name__,
        )
        return None


def _build_executive_ai_payload(
    encuesta: EncEncuesta,
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "contract_version": SURVEY_AI_BRIEF_CONTRACT_VERSION,
        "encuesta_id": encuesta.id,
        "data_provenance": summary.get("data_provenance"),
        "titulo": encuesta.titulo,
        "summary": {
            "total_respuestas": summary.get("total_respuestas", 0),
            "participantes_unicos": summary.get("participantes_unicos", 0),
            "tasa_completitud": summary.get("tasa_completitud", 0),
        },
        "forecast": {
            "projected_total": forecast.get("projected_total", 0),
            "horizon_minutes": forecast.get("horizon_minutes", 0),
            "momentum": forecast.get("momentum", "stable"),
        },
        "alerts": alerts.get("alerts", []),
        "policy": SURVEY_AI_ADVISORY_POLICY,
    }


def _executive_ai_system_prompt() -> str:
    return (
        "Eres un consultor senior de analitica civica y experiencia ciudadana. "
        "Devuelve SOLO JSON con campos: headline (string <= 35 palabras), "
        "insights (array de 2 strings accionables), risk_level (low|medium|high). "
        "No indiques cambios de estado ni automatizaciones operativas; solo recomendaciones advisory."
    )


def _strip_json_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[len("```json") :].strip()
    if text.startswith("```"):
        text = text[len("```") :].strip()
    if text.endswith("```"):
        text = text[: -len("```")].strip()
    return text


def _normalize_ai_brief_response(
    raw_payload: Any,
    *,
    provider: str,
    model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    try:
        parsed = raw_payload if isinstance(raw_payload, dict) else json.loads(_strip_json_fence(str(raw_payload or "")))
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None

    headline = str(parsed.get("headline") or "").strip()
    insights = parsed.get("insights") if isinstance(parsed.get("insights"), list) else []
    insights = [str(item).strip()[:240] for item in insights if str(item).strip()][:2]
    risk_level = str(parsed.get("risk_level") or "").strip().lower()

    if not headline:
        return None
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "medium"

    return {
        "contract_version": SURVEY_AI_BRIEF_CONTRACT_VERSION,
        "provider": provider,
        "model": model,
        "headline": headline[:280],
        "insights": insights,
        "risk_level": risk_level,
        "advisory_policy": dict(SURVEY_AI_ADVISORY_POLICY),
        "state_mutation": {
            "requested": False,
            "applied": False,
            "reason": "survey_ai_brief_is_advisory_only",
        },
    }


def _generate_gemini_executive_brief(
    encuesta: EncEncuesta,
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Optionally enrich executive brief with Gemini when credentials are configured."""

    try:
        from services import gemini_bridge

        if not gemini_bridge.is_gemini_llm_configured():
            return None

        genai, types = gemini_bridge._get_genai_modules()
        api_key = gemini_bridge._gemini_api_key()
        if not api_key:
            return None

        model = os.getenv("GEMINI_SURVEY_ANALYTICS_MODEL") or gemini_bridge._gemini_chat_model()
        client = genai.Client(api_key=api_key)
        payload = _build_executive_ai_payload(encuesta, summary, forecast, alerts)
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text=json.dumps(payload, ensure_ascii=False, default=str))],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=_executive_ai_system_prompt(),
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        raw = gemini_bridge._extract_response_text(response)
        return _normalize_ai_brief_response(raw, provider="gemini", model=model)
    except Exception:
        logger.warning("[encuestas_analytics] Gemini brief enrichment failed", exc_info=True)
        return None


def _executive_brief_provider_order() -> List[str]:
    raw = os.getenv("ENCUESTAS_AI_BRIEF_PROVIDERS", "gemini,openai")
    providers = [item.strip().lower() for item in raw.split(",") if item.strip()]
    ordered = [provider for provider in providers if provider in {"gemini", "openai"}]
    return ordered or ["gemini", "openai"]


def _generate_ai_executive_brief(
    encuesta: EncEncuesta,
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    for provider in _executive_brief_provider_order():
        if provider == "gemini":
            brief = _generate_gemini_executive_brief(encuesta, summary, forecast, alerts)
        else:
            raw_brief = _generate_openai_executive_brief(encuesta, summary, forecast, alerts)
            brief = _normalize_ai_brief_response(
                raw_brief,
                provider="openai",
                model=_survey_analytics_model(),
            )
        if brief:
            return brief
    return None


def get_executive_brief(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return an executive-ready summary object for frontend reporting."""

    encuesta = get_encuesta(encuesta_id)
    summary = get_summary(encuesta_id, filtros)
    forecast = get_forecast(encuesta_id, filtros=filtros)
    alerts = get_alerts(encuesta_id, filtros=filtros)

    headline = (
        f"{summary['total_respuestas']} respuestas totales con proyección a "
        f"{forecast['projected_total']} en {forecast['horizon_minutes']} minutos."
    )

    ai_brief = _generate_ai_executive_brief(encuesta, summary, forecast, alerts)
    final_headline = ai_brief.get("headline") if ai_brief else headline
    final_insights = (ai_brief.get("insights") if ai_brief else None) or [
        headline,
        "Monitorear delta de momentum para decisiones tácticas de difusión.",
    ]

    return {
        "contract_version": SURVEY_AI_BRIEF_CONTRACT_VERSION,
        "encuesta_id": encuesta.id,
        "titulo": encuesta.titulo,
        "headline": final_headline,
        "data_provenance": summary.get("data_provenance"),
        "summary": {
            "total_respuestas": summary.get("total_respuestas", 0),
            "participantes_unicos": summary.get("participantes_unicos", 0),
            "tasa_completitud": summary.get("tasa_completitud", 0),
        },
        "forecast": forecast,
        "alerts": alerts,
        "insights": final_insights,
        "ai_enhanced": bool(ai_brief),
        "ai_provider": (ai_brief or {}).get("provider") or "deterministic_fallback",
        "ai_model": (ai_brief or {}).get("model"),
        "ai_policy": dict(SURVEY_AI_ADVISORY_POLICY),
        "advisory_only": True,
        "state_mutation": {
            "requested": False,
            "applied": False,
            "reason": "survey_ai_brief_is_advisory_only",
        },
        "risk_level": (ai_brief or {}).get("risk_level", "medium"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }



def _matches_segment(respuesta: EncRespuesta, segment: Optional[Dict[str, Any]]) -> bool:
    if not segment:
        return True
    for key in ("canal", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais"):
        expected = segment.get(key)
        if expected is None or expected == "":
            continue
        value = getattr(respuesta, key, None)
        normalized_value = str(value or "").strip().lower()
        if isinstance(expected, (list, tuple, set)):
            candidates = {str(item or "").strip().lower() for item in expected if str(item or "").strip()}
            if normalized_value not in candidates:
                return False
            continue
        if "," in str(expected):
            candidates = {
                chunk.strip().lower()
                for chunk in str(expected).split(",")
                if chunk.strip()
            }
            if normalized_value not in candidates:
                return False
            continue
        if normalized_value != str(expected).strip().lower():
            return False
    return True


def _segment_distribution(
    respuestas: Sequence[EncRespuesta],
    encuesta: EncEncuesta,
) -> Dict[str, Any]:
    question_payload: List[Dict[str, Any]] = []
    for pregunta in encuesta.preguntas:
        normalized_tipo = _normalize_question_type(pregunta.tipo)
        if normalized_tipo not in {"single_choice", "multiple_choice"}:
            continue

        option_counter: Counter = Counter()
        for respuesta in respuestas:
            for detalle in respuesta.detalles:
                if detalle.pregunta_id != pregunta.id or not detalle.opcion_id:
                    continue
                option_counter[detalle.opcion_id] += 1

        options = []
        total_votes = sum(option_counter.values())
        for opcion in pregunta.opciones:
            votos = int(option_counter.get(opcion.id, 0))
            pct = round((votos / total_votes * 100), 2) if total_votes else 0.0
            options.append({
                "opcion_id": opcion.id,
                "label": opcion.texto,
                "votos": votos,
                "porcentaje": pct,
            })
        options.sort(key=lambda item: item["votos"], reverse=True)
        question_payload.append(
            {
                "pregunta_id": pregunta.id,
                "texto": pregunta.texto,
                "tipo": normalized_tipo,
                "total_votos": total_votes,
                "opciones": options,
            }
        )

    canales = Counter((respuesta.canal or "sin_canal") for respuesta in respuestas)
    return {
        "total_respuestas": len(respuestas),
        "canales": [{"label": k, "value": v} for k, v in canales.most_common()],
        "preguntas": question_payload,
    }


def get_segment_suggestions(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    """Return dynamic A/B segmentation suggestions from available survey data."""

    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)
    total = max(len(respuestas), 1)
    effective_limit = max(2, min(int(limit or 5), 10))

    suggestions: Dict[str, List[Dict[str, Any]]] = {}
    for key in ("canal", "genero", "rango_etario", "barrio", "ciudad", "provincia", "pais"):
        counter = Counter(str(getattr(respuesta, key) or "").strip() for respuesta in respuestas)
        options: List[Dict[str, Any]] = []
        for label, count in counter.most_common(effective_limit):
            if not label:
                continue
            coverage = round((count / total) * 100, 2)
            options.append(
                {
                    "label": label,
                    "filters": {key: label},
                    "count": count,
                    "coverage": coverage,
                }
            )
        if options:
            suggestions[key] = options

    return {
        "encuesta_id": encuesta.id,
        "total_respuestas": len(respuestas),
        "dimensions": suggestions,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }




def _build_executive_summary_text(
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
    heatmap: Dict[str, Any],
) -> Dict[str, Any]:
    """Compose concise executive-ready narrative from analytics signals."""

    total = int(summary.get("total_respuestas") or 0)
    completion = float(summary.get("tasa_completitud") or 0)
    projected_total = int(forecast.get("projected_total") or total)
    projected_additional = int(forecast.get("projected_additional") or 0)
    alerts_list = alerts.get("alerts") or []

    top_barrio = None
    territorio = (summary.get("demografia") or {}).get("territorio_map") or {}
    barrios = territorio.get("barrios") if isinstance(territorio, dict) else None
    if isinstance(barrios, list) and barrios:
        top_barrio = barrios[0].get("label")

    top_hotspot = None
    map_hotspots = ((heatmap.get("metadata") or {}).get("map") or {}).get("hotspots")
    if isinstance(map_hotspots, list) and map_hotspots:
        top_hotspot = map_hotspots[0].get("label")

    priorities = []
    if completion < 65:
        priorities.append("Subir completitud: revisar fricción del formulario y recordar cierre de encuesta.")
    if projected_additional <= 0:
        priorities.append("Activar difusión táctica en canales de mayor conversión para recuperar ritmo.")
    if alerts_list:
        priorities.append("Atender alertas operativas detectadas antes de escalar pauta/publicidad.")
    if top_hotspot or top_barrio:
        priorities.append(
            f"Enfocar acciones territoriales en {top_hotspot or top_barrio} y replicar aprendizaje en zonas similares."
        )

    if not priorities:
        priorities.append("Mantener estrategia actual y escalar en los canales con mejor desempeño.")

    headline = (
        f"{total} respuestas registradas ({completion:.1f}% de completitud) "
        f"con proyección a {projected_total} en la ventana actual."
    )

    return {
        "headline": headline,
        "one_liner": (
            "La operación está en curso con señales accionables para priorizar territorio, "
            "canales y experiencia de respuesta."
        ),
        "focus_points": priorities[:4],
        "alert_count": len(alerts_list),
        "projected_additional": projected_additional,
    }


def _build_visual_blueprint(
    *,
    encuesta_id: int,
    summary: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
    heatmap: Dict[str, Any],
) -> Dict[str, Any]:
    """Return chart/table specs so frontend can render a premium dashboard quickly."""

    preguntas = summary.get("preguntas") or []
    ranked_questions = sorted(
        [p for p in preguntas if isinstance(p, dict)],
        key=lambda item: int(item.get("total_respuestas") or 0),
        reverse=True,
    )

    top_questions = [
        {
            "pregunta_id": q.get("pregunta_id"),
            "texto": q.get("texto"),
            "tipo": q.get("tipo"),
            "series": q.get("series") or [],
        }
        for q in ranked_questions[:5]
    ]

    heatmap_meta = (heatmap.get("metadata") or {}).get("map") or {}
    hotspot_data = heatmap_meta.get("hotspots") if isinstance(heatmap_meta, dict) else []

    return {
        "charts": [
            {
                "id": "activity_timeseries",
                "type": "line",
                "title": "Evolución temporal de respuestas",
                "dataset_key": "timeseries",
                "x": "fecha",
                "y": "total",
            },
            {
                "id": "territory_heatmap",
                "type": "geospatial_heatmap",
                "title": "Intensidad territorial",
                "dataset_key": "heatmap.points",
                "layer_key": "heatmap_layer",
            },
            {
                "id": "hotspots_rank",
                "type": "bar",
                "title": "Top zonas calientes",
                "dataset_key": "heatmap.hotspots",
                "x": "label",
                "y": "weight",
            },
            {
                "id": "demography_gender",
                "type": "donut",
                "title": "Distribución por género",
                "dataset_key": "summary.demografia.genero",
                "x": "label",
                "y": "value",
            },
        ],
        "tables": [
            {
                "id": "questions_priority",
                "title": "Preguntas con mayor volumen",
                "dataset_key": "summary.top_questions",
                "columns": ["pregunta_id", "texto", "tipo"],
            },
            {
                "id": "hotspot_table",
                "title": "Detalle de hotspots",
                "dataset_key": "heatmap.hotspots",
                "columns": ["rank", "label", "weight", "intensity"],
            },
        ],
        "datasets": {
            "summary": summary,
            "timeseries": list(timeseries),
            "heatmap": {
                "points": heatmap.get("points") or [],
                "cells": heatmap.get("cells") or [],
                "hotspots": hotspot_data if isinstance(hotspot_data, list) else [],
                "ai_layers": heatmap.get("ai_layers") or ((heatmap.get("metadata") or {}).get("ai_layers") or {}),
                "ai_insights": heatmap.get("ai_insights") or ((heatmap.get("metadata") or {}).get("ai_insights") or {}),
            },
            "top_questions": top_questions,
        },
        "frontend_contract": {
            "version": "2026.02",
            "auth_demo": {
                "catalog_endpoint": "/auth/demo/catalog",
                "login_endpoint": "/auth/demo",
                "quick_login_field": "quick_login_payload.tenant_slug",
            },
            "map": {
                "preferred_provider": ((heatmap.get("metadata") or {}).get("map_config") or {}).get("provider") or "maplibre",
                "required_fields": ["lat", "lng", "weight"],
                "optional_layers": ["heatmap", "cells", "pulses", "hotspots", "ai_risk_layers", "survey_participation", "interactive_globe"],
                "advanced_engines": ["deckgl", "maplibre"],
                "fallback": "2d_heatmap_with_same_datasets",
            },
        },
    }




def _metric_number(value: Any, *, decimals: int = 2) -> float:
    """Normalize numeric KPI values for UI payloads."""

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(numeric, decimals)


def _build_dashboard_cards(summary: Dict[str, Any], forecast: Dict[str, Any], anomalies: Dict[str, Any]) -> List[Dict[str, Any]]:
    total = int(summary.get("total_respuestas") or 0)
    participantes = int(summary.get("participantes_unicos") or 0)
    completitud = _metric_number(summary.get("tasa_completitud"), decimals=1)
    projected = int(forecast.get("projected_total") or total)
    risk_score = _metric_number(anomalies.get("risk_score"), decimals=3)

    return [
        {"id": "total_respuestas", "label": "Total de respuestas", "value": total, "unit": "count", "kind": "kpi"},
        {"id": "participantes_unicos", "label": "Participantes únicos", "value": participantes, "unit": "count", "kind": "kpi"},
        {"id": "tasa_completitud", "label": "Tasa de completitud", "value": completitud, "unit": "percent", "kind": "kpi"},
        {"id": "projected_total", "label": "Proyección total", "value": projected, "unit": "count", "kind": "forecast"},
        {"id": "risk_score", "label": "Riesgo operativo", "value": risk_score, "unit": "score", "kind": "anomaly"},
    ]


def _build_dashboard_ui_state(summary: Dict[str, Any], heatmap: Dict[str, Any], alerts: Dict[str, Any], *, latest_responses_state: Optional[str] = None) -> Dict[str, Any]:
    total_respuestas = int(summary.get("total_respuestas") or 0)
    points = heatmap.get("points") or []
    cells = heatmap.get("cells") or []
    has_points = isinstance(points, list) and len(points) > 0
    has_cells = isinstance(cells, list) and len(cells) > 0
    has_alerts = bool((alerts.get("alerts") or []))

    latest_state = latest_responses_state or ("ready" if total_respuestas > 0 else "empty")

    return {
        "latest_responses": latest_state,
        "map_participation": "ready" if (has_points or has_cells) else "empty",
        "alerts": "attention" if has_alerts else "normal",
    }


def _build_dashboard_sections(
    *,
    summary: Dict[str, Any],
    heatmap: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
    brief: Dict[str, Any],
) -> Dict[str, Any]:
    """Expose explicit sections for FE tabs (mapas/estadísticas/IA)."""

    demografia = summary.get("demografia") or {}
    territorio = demografia.get("territorio") or []
    map_meta = (heatmap.get("metadata") or {}).get("map") or {}

    return {
        "mapas": {
            "heatmap": {
                "points": heatmap.get("points") or [],
                "cells": heatmap.get("cells") or [],
                "hotspots": map_meta.get("hotspots") or [],
                "category_layers": ((heatmap.get("metadata") or {}).get("category_layers") or {}),
                "ai_insights": heatmap.get("ai_insights") or ((heatmap.get("metadata") or {}).get("ai_insights") or {}),
                "ai_layers": heatmap.get("ai_layers") or ((heatmap.get("metadata") or {}).get("ai_layers") or {}),
                "map_experience": heatmap.get("map_experience") or ((heatmap.get("metadata") or {}).get("map_experience") or {}),
                "headline": heatmap.get("headline"),
                "legend": heatmap.get("legend") or {},
                "empty_state": heatmap.get("empty_state"),
                "recommended_action": heatmap.get("recommended_action"),
                "provider_hint": ((heatmap.get("metadata") or {}).get("map_config") or {}).get("provider") or "maplibre",
                "state": "ready" if bool((heatmap.get("points") or []) or (heatmap.get("cells") or [])) else "empty",
            },
            "territorio": territorio,
        },
        "estadisticas": {
            "resumen": {
                "total_respuestas": int(summary.get("total_respuestas") or 0),
                "participantes_unicos": int(summary.get("participantes_unicos") or 0),
                "tasa_completitud": _metric_number(summary.get("tasa_completitud"), decimals=2),
                "projected_total": int(forecast.get("projected_total") or 0),
            },
            "categorias": _build_category_rankings(summary),
            "demografia": {
                "genero": demografia.get("genero") or [],
                "rango_etario": demografia.get("rango_etario") or [],
                "edad": demografia.get("edad") or {},
            },
            "canales": summary.get("canales") or [],
            "series": list(timeseries or []),
        },
        "ia": {
            "headline": brief.get("headline") or "",
            "insights": brief.get("insights") or [],
            "risk_level": brief.get("risk_level") or "medium",
            "ai_enhanced": bool(brief.get("ai_enhanced")),
            "survey_ai_insights": heatmap.get("ai_insights") or ((heatmap.get("metadata") or {}).get("ai_insights") or {}),
            "alerts": alerts.get("alerts") or [],
        },
    }


def _build_frontend_render_contract(
    *,
    heatmap: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
    latest_responses_state: str,
    fast_mode: bool,
    publication: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return explicit FE orchestration hints to keep charts/maps stable.

    This contract is consumed by the analytics frontend so it can prioritize
    chart engines, apply deterministic fallbacks and avoid mounting maps/charts
    when required datasets are absent.
    """

    map_config = ((heatmap.get("metadata") or {}).get("map_config") or {})
    preferred_provider = map_config.get("provider") or "maplibre"
    if preferred_provider == "none":
        preferred_provider = "maplibre"

    map_ready = bool(heatmap.get("points") or heatmap.get("cells"))
    timeseries_ready = bool(timeseries)
    publication_state = (publication or {}).get("public_state") or "unavailable"

    return {
        "version": "2026.04",
        "product_surface": NOETHER_ANALYTICS_MAPS_SURFACE,
        "hierarchy": {
            "chart_engines": ["echarts", "recharts", "plotly"],
            "map_engines": [preferred_provider, "deckgl", "maplibre", "google"],
        },
        "modules": {
            "timeseries": {
                "state": "ready" if timeseries_ready else "empty",
                "dataset_key": "modules.timeseries",
            },
            "heatmap": {
                "state": "ready" if map_ready else "empty",
                "dataset_key": "modules.heatmap.points",
                "fallback_dataset_key": "modules.heatmap.cells",
                "preferred_provider": preferred_provider,
                "fallback_provider": "maplibre",
                "ai_layers_dataset_key": "modules.heatmap.ai_layers.layers",
                "advanced_view": "interactive_globe_heatmap",
            },
            "latest_responses": {
                "state": latest_responses_state,
                "dataset_key": "modules.latest_responses",
            },
            "publication": {
                "state": publication_state,
                "dataset_key": "survey_publication",
                "links_dataset_key": "survey_publication.links",
            },
        },
        "render_strategy": "fast" if fast_mode else "full",
    }


def _build_latest_responses_preview(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None, *, limit: int = 10) -> List[Dict[str, Any]]:
    """Return a compact latest responses list to keep UI summary consistent."""

    encuesta = get_encuesta(encuesta_id)
    respuestas = _collect_respuestas(encuesta, filtros)
    ordered = sorted(
        respuestas,
        key=lambda item: item.submitted_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    preview: List[Dict[str, Any]] = []
    for respuesta in ordered[: max(1, min(int(limit or 10), 50))]:
        preview.append(
            {
                "id": respuesta.id,
                "submitted_at": (respuesta.submitted_at.astimezone(timezone.utc).isoformat() if respuesta.submitted_at else None),
                "canal": respuesta.canal,
                "genero": respuesta.genero,
                "rango_etario": respuesta.rango_etario,
                "lat": respuesta.lat,
                "lng": respuesta.lng,
            }
        )

    return preview


def _build_geo_rankings(points: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Aggregate heatmap points by geo dimensions for executive drilldowns."""

    counters: Dict[str, Counter] = {
        "barrio": Counter(),
        "ciudad": Counter(),
        "provincia": Counter(),
    }

    for point in points:
        if not isinstance(point, Mapping):
            continue
        for key in counters:
            value = point.get(key)
            if value is None:
                continue
            label = str(value).strip()
            if label:
                counters[key][label] += 1

    return {
        key: [{"label": label, "value": value} for label, value in counter.most_common(10)]
        for key, counter in counters.items()
    }


def _build_heatmap_territorial_aggregations(
    points: Sequence[Dict[str, Any]],
    *,
    total_respuestas: int,
    baseline_rate: float,
) -> Dict[str, List[Dict[str, Any]]]:
    """Aggregate territorial metrics for barrio/distrito(ciudad)/ciudad views."""

    dimensions = {
        "by_barrio": "barrio",
        "by_distrito": "ciudad",
        "by_ciudad": "ciudad",
    }
    payload: Dict[str, List[Dict[str, Any]]] = {}
    safe_total = max(total_respuestas, 1)
    safe_baseline = baseline_rate if baseline_rate > 0 else 1.0

    for output_key, attr_key in dimensions.items():
        grouped: Dict[str, Dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, Mapping):
                continue
            label = str(point.get(attr_key) or "").strip()
            if not label:
                continue
            item = grouped.setdefault(label, {"respuestas": 0, "lat_sum": 0.0, "lng_sum": 0.0})
            item["respuestas"] += 1
            item["lat_sum"] += float(point.get("lat") or 0.0)
            item["lng_sum"] += float(point.get("lng") or 0.0)

        rows: List[Dict[str, Any]] = []
        for label, item in sorted(grouped.items(), key=lambda x: x[1]["respuestas"], reverse=True)[:20]:
            respuestas = int(item["respuestas"])
            participacion = round((respuestas / safe_total) * 100, 2)
            tasa_crecimiento = round((respuestas / safe_baseline), 3)
            normalized_density = round(participacion / 100, 4)
            centroid_lat = round(item["lat_sum"] / respuestas, 6) if respuestas else None
            centroid_lng = round(item["lng_sum"] / respuestas, 6) if respuestas else None
            rows.append(
                {
                    "label": label,
                    "respuestas": respuestas,
                    "participacion": participacion,
                    "tasa_crecimiento": tasa_crecimiento,
                    "riesgo": "high" if participacion >= 30 else "medium" if participacion >= 15 else "low",
                    "normalized_density": normalized_density,
                    "centroid": {"lat": centroid_lat, "lng": centroid_lng},
                }
            )
        payload[output_key] = rows

    return payload


def _build_visual_module_contract(
    module_id: str,
    *,
    title: str,
    description: str,
    empty_state: str,
    units: str,
    decimals: int,
    sort: str,
    thresholds: Optional[Dict[str, Any]] = None,
    palette: Optional[List[str]] = None,
    min_width: int = 280,
    min_height: int = 220,
    aspect_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "id": module_id,
        "title": title,
        "description": description,
        "empty_state": empty_state,
        "units": units,
        "decimals": decimals,
        "sort": sort,
        "thresholds": thresholds or {},
        "palette": palette or ["#1D4ED8", "#2563EB", "#38BDF8"],
        "container": {
            "min_width": int(min_width),
            "min_height": int(min_height),
            "aspect_ratio": aspect_ratio,
        },
    }


def _build_category_rankings(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build category-like ranking from survey questions/options distribution."""

    category_counter: Counter = Counter()
    for pregunta in summary.get("preguntas") or []:
        for opcion in pregunta.get("opciones") or []:
            label = str(opcion.get("texto") or "").strip()
            value = int(opcion.get("conteo") or opcion.get("value") or 0)
            if label and value > 0:
                category_counter[label] += value

    return [{"label": label, "value": value} for label, value in category_counter.most_common(12)]


def _build_admin_decision_cards(
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
    geo_rankings: Dict[str, List[Dict[str, Any]]],
    category_rankings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return concise decision cards for municipality and business operators."""

    top_barrio = (geo_rankings.get("barrio") or [{}])[0]
    top_city = (geo_rankings.get("ciudad") or [{}])[0]
    top_category = (category_rankings or [{}])[0]
    active_alerts = len(alerts.get("alerts") or [])

    return [
        {
            "id": "territory_focus",
            "title": "Foco territorial",
            "priority": "high" if top_barrio.get("label") else "medium",
            "message": (
                f"{top_barrio.get('label')} concentra mayor actividad"
                if top_barrio.get("label")
                else "No hay suficientes datos geográficos para priorizar barrios"
            ),
            "evidence": {
                "barrio": top_barrio,
                "ciudad": top_city,
            },
        },
        {
            "id": "category_focus",
            "title": "Categoría con mayor demanda",
            "priority": "medium",
            "message": (
                f"{top_category.get('label')} lidera las respuestas"
                if top_category.get("label")
                else "No se detectó una categoría dominante"
            ),
            "evidence": {
                "category": top_category,
                "total_respuestas": int(summary.get("total_respuestas") or 0),
            },
        },
        {
            "id": "operational_pulse",
            "title": "Pulso operativo",
            "priority": "high" if active_alerts > 0 else "low",
            "message": (
                f"{active_alerts} alertas activas: revisar campañas y soporte"
                if active_alerts > 0
                else "Sin alertas críticas activas"
            ),
            "evidence": {
                "projected_total": int(forecast.get("projected_total") or 0),
                "alerts": active_alerts,
            },
        },
    ]


def _build_admin_analytics_template(
    summary: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
    heatmap: Dict[str, Any],
    forecast: Dict[str, Any],
    alerts: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a frontend-friendly advanced layout contract for survey analytics."""

    points = heatmap.get("points") or []
    geo_rankings = _build_geo_rankings(points)
    category_rankings = _build_category_rankings(summary)
    age_distribution = ((summary.get("demografia") or {}).get("rango_etario") or [])
    total_respuestas = int(summary.get("total_respuestas") or 0)
    territorial_aggregations = _build_heatmap_territorial_aggregations(
        points,
        total_respuestas=total_respuestas,
        baseline_rate=float(forecast.get("current_rate_per_minute") or 0.0),
    )

    return {
        "layout_version": "2026.04",
        "tabs": [
            {"id": "overview", "label": "Resumen ejecutivo", "default": True},
            {"id": "territory", "label": "Mapa territorial"},
            {"id": "categories", "label": "Categorías"},
            {"id": "demography", "label": "Demografía"},
            {"id": "ai_copilot", "label": "Copiloto IA"},
        ],
        "chart_stack": {
            "recommended": ["echarts", "plotly", "maplibre"],
            "notes": "Usar ECharts para KPIs/series y MapLibre para heatmaps por barrio, distrito y ciudad.",
        },
        "datasets": {
            "geo_rankings": geo_rankings,
            "category_rankings": category_rankings,
            "age_distribution": age_distribution,
            "activity_timeseries": list(timeseries),
            "heatmap_points": points,
            **territorial_aggregations,
        },
        "decision_cards": _build_admin_decision_cards(
            summary=summary,
            forecast=forecast,
            alerts=alerts,
            geo_rankings=geo_rankings,
            category_rankings=category_rankings,
        ),
        "ux_guardrails": {
            "chart_container": {
                "default_min_width": 280,
                "default_min_height": 220,
                "mobile_min_height": 240,
                "render_when_visible": True,
                "require_non_zero_parent_size": True,
            },
            "responsive": {
                "mobile_breakpoint_px": 768,
                "card_gap_mobile": 12,
                "stack_cards_on_mobile": True,
            },
            "maps": {
                "prefer_interactive_providers": ["maplibre", "google"],
                "fallback_to_static_geo_table": True,
            },
            "telemetry": {
                "event_endpoint_preferred": "/api/analytics/event",
                "event_endpoint_legacy": "/analytics/event",
                "requires_tenant": True,
                "fallback_event_name": "frontend_analytics_event",
            },
            "widget": {
                "config_endpoint_preferred": "/api/public/widget-config",
                "config_endpoint_legacy": "/public/widget-config",
                "retry_recommended": True,
            },
        },
        "visual_modules": [
            _build_visual_module_contract(
                "kpi_total",
                title="Participación total",
                description="Respuestas acumuladas en el período filtrado.",
                empty_state="Sin respuestas todavía.",
                units="count",
                decimals=0,
                sort="desc",
            ),
            _build_visual_module_contract(
                "heatmap_territory",
                title="Mapa territorial",
                description="Concentración por barrio, distrito y ciudad.",
                empty_state="No hay geodatos para el rango actual.",
                units="density",
                decimals=4,
                sort="desc",
                thresholds={"low": 0.1, "medium": 0.2, "high": 0.3},
            ),
            _build_visual_module_contract(
                "anomalies",
                title="Anomalías operativas",
                description="Detección de patrones de riesgo y manipulación.",
                empty_state="No se detectaron anomalías relevantes.",
                units="score",
                decimals=2,
                sort="desc",
                thresholds={"medium": 35, "high": 65, "critical": 80},
                palette=["#16A34A", "#F59E0B", "#EF4444", "#991B1B"],
            ),
        ],
    }




def _build_executive_kpis(
    summary: Dict[str, Any],
    forecast: Dict[str, Any],
    anomalies: Dict[str, Any],
    heatmap: Dict[str, Any],
    segment_compare: Dict[str, Any],
    timeseries: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Return sales-ready KPI objects with trend/status/explanation."""

    total = int(summary.get("total_respuestas") or 0)
    unique_participants = int(summary.get("participantes_unicos") or 0)
    points = heatmap.get("points") or []
    unique_geo = len({(round(float(item.get("lat") or 0), 3), round(float(item.get("lng") or 0), 3)) for item in points if isinstance(item, Mapping)})
    territorial = round((unique_geo / max(len(points), 1)) * 100, 2) if points else 0.0

    segment_a_total = int((((segment_compare.get("segment_a") or {}).get("stats") or {}).get("total_respuestas") or 0))
    segment_b_total = int((((segment_compare.get("segment_b") or {}).get("stats") or {}).get("total_respuestas") or 0))
    brecha_segmento = abs(segment_a_total - segment_b_total)

    risk_score = _metric_number(anomalies.get("risk_score"), decimals=2)
    confidence_index = round(max(0.0, 100.0 - risk_score), 2)
    avg_response_time = 0.0
    if len(timeseries) >= 2:
        avg_response_time = round(1440 / max((sum(item.get("total", 0) for item in timeseries) / len(timeseries)), 1), 2)

    trend_7d = _metric_number(forecast.get("current_rate_per_minute"), decimals=3)
    trend_30d = _metric_number((forecast.get("projected_additional") or 0) / max(forecast.get("horizon_minutes") or 1, 1), decimals=3)

    return {
        "participacion_total": {"value": total, "trend": trend_7d, "status": "good" if total > 0 else "neutral", "explanation": "Respuestas acumuladas en el período seleccionado."},
        "representatividad_territorial": {"value": territorial, "trend": 0.0, "status": "good" if territorial >= 40 else "watch", "explanation": "Cobertura geográfica en base a puntos únicos relevados."},
        "brecha_segmento_max": {"value": brecha_segmento, "trend": 0.0, "status": "good" if brecha_segmento <= max(unique_participants * 0.2, 5) else "watch", "explanation": "Diferencia absoluta de participación entre segmentos A/B."},
        "indice_confianza_datos": {"value": confidence_index, "trend": 0.0, "status": "good" if confidence_index >= 70 else "risk", "explanation": "Índice inverso al riesgo de anomalías detectadas."},
        "tiempo_respuesta_medio": {"value": avg_response_time, "trend": 0.0, "status": "good" if avg_response_time <= 60 else "watch", "explanation": "Minutos promedio estimados entre bloques de respuestas."},
        "tendencia_7d": {"value": trend_7d, "trend": trend_7d, "status": "good" if trend_7d >= 0.05 else "neutral", "explanation": "Tasa actual de participación por minuto (proxy 7d)."},
        "tendencia_30d": {"value": trend_30d, "trend": trend_30d, "status": "good" if trend_30d >= 0.05 else "neutral", "explanation": "Proyección media por minuto para horizonte extendido (proxy 30d)."},
    }


def _append_query(endpoint: Optional[str], params: Mapping[str, Any]) -> Optional[str]:
    if not endpoint:
        return endpoint
    clean_params = {key: value for key, value in (params or {}).items() if value not in (None, "")}
    if not clean_params:
        return endpoint
    separator = "&" if "?" in endpoint else "?"
    return endpoint + separator + "&".join(
        f"{quote_plus(str(key))}={quote_plus(str(value))}" for key, value in clean_params.items()
    )


def _build_survey_publication_contract(encuesta_id: int) -> Dict[str, Any]:
    base_payload: Dict[str, Any] = {
        "contract_version": "surveys.dashboard_publication.v1",
        "encuesta_id": encuesta_id,
        "public_state": "unavailable",
        "is_published": False,
        "has_public_link": False,
        "live_results_enabled": False,
        "links": {},
        "actions": [],
    }

    try:
        encuesta = get_encuesta(encuesta_id)
    except Exception:
        base_payload["reason_code"] = "survey_context_unavailable"
        return base_payload

    link = (
        EncLink.query.filter_by(encuesta_id=encuesta.id)
        .order_by(EncLink.id.asc())
        .first()
    )
    tenant_slug = None
    tenant_id = getattr(encuesta, "tenant_id", None)
    if tenant_id:
        try:
            tenant = db.session.get(TenantProfile, tenant_id)
            tenant_slug = getattr(tenant, "slug", None) if tenant else None
        except Exception:
            tenant_slug = None

    slug_publico = str(getattr(link, "slug_publico", "") or "").strip() if link else ""
    has_public_link = bool(slug_publico)
    is_published = str(getattr(encuesta, "estado", "") or "").lower() == "publicada" and has_public_link
    live_results_enabled = bool(getattr(encuesta, "mostrar_resultados_envivo", False))
    public_state = "published" if is_published else "closed" if getattr(encuesta, "estado", None) == "cerrada" else "draft"

    links: Dict[str, Any] = {}
    if has_public_link:
        tenant_query = {"tenant_slug": tenant_slug}
        public_page_path = f"/e/{slug_publico}"
        public_api_endpoint = _append_query(f"/api/v2/public/surveys/{slug_publico}", tenant_query)
        respond_endpoint = _append_query(f"/api/v2/public/surveys/{slug_publico}/respond", tenant_query)
        live_results_endpoint = _append_query(f"/api/v2/public/surveys/{slug_publico}/live-results", tenant_query)
        legacy_public_api_endpoint = f"/api/public/encuestas/v1/{slug_publico}"
        legacy_live_results_endpoint = f"/api/public/encuestas/v1/{slug_publico}/live-results"
        qr_endpoint = f"/api/public/encuestas/v1/{slug_publico}/qr?size=320"
        share_text = f"Participa en {getattr(encuesta, 'titulo', None) or 'esta encuesta'}: {public_page_path}"
        links = {
            "public_page_path": public_page_path,
            "public_url": public_page_path,
            "share_url": public_page_path,
            "copy_url": public_page_path,
            "copy_text": share_text,
            "public_api_endpoint": public_api_endpoint,
            "respond_endpoint": respond_endpoint,
            "live_results_endpoint": live_results_endpoint,
            "results_endpoint": live_results_endpoint,
            "legacy_public_api_endpoint": legacy_public_api_endpoint,
            "legacy_live_results_endpoint": legacy_live_results_endpoint,
            "qr_endpoint": qr_endpoint,
            "qr_image_url": qr_endpoint,
            "whatsapp_share_url": f"https://wa.me/?text={quote_plus(share_text)}",
        }

    actions: List[Dict[str, Any]] = []
    if has_public_link:
        actions.extend(
            [
                {"id": "copy_public_link", "label": "Copiar link", "ui_hint": "copy", "href": links.get("copy_url")},
                {"id": "open_public_survey", "label": "Abrir encuesta", "ui_hint": "open", "href": links.get("public_page_path")},
                {"id": "download_qr", "label": "QR", "ui_hint": "qr", "href": links.get("qr_endpoint")},
            ]
        )
        actions.append(
            {
                "id": "open_live_results" if live_results_enabled else "enable_live_results",
                "label": "Resultados en vivo" if live_results_enabled else "Activar resultados",
                "ui_hint": "live_results" if live_results_enabled else "settings",
                "href": links.get("live_results_endpoint") if live_results_enabled else None,
                "enabled": live_results_enabled,
            }
        )
    else:
        actions.append({"id": "publish_survey", "label": "Publicar encuesta", "ui_hint": "publish"})

    return {
        **base_payload,
        "encuesta_id": encuesta.id,
        "tenant_id": tenant_id,
        "tenant_slug": tenant_slug,
        "slug_publico": slug_publico or None,
        "canonical_slug": slug_publico or None,
        "estado": getattr(encuesta, "estado", None),
        "public_state": public_state,
        "is_published": is_published,
        "has_public_link": has_public_link,
        "is_live_vote": bool(getattr(encuesta, "es_votacion_envivo", False)),
        "live_results_enabled": live_results_enabled,
        "requires_identity": bool(getattr(encuesta, "requiere_identidad", False)),
        "anonymous_allowed": bool(getattr(encuesta, "anonimo_permitido", True)),
        "links": links,
        "actions": actions,
    }


def get_dashboard_bundle(
    encuesta_id: int,
    filtros: Optional[Dict[str, Any]] = None,
    *,
    granularity: str = "day",
    fast_mode: bool = False,
) -> Dict[str, Any]:
    """Return one request-scoped analytics snapshot and its derived modules."""

    with _analytics_snapshot_scope():
        return _get_dashboard_bundle_impl(
            encuesta_id,
            filtros,
            granularity=granularity,
            fast_mode=fast_mode,
        )


def _get_dashboard_bundle_impl(
    encuesta_id: int,
    filtros: Optional[Dict[str, Any]] = None,
    *,
    granularity: str = "day",
    fast_mode: bool = False,
) -> Dict[str, Any]:
    """Return a complete analytics payload optimized for executive dashboards."""

    summary = get_summary(encuesta_id, filtros)
    timeseries = get_timeseries(encuesta_id, granularity, filtros)
    heatmap = get_heatmap(encuesta_id, filtros)
    forecast = get_forecast(encuesta_id, filtros=filtros)
    alerts = get_alerts(encuesta_id, filtros=filtros)
    brief = get_executive_brief(encuesta_id, filtros)
    anomalies = {
        "encuesta_id": encuesta_id,
        "risk_score": 0.0,
        "risk_level": "low",
        "severity": "low",
        "signals": {},
        "top_anomalies": [],
    }
    segment_compare_default = {
        "segment_a": {"stats": {"total_respuestas": 0}},
        "segment_b": {"stats": {"total_respuestas": 0}},
    }
    if not fast_mode:
        anomalies = get_anomaly_report(encuesta_id, filtros=filtros)
        segment_compare_default = get_segment_compare(
            encuesta_id,
            filtros=filtros,
            segment_a={"canal": "web"},
            segment_b={"canal": "whatsapp"},
        )

    executive_summary = _build_executive_summary_text(summary, forecast, alerts, heatmap)
    visual_blueprint = _build_visual_blueprint(
        encuesta_id=encuesta_id,
        summary=summary,
        timeseries=timeseries,
        heatmap=heatmap,
    )
    admin_template = _build_admin_analytics_template(
        summary=summary,
        timeseries=timeseries,
        heatmap=heatmap,
        forecast=forecast,
        alerts=alerts,
    )

    latest_responses: List[Dict[str, Any]] = []
    latest_responses_error: Optional[str] = None
    latest_responses_state = "empty"
    if fast_mode:
        latest_responses_state = "deferred"
    else:
        try:
            latest_responses = _build_latest_responses_preview(encuesta_id, filtros=filtros, limit=10)
            latest_responses_state = "ready" if len(latest_responses) > 0 else "empty"
        except Exception as exc:  # pragma: no cover - defensive guard for dashboard stability
            logger.exception("[encuestas.analytics] latest responses preview failed encuesta_id=%s", encuesta_id)
            latest_responses_error = str(exc)
            latest_responses_state = "degraded"

    cards = _build_dashboard_cards(summary, forecast, anomalies)
    ui_state = _build_dashboard_ui_state(summary, heatmap, alerts, latest_responses_state=latest_responses_state)
    ui_state["render_strategy"] = "fast" if fast_mode else "full"
    active_alerts = int(len(alerts.get("alerts") or []))
    executive_kpis = _build_executive_kpis(
        summary=summary,
        forecast=forecast,
        anomalies=anomalies,
        heatmap=heatmap,
        segment_compare=segment_compare_default,
        timeseries=timeseries,
    )
    sections = _build_dashboard_sections(
        summary=summary,
        heatmap=heatmap,
        timeseries=timeseries,
        forecast=forecast,
        alerts=alerts,
        brief=brief,
    )
    survey_publication = _build_survey_publication_contract(encuesta_id)
    frontend_render_contract = _build_frontend_render_contract(
        heatmap=heatmap,
        timeseries=timeseries,
        latest_responses_state=latest_responses_state,
        fast_mode=fast_mode,
        publication=survey_publication,
    )

    return {
        "encuesta_id": encuesta_id,
        "data_provenance": summary.get("data_provenance"),
        "survey_publication": survey_publication,
        "public_links": survey_publication.get("links") or {},
        "executive_summary": executive_summary,
        "brief": brief,
        "kpis": {
            "total_respuestas": int(summary.get("total_respuestas") or 0),
            "participantes_unicos": int(summary.get("participantes_unicos") or 0),
            "tasa_completitud": _metric_number(summary.get("tasa_completitud"), decimals=2),
            "projected_total": int(forecast.get("projected_total") or 0),
            "risk_score": _metric_number(anomalies.get("risk_score"), decimals=3),
            "active_alerts": active_alerts,
        },
        "cards": cards,
        "kpis_executive": executive_kpis,
        "ui_state": ui_state,
        "meta": {
            "schema_version": "2026.03",
            "filters": dict(filtros or {}),
            "fast_mode": fast_mode,
            "sampling": dict(summary.get("data_provenance") or {}),
            "module_state": {
                "summary": "ready" if int(summary.get("total_respuestas") or 0) > 0 else "empty",
                "timeseries": "ready" if len(timeseries or []) > 0 else "empty",
                "heatmap": "ready" if bool((heatmap.get("points") or []) or (heatmap.get("cells") or [])) else "empty",
                "alerts": "attention" if active_alerts > 0 else "normal",
                "latest_responses": latest_responses_state,
                "publication": survey_publication.get("public_state") or "unavailable",
            },
        },
        "modules": {
            "summary": summary,
            "timeseries": timeseries,
            "heatmap": heatmap,
            "forecast": forecast,
            "alerts": alerts,
            "anomalies": anomalies,
            "publication": survey_publication,
            "latest_responses": latest_responses,
            "latest_responses_meta": {
                "state": latest_responses_state,
                "error": latest_responses_error,
            },
        },
        "visual_blueprint": visual_blueprint,
        "admin_template": admin_template,
        "frontend_render_contract": frontend_render_contract,
        "sections": sections,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_segment_compare(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    segment_a: Optional[Dict[str, Any]] = None,
    segment_b: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    encuesta = get_encuesta(encuesta_id)
    respuestas, data_provenance = _collect_respuestas_with_provenance(
        encuesta,
        filtros,
    )

    group_a = [respuesta for respuesta in respuestas if _matches_segment(respuesta, segment_a)]
    group_b = [respuesta for respuesta in respuestas if _matches_segment(respuesta, segment_b)]

    total_base = max(len(respuestas), 1)

    def _segment_meta(name: str, filters_payload: Optional[Dict[str, Any]], group: Sequence[EncRespuesta]) -> Dict[str, Any]:
        count = len(group)
        return {
            "name": name,
            "label": f"Segmento {name.upper()}",
            "filters": filters_payload or {},
            "count": count,
            "coverage": round((count / total_base) * 100, 2),
        }

    return {
        "encuesta_id": encuesta.id,
        "data_provenance": data_provenance,
        "segment_a": {
            "meta": _segment_meta("a", segment_a, group_a),
            "filters": segment_a or {},
            "stats": _segment_distribution(group_a, encuesta),
        },
        "segment_b": {
            "meta": _segment_meta("b", segment_b, group_b),
            "filters": segment_b or {},
            "stats": _segment_distribution(group_b, encuesta),
        },
        "comparison_meta": {
            "base_total": len(respuestas),
            "gap_respuestas": len(group_a) - len(group_b),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_anomaly_report(
    encuesta_id: int,
    *,
    filtros: Optional[Dict[str, Any]] = None,
    burst_window_minutes: int = 5,
    burst_threshold: int = 10,
) -> Dict[str, Any]:
    encuesta = get_encuesta(encuesta_id)
    snapshot = _get_response_snapshot(encuesta, filtros)
    selected_query = snapshot["selected_query"]
    window = max(1, min(int(burst_window_minutes or 5), 30))
    threshold = max(3, int(burst_threshold or 10))
    now = _utc_now()
    burst_count, _previous_burst, _last_hour = _exact_recent_windows(
        snapshot,
        now=now,
        window_minutes=window,
    )

    ip_count_expr = db.func.count(EncRespuesta.id)
    suspicious_ips = [
        {"ip": str(ip), "count": int(count or 0)}
        for ip, count in (
            selected_query.with_entities(
                EncRespuesta.ip,
                ip_count_expr.label("total"),
            )
            .filter(EncRespuesta.ip.isnot(None))
            .group_by(EncRespuesta.ip)
            .having(ip_count_expr >= 3)
            .order_by(ip_count_expr.desc())
            .limit(5)
            .all()
        )
    ]
    fingerprint_count_expr = db.func.count(EncRespuesta.id)
    repeated_fingerprints = [
        {"fingerprint": str(fingerprint), "count": int(count or 0)}
        for fingerprint, count in (
            selected_query.with_entities(
                EncRespuesta.huella_unica,
                fingerprint_count_expr.label("total"),
            )
            .filter(EncRespuesta.huella_unica.isnot(None))
            .group_by(EncRespuesta.huella_unica)
            .having(fingerprint_count_expr >= 2)
            .order_by(fingerprint_count_expr.desc())
            .limit(5)
            .all()
        )
    ]
    lat_cell = db.func.round(cast(EncRespuesta.lat, Numeric), 3)
    lng_cell = db.func.round(cast(EncRespuesta.lng, Numeric), 3)
    geo_count_expr = db.func.count(EncRespuesta.id)
    concentrated_geo = [
        {"lat": float(lat), "lng": float(lng), "count": int(count or 0)}
        for lat, lng, count in (
            selected_query.with_entities(
                lat_cell.label("lat"),
                lng_cell.label("lng"),
                geo_count_expr.label("total"),
            )
            .filter(
                EncRespuesta.lat.isnot(None),
                EncRespuesta.lng.isnot(None),
                EncRespuesta.lat.between(-90, 90),
                EncRespuesta.lng.between(-180, 180),
            )
            .group_by(lat_cell, lng_cell)
            .having(geo_count_expr >= 3)
            .order_by(geo_count_expr.desc())
            .limit(5)
            .all()
        )
    ]

    score = 0
    score += min(len(suspicious_ips) * 12, 36)
    score += min(len(repeated_fingerprints) * 15, 30)
    score += min(len(concentrated_geo) * 10, 20)
    if burst_count >= threshold:
        score += 20
    score = min(score, 100)

    risk_level = "bajo"
    if score >= 65:
        risk_level = "alto"
    elif score >= 35:
        risk_level = "medio"

    def _severity_from_score(value: int) -> str:
        if value >= 80:
            return "critical"
        if value >= 60:
            return "high"
        if value >= 35:
            return "medium"
        return "low"

    def _confidence_from_count(count: int) -> str:
        if count >= 10:
            return "high"
        if count >= 4:
            return "medium"
        return "low"

    anomaly_signals: List[Dict[str, Any]] = []
    for item in suspicious_ips:
        anomaly_signals.append(
            {
                "type": "suspicious_ip",
                "detail": f"IP con repetición inusual ({item['count']} respuestas)",
                "score": min(100, item["count"] * 10),
                "why_it_matters": "Puede indicar automatización o manipulación de votos.",
                "recommended_action": "Aplicar verificación adicional (captcha/validación humana).",
                "affected_segment": {"ip": item["ip"]},
                "confidence": _confidence_from_count(item["count"]),
                "severity": _severity_from_score(min(100, item["count"] * 10)),
                "timestamp": now.isoformat(),
            }
        )
    for item in repeated_fingerprints:
        anomaly_signals.append(
            {
                "type": "repeated_fingerprint",
                "detail": f"Huella repetida ({item['count']} veces)",
                "score": min(100, item["count"] * 12),
                "why_it_matters": "Puede reflejar cuentas duplicadas o abuso desde mismo dispositivo.",
                "recommended_action": "Revisar reglas de unicidad y limitar múltiples envíos.",
                "affected_segment": {"fingerprint": item["fingerprint"]},
                "confidence": _confidence_from_count(item["count"]),
                "severity": _severity_from_score(min(100, item["count"] * 12)),
                "timestamp": now.isoformat(),
            }
        )
    for item in concentrated_geo:
        anomaly_signals.append(
            {
                "type": "geo_concentration",
                "detail": f"Concentración geográfica alta ({item['count']} respuestas en un punto)",
                "score": min(100, item["count"] * 8),
                "why_it_matters": "Concentraciones extremas sesgan representatividad territorial.",
                "recommended_action": "Comparar con histórico y abrir revisión territorial.",
                "affected_segment": {"lat": item["lat"], "lng": item["lng"]},
                "confidence": _confidence_from_count(item["count"]),
                "severity": _severity_from_score(min(100, item["count"] * 8)),
                "timestamp": now.isoformat(),
            }
        )
    if burst_count >= threshold:
        anomaly_signals.append(
            {
                "type": "burst_activity",
                "detail": f"Pico abrupto de actividad ({burst_count} respuestas en {window} min)",
                "score": min(100, burst_count * 3),
                "why_it_matters": "Picos repentinos pueden requerir moderación y capacidad operativa.",
                "recommended_action": "Escalar monitoreo en tiempo real y revisar fuentes de tráfico.",
                "affected_segment": {"window_minutes": window},
                "confidence": _confidence_from_count(burst_count),
                "severity": _severity_from_score(min(100, burst_count * 3)),
                "timestamp": now.isoformat(),
            }
        )

    anomaly_signals.sort(key=lambda signal: signal.get("score", 0), reverse=True)

    return {
        "encuesta_id": encuesta.id,
        "data_provenance": dict(snapshot["provenance"]),
        "advisory_policy": dict(SURVEY_AI_ADVISORY_POLICY),
        "risk_score": score,
        "risk_level": risk_level,
        "severity": _severity_from_score(score),
        "burst_window_minutes": window,
        "burst_threshold": threshold,
        "thresholds": {
            "burst_count": threshold,
            "risk_score_medium": 35,
            "risk_score_high": 65,
            "risk_score_critical": 80,
        },
        "state_mutation": {
            "requested": False,
            "applied": False,
            "reason": "survey_anomaly_detection_is_advisory_only",
        },
        "burst_count": burst_count,
        "signals": {
            "suspicious_ips": suspicious_ips,
            "repeated_fingerprints": repeated_fingerprints,
            "concentrated_geo": concentrated_geo,
        },
        "top_anomalies": anomaly_signals[:10],
        "updated_at": now.isoformat(),
    }

def get_heatmap(
    encuesta_id: int,
    filtros: Optional[Dict[str, Any]] = None,
    *,
    resolution: Optional[int] = None,
) -> Dict[str, Any]:
    encuesta = get_encuesta(encuesta_id)
    survey_tenant_id = getattr(encuesta, "tenant_id", None)
    tenant = (
        db.session.get(TenantProfile, int(survey_tenant_id))
        if survey_tenant_id is not None
        else None
    )
    government_evidence_required = tenant_requires_government_survey_evidence(
        tenant
    )
    jurisdiction = (
        resolve_tenant_jurisdiction(tenant)
        if government_evidence_required and tenant is not None
        else {}
    )
    authority = (
        jurisdiction.get("boundary_authority")
        if isinstance(jurisdiction.get("boundary_authority"), Mapping)
        else {}
    )
    jurisdiction_verified = bool(
        government_evidence_required
        and jurisdiction.get("enforced") is True
        and jurisdiction.get("containment_verified") is True
        and jurisdiction.get("containment_method") == "point_in_polygon"
        and authority.get("kind") == "official"
        and authority.get("source_ref")
        and authority.get("snapshot_sha256")
    )
    snapshot = _get_response_snapshot(encuesta, filtros)
    respuestas = list(snapshot["sample"])
    data_provenance = dict(snapshot["provenance"])
    points, point_sampling = _bounded_geo_points(
        snapshot,
        limit=_analytics_bounded_int(
            os.environ.get("SURVEY_ANALYTICS_GEO_POINT_LIMIT"),
            default=2000,
            minimum=0,
            maximum=5000,
        ),
    )
    cells, cell_sampling = _exact_geo_cells(
        snapshot,
        max_cells=_analytics_bounded_int(
            os.environ.get("SURVEY_ANALYTICS_GEO_CELL_LIMIT"),
            default=1000,
            minimum=1,
            maximum=5000,
        ),
        minimum_count=1,
        precision=max(2, min(int(resolution or DEFAULT_HEATMAP_RESOLUTION) - 5, 5)),
    )
    allow_synthetic = bool(_as_bool((filtros or {}).get("allow_synthetic_geo") or (filtros or {}).get("include_synthetic_geo")))
    used_synthetic_points = False
    if (
        allow_synthetic
        and not government_evidence_required
        and not points
        and respuestas
    ):
        synthetic_points = _build_synthetic_heatmap_points(encuesta, respuestas)
        if synthetic_points:
            points = synthetic_points
            used_synthetic_points = True
    jurisdiction_input_points = len(points)
    jurisdiction_excluded_points = 0
    if government_evidence_required:
        authorized_points: List[Dict[str, Any]] = []
        for point in points:
            coordinate_status = coordinate_jurisdiction_status(
                point.get("lat"), point.get("lng"), jurisdiction
            )
            if not jurisdiction_verified or coordinate_status != "within":
                jurisdiction_excluded_points += 1
                continue
            source_ref = authority.get("source_ref")
            snapshot_sha256 = authority.get("snapshot_sha256")
            authorized_points.append(
                {
                    **point,
                    "coordinate_jurisdiction_status": coordinate_status,
                    "containment_verified": True,
                    "source_ref": source_ref,
                    "snapshot_sha256": snapshot_sha256,
                    "jurisdiction_evidence": {
                        "contract_version": (
                            "surveys.heatmap.point_jurisdiction_evidence.v1"
                        ),
                        "containment_verified": True,
                        "coordinate_jurisdiction_status": coordinate_status,
                        "containment_method": "point_in_polygon",
                        "authority_kind": "official",
                        "source_ref": source_ref,
                        "snapshot_sha256": snapshot_sha256,
                    },
                }
            )
        points = authorized_points
        enrich_heatmap_points(
            points,
            property_keys=(
                "barrio",
                "ciudad",
                "provincia",
                "pais",
                "canal",
                "containment_verified",
                "coordinate_jurisdiction_status",
                "source_ref",
                "snapshot_sha256",
                "jurisdiction_evidence",
            ),
        )
        # Exact SQL cells can mix accepted and rejected coordinates. Rebuild
        # cells solely from authorized points so no rejected observation can
        # influence a centroid or count.
        authorized_cells: Dict[str, Dict[str, Any]] = {}
        effective_resolution = resolution or DEFAULT_HEATMAP_RESOLUTION
        for point in points:
            cell_id = compute_heatmap_cell_id(
                float(point["lat"]), float(point["lng"]), effective_resolution
            )
            cell = authorized_cells.setdefault(
                cell_id,
                {
                    "cell_id": cell_id,
                    "count": 0,
                    "lat_sum": 0.0,
                    "lng_sum": 0.0,
                    "barrios": defaultdict(int),
                    "canales": defaultdict(int),
                },
            )
            cell["count"] += 1
            cell["lat_sum"] += float(point["lat"])
            cell["lng_sum"] += float(point["lng"])
            if point.get("barrio"):
                cell["barrios"][point["barrio"]] += 1
            if point.get("canal"):
                cell["canales"][point["canal"]] += 1
        cells = []
        for cell in authorized_cells.values():
            centroid_lat, centroid_lng = compute_heatmap_centroid(
                cell["cell_id"],
                lat_sum=cell["lat_sum"],
                lng_sum=cell["lng_sum"],
                count=cell["count"],
            )
            cells.append(
                {
                    "cell_id": cell["cell_id"],
                    "count": cell["count"],
                    "centroid_lat": round(centroid_lat, 6),
                    "centroid_lon": round(centroid_lng, 6),
                    "barrios": dict(cell["barrios"]),
                    "canales": dict(cell["canales"]),
                }
            )
        cells.sort(key=lambda item: item["count"], reverse=True)
        enrich_heatmap_cells(cells, property_keys=("barrios", "canales"))
        cell_sampling = {
            "cell_count": len(cells),
            "partial": bool(point_sampling.get("partial")),
            "source": "authorized_contained_points",
            "excluded_by_jurisdiction": jurisdiction_excluded_points,
        }
    metadata = _build_heatmap_metadata(encuesta, points)
    map_filter = _build_map_filter(points)
    metadata.update(
        {
            "resolution": resolution or DEFAULT_HEATMAP_RESOLUTION,
            "unique_cells": len(cells),
            "has_coordinates": bool(points),
            "using_synthetic_points": used_synthetic_points,
            "can_render_heatmap": bool(points or cells),
            "empty_reason": (
                "official_jurisdiction_boundary_unavailable"
                if government_evidence_required and not jurisdiction_verified
                else (
                    None
                    if points or cells
                    else (
                        "no_contained_geo_points"
                        if government_evidence_required
                        else "no_real_geo_points"
                    )
                )
            ),
            "point_sampling": point_sampling,
            "cell_aggregation": cell_sampling,
            "jurisdiction": {
                "contract_version": "surveys.heatmap.jurisdiction.v1",
                "required": government_evidence_required,
                "enforced": government_evidence_required,
                "state": (
                    "verified"
                    if jurisdiction_verified
                    else (
                        "blocked"
                        if government_evidence_required
                        else "not_required"
                    )
                ),
                "containment_verified": jurisdiction_verified,
                "containment_method": (
                    jurisdiction.get("containment_method")
                    if jurisdiction_verified
                    else None
                ),
                "boundary_authority": dict(authority),
                "reason_code": (
                    "official_point_in_polygon_verified"
                    if jurisdiction_verified
                    else (
                        "official_jurisdiction_boundary_unavailable"
                        if government_evidence_required
                        else "government_jurisdiction_not_required"
                    )
                ),
            },
            "provenance": {
                "contract_version": "surveys.heatmap.territorial_provenance.v1",
                "coordinate_policy": "persisted_coordinates_only",
                "writes_performed": False,
                "synthetic_coordinates_allowed": not government_evidence_required,
                "source_ref": authority.get("source_ref") if jurisdiction_verified else None,
                "snapshot_sha256": (
                    authority.get("snapshot_sha256")
                    if jurisdiction_verified
                    else None
                ),
                "input_points": jurisdiction_input_points,
                "authorized_points": len(points),
                "excluded_points": jurisdiction_excluded_points,
            },
        }
    )
    data_provenance.update(
        {
            "geo_points": point_sampling,
            "geo_cells": cell_sampling,
        }
    )
    points_geojson = build_feature_collection(points)
    cells_geojson = build_feature_collection(cells)
    if points_geojson:
        metadata["points_geojson"] = points_geojson
    if cells_geojson:
        metadata["cells_geojson"] = cells_geojson

    map_config = get_map_config()
    if map_config:
        metadata["map_config"] = map_config
    supported_formats = ["points"]
    preferred_format = "points"
    if points_geojson:
        supported_formats.append("geojson")
        preferred_format = "geojson"
    provider_hint = map_config.get("provider") if isinstance(map_config, dict) else None
    if not provider_hint or provider_hint == "none":
        provider_hint = "maplibre"
    map_render_ready = bool(
        (points or cells)
        and (
            not government_evidence_required
            or jurisdiction_verified
        )
    )
    metadata["map"] = {
        "contract_version": "surveys.heatmap.map_render.v1",
        "render_ready": map_render_ready,
        "available": bool(points or cells),
        "provider_hint": provider_hint,
        "fallback_provider": "maplibre",
        "reason_code": (
            "authorized_points_available"
            if map_render_ready
            else (
                "official_jurisdiction_boundary_unavailable"
                if government_evidence_required and not jurisdiction_verified
                else (
                    "no_contained_geo_points"
                    if government_evidence_required
                    else "no_real_geo_points"
                )
            )
        ),
    }
    heatmap_layer = {
        "kind": "heatmap",
        "supported_formats": supported_formats,
        "preferred_format": preferred_format,
        "provider_hint": provider_hint,
        "supports_filters": bool(map_filter["keys"]),
        "filter_keys": map_filter["keys"],
        "source_keys": {"points": "points", "geojson": "points_geojson"},
    }
    metadata["heatmap_layer"] = heatmap_layer
    metadata["map_layers"] = {"heatmap": heatmap_layer}
    metadata["category_layers"] = _build_category_heatmap_layers(points)
    metadata["map_filter"] = map_filter
    category_layers = metadata["category_layers"]
    ai_insights = build_collection_ai_insights(
        _build_survey_ai_items(encuesta, points, cells),
        domain="surveys",
    )
    ai_layers = build_map_ai_layers(
        [{**point, "source": "survey"} for point in points if isinstance(point, Mapping)],
        category_layers=category_layers,
        insights=ai_insights,
    )
    map_experience = {
        "contract_version": "encuestas.map_experience.v1",
        "preferred_visualization": "interactive_globe_heatmap",
        "map_engines": ["maplibre", "deckgl", "google"],
        "layer_groups": ["heatmap", "category_layers", "ai_risk_layers", "survey_participation"],
        "supports_reduced_motion": True,
    }
    metadata["ai_insights"] = ai_insights
    metadata["ai_layers"] = ai_layers
    metadata["ai_policy"] = dict(SURVEY_AI_ADVISORY_POLICY)
    metadata["map_experience"] = map_experience
    metadata["map_layers"]["ai_risk"] = {
        "kind": "ai_risk",
        "source_keys": {"points": "metadata.ai_layers.layers.risk_pulses.points"},
        "provider_hint": provider_hint,
    }
    metadata["map_layers"]["survey_participation"] = {
        "kind": "survey_participation",
        "source_keys": {"points": "metadata.ai_layers.layers.survey_participation.points"},
        "provider_hint": provider_hint,
    }
    has_map_data = bool(points or cells or ((category_layers.get("source") or {}).get("features") or []))
    if has_map_data:
        headline = f"Mapa de respuestas con {len(points)} puntos y {len(cells)} celdas disponibles."
        empty_state = None
        recommended_action = {"label": "Analizar mapa", "route": f"/admin/encuestas/{encuesta_id}/analytics/dashboard"}
    else:
        headline = "Sin puntos geograficos reales para esta encuesta."
        empty_state = "Sin puntos geograficos publicados para los filtros actuales."
        recommended_action = {"label": "Cambiar filtros", "route": f"/admin/encuestas/{encuesta_id}/analytics/heatmap"}
    legend = category_layers.get("legend") or {"mode": "category_weight", "min_weight": 0, "max_weight": 0}
    government_boundary_blocked = bool(
        government_evidence_required and not jurisdiction_verified
    )
    render_contract = {
        "module": "heatmap",
        "state": (
            "blocked"
            if government_boundary_blocked
            else ("ready" if bool(points or cells) else "empty")
        ),
        "dataset_key": "points",
        "fallback_dataset_key": "cells",
        "source_keys": ["points", "cells", "metadata.map_layers.heatmap"],
        "chart_hierarchy": ["echarts", "recharts", "plotly"],
        "map_hierarchy": [provider_hint, "maplibre", "google"],
        "can_render_heatmap": bool(points or cells),
        "empty_reason": (
            "official_jurisdiction_boundary_unavailable"
            if government_boundary_blocked
            else (
                None
                if points or cells
                else (
                    "no_contained_geo_points"
                    if government_evidence_required
                    else "no_real_geo_points"
                )
            )
        ),
        "ai_layers": True,
        "recommended_views": [
            "interactive_heatmap",
            "category_layers",
            "ai_risk_layers",
            "survey_participation",
            "interactive_globe",
        ],
    }
    return {
        "data_provenance": data_provenance,
        "points": points,
        "cells": cells,
        "headline": headline,
        "legend": legend,
        "empty_state": empty_state,
        "recommended_action": recommended_action,
        "ai_insights": ai_insights,
        "ai_layers": ai_layers,
        "ai_policy": dict(SURVEY_AI_ADVISORY_POLICY),
        "map_experience": map_experience,
        "metadata": metadata,
        "render_contract": render_contract,
    }


def _mask_ip(ip: Optional[str]) -> str:
    if not ip:
        return ""
    if ":" in ip:  # IPv6
        parts = ip.split(":")
        if len(parts) > 4:
            parts = parts[:4] + ["****"]
        return ":".join(parts)
    parts = ip.split(".")
    if len(parts) == 4:
        parts[-1] = "***"
        return ".".join(parts)
    return ip


def export_csv(encuesta_id: int, filtros: Optional[Dict[str, Any]] = None) -> Iterable[str]:
    encuesta = get_encuesta(encuesta_id)
    _base_query, selected_query, _mode = _response_queries(encuesta, filtros)
    respuestas = (
        selected_query.options(
            selectinload(EncRespuesta.detalles).joinedload(
                EncRespuestaDetalle.opcion
            )
        )
        .order_by(EncRespuesta.id.asc())
        .yield_per(250)
    )

    preguntas = encuesta.preguntas
    fieldnames = [
        "respuesta_id",
        "submitted_at",
        "canal",
        "utm_source",
        "utm_campaign",
        "genero",
        "rango_etario",
        "edad",
        "anio_nacimiento",
        "barrio",
        "ciudad",
        "provincia",
        "pais",
        "ip",
        "lat",
        "lng",
    ]
    for pregunta in preguntas:
        fieldnames.append(f"pregunta_{pregunta.id}")

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    for respuesta in respuestas:
        row = {
            "respuesta_id": respuesta.id,
            "submitted_at": respuesta.submitted_at.isoformat() if respuesta.submitted_at else "",
            "canal": respuesta.canal,
            "utm_source": respuesta.utm_source,
            "utm_campaign": respuesta.utm_campaign,
            "genero": respuesta.genero,
            "rango_etario": respuesta.rango_etario,
            "edad": respuesta.edad,
            "anio_nacimiento": respuesta.anio_nacimiento,
            "barrio": respuesta.barrio,
            "ciudad": respuesta.ciudad,
            "provincia": respuesta.provincia,
            "pais": respuesta.pais,
            "ip": _mask_ip(respuesta.ip),
            "lat": respuesta.lat,
            "lng": respuesta.lng,
        }
        detalles_por_pregunta = defaultdict(list)
        for detalle in respuesta.detalles:
            if detalle.opcion:
                detalles_por_pregunta[detalle.pregunta_id].append(detalle.opcion.texto)
            if detalle.texto_libre:
                detalles_por_pregunta[detalle.pregunta_id].append(detalle.texto_libre)
        for pregunta in preguntas:
            value = " | ".join(detalles_por_pregunta.get(pregunta.id, []))
            row[f"pregunta_{pregunta.id}"] = value
        writer.writerow(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

def calculate_live_results(
    slug_publico: str,
    *,
    preferred_tenant_id: Optional[int] = None,
    require_tenant_match: bool = False,
    allow_closed_for_read: bool = False,
    include_heatmap: bool = True,
    max_points: int = 2000,
    max_cells: int = 200,
    momentum_window_minutes: int = 10,
    filtros: Optional[Dict[str, Any]] = None,
    geo_privacy: Optional[str] = "public_aggregated",
) -> Dict[str, Any]:
    with _analytics_snapshot_scope():
        return _calculate_live_results_impl(
            slug_publico,
            preferred_tenant_id=preferred_tenant_id,
            require_tenant_match=require_tenant_match,
            allow_closed_for_read=allow_closed_for_read,
            include_heatmap=include_heatmap,
            max_points=max_points,
            max_cells=max_cells,
            momentum_window_minutes=momentum_window_minutes,
            filtros=filtros,
            geo_privacy=geo_privacy,
        )


def _calculate_live_results_impl(
    slug_publico: str,
    *,
    preferred_tenant_id: Optional[int] = None,
    require_tenant_match: bool = False,
    allow_closed_for_read: bool = False,
    include_heatmap: bool = True,
    max_points: int = 2000,
    max_cells: int = 200,
    momentum_window_minutes: int = 10,
    filtros: Optional[Dict[str, Any]] = None,
    geo_privacy: Optional[str] = "public_aggregated",
) -> Dict[str, Any]:
    """
    Returns simplified aggregate counts for live voting animations.
    Optimized for frequent polling.
    """
    encuesta = get_public_encuesta(
        slug_publico,
        preferred_tenant_id=preferred_tenant_id,
        require_tenant_match=require_tenant_match,
        allow_closed_for_read=allow_closed_for_read,
    )
    if not bool(getattr(encuesta, "mostrar_resultados_envivo", False)):
        raise EncuestaError(
            "Los resultados en vivo no estan publicados para esta encuesta.",
            status_code=403,
            payload={
                "reason_code": "live_results_hidden",
                "action_hint": "wait_for_results_publication",
            },
        )

    requested_filters = dict(filtros or {})
    privacy_mode = str(
        getattr(encuesta, "privacy_mode", "legacy") or "legacy"
    ).strip().lower()
    results_final = (
        str(getattr(encuesta, "estado", "") or "").strip().lower() == "cerrada"
    )
    active_source_anonymous = privacy_mode == "source_anonymous" and not results_final
    if privacy_mode == "source_anonymous" and requested_filters:
        # Arbitrary public ranges/segments can be differenced even when every
        # individual result satisfies k-anonymity (for example, cohorts of six
        # and five isolate the excluded response). Public source-anonymous
        # analytics therefore expose one canonical, unfiltered cohort only.
        raise EncuestaError(
            "Los filtros personalizados no estan disponibles en resultados publicos anonimos.",
            status_code=400,
            payload={
                "contract_version": PUBLIC_SMALL_CELL_CONTRACT_VERSION,
                "reason_code": "privacy_filters_not_available",
                "retryable": False,
                "action_hint": "request_canonical_public_results",
                "blocked_filter_keys": sorted(str(key) for key in requested_filters),
            },
        )
    now = _utc_now()
    effective_filters, analytics_range = _resolve_live_analytics_range(
        requested_filters,
        now=now,
    )
    snapshot = _get_response_snapshot(
        encuesta,
        effective_filters,
        sample_limit=0,
    )
    data_provenance = dict(snapshot["provenance"])
    data_provenance.update(
        {
            "exact_aggregates": True,
            "raw_responses_materialized": 0,
        }
    )
    responses_count = int(snapshot["population_size"])
    result_version = int(snapshot["result_version"])
    filters_fingerprint = hashlib.sha1(
        json.dumps(requested_filters, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    structure_material = [
        {
            "id": int(pregunta.id),
            "texto": str(pregunta.texto or ""),
            "tipo": str(pregunta.tipo or ""),
            "opciones": [
                {
                    "id": int(opcion.id),
                    "texto": str(opcion.texto or ""),
                    "valor": str(opcion.valor or ""),
                }
                for opcion in pregunta.opciones
            ],
        }
        for pregunta in encuesta.preguntas
    ]
    structure_fingerprint = hashlib.sha1(
        json.dumps(structure_material, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    snapshot_version = (
        f"{encuesta.id}:{responses_count}:{result_version}:"
        f"{filters_fingerprint}:{structure_fingerprint}"
    )
    if active_source_anonymous:
        stable_material = (
            f"survey:{encuesta.id}:source_anonymous_active:{structure_fingerprint}"
        )
        snapshot_version = (
            "private:" + hashlib.sha256(stable_material.encode("utf-8")).hexdigest()[:16]
        )
    effective_max_points = _analytics_bounded_int(
        max_points,
        default=2000,
        minimum=0,
        maximum=5000,
    )
    effective_max_cells = _analytics_bounded_int(
        max_cells,
        default=200,
        minimum=1,
        maximum=1000,
    )
    cache_material = {
        "survey_id": int(encuesta.id),
        "tenant_id": int(encuesta.tenant_id),
        "slug": str(slug_publico),
        "snapshot_version": snapshot_version,
        "updated_at": getattr(encuesta, "updated_at", None),
        "survey_state": str(getattr(encuesta, "estado", "") or "").strip().lower(),
        "range": analytics_range,
        "filters": requested_filters,
        "include_heatmap": bool(include_heatmap),
        "max_points": effective_max_points,
        "max_cells": effective_max_cells,
        "window": int(momentum_window_minutes or 10),
        "geo_privacy": "public_aggregated",
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_material, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    cached_payload = _public_live_cache_get(cache_key)
    if cached_payload is not None:
        return cached_payload

    exact_option_counts, _unique_option_counts, _answered_counts = (
        _exact_option_statistics(snapshot)
    )
    option_counts: Dict[Tuple[int, int], int] = {
        (int(question_id), int(option_id)): int(total or 0)
        for question_id, counts in exact_option_counts.items()
        for option_id, total in counts.items()
    }

    preguntas: List[Dict[str, Any]] = []
    highlights: List[str] = []
    for pregunta in encuesta.preguntas:
        normalized_type = _normalize_question_type(pregunta.tipo)
        if normalized_type not in {"single_choice", "multiple_choice"}:
            continue

        total_pregunta = 0
        opciones_payload: List[Dict[str, Any]] = []
        for opcion in pregunta.opciones:
            votos = int(option_counts.get((pregunta.id, opcion.id), 0) or 0)
            total_pregunta += votos
            opciones_payload.append(
                {
                    "id": opcion.id,
                    "label": opcion.texto,
                    "texto": opcion.texto,
                    "value": votos,
                    "votos": votos,
                }
            )

        opciones_payload.sort(key=lambda item: item["value"], reverse=True)
        for opcion_data in opciones_payload:
            porcentaje = (opcion_data["value"] / total_pregunta * 100) if total_pregunta else 0.0
            opcion_data["porcentaje"] = round(porcentaje, 2)

        lider = opciones_payload[0] if opciones_payload else None
        if lider and lider["value"] > 0:
            highlights.append(
                f"{pregunta.texto[:70]}: lidera '{lider['label']}' con {lider['porcentaje']}%."
            )

        preguntas.append(
            {
                "id": pregunta.id,
                "titulo": pregunta.texto,
                "tipo": normalized_type,
                "opciones": opciones_payload,
                "total_votos": total_pregunta,
                "is_multi": normalized_type == "multiple_choice",
            }
        )

    window = max(5, min(int(momentum_window_minutes or 10), 30))
    last_window, previous_window, responses_last_hour = _exact_recent_windows(
        snapshot,
        now=now,
        window_minutes=window,
    )

    trend = "estable"
    if last_window > previous_window:
        trend = "subiendo"
    elif last_window < previous_window:
        trend = "bajando"

    exact_timeline, timeline_metadata = _exact_timeseries(
        snapshot,
        "minute",
    )
    timeline = [
        {
            "timestamp": item["fecha"],
            "minute": item["fecha"],
            "total": int(item["total"]),
            "respuestas": int(item["total"]),
            "value": int(item["total"]),
        }
        for item in exact_timeline
    ]

    points: List[Dict[str, Any]] = []
    cells: List[Dict[str, Any]] = []
    geo_aggregation = {
        "minimum_cell_size": _public_small_cell_minimum(),
        "cell_limit": effective_max_cells,
        "cell_count": 0,
        "partial": False,
    }
    if include_heatmap:
        cells, geo_aggregation = _exact_geo_cells(
            snapshot,
            max_cells=effective_max_cells,
            minimum_count=_public_small_cell_minimum(),
            precision=3,
        )
    heatmap_points, heatmap_cells, heatmap_privacy = _prepare_live_heatmap_payload(
        [],
        cells,
        geo_privacy="public_aggregated",
    )

    ai_summary = "Sin datos suficientes para resumen en vivo."
    if responses_count > 0:
        momentum_text = (
            "participación acelerando" if trend == "subiendo" else "participación estable" if trend == "estable" else "participación desacelerando"
        )
        top_highlights = " ".join(highlights[:2]) if highlights else "Todavía no hay liderazgo claro por opción."
        ai_summary = (
            f"{responses_count} respuestas registradas, con {momentum_text} en los últimos minutos. "
            f"{top_highlights}"
        )

    participation_per_minute = round(responses_last_hour / 60.0, 3) if responses_last_hour else 0.0
    top_question = None
    for pregunta in preguntas:
        if not pregunta.get("opciones"):
            continue
        top_option = pregunta["opciones"][0]
        if not top_question or top_option["value"] > top_question["lider"]["value"]:
            top_question = {
                "pregunta_id": pregunta["id"],
                "pregunta": pregunta["titulo"],
                "lider": top_option,
            }

    kpis = {
        "responses_last_hour": responses_last_hour,
        "participation_per_minute": participation_per_minute,
        "heatmap_coverage_cells": len(cells),
        "leader": top_question,
        "leader_label": (top_question or {}).get("lider", {}).get("label") if top_question else None,
        "active_filters": requested_filters,
    }

    ai_insights: List[str] = []
    if responses_count == 0:
        ai_insights.append("Todavia no hay respuestas para mostrar: conviene revisar difusion y canales activos.")
    elif top_question and top_question.get("lider"):
        ai_insights.append(
            f"La pregunta con mayor tracción es '{top_question['pregunta'][:70]}' y lidera '{top_question['lider']['label']}' con {top_question['lider']['porcentaje']}%."
        )
    if responses_count > 0 and trend == "subiendo":
        ai_insights.append("La curva reciente de participación está acelerando: conviene reforzar distribución del link ahora.")
    elif responses_count > 0 and trend == "bajando":
        ai_insights.append("La curva reciente está desacelerando: conviene activar recordatorios o pauta segmentada.")
    elif responses_count > 0:
        ai_insights.append("La curva reciente se mantiene estable: se sugiere sostener frecuencia de difusión.")

    polling_interval_ms = 3000 if trend == "subiendo" else 8000 if trend == "bajando" else 5000
    public_endpoint = f"/api/public/encuestas/v1/{slug_publico}/live-results"
    v2_endpoint = f"/api/v2/public/surveys/{slug_publico}/live-results"
    empty_state = {
        "is_empty": responses_count == 0,
        "title": "Todavia no hay respuestas",
        "message": "Publica el enlace o espera nuevas participaciones para ver metricas en vivo.",
        "action_hint": "share_survey" if responses_count == 0 else None,
    }
    live_telemetry = {
        "has_responses": responses_count > 0,
        "responses_total": responses_count,
        "responses_last_hour": responses_last_hour,
        "participation_per_minute": participation_per_minute,
        "trend": trend,
        "polling_interval_ms": polling_interval_ms,
        "active_filters": requested_filters,
        "analytics_range": analytics_range,
    }
    suppress_ai_for_privacy = active_source_anonymous or (
        _public_small_cell_ai_requires_suppression(
            privacy_mode=privacy_mode,
            total_responses=responses_count,
            questions=preguntas,
            timeline=timeline,
            heatmap_cells=heatmap_cells,
        )
    )
    if suppress_ai_for_privacy:
        # Exact small-cell aggregates must never cross the provider boundary.
        live_ai_insights = {
            "provider_family": "none",
            "mode": "privacy_suppressed",
            "hf_status": {
                "enabled": False,
                "reason_code": "minimum_cell_size_not_met",
            },
            "summary": {
                "text": "Resultados protegidos por el umbral minimo de privacidad.",
                "contains_exact_counts": False,
            },
            "collection": {"item_count": 0},
            "recommended_actions": [],
        }
        live_ai_layers = {}
    else:
        live_ai_items: List[Dict[str, Any]] = []
        for pregunta in preguntas:
            if not isinstance(pregunta, dict):
                continue
            opciones = pregunta.get("opciones") if isinstance(pregunta.get("opciones"), list) else []
            for opcion in opciones[:4]:
                if not isinstance(opcion, dict):
                    continue
                live_ai_items.append(
                    {
                        "source": "live_vote",
                        "text": (
                            f"{pregunta.get('titulo') or ''} "
                            f"{opcion.get('label') or opcion.get('texto') or ''} "
                            f"{opcion.get('votos') or 0} votos {opcion.get('porcentaje') or 0}%"
                        ),
                        "category": "encuesta o votacion",
                        "channel": "public_live_results",
                        "status": trend,
                    }
                )
        for point in heatmap_points[:120]:
            if not isinstance(point, Mapping):
                continue
            weight = point.get("weight") or point.get("w") or point.get("count") or 1
            live_ai_items.append(
                {
                    "source": "survey",
                    "text": " ".join(
                        str(value)
                        for value in (
                            "encuesta",
                            point.get("categoria"),
                            point.get("barrio"),
                            point.get("ciudad"),
                            point.get("provincia"),
                            point.get("canal"),
                            weight,
                        )
                        if value is not None and value != ""
                    ),
                    "category": point.get("categoria") or "encuesta o votacion",
                    "channel": point.get("canal"),
                    "lat": point.get("lat"),
                    "lng": point.get("lng"),
                    "weight": weight,
                    "status": "active" if responses_count > 0 else "empty",
                }
            )
        if not live_ai_items:
            live_ai_items.append(
                {
                    "source": "survey_empty_state",
                    "text": "Encuesta o votacion sin respuestas. Revisar difusion, QR, WhatsApp y canales activos.",
                    "category": "encuesta o votacion",
                    "channel": "public_link",
                    "status": "empty",
                }
            )
        live_ai_insights = build_collection_ai_insights(
            live_ai_items,
            domain="survey_live_results",
        )
        live_ai_layers = build_map_ai_layers(
            [
                {
                    **dict(point),
                    "source": "survey",
                    "channel": point.get("canal"),
                    "weight": point.get("weight") or point.get("w") or point.get("count") or 1,
                }
                for point in heatmap_points[:effective_max_points]
                if isinstance(point, Mapping)
            ],
            insights=live_ai_insights,
        )
    raw_recommendations = live_ai_insights.get("recommended_actions")
    operator_recommendations = [
        {
            "id": str(action.get("id") or f"ai_recommendation_{index + 1}"),
            "label": str(action.get("label") or "Revisar senal IA"),
            "priority": action.get("priority") or "medium",
            "ui_hint": action.get("ui_hint") or "open_ai_summary",
            "source": "huggingface_ai_insights",
            "requires_operator_confirmation": True,
        }
        for index, action in enumerate(raw_recommendations or [])
        if isinstance(action, Mapping)
    ]
    if responses_count == 0:
        operator_recommendations.insert(
            0,
            {
                "id": "share_survey_now",
                "label": "Reforzar difusion por WhatsApp, QR y redes",
                "priority": "high",
                "ui_hint": "share_public_link",
                "source": "survey_live_results",
                "requires_operator_confirmation": True,
            },
        )
    ai_signal = {
        "contract_version": "surveys.live_ai_signal.v1",
        "provider_family": live_ai_insights.get("provider_family") or "huggingface",
        "mode": live_ai_insights.get("mode"),
        "hf_status": live_ai_insights.get("hf_status") or {},
        "summary": live_ai_insights.get("summary") or {},
        "collection": live_ai_insights.get("collection") or {},
        "recommended_actions": operator_recommendations[:6],
        "advisory_policy": dict(SURVEY_AI_ADVISORY_POLICY),
        "frontend_contract": {
            "recommended_widgets": [
                "live_ai_signal_card",
                "operator_recommendations",
                "map_ai_layers",
                "survey_heatmap",
            ],
            "safe_to_render_without_hf_token": True,
            "refresh_seconds": 30,
            "advisory_only": True,
        },
    }

    payload = {
        "contract_version": "surveys.live_results.v2",
        "result_version": result_version,
        "snapshot_version": snapshot_version,
        "encuesta_id": encuesta.id,
        "slug": slug_publico,
        "slug_publico": slug_publico,
        "total_respuestas": responses_count,
        "data_provenance": data_provenance,
        "analytics_range": analytics_range,
        "empty_state": empty_state,
        "live_telemetry": live_telemetry,
        "preguntas": preguntas,
        "timeline_minute": timeline,
        "timeline_metadata": timeline_metadata,
        "momentum": {
            "window_minutes": window,
            "last_window": last_window,
            "previous_window": previous_window,
            "trend": trend,
            "delta": last_window - previous_window,
            "last_10m": last_window,
            "previous_10m": previous_window,
        },
        "kpis": kpis,
        "heatmap": {
            "enabled": include_heatmap,
            "points": heatmap_points[:effective_max_points],
            "cells": heatmap_cells[:effective_max_cells],
            "metadata": {
                "resolution": 9,
                "points_count": len(heatmap_points),
                "cells_count": len(heatmap_cells),
                "truncated_points": max(
                    0,
                    len(heatmap_points) - effective_max_points,
                ),
                "truncated_cells": max(
                    0,
                    len(heatmap_cells) - effective_max_cells,
                ),
                "analytics_range": analytics_range,
                "aggregation": geo_aggregation,
                **heatmap_privacy,
            },
        },
        "ai_summary": ai_summary,
        "ai_insights": ai_insights,
        "ai_signal": ai_signal,
        "ai_layers": live_ai_layers,
        "operator_recommendations": operator_recommendations[:6],
        "render_contract": {
            "preferred_visualization": "live_vote_command_center",
            "product_surface": NOETHER_ANALYTICS_MAPS_SURFACE,
            "supports": [
                "cards",
                "bars",
                "timeline",
                "heatmap",
                "map_pulses",
                "ai_summary",
                "hf_ai_signals",
                "ai_map_layers",
                "operator_recommendations",
                "csv_export",
            ],
            "polling_interval_ms": polling_interval_ms,
            "empty_state": "Todavia no hay respuestas para mostrar.",
            "filter_keys": [
                "range_preset",
                "range_timezone",
                "desde",
                "hasta",
                "canal",
                "barrio",
                "ciudad",
                "provincia",
            ],
            "map_experience": "interactive_heatmap_with_ai_layers",
        },
        "ui_actions": [
            {"id": "refresh_live_results", "label": "Actualizar resultados", "ui_hint": "refresh"},
            {"id": "export_live_csv", "label": "Exportar CSV", "ui_hint": "download_csv"},
            {"id": "inspect_ai_signals", "label": "Ver senales IA", "ui_hint": "open_ai_summary"},
        ],
        "links": {
            "public_live_results": public_endpoint,
            "v2_live_results": v2_endpoint,
        },
        "updated_at": now.isoformat(),
    }
    payload = _apply_public_small_cell_policy(
        payload,
        privacy_mode=privacy_mode,
        results_final=results_final,
    )
    payload["cache_etag"] = live_results_http_etag(payload)
    payload["cache_control"] = {
        "max_age_seconds": 3,
        "stale_while_revalidate_seconds": 5,
    }
    _public_live_cache_put(cache_key, payload)
    return payload
