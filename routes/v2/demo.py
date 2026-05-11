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
from services.demo_pillar_catalog import (
    DEMO_PILLAR_CONTRACT_VERSION,
    catalog_resources_for_rubro,
    curated_demo_rubros,
    default_rubro_for_sector,
    demo_pillars,
    demo_pillar_keys,
    normalize_demo_sector,
    sector_for_rubro,
)
from services.education_contracts import (
    build_education_admin_menu,
    build_education_profile,
    build_education_whatsapp_playbook,
    fold_text,
    is_education_tenant,
)
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
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
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


def _payload_slug(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("slug") or value.get("key") or value.get("id") or value.get("label")
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _payload_tenant_slug(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("slug") or value.get("tenant_slug") or value.get("key") or value.get("id")
    return str(value or "").strip().lower()


def _first_payload_slug(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _payload_slug(data.get(key))
        if value:
            return value
    return ""


def _first_payload_tenant_slug(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _payload_tenant_slug(data.get(key))
        if value:
            return value
    return ""


def _first_education_tenant_for_demo() -> TenantProfile | None:
    candidates = TenantProfile.query.filter_by(is_active=True).order_by(TenantProfile.id.asc()).all()
    for tenant in candidates:
        if is_education_tenant(tenant):
            return tenant
    return _first_active_tenant_for_demo("pyme")


def _normalize_rubro(item: dict[str, Any]) -> dict[str, Any]:
    slug = item.get("slug") or item.get("key") or item.get("tenant_slug")
    label = item.get("label") or slug
    text = " ".join([fold_text(slug), fold_text(label), fold_text(item.get("vertical"))])
    is_education = any(keyword in text for keyword in ("colegio", "escuela", "educacion", "instituto", "jardin"))
    sector = "educacion" if is_education else item.get("sector") or sector_for_rubro(slug)
    return {
        "slug": slug,
        "key": item.get("key") or slug,
        "label": label,
        "tipo_chat": item.get("tipo_chat"),
        "tenant_slug": item.get("tenant_slug") or slug,
        "vertical": "educacion" if is_education else item.get("vertical"),
        "subvertical": item.get("subvertical"),
        "sector": sector,
        "pillar": item.get("pillar") or sector,
        "resources": item.get("resources") or catalog_resources_for_rubro(slug, sector),
        "sample_prompts": item.get("sample_prompts") or [],
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
    vertical: str | None = None,
    education_profile: dict[str, Any] | None = None,
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
            "vertical": vertical,
            "education_profile": education_profile,
            "demo_session_id": demo_session_id,
            "demo_mode": True,
        },
        "context": {
            "sector": sector,
            "rubro": canonical_rubro,
            "tenant_slug": tenant.slug,
            "tenant_tipo": tenant_type,
            "vertical": vertical,
            "demo_session_id": demo_session_id,
        },
        "start_event": {
            "type": "demo_chat_start",
            "tenant_slug": tenant.slug,
            "tipo_chat": tenant_type,
            "vertical": vertical,
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
    seen_slugs = {str(item.get("slug") or item.get("key") or "").lower() for item in rubros}
    for curated in curated_demo_rubros():
        curated_slug = str(curated.get("slug") or curated.get("key") or "").lower()
        if curated_slug and curated_slug not in seen_slugs:
            rubros.append(_normalize_rubro(curated))
            seen_slugs.add(curated_slug)

    def _is_education_rubro(rubro: dict[str, Any]) -> bool:
        inferred_sector = sector_for_rubro(rubro.get("slug") or rubro.get("key") or rubro.get("label"))
        return (
            (rubro.get("vertical") or "").lower() == "educacion"
            or (rubro.get("sector") or "").lower() == "educacion"
            or inferred_sector == "educacion"
            or any(
                keyword in fold_text(rubro.get("label") or rubro.get("slug"))
                for keyword in ("colegio", "escuela", "educacion", "instituto", "jardin")
            )
        )

    educacion = [
        r
        for r in rubros
        if _is_education_rubro(r)
    ]
    gobierno = [r for r in rubros if (r.get("tipo_chat") or "").lower() == "municipio" and not _is_education_rubro(r)]
    empresas = [r for r in rubros if (r.get("tipo_chat") or "").lower() == "pyme" and not _is_education_rubro(r)]
    if not educacion:
        educacion = [
            {
                "slug": "colegios",
                "key": "colegios",
                "label": "Colegios",
                "tipo_chat": "pyme",
                "tenant_slug": "colegios",
                "vertical": "educacion",
                "sector": "educacion",
            }
        ]
    pillars = demo_pillars()
    pillar_categories = {pillar.get("key"): pillar.get("categories") or [] for pillar in pillars}

    return jsonify(
        {
            "contract_version": "demo.catalog.v2",
            "pillar_contract_version": DEMO_PILLAR_CONTRACT_VERSION,
            "sectors": ["gobierno", "empresas", "educacion"],
            "pillars": pillars,
            "rubros": rubros,
            "sector_groups": [
                {
                    "key": "gobierno",
                    "label": "Gobiernos",
                    "tenant_slug": "municipio",
                    "default_rubro": default_rubro_for_sector("gobierno"),
                    "rubros": gobierno,
                    "categories": pillar_categories.get("gobierno", []),
                },
                {
                    "key": "empresas",
                    "label": "Empresas",
                    "tenant_slug": "bodega",
                    "default_rubro": default_rubro_for_sector("empresas"),
                    "rubros": empresas,
                    "categories": pillar_categories.get("empresas", []),
                },
                {
                    "key": "educacion",
                    "label": "Colegios",
                    "tenant_slug": "colegio-demo",
                    "default_rubro": default_rubro_for_sector("educacion"),
                    "rubros": educacion,
                    "categories": pillar_categories.get("educacion", []),
                },
            ],
        }
    )


@v2_demo_bp.route("/session", methods=["POST"])
def demo_session_v2():
    data = request.get_json(silent=True) or {}
    sector = normalize_demo_sector(
        data.get("sector")
        or data.get("pillar")
        or data.get("segment")
        or data.get("vertical")
        or ""
    )
    rubro = _first_payload_slug(
        data,
        "rubro",
        "rubro_slug",
        "rubro_key",
        "rubro_clave",
        "category",
        "category_slug",
        "demo_rubro",
        "subvertical",
    )
    tenant_slug = _first_payload_tenant_slug(data, "tenant_slug", "tenant", "slug")

    if not sector:
        sector = sector_for_rubro(rubro) or "empresas"
    if sector not in set(demo_pillar_keys()):
        inferred_sector = sector_for_rubro(sector) or sector_for_rubro(rubro)
        sector = inferred_sector or sector

    if sector not in {"gobierno", "empresas", "educacion"}:
        return _error_response("sector debe ser 'gobierno', 'empresas' o 'educacion'", 400, "validation_error", "send_valid_sector")

    if not rubro and not tenant_slug:
        rubro = default_rubro_for_sector(sector)

    tenant = None
    if tenant_slug:
        try:
            tenant = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=True)
        except Exception:
            tenant = None
        if not tenant:
            if sector == "educacion" or tenant_slug in {"colegio-demo", "colegios", "colegio"}:
                tenant = _first_education_tenant_for_demo() or _first_active_tenant_for_demo("pyme")
            elif sector == "gobierno" or tenant_slug in {"municipio", "municipios"}:
                tenant = _first_active_tenant_for_demo("municipio")
            elif sector == "empresas" or tenant_slug in {"bodega", "empresa", "pyme"}:
                tenant = _first_active_tenant_for_demo("pyme")
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
            if sector == "educacion":
                tenant = _first_education_tenant_for_demo()
            else:
                requested_tipo = "municipio" if sector == "gobierno" else "pyme"
                tenant = _first_active_tenant_for_demo(requested_tipo)
        if not tenant:
            return _error_response("No se pudo resolver tenant demo", 404, "tenant_resolution_failed", "send_tenant_slug")

    tenant_type = (tenant.tipo or "pyme").strip().lower()
    education_profile = build_education_profile(tenant, rubro_label=tenant.nombre)
    vertical = "educacion" if sector == "educacion" or education_profile.get("is_education") else tenant.vertical
    experience = build_demo_experience_contract(
        tenant_type=tenant_type,
        rubro_label=tenant.nombre,
        vertical=vertical,
        subvertical=tenant.subvertical,
        education_profile=education_profile if education_profile.get("is_education") else None,
    )
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
        vertical=vertical,
        education_profile=education_profile if education_profile.get("is_education") else None,
    )
    education_payload = None
    if education_profile.get("is_education"):
        education_payload = {
            "profile": education_profile,
            "whatsapp_playbook": build_education_whatsapp_playbook(tenant),
            "admin_menu": build_education_admin_menu(tenant),
            "quick_menu": experience.get("education_quick_menu") or [],
        }

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
        "education": education_payload,
        "pillar_selector": {
            "contract_version": DEMO_PILLAR_CONTRACT_VERSION,
            "selected_sector": sector,
            "selected_rubro": rubro or tenant.slug,
            "pillars": demo_pillars(),
        },
        "catalog_resources": catalog_resources_for_rubro(rubro or tenant.slug, sector),
    }

    return _json_response(
        {
            "contract_version": "demo.session.v2",
            "demo_session_id": demo_session_id,
            "session_id": demo_session_id,
            "tenant_slug": tenant.slug,
            "tenant": _tenant_dict(tenant),
            "workspace": workspace,
            "pillar_selector": workspace["pillar_selector"],
            "catalog_resources": workspace["catalog_resources"],
            "chat_bootstrap": chat_bootstrap,
            "experience_blueprint": experience,
            "first_visit": workspace["first_visit"],
            "sample_conversations": workspace["sample_conversations"],
            "trust_signals": workspace["trust_signals"],
            "lead_capture": workspace["lead_capture"],
            "media_capabilities": media_capabilities,
            "conversion_ctas": conversion_ctas,
            "animation_tokens": animation_tokens,
            "education": education_payload,
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
                "education": education_payload,
            },
            "quick_replies": quick_replies,
        }
    )
