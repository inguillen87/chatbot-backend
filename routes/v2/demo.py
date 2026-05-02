from __future__ import annotations

from typing import Any
import uuid

from flask import Blueprint, jsonify, request

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


def _chat_endpoint_for_tenant_type(tenant_type: str) -> str:
    normalized = (tenant_type or "").strip().lower()
    if normalized == "municipio":
        return "/ask/municipio"
    if normalized == "pyme":
        return "/ask/pyme"
    return "/ask"


def _media_supports(media_capabilities: dict[str, Any]) -> dict[str, bool]:
    input_modes = media_capabilities.get("input_modes") if isinstance(media_capabilities, dict) else {}
    modes = input_modes if isinstance(input_modes, dict) else {}

    def enabled(key: str) -> bool:
        mode = modes.get(key)
        if isinstance(mode, dict):
            return bool(mode.get("enabled", True))
        return False

    return {
        "text": enabled("text"),
        "image": enabled("image"),
        "audio": enabled("audio"),
        "location": enabled("location"),
        "file": enabled("file"),
    }


def _chat_bootstrap(
    *,
    tenant: TenantProfile,
    tenant_type: str,
    sector: str,
    rubro: str,
    demo_session_id: str,
    quick_replies: list[dict[str, str]],
    media_capabilities: dict[str, Any],
) -> dict[str, Any]:
    endpoint = _chat_endpoint_for_tenant_type(tenant_type)
    canonical_rubro = (rubro or tenant.slug or tenant_type or "").strip().lower()
    first_prompt = next((item.get("payload") or item.get("label") for item in quick_replies if item.get("label")), "")

    return {
        "contract_version": "demo.chat_bootstrap.v1",
        "endpoint": endpoint,
        "fallback_endpoint": "/ask",
        "method": "POST",
        "headers": {
            "X-Chat-Session-Id": demo_session_id,
            "X-Demo-Session-Id": demo_session_id,
            "X-Tenant-Slug": tenant.slug,
        },
        "query": {
            "tenant_slug": tenant.slug,
        },
        "payload": {
            "pregunta": "",
            "tipo_chat": tenant_type,
            "tenant_slug": tenant.slug,
            "rubro": canonical_rubro,
            "rubro_clave": canonical_rubro,
            "demo_session_id": demo_session_id,
            "demo_mode": True,
        },
        "context": {
            "sector": sector,
            "rubro": canonical_rubro,
            "tenant_slug": tenant.slug,
            "tenant_tipo": tenant_type,
            "demo_session_id": demo_session_id,
        },
        "start_event": {
            "type": "demo_chat_start",
            "tenant_slug": tenant.slug,
            "tipo_chat": tenant_type,
            "rubro": canonical_rubro,
        },
        "initial_prompt": first_prompt,
        "supports": _media_supports(media_capabilities),
        "notes": [
            "Enviar siempre X-Chat-Session-Id.",
            "Para imagen/archivo subir primero a /archivos/upload/chat_attachment y luego llamar al endpoint con attachmentInfo.",
            "Para audio enviar multipart al endpoint con campo audio_file.",
            "Para ubicacion enviar payload JSON con location.",
        ],
    }


@v2_demo_bp.route("/catalog", methods=["GET"])
def demo_catalog_v2():
    legacy_response = legacy_demo_catalog()
    legacy_payload = legacy_response.get_json(silent=True) if hasattr(legacy_response, "get_json") else {}

    rubros = [_normalize_rubro(item) for item in (legacy_payload or {}).get("tenant_demos") or []]
    if not rubros:
        rubros = [_normalize_rubro(item) for item in _safe_demo_rubros()]
    gobierno = [r for r in rubros if (r.get("tipo_chat") or "").lower() == "municipio"]
    empresas = [r for r in rubros if (r.get("tipo_chat") or "").lower() == "pyme"]

    return jsonify(
        {
            "contract_version": "demo.catalog.v2",
            "sectors": ["gobierno", "empresas"],
            "rubros": rubros,
            "sector_groups": [
                {"key": "gobierno", "label": "Gobiernos y municipios", "rubros": gobierno},
                {"key": "empresas", "label": "Empresas y pymes", "rubros": empresas},
            ],
        }
    )


@v2_demo_bp.route("/session", methods=["POST"])
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
    experience = build_demo_experience_contract(tenant_type=tenant_type, rubro_label=tenant.nombre)
    onboarding = experience.get("guided_onboarding") or {}
    quick_replies = _quick_reply_items(onboarding.get("starter_prompts") or [])
    media_capabilities = experience.get("media_capabilities") or {}
    conversion_ctas = experience.get("conversion_ctas") or {}
    animation_tokens = experience.get("animation_tokens") or {}

    demo_session_id = create_demo_session_token(tenant_slug=tenant.slug, sector=sector, rubro=rubro or tenant.slug)
    chat_bootstrap = _chat_bootstrap(
        tenant=tenant,
        tenant_type=tenant_type,
        sector=sector,
        rubro=rubro or tenant.slug,
        demo_session_id=demo_session_id,
        quick_replies=quick_replies,
        media_capabilities=media_capabilities,
    )

    workspace = {
        "title": tenant.nombre or "Demo Chatboc",
        "subtitle": (experience.get("hero") or {}).get("subtitle"),
        "welcome_message": onboarding.get("entry_prompt") or "Que queres probar primero?",
        "quick_replies": quick_replies,
        "value_cards": _workspace_cards(experience),
        "handoff_labels": _handoff_labels(experience),
        "first_visit": experience.get("first_visit") or {},
        "sample_conversations": experience.get("sample_conversations") or [],
        "trust_signals": experience.get("trust_signals") or [],
        "lead_capture": experience.get("lead_capture") or {},
        "media_capabilities": media_capabilities,
        "conversion_ctas": conversion_ctas,
        "animation_tokens": animation_tokens,
        "chat_bootstrap": chat_bootstrap,
    }

    return _json_response(
        {
            "contract_version": "demo.session.v2",
            "demo_session_id": demo_session_id,
            "session_id": demo_session_id,
            "tenant_slug": tenant.slug,
            "tenant": _tenant_dict(tenant),
            "workspace": workspace,
            "chat_bootstrap": chat_bootstrap,
            "experience_blueprint": experience,
            "first_visit": workspace["first_visit"],
            "sample_conversations": workspace["sample_conversations"],
            "trust_signals": workspace["trust_signals"],
            "lead_capture": workspace["lead_capture"],
            "media_capabilities": media_capabilities,
            "conversion_ctas": conversion_ctas,
            "animation_tokens": animation_tokens,
            "welcome_message": workspace["welcome_message"],
            "value_cards": workspace["value_cards"],
            "handoff_labels": workspace["handoff_labels"],
            "chat_seed": {
                "entry_prompt": workspace["welcome_message"],
                "autostart_chat": bool(onboarding.get("autostart_chat", True)),
                "open_widget": bool(onboarding.get("open_widget", True)),
                "starter_prompts": onboarding.get("starter_prompts") or [],
                "sample_conversations": workspace["sample_conversations"],
                "media_capabilities": media_capabilities,
                "conversion_ctas": conversion_ctas,
                "chat_bootstrap": chat_bootstrap,
            },
            "quick_replies": quick_replies,
        }
    )
