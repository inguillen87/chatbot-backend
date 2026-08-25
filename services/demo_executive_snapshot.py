from __future__ import annotations

from copy import deepcopy
from typing import Any


EXECUTIVE_PRESENTATION_MODE = "executive"
EXECUTIVE_PROVENANCE_CONTRACT_VERSION = "demo.executive_provenance.v1"
EXECUTIVE_CHANNEL_SUMMARY_CONTRACT_VERSION = "demo.channel_summary.v1"

_SYNTHETIC_DATA_MODE = "synthetic_demo_scenario"
_SESSION_DATA_MODE = "session_generated_events"
_EXECUTIVE_SCENARIO_TIMESTAMPS = {
    "server_time": "2026-08-25T12:00:00+00:00",
    "inicio_at": "2026-08-25T12:00:00+00:00",
    "fin_at": "2026-09-24T12:00:00+00:00",
}


def normalize_demo_presentation_mode(value: Any) -> str:
    """Resolve the opt-in presentation mode without changing legacy responses."""

    normalized = str(value or "").strip().lower()
    return EXECUTIVE_PRESENTATION_MODE if normalized == EXECUTIVE_PRESENTATION_MODE else ""


def _executive_provenance(*, mode: str, tenant_slug: str) -> dict[str, Any]:
    synthetic = mode == _SYNTHETIC_DATA_MODE
    label = (
        "Escenario demostrativo sintético de Junín. No representa datos municipales "
        "reales ni debe usarse para tomar decisiones de gobierno."
        if synthetic
        else (
            "El panel ejecutivo muestra únicamente actividad real generada en esta "
            "sesión demo. Las encuestas sembradas conservan su procedencia sintética "
            "y no se computan como estadísticas municipales oficiales."
        )
    )
    provenance = {
        "contract_version": EXECUTIVE_PROVENANCE_CONTRACT_VERSION,
        "scope": "executive_snapshot",
        "mode": mode,
        "synthetic": synthetic,
        "municipal_truth": False,
        "suitable_for_product_demonstration": True,
        "suitable_for_government_decisions": False,
        "label": label,
        "tenant_scope": str(tenant_slug or "municipio").strip().lower() or "municipio",
        "scenario_scope": "Junín, Mendoza" if synthetic else "Sesión demo validada",
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


def _stabilize_synthetic_contract(value: Any) -> Any:
    """Replace request-time clocks inside seeded survey data for stable demos."""

    if isinstance(value, dict):
        return {
            key: (
                _EXECUTIVE_SCENARIO_TIMESTAMPS[key]
                if key in _EXECUTIVE_SCENARIO_TIMESTAMPS
                else _stabilize_synthetic_contract(nested)
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_stabilize_synthetic_contract(item) for item in value]
    return value


def _synthetic_cards() -> list[dict[str, Any]]:
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
            "label": "Participaciones en encuesta",
            "value": "1.248",
            "detail": "Votos válidos del escenario demostrativo",
            "period": "consulta barrial simulada",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
    ]


def _synthetic_metrics() -> list[dict[str, Any]]:
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
            "value": 1248,
            "unit": "votos válidos",
            "detail": "Prioridades barriales; luminarias lidera con 42%",
            "period": "consulta simulada",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
    ]


def _synthetic_timeline() -> list[dict[str, Any]]:
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
            "detail": "1.248 participaciones válidas en la consulta simulada",
            "status": "in_progress",
            "channel": "survey",
            "data_mode": _SYNTHETIC_DATA_MODE,
        },
    ]


def _synthetic_map() -> dict[str, Any]:
    return {
        "enabled": True,
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
                "lat": -33.1513,
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
                "lat": -33.1560,
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
    result["survey_voting"] = _stabilize_synthetic_contract(result.get("survey_voting") or {})

    session_activity = dict(result.get("session_activity") or {})
    if session_activity.get("has_session_data"):
        items = [dict(item) for item in (session_activity.get("items") or []) if isinstance(item, dict)]
        result["data_provenance"] = _executive_provenance(
            mode=_SESSION_DATA_MODE,
            tenant_slug=tenant_slug,
        )
        result["cases"] = _session_cases(items)
        result["channel_summary"] = {
            "contract_version": EXECUTIVE_CHANNEL_SUMMARY_CONTRACT_VERSION,
            "data_mode": _SESSION_DATA_MODE,
            "total_interactions": len(items),
            "observed_items": len(items),
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
    result["cards"] = _synthetic_cards()
    result["metrics"] = _synthetic_metrics()
    result["timeline"] = _synthetic_timeline()
    result["map"] = _synthetic_map()
    result["cases"] = _synthetic_cases()
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
