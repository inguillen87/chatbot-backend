from __future__ import annotations

from copy import deepcopy
from typing import Any


EXECUTIVE_PRESENTATION_MODE = "executive"
EXECUTIVE_PROVENANCE_CONTRACT_VERSION = "demo.executive_provenance.v1"
EXECUTIVE_CHANNEL_SUMMARY_CONTRACT_VERSION = "demo.channel_summary.v1"

_SYNTHETIC_DATA_MODE = "synthetic_demo_scenario"
_SESSION_DATA_MODE = "session_generated_events"
_MIXED_PARTITIONED_DATA_MODE = "mixed_partitioned"


def normalize_demo_presentation_mode(value: Any) -> str:
    """Resolve the opt-in presentation mode without changing legacy responses."""

    normalized = str(value or "").strip().lower()
    return EXECUTIVE_PRESENTATION_MODE if normalized == EXECUTIVE_PRESENTATION_MODE else ""


def _executive_provenance(*, mode: str, tenant_slug: str) -> dict[str, Any]:
    synthetic = mode == _SYNTHETIC_DATA_MODE
    contains_synthetic = mode in {
        _SYNTHETIC_DATA_MODE,
        _MIXED_PARTITIONED_DATA_MODE,
    }
    label = (
        "Escenario demostrativo sintético de Junín. No representa datos municipales "
        "reales ni debe usarse para tomar decisiones de gobierno."
        if synthetic
        else (
            "El panel separa la actividad real generada en esta sesión demo de la "
            "encuesta sintética adjunta. Ninguna de las dos fuentes representa "
            "estadísticas municipales oficiales."
        )
    )
    provenance = {
        "contract_version": EXECUTIVE_PROVENANCE_CONTRACT_VERSION,
        "scope": "executive_snapshot",
        "mode": mode,
        "synthetic": synthetic,
        "contains_synthetic": contains_synthetic,
        "municipal_truth": False,
        "suitable_for_product_demonstration": True,
        "suitable_for_government_decisions": False,
        "label": label,
        "tenant_scope": str(tenant_slug or "municipio").strip().lower() or "municipio",
        "requested_tenant_slug": str(tenant_slug or "municipio").strip().lower() or "municipio",
        "scenario_tenant_slug": "junin" if synthetic else None,
        "scenario_scope": (
            "Junín, Mendoza"
            if synthetic
            else "Sesión demo validada con encuesta sintética separada"
        ),
    }
    provenance["source_partitions"] = (
        [
            {
                "scope": "cards,metrics,timeline,map,cases,channel_summary",
                "mode": _SYNTHETIC_DATA_MODE,
                "synthetic": True,
            }
        ]
        if synthetic
        else [
            {
                "scope": "cards,timeline,map,cases,channel_summary,session_activity",
                "mode": _SESSION_DATA_MODE,
                "synthetic": False,
            },
            {
                "scope": "survey_voting",
                "mode": _SYNTHETIC_DATA_MODE,
                "synthetic": True,
            },
        ]
    )
    return provenance


def _as_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_percentage(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _survey_snapshot_summary(survey_voting: dict[str, Any]) -> dict[str, Any]:
    """Derive the executive survey KPI from the embedded survey contract."""

    raw_items = survey_voting.get("items") or survey_voting.get("all_items") or []
    item = next((entry for entry in raw_items if isinstance(entry, dict)), {})
    results = item.get("results") if isinstance(item.get("results"), dict) else {}
    seeded_responses = _as_non_negative_int(
        results.get("seeded_responses")
        if results.get("seeded_responses") is not None
        else item.get("seeded_responses")
        if item.get("seeded_responses") is not None
        else (survey_voting.get("seed_policy") or {}).get("responses_per_item")
    )
    has_interactive_partition = (
        "interactive_demo_responses" in results
        or "interactive_demo_responses" in item
    )
    interactive_demo_responses = _as_non_negative_int(
        results.get("interactive_demo_responses")
        if results.get("interactive_demo_responses") is not None
        else item.get("interactive_demo_responses")
    )
    total = (
        seeded_responses + interactive_demo_responses
        if has_interactive_partition
        else _as_non_negative_int(
            results.get("total_respuestas")
            if results.get("total_respuestas") is not None
            else seeded_responses
        )
    )
    options = [
        option
        for option in (results.get("options") or [])
        if isinstance(option, dict)
    ]
    top_option = max(
        options,
        key=lambda option: _as_non_negative_int(option.get("count") or option.get("votos")),
        default={},
    )
    top_count = _as_non_negative_int(top_option.get("count") or top_option.get("votos"))
    top_percentage = _as_percentage(top_option.get("porcentaje"))
    if not top_percentage and total:
        top_percentage = round((top_count / total) * 100, 2)
    return {
        "title": str(item.get("titulo") or item.get("title") or "encuesta demostrativa"),
        "total": total,
        "seeded_responses": seeded_responses,
        "interactive_demo_responses": interactive_demo_responses,
        "verified_citizen_responses": 0,
        "partitioned": has_interactive_partition,
        "top_label": str(top_option.get("label") or top_option.get("texto") or "Sin resultados"),
        "top_percentage": top_percentage,
    }


def _format_percentage(value: float) -> str:
    return f"{value:g}%"


def _survey_composition_detail(survey_summary: dict[str, Any]) -> str:
    if survey_summary.get("partitioned"):
        return (
            f"{survey_summary['seeded_responses']} base sintética + "
            f"{survey_summary['interactive_demo_responses']} participaciones demo = "
            f"{survey_summary['total']} total; 0 respuestas ciudadanas verificadas"
        )
    return f"Base sintética de {survey_summary['title']}"


def _survey_composition_fields(survey_summary: dict[str, Any]) -> dict[str, int]:
    return {
        "seeded_responses": survey_summary["seeded_responses"],
        "interactive_demo_responses": survey_summary["interactive_demo_responses"],
        "total_respuestas": survey_summary["total"],
        "verified_citizen_responses": 0,
    }


def _synthetic_cards(survey_summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "claims_received",
            "label": "Reclamos ingresados",
            "value": "184",
            "detail": "Casos del escenario demostrativo consolidado",
            "period": "últimos 30 días simulados",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "sla_compliance",
            "label": "SLA en término",
            "value": "87%",
            "detail": "Resoluciones dentro del objetivo operativo simulado",
            "period": "últimos 30 días simulados",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "whatsapp_conversations",
            "label": "Conversaciones por WhatsApp",
            "value": "326",
            "detail": "Atenciones ciudadanas del escenario demostrativo",
            "period": "últimos 30 días simulados",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "survey_participation",
            "label": "Respuestas en encuesta demo",
            "value": str(survey_summary["total"]),
            "detail": _survey_composition_detail(survey_summary),
            "period": (
                "base sintética + participaciones Preview"
                if survey_summary.get("partitioned")
                else "muestra determinística simulada"
            ),
            "data_mode": _SYNTHETIC_DATA_MODE,
            **_survey_composition_fields(survey_summary),
        },
    ]


def _synthetic_metrics(survey_summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "claims_received",
            "label": "Reclamos ingresados",
            "value": 184,
            "unit": "casos",
            "detail": "38 permanecen abiertos en el escenario",
            "period": "últimos 30 días simulados",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "sla_compliance_pct",
            "label": "Cumplimiento de SLA",
            "value": 87,
            "unit": "%",
            "detail": "Objetivo demostrativo: 85%",
            "period": "últimos 30 días simulados",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "whatsapp_first_response_minutes",
            "label": "Primera respuesta por WhatsApp",
            "value": 3.4,
            "unit": "min",
            "detail": "Mediana del escenario demostrativo",
            "period": "últimos 30 días simulados",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "survey_valid_votes",
            "label": "Participación en encuesta",
            "value": survey_summary["total"],
            "unit": (
                "respuestas demo"
                if survey_summary.get("partitioned")
                else "respuestas sintéticas"
            ),
            "detail": (
                f"{_survey_composition_detail(survey_summary)}. "
                f"{survey_summary['top_label']} lidera con "
                f"{_format_percentage(survey_summary['top_percentage'])}"
            ),
            "period": survey_summary["title"],
            "data_mode": _SYNTHETIC_DATA_MODE,
            **_survey_composition_fields(survey_summary),
        },
    ]


def _synthetic_timeline(survey_summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": "timeline-01",
            "time": "08:40",
            "label": "Nuevo reclamo de alumbrado",
            "detail": "Clasificado y georreferenciado automáticamente",
            "status": "done",
            "channel": "whatsapp",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "timeline-02",
            "time": "09:05",
            "label": "Derivación a Servicios Públicos",
            "detail": "Caso JN-DEMO-1042 asignado con SLA de 24 horas",
            "status": "done",
            "channel": "operations",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "timeline-03",
            "time": "10:20",
            "label": "Cuadrilla confirma intervención",
            "detail": "Evidencia incorporada al historial del caso",
            "status": "done",
            "channel": "field_team",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "timeline-04",
            "time": "11:15",
            "label": "Actualización automática al vecino",
            "detail": "Notificación de avance enviada por WhatsApp",
            "status": "in_progress",
            "channel": "whatsapp",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "timeline-05",
            "time": "12:00",
            "label": "Corte ejecutivo de prioridades barriales",
            "detail": (
                f"{_survey_composition_detail(survey_summary)}. "
                f"{survey_summary['top_label']} lidera con "
                f"{_format_percentage(survey_summary['top_percentage'])}"
            ),
            "status": "in_progress",
            "channel": "survey",
            "data_mode": _SYNTHETIC_DATA_MODE,
            **_survey_composition_fields(survey_summary),
        },
    ]


def _synthetic_map() -> dict[str, Any]:
    return {
        "enabled": True,
        "sample": True,
        "displayed_points": 5,
        "represented_cases": 52,
        "total_cases": 184,
        "coverage_note": (
            "Cinco zonas de muestra representan 52 de 184 reclamos del escenario; "
            "no son un relevamiento territorial real."
        ),
        "center": {"lat": -33.144539, "lng": -68.485729},
        "zoom": 13,
        "label": "Mapa demostrativo de demanda ciudadana",
        "data_mode": _SYNTHETIC_DATA_MODE,
        "points": [
            {
                "id": "synthetic-junin-01",
                "lat": -33.1422,
                "lng": -68.4824,
                "label": "Luminaria fuera de servicio",
                "status": "assigned",
                "category": "Alumbrado",
                "zone": "Centro",
                "weight": 18,
                "data_mode": _SYNTHETIC_DATA_MODE,
            },
            {
                "id": "synthetic-junin-02",
                "lat": -33.1379,
                "lng": -68.4921,
                "label": "Mantenimiento de calzada",
                "status": "in_progress",
                "category": "Bacheo",
                "zone": "Noroeste",
                "weight": 12,
                "data_mode": _SYNTHETIC_DATA_MODE,
            },
            {
                "id": "synthetic-junin-03",
                # Keep the synthetic sample inside Junin's municipal boundary.
                # The previous latitude crossed into neighboring Rivadavia.
                "lat": -33.1463,
                "lng": -68.4786,
                "label": "Retiro de residuos voluminosos",
                "status": "resolved",
                "category": "Limpieza",
                "zone": "Este",
                "weight": 9,
                "data_mode": _SYNTHETIC_DATA_MODE,
            },
            {
                "id": "synthetic-junin-04",
                # This is still south of the demo center while remaining in Junin.
                "lat": -33.1480,
                "lng": -68.4899,
                "label": "Poda preventiva solicitada",
                "status": "new",
                "category": "Arbolado",
                "zone": "Sur",
                "weight": 7,
                "data_mode": _SYNTHETIC_DATA_MODE,
            },
            {
                "id": "synthetic-junin-05",
                "lat": -33.1472,
                "lng": -68.4992,
                "label": "Revisión de señalización",
                "status": "assigned",
                "category": "Tránsito",
                "zone": "Oeste",
                "weight": 6,
                "data_mode": _SYNTHETIC_DATA_MODE,
            },
        ],
        "legend": [
            {"status": "new", "label": "Nuevo"},
            {"status": "assigned", "label": "Asignado"},
            {"status": "in_progress", "label": "En curso"},
            {"status": "resolved", "label": "Resuelto"},
        ],
    }


def _synthetic_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "synthetic-case-1042",
            "case_code": "JN-DEMO-1042",
            "title": "Luminaria fuera de servicio",
            "category": "Alumbrado",
            "status": "assigned",
            "priority": "alta",
            "channel": "whatsapp",
            "zone": "Centro",
            "sla_status": "in_target",
            "opened_at_label": "08:40 del día simulado",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "synthetic-case-1038",
            "case_code": "JN-DEMO-1038",
            "title": "Mantenimiento de calzada",
            "category": "Bacheo",
            "status": "in_progress",
            "priority": "media",
            "channel": "web",
            "zone": "Noroeste",
            "sla_status": "in_target",
            "opened_at_label": "día simulado anterior",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "synthetic-case-1031",
            "case_code": "JN-DEMO-1031",
            "title": "Retiro de residuos voluminosos",
            "category": "Limpieza",
            "status": "resolved",
            "priority": "media",
            "channel": "whatsapp",
            "zone": "Este",
            "sla_status": "met",
            "opened_at_label": "hace 2 días simulados",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "synthetic-case-1027",
            "case_code": "JN-DEMO-1027",
            "title": "Poda preventiva solicitada",
            "category": "Arbolado",
            "status": "new",
            "priority": "baja",
            "channel": "telefono",
            "zone": "Sur",
            "sla_status": "in_target",
            "opened_at_label": "10:55 del día simulado",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
        {
            "id": "synthetic-case-1019",
            "case_code": "JN-DEMO-1019",
            "title": "Revisión de señalización",
            "category": "Tránsito",
            "status": "assigned",
            "priority": "media",
            "channel": "presencial",
            "zone": "Oeste",
            "sla_status": "at_risk",
            "opened_at_label": "hace 3 días simulados",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
    ]


def _synthetic_channel_summary() -> dict[str, Any]:
    return {
        "contract_version": EXECUTIVE_CHANNEL_SUMMARY_CONTRACT_VERSION,
        "data_mode": _SYNTHETIC_DATA_MODE,
        "total_interactions": 598,
        "channels": [
            {"id": "whatsapp", "label": "WhatsApp", "value": 326, "share_pct": 54.5},
            {"id": "web", "label": "Web", "value": 168, "share_pct": 28.1},
            {"id": "telefono", "label": "Teléfono", "value": 68, "share_pct": 11.4},
            {"id": "presencial", "label": "Presencial", "value": 36, "share_pct": 6.0},
        ],
        "whatsapp": {
            "conversations": 326,
            "first_response_minutes": 3.4,
            "resolved_without_handoff_pct": 68,
        },
        "label": "Distribución de canales del escenario demostrativo",
    }


def _session_cases(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        cases.append(
            {
                "id": str(item.get("id") or f"session-case-{index}"),
                "case_code": str(item.get("ticket_code") or item.get("ticket_id") or f"SESION-{index:02d}"),
                "title": str(item.get("label") or "Caso generado en esta sesión"),
                "category": item.get("category"),
                "status": item.get("status"),
                "priority": None,
                "channel": "sesion_demo",
                "zone": None,
                "sla_status": "not_available",
                "opened_at_label": "Actividad de esta sesión",
                "data_mode": _SESSION_DATA_MODE,
            }
        )
    return cases


def apply_gobierno_executive_snapshot(payload: dict[str, Any], *, tenant_slug: str) -> dict[str, Any]:
    """Add an honest executive view without persisting or blending data sources."""

    result = deepcopy(payload)
    result["presentation_mode"] = EXECUTIVE_PRESENTATION_MODE
    frontend_contract = dict(result.get("frontend_contract") or {})
    frontend_contract["presentation_mode"] = EXECUTIVE_PRESENTATION_MODE
    result["frontend_contract"] = frontend_contract
    result["survey_voting"] = deepcopy(result.get("survey_voting") or {})

    session_activity = dict(result.get("session_activity") or {})
    if session_activity.get("has_session_data"):
        items = [dict(item) for item in (session_activity.get("items") or []) if isinstance(item, dict)]
        result["data_provenance"] = _executive_provenance(
            mode=_MIXED_PARTITIONED_DATA_MODE,
            tenant_slug=tenant_slug,
        )
        result["cases"] = _session_cases(items)
        result["channel_summary"] = {
            "contract_version": EXECUTIVE_CHANNEL_SUMMARY_CONTRACT_VERSION,
            "data_mode": _SESSION_DATA_MODE,
            "total_interactions": None,
            "total_cases": len(items),
            "observed_cases": len(items),
            "channels": [],
            "whatsapp": {
                "conversations": None,
                "first_response_minutes": None,
                "resolved_without_handoff_pct": None,
            },
            "label": "Solo actividad observada en esta sesión; no se infieren métricas agregadas.",
        }
        operations = dict(result.get("operations") or {})
        operations["data_policy"] = "session_events_only"
        operations["setup_message"] = (
            "Vista ejecutiva limitada a eventos reales de esta sesión demo; "
            "no se mezclan valores del escenario sintético."
        )
        result["operations"] = operations
        return result

    result["data_provenance"] = _executive_provenance(
        mode=_SYNTHETIC_DATA_MODE,
        tenant_slug=tenant_slug,
    )
    survey_summary = _survey_snapshot_summary(result["survey_voting"])
    result["cards"] = _synthetic_cards(survey_summary)
    result["metrics"] = _synthetic_metrics(survey_summary)
    result["timeline"] = _synthetic_timeline(survey_summary)
    result["map"] = _synthetic_map()
    result["cases"] = _synthetic_cases()
    result["case_sample"] = {
        "contract_version": "demo.case_sample.v1",
        "sample": True,
        "total_cases": 184,
        "displayed_cases": len(result["cases"]),
        "represented_cases_on_map": 52,
        "label": (
            "Muestra de casos del escenario sintético; no representa el universo "
            "de reclamos de un municipio."
        ),
    }
    result["channel_summary"] = _synthetic_channel_summary()

    modules: list[dict[str, Any]] = []
    for module in result.get("modules") or []:
        cloned = dict(module)
        cloned["data_mode"] = _SYNTHETIC_DATA_MODE
        if cloned.get("id") == "heatmap":
            cloned["enabled"] = True
            cloned["label"] = "Mapa demostrativo"
            cloned.pop("empty_state", None)
        modules.append(cloned)
    result["modules"] = modules

    session_activity["has_session_data"] = False
    session_activity["items"] = []
    session_activity["empty_state"] = (
        "No hay actividad real vinculada a esta sesión. La vista ejecutiva usa un "
        "escenario sintético claramente identificado."
    )
    result["session_activity"] = session_activity

    operations = dict(result.get("operations") or {})
    operations["data_policy"] = _SYNTHETIC_DATA_MODE
    operations["setup_message"] = (
        "Escenario sintético para demostrar capacidades del producto; "
        "no contiene estadísticas municipales reales."
    )
    result["operations"] = operations
    return result
