"""Exact descriptive A/B comparison for authorized administrative reads."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, and_, case, cast, literal, or_

from database import db
from models import EncRespuesta, EncPregunta, EncOpcion, EncRespuestaDetalle
from services.encuestas_service import EncuestaError
from services.survey_analytics_evidence import FILTER_LABELS
from services.survey_response_provenance import build_survey_response_provenance

SEGMENT_COLUMNS = {
    "canal": EncRespuesta.canal, "genero": EncRespuesta.genero,
    "rango_etario": EncRespuesta.rango_etario, "barrio": EncRespuesta.barrio,
    "ciudad": EncRespuesta.ciudad, "provincia": EncRespuesta.provincia, "pais": EncRespuesta.pais,
}
TRIM_CHARS = " \t\r\n\f\v"


def invalid_filters():
    return EncuestaError("Revisá los filtros de la comparación.", status_code=400,
        payload={"reason_code": "survey_segment_filters_invalid"})


def validate_segment_request_args(args, *, suggestions=False):
    allowed = set(FILTER_LABELS) | {"data_mode", "exclude_demo", "tenant", "tenant_slug", "tenant_id"}
    # These two transport aliases name the same scope; never accept an
    # ambiguous request merely because one alias won the authentication path.
    if args.get("tenant") and args.get("tenant_slug") and (
        args["tenant"].strip().lower() != args["tenant_slug"].strip().lower()
    ):
        raise invalid_filters()
    if suggestions:
        allowed.add("limit")
    else:
        allowed.update(f"{group}_{key}" for group in ("a", "b") for key in SEGMENT_COLUMNS)
    for key in args:
        values = args.getlist(key)
        if key not in allowed or len(values) != 1 or not values[0].strip(TRIM_CHARS):
            raise invalid_filters()
        if key in SEGMENT_COLUMNS or key.startswith(("a_", "b_")):
            if any(not part.strip(TRIM_CHARS) for part in values[0].split(",")):
                raise invalid_filters()
        if key == "limit" and (not values[0].isdigit() or not 2 <= int(values[0]) <= 10):
            raise invalid_filters()


def normalize_segment(segment):
    if segment is None:
        return {}
    if not isinstance(segment, dict) or set(segment) - set(SEGMENT_COLUMNS):
        raise invalid_filters()
    normalized = {}
    for key, raw in segment.items():
        if not isinstance(raw, (str, list, tuple)):
            raise invalid_filters()
        values = raw.split(",") if isinstance(raw, str) else raw
        if not values or len(values) > 25 or any(not isinstance(v, str) for v in values):
            raise invalid_filters()
        values = [value.strip(TRIM_CHARS) for value in values]
        if any(not value or len(value) > 120 or any(ord(c) < 32 for c in value) for value in values):
            raise invalid_filters()
        values = list(dict.fromkeys(values))
        normalized[key] = values[0] if len(values) == 1 else values
    return normalized


def segment_expression(segment):
    conditions = []
    for key, values in segment.items():
        candidates = values if isinstance(values, list) else [values]
        column = db.func.lower(db.func.trim(cast(SEGMENT_COLUMNS[key], String), TRIM_CHARS))
        conditions.append(column.in_([value.lower() for value in candidates]))
    return and_(*conditions) if conditions else literal(True)


def validate_global_filters(filters, parse_datetime, parse_bbox):
    if filters is None:
        return {}
    if not isinstance(filters, dict) or set(filters) - (set(FILTER_LABELS) | {"data_mode", "exclude_demo"}):
        raise invalid_filters()
    for key in ("desde", "hasta"):
        if key in filters and (not isinstance(filters[key], str) or not parse_datetime(filters[key])):
            raise invalid_filters()
    if filters.get("desde") and filters.get("hasta"):
        start, end = parse_datetime(filters["desde"]), parse_datetime(filters["hasta"])
        start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start
        end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end
        if start > end: raise invalid_filters()
    if "bbox" in filters and not parse_bbox(filters["bbox"]): raise invalid_filters()
    if "data_mode" in filters and filters["data_mode"] not in ("real", "synthetic"): raise invalid_filters()
    if "exclude_demo" in filters and str(filters["exclude_demo"]).lower() not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
        raise invalid_filters()
    for key in set(FILTER_LABELS) - {"desde", "hasta", "bbox"}:
        if key in filters:
            raw = filters[key]
            if key in ("canal", "utm_source", "utm_campaign"):
                if not isinstance(raw, str) or not raw.strip() or len(raw) > 120: raise invalid_filters()
            else:
                normalize_segment({key: raw})
    return {key: value for key, value in filters.items() if key in FILTER_LABELS}


def exact_segment_comparison(encuesta, base_query, selected_query, mode, *,
                             global_filters, segment_a, segment_b, normalize_type):
    """Bounded SQL aggregates; duplicate details never multiply respondents."""
    segment_a, segment_b = normalize_segment(segment_a), normalize_segment(segment_b)
    condition_a, condition_b = segment_expression(segment_a), segment_expression(segment_b)
    origin = "synthetic_demo" if mode == "synthetic" else "real"
    selected_origin = EncRespuesta.response_origin == origin
    base_query = base_query.filter(EncRespuesta.tenant_id == encuesta.tenant_id)
    selected_query = selected_query.filter(EncRespuesta.tenant_id == encuesta.tenant_id)
    def summed(condition, name):
        return db.func.coalesce(db.func.sum(case((condition, 1), else_=0)), 0).label(name)
    totals_query = base_query.with_entities(
        literal("basis").label("row_kind"), literal(0).label("question_id"), literal(0).label("option_id"),
        summed(selected_origin, "selected"), summed(selected_origin & condition_a, "a"),
        summed(selected_origin & condition_b, "b"), summed(selected_origin & condition_a & condition_b, "overlap"),
        summed(EncRespuesta.response_origin == "real", "real"),
        summed(EncRespuesta.response_origin == "synthetic_demo", "synthetic"),
        summed(EncRespuesta.response_origin == "legacy_unverified", "unverified"),
    ).order_by(None)
    questions = [q for q in encuesta.preguntas if normalize_type(q.tipo) in ("single_choice", "multiple_choice")]
    question_ids = [q.id for q in questions]
    union_parts = []
    if question_ids:
        detail_query = (selected_query
            .join(EncRespuestaDetalle, EncRespuestaDetalle.respuesta_id == EncRespuesta.id)
            .join(EncPregunta, and_(EncPregunta.id == EncRespuestaDetalle.pregunta_id, EncPregunta.encuesta_id == encuesta.id))
            .join(EncOpcion, and_(EncOpcion.id == EncRespuestaDetalle.opcion_id, EncOpcion.pregunta_id == EncPregunta.id))
            .filter(EncPregunta.id.in_(question_ids)))
        def unique_in(condition):
            return db.func.count(db.func.distinct(case((condition, EncRespuesta.id))))
        response_questions = detail_query.with_entities(
            EncRespuesta.id.label("response_id"), EncPregunta.id.label("question_id"),
            db.func.count(db.func.distinct(EncOpcion.id)).label("option_count"),
        ).order_by(None).group_by(EncRespuesta.id, EncPregunta.id).subquery()
        detail_query = detail_query.join(response_questions, and_(
            response_questions.c.response_id == EncRespuesta.id,
            response_questions.c.question_id == EncPregunta.id))
        single_ids = [q.id for q in questions if normalize_type(q.tipo) == "single_choice"]
        conflict = EncPregunta.id.in_(single_ids) & (response_questions.c.option_count > 1)
        valid = ~conflict
        answered_query = detail_query.with_entities(literal("question"), EncPregunta.id, literal(0), literal(0),
            unique_in(condition_a & valid), unique_in(condition_b & valid),
            literal(0), unique_in(condition_a & conflict), unique_in(condition_b & conflict), literal(0),
        ).order_by(None).group_by(EncPregunta.id)
        option_query = detail_query.filter(valid).with_entities(literal("option"), EncPregunta.id, EncOpcion.id,
            literal(0), unique_in(condition_a), unique_in(condition_b), literal(0), literal(0), literal(0), literal(0),
        ).order_by(None).group_by(EncPregunta.id, EncOpcion.id)
        # One SQL statement gives all bases and cells the same READ COMMITTED
        # statement snapshot on PostgreSQL, including concurrent arrivals.
        union_parts.extend((answered_query, option_query))
    channel = db.func.coalesce(db.func.nullif(db.func.lower(db.func.trim(EncRespuesta.canal)), ""), "sin_canal")
    for name, condition in (("a", condition_a), ("b", condition_b)):
        top = selected_query.filter(condition).with_entities(channel.label("label"), db.func.count(EncRespuesta.id).label("n")).group_by(
            channel).order_by(db.func.count(EncRespuesta.id).desc(), channel).limit(20).subquery()
        union_parts.append(db.session.query(literal(f"channel_{name}:") + top.c.label,
            literal(0), literal(0), top.c.n, literal(0), literal(0), literal(0), literal(0), literal(0), literal(0)))
    aggregate_query = totals_query.union_all(*union_parts)
    rows = aggregate_query.all()
    totals = next(row for row in rows if row.row_kind == "basis")
    answered = {int(row.question_id): (int(row.a), int(row.b)) for row in rows if row.row_kind == "question"}
    conflicts = {int(row.question_id): (int(row.real), int(row.synthetic)) for row in rows if row.row_kind == "question"}
    options = {(int(row.question_id), int(row.option_id)): (int(row.a), int(row.b)) for row in rows if row.row_kind == "option"}
    question_payload = []
    for question in questions:
        a_base, b_base = answered.get(question.id, (0, 0))
        a_conflicts, b_conflicts = conflicts.get(question.id, (0, 0))
        if a_base + a_conflicts > totals.a or b_base + b_conflicts > totals.b:
            raise EncuestaError("La base cambió durante la comparación. Actualizá para volver a consultar.", status_code=409,
                payload={"reason_code": "survey_segment_base_changed"})
        entries = []
        for option in question.opciones:
            a, b = options.get((question.id, option.id), (0, 0))
            if a > a_base or b > b_base:
                raise EncuestaError("La base cambió durante la comparación. Actualizá para volver a consultar.", status_code=409,
                    payload={"reason_code": "survey_segment_base_changed"})
            a_pct, b_pct = (round(a / a_base * 100, 2) if a_base else None), (round(b / b_base * 100, 2) if b_base else None)
            entries.append({"id": option.id, "label": option.texto, "segment_a_count": a, "segment_b_count": b,
                "segment_a_percent": a_pct, "segment_b_percent": b_pct,
                "delta_percentage_points": round(a_pct - b_pct, 2) if a_pct is not None and b_pct is not None else None})
        question_payload.append({"id": question.id, "label": question.texto, "type": normalize_type(question.tipo),
            "segment_a_answered": a_base, "segment_b_answered": b_base,
            "segment_a_conflicts": a_conflicts, "segment_b_conflicts": b_conflicts, "options": entries})
    total, a_total, b_total = int(totals.selected), int(totals.a), int(totals.b)
    provenance = build_survey_response_provenance(real_count=int(totals.real), synthetic_count=int(totals.synthetic),
        unverified_count=int(totals.unverified), mode=mode, synthetic_excluded=int(totals.synthetic) if mode == "real" else 0,
        unverified_excluded=int(totals.unverified))
    provenance.update(exact_aggregates=True, population_size=total, sample_size=0, sample_limit=0,
        raw_responses_materialized=0, aggregate_scope="all_selected_records")
    def legacy_group(name, filters, group_total):
        qs = []
        for q in question_payload:
            votes = sum(row[f"segment_{name}_count"] for row in q["options"])
            qs.append({"pregunta_id": q["id"], "texto": q["label"], "tipo": q["type"], "total_votos": votes,
                "opciones": [{"opcion_id": row["id"], "label": row["label"], "votos": row[f"segment_{name}_count"],
                    "porcentaje": round(row[f"segment_{name}_count"] / votes * 100, 2) if votes else 0.0} for row in q["options"]]})
        prefix = f"channel_{name}:"
        channels = [{"label": row.row_kind[len(prefix):], "value": int(row.selected)}
            for row in rows if row.row_kind.startswith(prefix)]
        remainder = group_total - sum(row["value"] for row in channels)
        if remainder > 0: channels.append({"label": "otros", "value": remainder})
        return {"meta": {"name": name, "label": f"Segmento {name.upper()}", "filters": filters, "count": group_total,
                "coverage": round(group_total / total * 100, 2) if total else None}, "filters": filters,
            "stats": {"total_respuestas": group_total, "preguntas": qs, "canales": channels}}
    return {"contract_version": "surveys.segment_compare.v1", "encuesta_id": encuesta.id,
        "scope": {"survey_id": encuesta.id, "tenant_id": encuesta.tenant_id, "mode": mode,
            "filtered": bool(global_filters), "global_filters": global_filters,
            "segment_a_filters": segment_a, "segment_b_filters": segment_b},
        "basis": {"selected_records": total, "segment_a_records": a_total, "segment_b_records": b_total,
            "overlap_records": int(totals.overlap), "exact": True},
        "questions": question_payload, "data_provenance": provenance,
        "segment_a": legacy_group("a", segment_a, a_total), "segment_b": legacy_group("b", segment_b, b_total),
        "comparison_meta": {"base_total": total, "gap_respuestas": a_total - b_total},
        "ui": {"heading": "Comparar segmentos", "description": ("Datos sintéticos de demostración. " if mode == "synthetic" else "")
            + "Comparación descriptiva de todas las respuestas seleccionadas. Cada opción usa como base las respuestas con una opción válida registrada en esa pregunta.",
            "segment_a": "Segmento A", "segment_b": "Segmento B", "base": "Respuestas seleccionadas",
            "answered": "Respondieron esta pregunta", "option": "Opción", "selected": "Respuestas que eligieron la opción",
            "conflicts": "Respuestas con selecciones incompatibles",
            "percent": "Porcentaje entre quienes respondieron", "delta": "Diferencia A − B (puntos porcentuales)",
            "delta_unit": "p.p.", "overlap": "Respuestas presentes en ambos segmentos",
            "empty": "No hay respuestas o preguntas con opciones para esta comparación.", "details": "Ver bases y límites"},
        "limitations": [
            {"id": "descriptive", "title": "Sin inferencia poblacional", "detail": "Las diferencias describen registros recibidos; no miden causalidad ni significación estadística."},
            {"id": "multiple", "title": "Selección múltiple", "detail": "Cada respuesta cuenta una sola vez por opción. Una respuesta puede incluir varias opciones, por lo que sus porcentajes pueden sumar más de 100%."},
            {"id": "overlap", "title": "Grupos que pueden solaparse", "detail": "Una misma respuesta puede pertenecer a ambos grupos. No son muestras independientes; el solapamiento se informa por separado."},
            {"id": "recorded", "title": "Base registrada por pregunta", "detail": "Se cuentan opciones válidas del cuestionario actual. No se infiere si una pregunta fue mostrada, omitida o rechazada; no es una tasa de respuesta."},
            {"id": "conflicts", "title": "Selecciones incompatibles excluidas", "detail": "En preguntas de opción única se excluye de esa pregunta a cada respuesta con más de una opción distinta. Se informa su cantidad, sin modificar ni borrar los registros originales."},
        ], "inference_authorized": False, "updated_at": datetime.now(timezone.utc).isoformat()}
