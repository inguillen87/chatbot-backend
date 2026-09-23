from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote_plus

from services.demo_pillar_catalog import catalog_resources_for_rubro, curated_demo_rubros, normalize_demo_sector
from services.demo_surveys import (
    DEFAULT_DEMO_PUBLIC_FRONTEND_ORIGIN,
    build_demo_surveys_votings_contract,
)


DEFAULT_SANDBOX_MESSAGE_LIMIT = 10


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _display_number(number: str) -> str:
    digits = _digits(number)
    if digits == "14155238886":
        return "+1 (415) 523-8886"
    return str(number or "").strip()


def _scenario_scripts(sector: str, rubro: str) -> list[dict[str, Any]]:
    normalized = normalize_demo_sector(sector)
    if normalized == "educacion":
        return [
            {
                "id": "school_absence_certificate",
                "label": "Inasistencia con certificado",
                "messages": [
                    "Hola, soy Marcela.",
                    "Necesito justificar la inasistencia de Sofia de 3A.",
                    "Te mando foto del certificado medico.",
                ],
                "expected_result": "caso_escolar",
                "inputs": ["text", "image", "file"],
            },
            {
                "id": "school_vote_live",
                "label": "Encuesta o votacion escolar",
                "messages": [
                    "Quiero participar en una votacion del colegio.",
                    "Voto por extender el horario de biblioteca.",
                ],
                "expected_result": "survey_response",
                "inputs": ["text"],
            },
        ]
    if normalized == "gobierno":
        return [
            {
                "id": "municipal_claim_location",
                "label": "Reclamo con ubicacion",
                "messages": [
                    "Hola, soy Marcelo.",
                    "Quiero iniciar un reclamo por alumbrado publico.",
                    "Te comparto ubicacion y una foto de la luminaria.",
                ],
                "expected_result": "ticket",
                "inputs": ["text", "image", "audio", "location"],
            },
            {
                "id": "municipal_live_vote",
                "label": "Votacion ciudadana",
                "messages": [
                    "Quiero responder una encuesta del municipio.",
                    "Mi prioridad es mejorar luminarias y plazas.",
                ],
                "expected_result": "survey_response",
                "inputs": ["text", "location"],
            },
        ]
    if str(rubro or "").strip().lower() in {"bodega", "vinoteca", "winery"}:
        return [
            {
                "id": "winery_order",
                "label": "Pedido de vinos",
                "messages": [
                    "Hola, quiero ver catalogo y precios.",
                    "Armame un pedido con 2 Malbec Reserva y una caja degustacion.",
                    "Necesito envio a Mendoza centro.",
                ],
                "expected_result": "order_or_cart",
                "inputs": ["text", "location"],
            },
            {
                "id": "winery_catalog_file",
                "label": "Catalogo PDF/Excel",
                "messages": [
                    "Tenes lista de precios en PDF o Excel?",
                    "Quiero compartir el catalogo con mi socio.",
                ],
                "expected_result": "catalog_resource",
                "inputs": ["text", "file"],
            },
            {
                "id": "commerce_live_vote",
                "label": "Encuesta de clientes",
                "messages": [
                    "Quiero ver encuestas de clientes.",
                    "Voto por la promo de envio bonificado.",
                ],
                "expected_result": "survey_response",
                "inputs": ["text"],
            },
        ]
    return [
        {
            "id": "commerce_catalog_order",
            "label": "Catalogo y pedido",
            "messages": [
                "Hola, quiero ver catalogo y precios.",
                "Necesito armar un pedido y saber el total.",
                "Te paso mi ubicacion para coordinar envio.",
            ],
            "expected_result": "order_or_cart",
            "inputs": ["text", "location"],
        },
        {
            "id": "commerce_visual_support",
            "label": "Consulta con foto",
            "messages": [
                "Te mando una foto para que me recomiendes una opcion.",
                "Quiero precio, stock y alternativas.",
            ],
            "expected_result": "lead_or_order",
            "inputs": ["text", "image"],
        },
        {
            "id": "commerce_live_vote",
            "label": "Encuesta de clientes",
            "messages": [
                "Quiero responder una encuesta del negocio.",
                "Mi prioridad es mejor precio y entrega rapida.",
            ],
            "expected_result": "survey_response",
            "inputs": ["text"],
        },
    ]


def _rubro_options(sector: str, limit: int = 12) -> list[dict[str, Any]]:
    normalized = normalize_demo_sector(sector)
    options: list[dict[str, Any]] = []
    for item in curated_demo_rubros():
        if normalize_demo_sector(item.get("sector")) != normalized:
            continue
        options.append(
            {
                "slug": item.get("slug"),
                "label": item.get("label"),
                "sector": item.get("sector"),
                "tipo_chat": item.get("tipo_chat"),
                "vertical": item.get("vertical"),
                "subvertical": item.get("subvertical"),
            }
        )
        if len(options) >= limit:
            break
    return options


def demo_trial_policy(max_messages: int = DEFAULT_SANDBOX_MESSAGE_LIMIT) -> dict[str, Any]:
    return {
        "contract_version": "demo.trial_policy.v1",
        "enabled": True,
        "enforced": True,
        "max_messages": max_messages,
        "scope": "anonymous_or_sandbox_demo",
        "free_inputs": ["text", "image", "audio", "location", "file"],
        "channels": {
            "widget_chat": {"max_messages": max_messages},
            "whatsapp_sandbox": {"max_messages": max_messages},
            "realtime_voice": {"max_sessions": 3, "window_seconds": 86400},
            "realtime_video": {"max_sessions": 1, "window_seconds": 86400},
        },
        "rules": [
            "No requiere usuario ni contrasena para iniciar demo.",
            "El backend debe conservar anon_id/chat_session_id para no perder contexto.",
            "Al llegar al limite, mostrar captura de lead o CTA comercial.",
        ],
        "limit_reached_reason_code": "demo_message_limit_reached",
        "upgrade_required_after_limit": True,
        "upgrade": {
            "required": True,
            "lead_capture_endpoint": "/api/public/lead-capture",
            "lead_capture_fields": ["name", "phone_or_email", "message", "tenant_slug", "sector"],
            "reason_code": "demo_message_limit_reached",
        },
    }


def build_demo_whatsapp_sandbox_contract(
    *,
    tenant_slug: str,
    sector: str,
    rubro: str,
    sandbox_number: str,
    join_phrase: str,
    source: str = "demo_profile_panel",
    max_messages: int = DEFAULT_SANDBOX_MESSAGE_LIMIT,
    provider: str = "twilio_sandbox",
    whatsapp_playbook: Mapping[str, Any] | None = None,
    education: Mapping[str, Any] | None = None,
    public_base_url: str | None = None,
) -> dict[str, Any]:
    normalized_sector = normalize_demo_sector(sector)
    rubro_slug = str(rubro or tenant_slug or "").strip().lower()
    sandbox_number = str(sandbox_number or "").strip() or "+14155238886"
    if sandbox_number.startswith("whatsapp:"):
        sandbox_number = sandbox_number.replace("whatsapp:", "", 1)
    join_phrase = str(join_phrase or "join brief-yesterday").strip()
    wa_number = _digits(sandbox_number)
    requires_join_phrase = provider == "twilio_sandbox"
    activation_message = join_phrase if requires_join_phrase else f"Hola, quiero probar la demo de {rubro_slug}."
    wa_deeplink = f"https://wa.me/{wa_number}?text={quote_plus(activation_message)}" if wa_number else None
    resources = catalog_resources_for_rubro(rubro_slug, normalized_sector)
    survey_contract = build_demo_surveys_votings_contract(
        sector=normalized_sector,
        tenant_slug=tenant_slug,
        rubro=rubro_slug,
        public_base_url=public_base_url or DEFAULT_DEMO_PUBLIC_FRONTEND_ORIGIN,
    )

    contract = {
        "contract_version": "demo.whatsapp_sandbox.v1",
        "enabled": bool(wa_number and join_phrase),
        "source": source,
        "tenant_slug": tenant_slug,
        "sector": normalized_sector,
        "rubro": rubro_slug,
        "provider": provider,
        "sandbox": {
            "number": f"whatsapp:{sandbox_number}",
            "display_number": _display_number(sandbox_number),
            "join_phrase": join_phrase if requires_join_phrase else None,
            "activation_message": activation_message,
            "wa_deeplink": wa_deeplink,
            "qr_url": f"https://api.qrserver.com/v1/create-qr-code/?size=220x220&data={quote_plus(wa_deeplink)}" if wa_deeplink else None,
            "requires_join_phrase": requires_join_phrase,
        },
        "trial_policy": demo_trial_policy(max_messages=max_messages),
        "rubro_options": _rubro_options(normalized_sector),
        "scenario_scripts": _scenario_scripts(normalized_sector, rubro_slug),
        "supported_inputs": {
            "text": True,
            "image": True,
            "audio": True,
            "location": True,
            "file": True,
        },
        "expected_results": [
            "ticket",
            "order_or_cart",
            "school_case",
            "lead",
            "survey_response",
            "tracking_view",
        ],
        "catalog": {
            "enabled": bool(resources),
            "resources": resources,
            "public_catalog_endpoint": f"/api/public/tenants/{tenant_slug}/catalog",
            "pdf_excel_upload_demo": {
                "enabled": normalized_sector == "empresas",
                "supported_formats": ["pdf", "xlsx", "csv"],
                "admin_upload_endpoint": "/catalogo/upload",
            },
        },
        "surveys_votings": {
            **survey_contract,
            "enabled": normalized_sector in {"gobierno", "educacion", "empresas"},
            "respond_endpoint_template": "/api/public/encuestas/v1/{survey_slug}/responder",
            "live_results_endpoint_template": "/api/public/encuestas/v1/{survey_slug}/live-results",
        },
        "chat_runtime": {
            "requires_chat_session_id": True,
            "requires_demo_session_id": True,
            "max_messages": max_messages,
            "backend_response_contract": "chat.response.v1",
        },
        "frontend_contract": {
            "render_as": "whatsapp_sandbox_demo_launcher",
            "allow_rubro_switch": True,
            "show_qr": True,
            "show_copy_join_phrase": True,
            "show_scenario_scripts": True,
            "show_trial_counter": True,
        },
    }
    if whatsapp_playbook:
        contract["whatsapp_playbook"] = dict(whatsapp_playbook)
    if education:
        contract["education"] = dict(education)
    return contract


def sandbox_context_from_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tenant_slug": contract.get("tenant_slug"),
        "sector": contract.get("sector"),
        "rubro": contract.get("rubro"),
        "quick_menu": contract.get("scenario_scripts") or [],
        "widget_config_endpoint": f"/api/public/tenants/{contract.get('tenant_slug')}/widget-config",
        "trial_policy": contract.get("trial_policy"),
        "supported_inputs": contract.get("supported_inputs"),
        "catalog": contract.get("catalog"),
        "surveys_votings": contract.get("surveys_votings"),
    }
