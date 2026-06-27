from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from flask import current_app

from services.llm_orchestrator import build_llm_task_policy, llamar_llm_con_fallback


def _safe_json_payload(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _normalize_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _analytics_fallback(metrics: dict[str, Any], tenant_type: str) -> dict[str, Any]:
    totals = metrics.get("totals") if isinstance(metrics, dict) else {}
    return {
        "summary": (
            f"Resumen operativo {tenant_type}: tickets={totals.get('tickets', 0)}, "
            f"pedidos={totals.get('pedidos', 0)}. Revisar acciones prioritarias del panel."
        ),
        "opportunities": ["Priorizar bandeja pendiente", "Revisar segmentos con mayor actividad", "Actualizar seguimiento al usuario"],
        "threats": ["Datos insuficientes o proveedor IA no disponible"],
        "tone": "Deterministic-Fallback",
    }


def _ticket_fallback(ticket_data: dict[str, Any]) -> dict[str, Any]:
    timeline = ticket_data.get("timeline") or []
    base = ticket_data.get("pregunta") or ticket_data.get("asunto") or "Caso sin descripcion"
    last_state = ticket_data.get("estado") or "sin estado"
    return {
        "summary": f"Caso: {base}. Estado actual: {last_state}. Historial analizado: {len(timeline)} eventos.",
        "next_steps": [
            "Confirmar prioridad y responsable",
            "Actualizar al usuario con estado actual",
            "Definir criterio de cierre y seguimiento",
        ],
        "confidence": "medium",
    }


def _call_backoffice_llm(
    *,
    prompt: str,
    tenant_type: str,
    task_type: str,
    model_env_default: str = "gpt-4o-mini",
) -> tuple[dict[str, Any], dict[str, Any]]:
    response, context = llamar_llm_con_fallback(
        current_app._get_current_object(),
        prompt,
        {
            "tipo_entidad": "municipio" if tenant_type == "municipio" else "pyme",
            "channel": "admin_backoffice",
            "ai_task_type": task_type,
        },
        [],
        f"backoffice-{task_type}-{uuid4().hex[:10]}",
        model=model_env_default,
        task_type=task_type,
    )
    return response if isinstance(response, dict) else {}, context if isinstance(context, dict) else {}


def generate_backoffice_analytics_summary(metrics: dict[str, Any], *, tenant_type: str = "pyme") -> dict[str, Any]:
    prompt = (
        "Actua como analista senior de operaciones Chatboc. "
        "Devuelve solo JSON valido con keys: summary, opportunities, threats, tone. "
        "summary debe tener maximo 60 palabras. opportunities y threats deben ser arrays de 3 strings concretos. "
        f"Vertical: {tenant_type}. Datos: {_safe_json_payload(metrics)}"
    )
    try:
        report, context = _call_backoffice_llm(prompt=prompt, tenant_type=tenant_type, task_type="analytics")
        normalized = {
            "summary": report.get("summary") or report.get("message_body") or report.get("respuesta_usuario") or "",
            "opportunities": _normalize_list(report.get("opportunities")),
            "threats": _normalize_list(report.get("threats")),
            "tone": report.get("tone") or "Professional",
        }
        if not normalized["summary"]:
            raise ValueError("provider returned empty analytics summary")
        return {
            **normalized,
            "meta": {
                "task_type": "analytics",
                "provider": context.get("provider"),
                "model_used": context.get("model_used"),
                "fallback": False,
                "policy": build_llm_task_policy("analytics"),
                "secret_values_exposed": False,
            },
        }
    except Exception:
        return {
            **_analytics_fallback(metrics, tenant_type),
            "meta": {
                "task_type": "analytics",
                "provider": "deterministic_local_fallback",
                "fallback": True,
                "policy": build_llm_task_policy("analytics"),
                "secret_values_exposed": False,
            },
        }


def generate_backoffice_ticket_summary(ticket_data: dict[str, Any]) -> dict[str, Any]:
    scope = str(ticket_data.get("scope") or "municipio").lower()
    prompt = (
        "Actua como analista senior de soporte y reclamos. "
        "No inventes datos. Devuelve solo JSON valido con keys: summary, next_steps, confidence. "
        "summary maximo 80 palabras. next_steps debe ser array de 3 acciones concretas. "
        "confidence debe ser low, medium o high. "
        f"Ticket: {_safe_json_payload(ticket_data)}"
    )
    try:
        report, context = _call_backoffice_llm(prompt=prompt, tenant_type=scope, task_type="ticket_summary")
        normalized = {
            "summary": report.get("summary") or report.get("message_body") or report.get("respuesta_usuario") or "",
            "next_steps": _normalize_list(report.get("next_steps")),
            "confidence": report.get("confidence") if report.get("confidence") in {"low", "medium", "high"} else "medium",
        }
        if not normalized["summary"]:
            raise ValueError("provider returned empty ticket summary")
        if len(normalized["next_steps"]) < 3:
            normalized["next_steps"] = [*normalized["next_steps"], *_ticket_fallback(ticket_data)["next_steps"]][:3]
        return {
            **normalized,
            "meta": {
                "task_type": "ticket_summary",
                "provider": context.get("provider"),
                "model_used": context.get("model_used"),
                "fallback": False,
                "policy": build_llm_task_policy("ticket_summary"),
                "secret_values_exposed": False,
            },
        }
    except Exception:
        return {
            **_ticket_fallback(ticket_data),
            "meta": {
                "task_type": "ticket_summary",
                "provider": "deterministic_local_fallback",
                "fallback": True,
                "policy": build_llm_task_policy("ticket_summary"),
                "secret_values_exposed": False,
            },
        }
