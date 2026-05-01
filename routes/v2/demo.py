from __future__ import annotations

from typing import Any
import uuid

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from models import TenantProfile
from routes.auth import (
    _first_active_tenant_for_demo,
    _resolve_demo_tenant_slug,
    demo_catalog as legacy_demo_catalog,
)
from services.tenant_resolver import resolve_tenant_only
from services.demo_experience_contract import build_demo_experience_contract
from services.demo_registry import load_demo_rubros
from routes.v2.tenants import create_demo_session_token

v2_demo_bp = Blueprint("v2_demo", __name__, url_prefix="/api/v2/demo")


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(message: str, status_code: int, reason_code: str, action_hint: str):
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": False,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
            "message": message,
        },
        status_code,
    )


def _tenant_dict(tenant: TenantProfile) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
    }


def _safe_demo_rubros() -> list[dict[str, Any]]:
    items = []
    for rubro in load_demo_rubros(require_owner=False):
        items.append(
            {
                "key": rubro.key,
                "label": rubro.label,
                "tipo_chat": rubro.tipo_chat,
                "tenant_slug": getattr(rubro, "tenant_slug", None) or rubro.key,
            }
        )
    return items


def _normalize_rubro(item: dict[str, Any]) -> dict[str, Any]:
    slug = item.get("slug") or item.get("key") or item.get("tenant_slug")
    return {
        "slug": slug,
        "key": item.get("key") or slug,
        "label": item.get("label") or slug,
        "tipo_chat": item.get("tipo_chat"),
        "tenant_slug": item.get("tenant_slug") or slug,
    }


def _quick_reply_items(prompts: list[Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, prompt in enumerate(prompts, start=1):
        if isinstance(prompt, dict):
            label = str(prompt.get("label") or prompt.get("title") or prompt.get("payload") or "").strip()
            payload = str(prompt.get("payload") or prompt.get("intent") or label).strip()
            item_id = str(prompt.get("id") or prompt.get("key") or f"quick_{index}").strip()
        else:
            label = str(prompt or "").strip()
            payload = label
            item_id = f"quick_{index}"
        if not label:
            continue
        items.append({"id": item_id, "label": label, "payload": payload})
    return items


def _workspace_cards(experience: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for action in experience.get("quick_actions") or []:
        if not isinstance(action, dict):
            continue
        cards.append(
            {
                "key": action.get("id") or action.get("key"),
                "title": action.get("label") or action.get("title"),
                "description": action.get("description") or action.get("intent") or "",
                "status": action.get("status") or "ready",
                "cta_label": action.get("cta_label") or "Abrir",
                "id": action.get("id") or action.get("key"),
                "label": action.get("label") or action.get("title"),
                "intent": action.get("intent"),
                "icon": action.get("icon"),
            }
        )
    return cards


def _handoff_labels(experience: dict[str, Any]) -> dict[str, str]:
    configured = experience.get("handoff_labels")
    labels = configured if isinstance(configured, dict) else {}
    return {
        "message": str(labels.get("message") or "Si hace falta, derivamos la conversacion."),
        "createTicket": str(labels.get("createTicket") or "Crear caso"),
        "openWhatsApp": str(labels.get("openWhatsApp") or "Continuar por WhatsApp"),
        "waitOperator": str(labels.get("waitOperator") or "Esperar respuesta"),
        "contact": str(labels.get("contact") or "Hablar con un asesor"),
        "whatsapp": str(labels.get("whatsapp") or "Seguir por WhatsApp"),
        "demo_limit": str(labels.get("demo_limit") or "Activar plan Full"),
    }


@v2_demo_bp.route('/catalog', methods=['GET'])
def demo_catalog_v2():
    # Reuse legacy catalog generation, then sanitize/reshape into v2 contract.
    legacy_response = legacy_demo_catalog()
    legacy_payload = legacy_response.get_json(silent=True) if hasattr(legacy_response, "get_json") else {}

    rubros = []
    for item in (legacy_payload or {}).get("tenant_demos") or []:
        rubros.append(_normalize_rubro(item))
    if not rubros:
        rubros = [_normalize_rubro(item) for item in _safe_demo_rubros()]
    gobierno = [r for r in rubros if (r.get("tipo_chat") or "").lower() == "municipio"]
    empresas = [r for r in rubros if (r.get("tipo_chat") or "").lower() == "pyme"]

    payload = {
        "contract_version": "demo.catalog.v2",
        "sectors": ["gobierno", "empresas"],
        "rubros": rubros,
        "sector_groups": [
            {
                "key": "gobierno",
                "label": "Gobiernos y municipios",
                "rubros": gobierno,
            },
            {
                "key": "empresas",
                "label": "Empresas y pymes",
                "rubros": empresas,
            },
        ]
    }
    return jsonify(payload)


@v2_demo_bp.route('/session', methods=['POST'])
def demo_session_v2():
    data = request.get_json(silent=True) or {}
    sector = str(data.get("sector") or "").strip().lower()
    rubro = str(data.get("rubro") or "").strip().lower()
    tenant_slug = str(data.get("tenant_slug") or "").strip().lower()

    if sector not in {"gobierno", "empresas"}:
        return _error_response("sector debe ser 'gobierno' o 'empresas'", 400, "validation_error", "send_valid_sector")

    if not rubro and not tenant_slug:
        return _error_response("rubro o tenant_slug es obligatorio", 400, "validation_error", "send_rubro_or_tenant_slug")

    tenant = None
    if tenant_slug:
        try:
            tenant = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=True)
        except Exception:
            tenant = None
        if not tenant:
            return _error_response("Tenant no encontrado", 404, "tenant_not_found", "check_tenant_slug")
    else:
        candidate = _resolve_demo_tenant_slug(rubro)
        if candidate:
            try:
                tenant = resolve_tenant_only(tenant_slug=candidate, require_explicit_slug=True)
            except Exception:
                tenant = None
        if not tenant:
            requested_tipo = "municipio" if sector == "gobierno" else "pyme"
            tenant = _first_active_tenant_for_demo(requested_tipo)
        if not tenant:
            return _error_response("No se pudo resolver tenant demo", 404, "tenant_resolution_failed", "send_tenant_slug")

    tenant_type = (tenant.tipo or "pyme").strip().lower()
    experience = build_demo_experience_contract(
        tenant_type=tenant_type,
        rubro_label=tenant.nombre,
    )
    onboarding = experience.get("guided_onboarding") or {}
    quick_replies = _quick_reply_items(onboarding.get("starter_prompts") or [])

    demo_session_id = create_demo_session_token(
        tenant_slug=tenant.slug,
        sector=sector,
        rubro=rubro or tenant.slug,
    )

    workspace = {
        "title": tenant.nombre or "Demo Chatboc",
        "welcome_message": onboarding.get("entry_prompt") or "¿Sobre qué te gustaría preguntar primero?",
        "quick_replies": quick_replies,
        "value_cards": _workspace_cards(experience),
        "handoff_labels": _handoff_labels(experience),
    }

    return _json_response(
        {
            "contract_version": "demo.session.v2",
            "demo_session_id": demo_session_id,
            "session_id": demo_session_id,
            "tenant_slug": tenant.slug,
            "tenant": _tenant_dict(tenant),
            "workspace": workspace,
            "welcome_message": workspace["welcome_message"],
            "value_cards": workspace["value_cards"],
            "handoff_labels": workspace["handoff_labels"],
            "chat_seed": {
                "entry_prompt": workspace["welcome_message"],
                "autostart_chat": bool(onboarding.get("autostart_chat", True)),
                "open_widget": bool(onboarding.get("open_widget", True)),
            },
            "quick_replies": quick_replies,
        }
    )
