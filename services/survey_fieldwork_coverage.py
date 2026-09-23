"""Private, descriptive disclosure of recorded segmentation fields.

The builder is query-free. Attach it only after the administrative analytics
authorization checks; it is not a contact disposition or response-rate model.
"""
from __future__ import annotations

from typing import Any

from services.survey_analytics_evidence import FILTER_LABELS

CONTRACT = "surveys.fieldwork_coverage.v1"
MAX_COUNT = 9007199254740991
DIMENSIONS = (
    ("channel", "Canal", "Canal registrado con texto no vacío."),
    ("campaign", "Campaña", "Campaña UTM registrada con texto no vacío; no acredita contactos enviados."),
    ("gender", "Género", "Dato registrado con texto no vacío; puede ser opcional y no se infiere."),
    ("age_range", "Rango etario", "Dato registrado con texto no vacío; no verifica edad ni identidad."),
    ("neighborhood", "Barrio", "Dato registrado con texto no vacío; no verifica residencia."),
    ("city", "Ciudad", "Dato registrado con texto no vacío; no verifica residencia."),
    ("province", "Provincia", "Dato registrado con texto no vacío; no verifica residencia."),
    ("country", "País", "Dato registrado con texto no vacío; no verifica residencia."),
    ("coordinates", "Coordenadas", "Latitud entre -90 y 90 y longitud entre -180 y 180, ambas presentes; no verifica jurisdicción ni precisión."),
)


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNT


def build_fieldwork_coverage(
    survey: Any, summary: Any, aggregates: Any, filters: Any = None,
) -> dict | None:
    """Withhold inconsistent reads instead of repairing denominators."""
    sid, tid = getattr(survey, "id", None), getattr(survey, "tenant_id", None)
    if (not _count(sid) or not sid or not _count(tid) or not tid
            or not isinstance(summary, dict) or not isinstance(aggregates, dict)
            or type(summary.get("encuesta_id")) is not int or summary["encuesta_id"] != sid):
        return None
    provenance = summary.get("data_provenance")
    total = aggregates.get("selected_records")
    mode = aggregates.get("mode")
    if (not _count(total) or not _count(summary.get("total_respuestas"))
            or total != summary["total_respuestas"]
            or aggregates.get("survey_id") != sid or aggregates.get("tenant_id") != tid
            or type(aggregates.get("survey_id")) is not int or type(aggregates.get("tenant_id")) is not int
            or mode not in ("real", "synthetic") or not isinstance(provenance, dict)
            or provenance.get("contract_version") != "surveys.response_provenance.v1"
            or provenance.get("server_trusted_classification") is not True
            or provenance.get("exact_aggregates") is not True
            or provenance.get("mode") != mode
            or provenance.get("contains_synthetic") is not (mode == "synthetic" and total > 0)
            or not _count(provenance.get("population_size")) or provenance["population_size"] != total
            or any(not _count(provenance.get(key)) for key in (
                "real_responses_included", "synthetic_responses_included", "unverified_responses_included"))
            or provenance.get("unverified_responses_included") != 0
            or provenance.get("real_responses_included") != (total if mode == "real" else 0)
            or provenance.get("synthetic_responses_included") != (total if mode == "synthetic" else 0)):
        return None
    recorded = aggregates.get("recorded")
    if (not isinstance(recorded, dict) or set(recorded) != {key for key, *_ in DIMENSIONS}
            or any(not _count(value) or value > total for value in recorded.values())):
        return None
    filtered = isinstance(filters, dict) and any(
        filters.get(key) is not None and filters.get(key) != "" for key in FILTER_LABELS
    )
    limitations = [
        {"id": "received_records", "title": "Base de respuestas recibidas",
         "detail": "Cada porcentaje divide registros con el dato entre respuestas del origen y filtros seleccionados. No mide contactos, elegibilidad, rechazos ni tasa de respuesta."},
        {"id": "optional_fields", "title": "Ausencia no significa rechazo",
         "detail": "Un campo puede ser opcional o no haberse solicitado. Sin dato utilizable no significa abandono, incumplimiento ni una respuesta inválida."},
        {"id": "recorded_not_verified", "title": "Presencia, no verificación",
         "detail": "El texto no vacío se cuenta como registrado, aunque su contenido no haya sido validado. Las coordenadas solo se comprueban por rango; no acreditan residencia o jurisdicción."},
        {"id": "descriptive_only", "title": "Alcance descriptivo",
         "detail": "La cobertura ayuda a interpretar filtros y cruces; no acredita representatividad poblacional ni autoriza inferencia estadística."},
    ]
    if mode == "synthetic":
        limitations.insert(0, {"id": "synthetic", "title": "Datos sintéticos",
            "detail": "Estos registros son de demostración y no representan participación real."})
    return {
        "contract_version": CONTRACT,
        "scope": {"survey_id": sid, "tenant_id": tid, "mode": mode, "filtered": filtered},
        "basis": {"selected_records": total, "exact": True},
        "dimensions": [{"id": key, "label": label, "recorded_count": recorded[key],
            "missing_count": total - recorded[key],
            "coverage_percent": round(recorded[key] / total * 100, 2) if total else None,
            "detail": detail} for key, label, detail in DIMENSIONS],
        "ui": {
            "heading": "Cobertura para segmentar", "eyebrow": "Trabajo de campo",
            "description": ("Datos sintéticos de demostración. " if mode == "synthetic" else "")
                + f"Base exacta: {total} respuestas del origen y filtros seleccionados. Revisá qué datos están disponibles para interpretar filtros y cruces.",
            "recorded": "Con dato", "missing": "Sin dato utilizable", "coverage": "Cobertura",
            "empty": "Todavía no hay respuestas en esta selección.",
            "details": "Ver definiciones y límites",
        },
        "limitations": limitations, "inference_authorized": False, "response_rate": None,
    }
