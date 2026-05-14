from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory

from models import TenantProfile, WhatsappNumero
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
from services.demo_sandbox_contract import build_demo_whatsapp_sandbox_contract
from services.education_contracts import (
    build_education_admin_menu,
    build_education_profile,
    build_education_whatsapp_playbook,
    fold_text,
    is_education_tenant,
)
from routes.v2.tenants import create_demo_session_token

v2_demo_bp = Blueprint("v2_demo", __name__, url_prefix="/api/v2/demo")
demo_compat_bp = Blueprint("demo_compat", __name__)


def _request_id() -> str:
    incoming = (request.headers.get("X-Request-Id") or "").strip()
    return incoming or uuid.uuid4().hex


def _stable_demo_chat_session_id(demo_session_id: str | None) -> str:
    token = str(demo_session_id or "").strip()
    if not token:
        return str(uuid.uuid4())
    if len(token) <= 36:
        return token
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return f"sid_{digest}"


def _twilio_sandbox_number() -> str:
    raw = (
        current_app.config.get("TWILIO_WHATSAPP_SANDBOX_NUMBER")
        or current_app.config.get("TWILIO_SANDBOX_WHATSAPP_NUMBER")
        or current_app.config.get("TWILIO_WHATSAPP_NUMBER_SANDBOX")
        or "+14155238886"
    )
    value = str(raw or "").strip()
    if value.startswith("whatsapp:"):
        value = value.replace("whatsapp:", "", 1)
    return value or "+14155238886"


def _twilio_sandbox_join_phrase() -> str:
    return str(
        current_app.config.get("TWILIO_WHATSAPP_SANDBOX_JOIN_PHRASE")
        or current_app.config.get("TWILIO_SANDBOX_JOIN_PHRASE")
        or "join brief-yesterday"
    ).strip()


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return _with_public_cors(response)


def _with_public_cors(response):
    origin = request.headers.get("Origin") or "*"
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type,Authorization,X-Tenant-Slug,X-Widget-Token,"
        "X-Chat-Session-Id,X-Demo-Session-Id,X-Anon-Id,Anon-Id,"
        "Idempotency-Key,X-Request-Id"
    )
    response.headers["Access-Control-Expose-Headers"] = "X-Request-Id"
    if origin != "*":
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


def _options_response():
    return _json_response(
        {
            "ok": True,
            "contract_version": "demo.session.compat.v1",
        }
    )


def _request_payload() -> dict[str, Any]:
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        return payload if isinstance(payload, dict) else {}
    return dict(request.args.items())


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


def _demo_catalog_asset_response(filename: str):
    aliases = {
        "colegio-demo.pdf": ("colegios", "catalogo-demo-colegios.pdf"),
        "colegios-demo.pdf": ("colegios", "catalogo-demo-colegios.pdf"),
        "municipio-demo.pdf": ("gobiernos", "catalogo-demo-gobiernos.pdf"),
        "gobierno-demo.pdf": ("gobiernos", "catalogo-demo-gobiernos.pdf"),
        "empresa-demo.pdf": ("empresas", "catalogo-demo-empresas.pdf"),
        "empresas-demo.pdf": ("empresas", "catalogo-demo-empresas.pdf"),
    }
    clean = str(filename or "").strip().replace("\\", "/").split("/")[-1]
    folder, asset_name = aliases.get(clean, ("", clean))
    base_dir = Path(current_app.root_path) / "data" / "demo_catalogs"
    if folder:
        base_dir = base_dir / folder
    response = send_from_directory(base_dir, asset_name, as_attachment=False)
    return _with_public_cors(response)


def _tenant_dict(tenant: TenantProfile, *, sector: str | None = None) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "nombre": tenant.nombre,
        "tipo": tenant.tipo,
        "sector": sector,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
    }


def _tenant_owner_user_id(tenant: TenantProfile) -> int | None:
    return getattr(tenant, "municipio_id", None) or getattr(tenant, "pyme_id", None)


def _demo_whatsapp_number_for_tenant(tenant: TenantProfile) -> str | None:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    candidates = [
        getattr(tenant, "whatsapp_sender_id", None),
        cfg.get("whatsapp_number"),
        cfg.get("numero_whatsapp"),
        cfg.get("sandbox_number"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value.replace("whatsapp:", "", 1)

    owner_id = _tenant_owner_user_id(tenant)
    if not owner_id:
        return None
    mapping = (
        WhatsappNumero.query.filter_by(user_id=owner_id, is_active=True)
        .order_by(WhatsappNumero.updated_at.desc(), WhatsappNumero.id.desc())
        .first()
    )
    if not mapping:
        return None
    return str(mapping.numero_whatsapp or "").replace("whatsapp:", "", 1).strip() or None


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


def _allowed_actions_from_experience(experience: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for source_list in (
        experience.get("quick_actions") or [],
        (experience.get("conversion_ctas") or {}).get("actions") or [],
    ):
        for source in source_list:
            if not isinstance(source, dict):
                continue
            action_id = source.get("intent") or source.get("id") or source.get("key")
            if not action_id:
                continue
            actions.append(
                {
                    "id": str(source.get("id") or action_id),
                    "intent": str(action_id),
                    "label": source.get("label") or source.get("title") or str(action_id),
                    "endpoint": source.get("endpoint") or "/ask",
                    "enabled": bool(source.get("enabled", True)),
                }
            )
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for action in actions:
        key = str(action.get("intent") or action.get("id"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(action)
    return unique


def _tracking_contract_for_demo(sector: str, tenant_slug: str) -> dict[str, Any]:
    normalized = normalize_demo_sector(sector)
    claim_endpoint = "/api/public/tracking/experience?kind=claim&code={code}&pin={pin}"
    order_endpoint = "/api/public/tracking/experience?kind=order&code={code}"
    return {
        "contract_version": "demo.tracking.v1",
        "enabled": True,
        "tenant_slug": tenant_slug,
        "fallback_when_no_coordinates": "timeline_only",
        "claim": {
            "enabled": normalized in {"educacion", "gobierno"},
            "experience_endpoint": claim_endpoint,
        },
        "order": {
            "enabled": normalized == "empresas",
            "experience_endpoint": order_endpoint,
        },
    }


def _sales_story_for_demo(sector: str, tenant_name: str) -> dict[str, Any]:
    normalized = normalize_demo_sector(sector)
    if normalized == "educacion":
        return {
            "contract_version": "demo.sales_story.v1",
            "problem": "Las familias escriben por muchos canales y el equipo administrativo pierde contexto, adjuntos y seguimiento.",
            "promise": "Chatboc convierte consultas escolares en casos trazables con IA, derivacion humana y panel operativo.",
            "value_path": [
                "La familia consulta por widget o WhatsApp.",
                "La IA pide solo alumno, curso, fecha o adjunto cuando corresponde.",
                "El colegio recibe el caso con contexto, prioridad y siguiente paso.",
            ],
            "commercial_cta": "Ver como quedaria para la comunidad de " + tenant_name,
        }
    if normalized == "gobierno":
        return {
            "contract_version": "demo.sales_story.v1",
            "problem": "Los reclamos llegan incompletos, sin ubicacion o por canales dispersos.",
            "promise": "Chatboc ordena reclamos, tramites, comentarios y encuestas en un flujo ciudadano medible.",
            "value_path": [
                "El vecino envia texto, foto, audio o ubicacion.",
                "La IA clasifica, pide faltantes y crea seguimiento.",
                "El equipo ve bandeja, mapa operativo y estado publico por codigo.",
            ],
            "commercial_cta": "Probar atencion ciudadana con trazabilidad",
        }
    return {
        "contract_version": "demo.sales_story.v1",
        "problem": "Las consultas comerciales se pierden entre chat, WhatsApp, catalogo y pagos.",
        "promise": "Chatboc une catalogo, carrito invitado, pedidos, leads e historial en una experiencia white-label.",
        "value_path": [
            "El visitante consulta productos o servicios.",
            "La IA responde con contexto del rubro y propone pedido, checkout o asesor.",
            "El panel conserva lead, carrito, pedido e historial omnicanal.",
        ],
        "commercial_cta": "Probar ventas asistidas para " + tenant_name,
    }


def _consulting_playbook_for_demo(sector: str) -> dict[str, Any]:
    normalized = normalize_demo_sector(sector)
    if normalized == "educacion":
        return {
            "contract_version": "demo.consulting_playbook.v1",
            "diagnosis": ["canales dispersos", "equipo administrativo saturado", "adjuntos sin trazabilidad", "casos sensibles sin ruta clara"],
            "detected_processes": ["inasistencias", "certificados", "admisiones", "pagos", "comunicados", "convivencia"],
            "handoff_rules": ["convivencia sensible", "salud o datos personales", "familia solicita humano", "caso incompleto por mas de dos turnos"],
            "required_data": ["adulto responsable", "alumno", "curso", "motivo", "fecha", "adjunto cuando aplique"],
            "followup_rules": ["crear caso escolar", "notificar equipo administrativo", "mantener historial por tenant", "ofrecer seguimiento publico si aplica"],
        }
    if normalized == "gobierno":
        return {
            "contract_version": "demo.consulting_playbook.v1",
            "diagnosis": ["reclamos incompletos", "ubicaciones ambiguas", "falta de estado publico", "baja lectura de demanda territorial"],
            "detected_processes": ["reclamos", "tramites", "estado por codigo", "mapa operativo", "encuestas", "comentarios"],
            "handoff_rules": ["urgencia", "riesgo ciudadano", "datos sensibles", "vecino pide operador", "categoria no configurada"],
            "required_data": ["categoria", "descripcion", "ubicacion", "contacto opcional", "foto/audio si existe"],
            "followup_rules": ["crear ticket", "generar codigo y PIN", "actualizar timeline", "mostrar mapa solo con coordenadas"],
        }
    return {
        "contract_version": "demo.consulting_playbook.v1",
        "diagnosis": ["catalogo desordenado", "carritos abandonados", "leads sin contexto", "pedidos por canales separados"],
        "detected_processes": ["catalogo", "carrito invitado", "checkout", "pedido", "comprobante", "seguimiento", "recuperacion de historial"],
        "handoff_rules": ["compra mayorista", "duda compleja", "pago o envio sensible", "cliente pide asesor"],
        "required_data": ["producto o necesidad", "cantidad", "contacto", "direccion/envio si aplica", "comprobante si aplica"],
        "followup_rules": ["crear pedido o lead", "conservar anon_id", "vincular usuario al registrarse", "mostrar tracking de pedido"],
    }


def _wow_flows_for_demo(sector: str) -> list[dict[str, Any]]:
    normalized = normalize_demo_sector(sector)
    if normalized == "educacion":
        return [
            {"id": "absence_certificate", "label": "Inasistencia con certificado", "trigger": "foto o archivo", "creates": "school_case", "primary": True},
            {"id": "admissions_lead", "label": "Consulta de admisiones", "trigger": "interes comercial", "creates": "lead", "primary": True},
            {"id": "sensitive_handoff", "label": "Caso sensible con derivacion", "trigger": "convivencia o datos sensibles", "creates": "handoff", "primary": True},
            {"id": "community_survey", "label": "Encuesta por comunidad", "trigger": "tenant con encuesta activa", "creates": "survey_response", "primary": False},
        ]
    if normalized == "gobierno":
        return [
            {"id": "claim_with_location", "label": "Reclamo con foto, audio o ubicacion", "trigger": "media o mapa", "creates": "ticket", "primary": True},
            {"id": "status_by_code", "label": "Estado por codigo y PIN", "trigger": "codigo de seguimiento", "creates": "tracking_view", "primary": True},
            {"id": "operations_map", "label": "Mapa operativo", "trigger": "ticket con coordenadas", "creates": "map_event", "primary": True},
            {"id": "live_vote", "label": "Votacion ciudadana", "trigger": "tenant con votacion activa", "creates": "survey_response", "primary": False},
        ]
    return [
        {"id": "guest_cart", "label": "Catalogo y carrito invitado", "trigger": "consulta de producto", "creates": "cart_or_order", "primary": True},
        {"id": "checkout_preview", "label": "Checkout y comprobante", "trigger": "carrito listo", "creates": "checkout_intent", "primary": True},
        {"id": "commercial_lead", "label": "Lead comercial con historial", "trigger": "interes alto", "creates": "lead", "primary": True},
        {"id": "order_tracking", "label": "Seguimiento de pedido", "trigger": "codigo de pedido", "creates": "tracking_view", "primary": True},
    ]


def _live_modules_for_demo(sector: str) -> list[dict[str, Any]]:
    normalized = normalize_demo_sector(sector)
    base = [
        {"id": "inbox", "label": "Inbox omnicanal", "enabled": True, "endpoint": "/api/v2/inbox/omnichannel", "primary": True},
        {"id": "human_handoff", "label": "Derivacion humana", "enabled": True, "endpoint": "/api/v2/inbox/omnichannel/actions", "primary": True},
        {"id": "analytics", "label": "Analiticas", "enabled": True, "endpoint": "/api/v2/analytics/overview", "primary": True},
        {"id": "surveys_votings", "label": "Encuestas y votaciones", "enabled": True, "endpoint": "/api/v2/surveys", "primary": False},
    ]
    if normalized in {"educacion", "gobierno"}:
        base.extend(
            [
                {"id": "claims", "label": "Reclamos/casos", "enabled": True, "endpoint": "/api/v2/inbox/omnichannel", "primary": True},
                {"id": "map", "label": "Mapa operativo", "enabled": normalized == "gobierno", "endpoint": "/api/v2/analytics/operations/heatmap", "primary": normalized == "gobierno"},
                {"id": "comments", "label": "Comentarios", "enabled": True, "endpoint": "/api/v2/inbox/omnichannel", "primary": False},
            ]
        )
    else:
        base.extend(
            [
                {"id": "catalog", "label": "Catalogo", "enabled": True, "endpoint": "/api/public/tenants/{tenant_slug}/catalog", "primary": True},
                {"id": "cart", "label": "Carrito invitado", "enabled": True, "endpoint": "/api/pwa/public/cart/items", "primary": True},
                {"id": "orders", "label": "Pedidos", "enabled": True, "endpoint": "/api/public/tracking/experience?kind=order&code={code}", "primary": True},
            ]
        )
    return base


def _openai_runtime_for_demo(sector: str, allowed_actions: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = normalize_demo_sector(sector)
    if normalized == "educacion":
        prompt_profile = "Asistente escolar prudente: equipo administrativo, familias, inasistencias, certificados, pagos, comunicados y derivacion sensible."
        safety = ["no diagnosticar salud", "no exponer datos de menores", "derivar convivencia sensible", "confirmar antes de crear caso"]
        tools = ["crear_caso_escolar", "capturar_lead_admisiones", "derivar_humano", "registrar_adjunto"]
    elif normalized == "gobierno":
        prompt_profile = "Asistente ciudadano: reclamos, tramites, ubicacion, foto/audio, estado por codigo y mapa operativo."
        safety = ["no prometer plazos no configurados", "pedir ubicacion si falta", "derivar urgencias", "confirmar antes de crear ticket"]
        tools = ["crear_reclamo", "consulta_estado_ticket", "registrar_ubicacion", "derivar_humano"]
    else:
        prompt_profile = "Asistente comercial: catalogo, carrito, checkout, pedido, comprobante, lead e historial de compra."
        safety = ["no inventar stock", "no inventar descuentos", "no procesar pagos fuera del checkout", "derivar compra compleja"]
        tools = ["buscar_catalogo", "agregar_item_carrito", "crear_pedido", "capturar_lead", "derivar_humano"]
    return {
        "contract_version": "demo.openai_runtime.v1",
        "provider": "openai_server_side",
        "prompt_profile": prompt_profile,
        "tools": tools,
        "safety_rules": safety,
        "actionable_intents": [action.get("intent") for action in allowed_actions if action.get("enabled")],
        "frontend_api_keys_allowed": False,
        "response_contract": "chat.response.v1",
    }


def _survey_voting_for_demo(sector: str, tenant_slug: str) -> dict[str, Any]:
    normalized = normalize_demo_sector(sector)
    return {
        "contract_version": "demo.survey_voting.v1",
        "enabled": normalized in {"educacion", "gobierno"},
        "primary_action_enabled": False,
        "availability_rule": "visible_when_tenant_has_active_survey",
        "tenant_slug": tenant_slug,
        "admin_endpoint": "/api/v2/surveys",
        "draft_endpoint": "/api/v2/surveys/draft",
        "create_endpoint": "/api/v2/surveys/draft",
        "respond_endpoint": "/api/public/encuestas/v1/{survey_slug}/responder",
        "results_endpoint": "/api/public/encuestas/v1/{survey_slug}/live-results",
        "comments_endpoint": "/api/public/encuestas/v1/{survey_slug}/comentarios",
        "public_response_endpoint_template": "/api/public/encuestas/v1/{survey_slug}/responder",
        "public_detail_endpoint_template": "/api/public/encuestas/v1/{survey_slug}",
        "public_legacy_aliases": [
            "/api/public/encuestas/{survey_slug}",
            "/public/encuestas/v1/{survey_slug}",
        ],
        "analytics_endpoint_template": "/api/v2/surveys/{survey_id}/analytics",
        "frontend_contract": {
            "render_as": "survey_voting_module",
            "show_only_when_enabled": True,
            "empty_state_behavior": "hide_primary_action_until_active_survey",
        },
    }


def _commercial_demo_bundle(
    *,
    sector: str,
    tenant_slug: str,
    tenant_name: str,
    allowed_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "sales_story": _sales_story_for_demo(sector, tenant_name),
        "consulting_playbook": _consulting_playbook_for_demo(sector),
        "wow_flows": _wow_flows_for_demo(sector),
        "live_modules": _live_modules_for_demo(sector),
        "openai_runtime": _openai_runtime_for_demo(sector, allowed_actions),
        "survey_voting": _survey_voting_for_demo(sector, tenant_slug),
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
    chat_session_id = _stable_demo_chat_session_id(demo_session_id)

    return {
        "contract_version": "demo.chat_bootstrap.v1",
        "endpoint": endpoint,
        "same_origin_endpoint": f"/api{endpoint}" if endpoint.startswith("/ask") else endpoint,
        "fallback_endpoint": None,
        "method": "POST",
        "response_contract": "chat.response.v1",
        "session": {
            "chat_session_id": chat_session_id,
            "demo_session_id": demo_session_id,
        },
        "runtime_contract": {
            "server_side_ai": True,
            "frontend_llm_keys_allowed": False,
            "lead_capture_source": "backend_action_handlers",
            "error_contract": "shared.error.v1",
        },
        "empty_states": {
            "runtime_unavailable": {
                "title": "Demo conversacional no disponible",
                "description": "No pudimos iniciar la respuesta en este momento. Reintenta en unos segundos o deja tus datos para seguimiento.",
            }
        },
        "headers": {
            "X-Chat-Session-Id": chat_session_id,
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
            "chat_session_id": chat_session_id,
            "demo_mode": True,
        },
        "context": {
            "sector": sector,
            "rubro": canonical_rubro,
            "tenant_slug": tenant.slug,
            "tenant_tipo": tenant_type,
            "vertical": vertical,
            "demo_session_id": demo_session_id,
            "chat_session_id": chat_session_id,
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
        "lead_result_contract": {
            "created_key": "lead.created",
            "lead_id_key": "lead.lead_id",
            "ticket_id_key": "lead.ticket_id",
            "detail_endpoint_template": "/api/v2/inbox/omnichannel/{ticket_id}",
        },
        "notes": [
            "Enviar X-Chat-Session-Id como id corto y X-Demo-Session-Id como token de demo.",
            "El endpoint responde con IA server-side; el frontend no debe llamar OpenAI directo.",
            "Para imagen/archivo subir primero a /archivos/upload/chat_attachment y luego llamar al endpoint con attachmentInfo.",
            "Para audio enviar multipart al endpoint con campo audio_file.",
            "Para ubicacion enviar payload JSON con location.",
        ],
    }


@v2_demo_bp.route("/catalog", methods=["GET", "OPTIONS"])
def demo_catalog_v2():
    if request.method == "OPTIONS":
        return _options_response()

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
    for rubro_item in rubros:
        rubro_sector = rubro_item.get("sector") or sector_for_rubro(rubro_item.get("slug") or rubro_item.get("key")) or "empresas"
        bundle = _commercial_demo_bundle(
            sector=rubro_sector,
            tenant_slug=str(rubro_item.get("tenant_slug") or rubro_item.get("slug") or ""),
            tenant_name=str(rubro_item.get("label") or rubro_item.get("slug") or "Demo Chatboc"),
            allowed_actions=[],
        )
        rubro_item.setdefault("sales_story", bundle["sales_story"])
        rubro_item.setdefault("consulting_playbook", bundle["consulting_playbook"])
        rubro_item.setdefault("wow_flows", bundle["wow_flows"])
        rubro_item.setdefault("live_modules", bundle["live_modules"])
        rubro_item.setdefault("openai_runtime", bundle["openai_runtime"])
        rubro_item.setdefault("survey_voting", bundle["survey_voting"])
        rubro_item.setdefault("admin_preview_endpoint", f"/api/v2/demo/admin-preview?sector={rubro_sector}&tenant_slug={rubro_item.get('tenant_slug') or rubro_item.get('slug')}")

    pillars = demo_pillars()
    pillar_categories = {pillar.get("key"): pillar.get("categories") or [] for pillar in pillars}
    resources_by_id: dict[str, dict[str, Any]] = {}
    for rubro_item in rubros:
        for resource in rubro_item.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            resource_id = str(resource.get("id") or resource.get("url") or resource.get("label") or "").strip()
            if resource_id and resource_id not in resources_by_id:
                resources_by_id[resource_id] = resource

    return _json_response(
        {
            "contract_version": "demo.catalog.v2",
            "pillar_contract_version": DEMO_PILLAR_CONTRACT_VERSION,
            "sectors": ["gobierno", "empresas", "educacion"],
            "pillars": pillars,
            "rubros": rubros,
            "resources": list(resources_by_id.values()),
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


def _admin_preview_for_sector(sector: str, tenant_slug: str = "") -> dict[str, Any]:
    normalized = normalize_demo_sector(sector or tenant_slug or "empresas")
    if normalized not in {"educacion", "gobierno", "empresas"}:
        slug_hint = str(tenant_slug or normalized or "").lower()
        if any(token in slug_hint for token in ("colegio", "escuela", "educacion")):
            normalized = "educacion"
        elif any(token in slug_hint for token in ("municipio", "gobierno", "ciudad")):
            normalized = "gobierno"
        else:
            normalized = sector_for_rubro(normalized) or "empresas"

    presets: dict[str, dict[str, Any]] = {
        "educacion": {
            "title": "Panel demo para direccion escolar",
            "subtitle": "Colegio privado integral",
            "modules": [
                {"id": "summary", "label": "Resumen", "enabled": True},
                {"id": "cases", "label": "Casos escolares", "enabled": True},
                {"id": "families", "label": "Familias", "enabled": True},
                {"id": "surveys", "label": "Encuestas", "enabled": True},
            ],
            "cards": [
                {"label": "Casos creados en esta sesion", "value": "0", "detail": "se actualiza cuando el chat crea un caso"},
                {"label": "Adjuntos recibidos", "value": "0", "detail": "certificados, comprobantes o autorizaciones"},
                {"label": "Derivaciones humanas", "value": "0", "detail": "equipo administrativo, admisiones o convivencia"},
            ],
            "timeline": [
                {"label": "Familia inicia consulta", "status": "setup"},
                {"label": "IA pide datos necesarios", "status": "waiting_for_session"},
                {"label": "Equipo administrativo recibe caso trazable", "status": "waiting_for_session"},
            ],
            "catalog_title": "Colegio privado integral",
            "catalog_file": "colegio-demo.pdf",
        },
        "gobierno": {
            "title": "Panel demo para gestion ciudadana",
            "subtitle": "Municipio inteligente",
            "modules": [
                {"id": "summary", "label": "Resumen", "enabled": True},
                {"id": "claims", "label": "Reclamos", "enabled": True},
                {"id": "heatmap", "label": "Mapa operativo", "enabled": False, "empty_state": "Disponible cuando la sesion genere puntos con coordenadas."},
                {"id": "surveys", "label": "Encuestas", "enabled": True},
            ],
            "cards": [
                {"label": "Reclamos creados en esta sesion", "value": "0", "detail": "se actualiza cuando el chat crea un ticket"},
                {"label": "Ubicaciones capturadas", "value": "0", "detail": "mapa disponible cuando hay coordenadas"},
                {"label": "Comentarios ciudadanos", "value": "0", "detail": "mensajes y actualizaciones del caso"},
            ],
            "timeline": [
                {"label": "Vecino envia ubicacion", "status": "setup"},
                {"label": "IA clasifica y pide faltantes", "status": "waiting_for_session"},
                {"label": "Equipo ve ticket y mapa", "status": "waiting_for_session"},
            ],
            "catalog_title": "Guia demo gobiernos",
            "catalog_file": "municipio-demo.pdf",
        },
        "empresas": {
            "title": "Panel demo para ventas y soporte",
            "subtitle": "Empresa con catalogo y pedidos",
            "modules": [
                {"id": "summary", "label": "Resumen", "enabled": True},
                {"id": "catalog", "label": "Catalogo", "enabled": True},
                {"id": "orders", "label": "Pedidos", "enabled": True},
                {"id": "customers", "label": "Clientes", "enabled": True},
            ],
            "cards": [
                {"label": "Pedidos creados en esta sesion", "value": "0", "detail": "se actualiza cuando el chat crea pedido"},
                {"label": "Items en carrito invitado", "value": "0", "detail": "se sincroniza por anon_id o widget token"},
                {"label": "Leads comerciales", "value": "0", "detail": "asesor recibe contexto de compra"},
            ],
            "timeline": [
                {"label": "Cliente consulta catalogo", "status": "setup"},
                {"label": "IA propone carrito o asesor", "status": "waiting_for_session"},
                {"label": "Panel conserva pedido y seguimiento", "status": "waiting_for_session"},
            ],
            "catalog_title": "Catalogo demo empresas",
            "catalog_file": "empresa-demo.pdf",
        },
    }
    preset = presets[normalized]
    resolved_tenant_slug = tenant_slug or {"educacion": "colegio-demo", "gobierno": "municipio", "empresas": "bodega"}[normalized]
    allowed_actions: list[dict[str, Any]] = []
    commercial = _commercial_demo_bundle(
        sector=normalized,
        tenant_slug=resolved_tenant_slug,
        tenant_name=preset["subtitle"],
        allowed_actions=allowed_actions,
    )
    return {
        "contract_version": "demo.admin_preview.v1",
        "sector": normalized,
        "tenant_slug": resolved_tenant_slug,
        "title": preset["title"],
        "subtitle": preset["subtitle"],
        "modules": preset["modules"],
        "cards": preset["cards"],
        "timeline": preset["timeline"],
        "metrics": [],
        "map": {
            "enabled": False,
            "points": [],
            "empty_state": "Disponible cuando la sesion genere ubicaciones reales.",
        },
        "session_activity": {
            "contract_version": "demo.session_activity.v1",
            "source": "session_generated_events",
            "has_session_data": False,
            "empty_state": "Inicia la demo y envia un mensaje para crear actividad real en este panel.",
            "items": [],
        },
        "operations": {
            "contract_version": "demo.operations_preview.v1",
            "data_policy": "session_events_only",
            "setup_message": "Sin actividad real todavia. El panel se llena con leads, tickets, pedidos o encuestas creadas por la demo.",
        },
        "sales_story": commercial["sales_story"],
        "consulting_playbook": commercial["consulting_playbook"],
        "wow_flows": commercial["wow_flows"],
        "live_modules": commercial["live_modules"],
        "openai_runtime": commercial["openai_runtime"],
        "survey_voting": commercial["survey_voting"],
        "catalog": {
            "enabled": True,
            "title": preset["catalog_title"],
            "download_endpoint": f"/api/v2/demo/catalog-assets/{preset['catalog_file']}",
        },
        "frontend_contract": {"render_as": "demo_admin_preview"},
    }


@v2_demo_bp.route("/admin-preview", methods=["GET", "OPTIONS"])
def demo_admin_preview_v2():
    if request.method == "OPTIONS":
        return _options_response()

    sector = request.args.get("sector") or request.args.get("pilar") or request.args.get("vertical") or ""
    tenant_slug = request.args.get("tenant_slug") or request.args.get("tenant") or ""
    if not sector:
        sector = sector_for_rubro(tenant_slug) or tenant_slug or "empresas"
    return _json_response(_admin_preview_for_sector(sector, tenant_slug=str(tenant_slug or "").strip().lower()))


@v2_demo_bp.route("/catalog-assets/<path:filename>", methods=["GET", "OPTIONS"])
def demo_catalog_asset_v2(filename: str):
    if request.method == "OPTIONS":
        return _options_response()
    return _demo_catalog_asset_response(filename)


@v2_demo_bp.route("/whatsapp-sandbox", methods=["GET", "POST", "OPTIONS"])
def demo_whatsapp_sandbox_launcher_v2():
    if request.method == "OPTIONS":
        return _json_response({"ok": True, "contract_version": "demo.whatsapp_sandbox_launcher.v1"})

    payload = _request_payload()
    requested_rubro = str(payload.get("rubro") or payload.get("subvertical") or "").strip()
    requested_sector = str(payload.get("sector") or sector_for_rubro(requested_rubro) or "empresas").strip()
    sector = normalize_demo_sector(requested_sector)
    rubro = requested_rubro or default_rubro_for_sector(sector)
    tenant_slug = str(payload.get("tenant_slug") or payload.get("tenant") or "").strip()

    tenant = None
    if tenant_slug:
        try:
            tenant = resolve_tenant_only(tenant_slug=tenant_slug, require_explicit_slug=True)
        except Exception:
            tenant = None
    if not tenant:
        candidate = _resolve_demo_tenant_slug(rubro)
        if candidate:
            try:
                tenant = resolve_tenant_only(tenant_slug=candidate, require_explicit_slug=True)
            except Exception:
                tenant = None
    if not tenant:
        if sector == "educacion":
            tenant = _first_education_tenant_for_demo() or _first_active_tenant_for_demo("pyme")
        else:
            tenant = _first_active_tenant_for_demo("municipio" if sector == "gobierno" else "pyme")
    if not tenant:
        return _error_response("No se pudo resolver tenant demo", 404, "tenant_resolution_failed", "send_sector_or_tenant_slug")

    demo_session_id = create_demo_session_token(tenant_slug=tenant.slug, sector=sector, rubro=rubro or tenant.slug)
    chat_session_id = _stable_demo_chat_session_id(demo_session_id)
    join_phrase = str(payload.get("join_phrase") or _twilio_sandbox_join_phrase()).strip()
    demo_whatsapp_number = _demo_whatsapp_number_for_tenant(tenant)
    whatsapp_sandbox = build_demo_whatsapp_sandbox_contract(
        tenant_slug=tenant.slug,
        sector=sector,
        rubro=rubro or tenant.slug,
        sandbox_number=demo_whatsapp_number or _twilio_sandbox_number(),
        join_phrase=join_phrase,
        source=str(payload.get("source") or "public_demo_profile"),
        provider="twilio_whatsapp_number" if demo_whatsapp_number else "twilio_sandbox",
    )

    return _json_response(
        {
            "contract_version": "demo.whatsapp_sandbox_launcher.v1",
            "ok": True,
            "requires_auth": False,
            "tenant": _tenant_dict(tenant, sector=sector),
            "session": {
                "demo_session_id": demo_session_id,
                "chat_session_id": chat_session_id,
                "max_messages": (whatsapp_sandbox.get("trial_policy") or {}).get("max_messages"),
            },
            "whatsapp_sandbox": whatsapp_sandbox,
            "trial_policy": whatsapp_sandbox.get("trial_policy"),
            "supported_inputs": whatsapp_sandbox.get("supported_inputs"),
            "scenario_scripts": whatsapp_sandbox.get("scenario_scripts"),
            "catalog": whatsapp_sandbox.get("catalog"),
            "surveys_votings": whatsapp_sandbox.get("surveys_votings"),
            "frontend_contract": {
                "render_as": "anonymous_whatsapp_sandbox_launcher",
                "requires_auth": False,
                "allow_sector_switch": True,
                "allow_rubro_switch": True,
                "show_qr": True,
                "show_trial_counter": True,
                "show_catalog_resources": True,
                "show_survey_voting_entry": True,
                "do_not_publish_mock_results": True,
            },
        }
    )


@v2_demo_bp.route("/session", methods=["POST", "OPTIONS"])
@demo_compat_bp.route("/v2/demo/session", methods=["POST", "OPTIONS"])
@demo_compat_bp.route("/api/v1/demo/session", methods=["POST", "OPTIONS"])
@demo_compat_bp.route("/v1/demo/session", methods=["POST", "OPTIONS"])
def demo_session_v2():
    if request.method == "OPTIONS":
        return _options_response()

    data = request.get_json(silent=True) or {}
    sector = normalize_demo_sector(
        data.get("sector")
        or data.get("pilar")
        or data.get("pillar")
        or data.get("pilar_key")
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
        "categoria",
        "categoria_slug",
        "category_key",
        "category",
        "category_slug",
        "demo_rubro",
        "subvertical",
    )
    tenant_slug = _first_payload_tenant_slug(
        data,
        "tenant_slug",
        "tenant",
        "slug",
        "tenantSlug",
        "tenant_key",
    )

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
    if rubro and tenant.slug and rubro.replace("_", "-") == tenant.slug:
        rubro = tenant.slug
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
    allowed_actions = _allowed_actions_from_experience(experience)
    tracking = _tracking_contract_for_demo(sector, tenant.slug)
    admin_preview_endpoint = f"/api/v2/demo/admin-preview?sector={sector}&tenant_slug={tenant.slug}"
    commercial = _commercial_demo_bundle(
        sector=sector,
        tenant_slug=tenant.slug,
        tenant_name=tenant.nombre or "Demo Chatboc",
        allowed_actions=allowed_actions,
    )

    demo_session_id = create_demo_session_token(tenant_slug=tenant.slug, sector=sector, rubro=rubro or tenant.slug)
    chat_session_id = _stable_demo_chat_session_id(demo_session_id)
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
    demo_whatsapp_number = _demo_whatsapp_number_for_tenant(tenant)
    whatsapp_sandbox = build_demo_whatsapp_sandbox_contract(
        tenant_slug=tenant.slug,
        sector=sector,
        rubro=rubro or tenant.slug,
        sandbox_number=demo_whatsapp_number or _twilio_sandbox_number(),
        join_phrase=_twilio_sandbox_join_phrase(),
        source="public_demo_session",
        provider="twilio_whatsapp_number" if demo_whatsapp_number else "twilio_sandbox",
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
        "sales_story": commercial["sales_story"],
        "consulting_playbook": commercial["consulting_playbook"],
        "wow_flows": commercial["wow_flows"],
        "live_modules": commercial["live_modules"],
        "openai_runtime": commercial["openai_runtime"],
        "survey_voting": commercial["survey_voting"],
        "allowed_actions": allowed_actions,
        "tracking": tracking,
        "admin_preview_endpoint": admin_preview_endpoint,
        "media_capabilities": media_capabilities,
        "conversion_ctas": conversion_ctas,
        "animation_tokens": animation_tokens,
        "chat_bootstrap": chat_bootstrap,
        "whatsapp_sandbox": whatsapp_sandbox,
        "empty_states": {
            "runtime_unavailable": chat_bootstrap["empty_states"]["runtime_unavailable"],
        },
        "education": education_payload,
        "pillar_selector": {
            "contract_version": DEMO_PILLAR_CONTRACT_VERSION,
            "selected_sector": sector,
            "selected_rubro": rubro or tenant.slug,
            "pillars": demo_pillars(),
        },
        "catalog_resources": catalog_resources_for_rubro(rubro or tenant.slug, sector),
        "runtime_contract": {
            "demo_data_source": "backend_tenant_contracts",
            "local_mock_allowed": False,
            "requires_demo_session_id": True,
            "chat_response_contract": "chat.response.v1",
        },
    }

    return _json_response(
        {
            "contract_version": "demo.session.v2",
            "contract_aliases": ["demo.session.v1"],
            "demo_session_id": demo_session_id,
            "session_id": chat_session_id,
            "chat_session_id": chat_session_id,
            "tenant_slug": tenant.slug,
            "tenant": _tenant_dict(tenant, sector=sector),
            "workspace": workspace,
            "pillar_selector": workspace["pillar_selector"],
            "catalog_resources": workspace["catalog_resources"],
            "chat_bootstrap": chat_bootstrap,
            "experience_blueprint": experience,
            "first_visit": workspace["first_visit"],
            "sample_conversations": workspace["sample_conversations"],
            "sales_story": commercial["sales_story"],
            "consulting_playbook": commercial["consulting_playbook"],
            "wow_flows": commercial["wow_flows"],
            "live_modules": commercial["live_modules"],
            "openai_runtime": commercial["openai_runtime"],
            "survey_voting": commercial["survey_voting"],
            "allowed_actions": allowed_actions,
            "tracking": tracking,
            "admin_preview_endpoint": admin_preview_endpoint,
            "trust_signals": workspace["trust_signals"],
            "lead_capture": workspace["lead_capture"],
            "whatsapp_sandbox": whatsapp_sandbox,
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
                "sales_story": commercial["sales_story"],
                "consulting_playbook": commercial["consulting_playbook"],
                "wow_flows": commercial["wow_flows"],
                "live_modules": commercial["live_modules"],
                "openai_runtime": commercial["openai_runtime"],
                "survey_voting": commercial["survey_voting"],
                "media_capabilities": media_capabilities,
                "conversion_ctas": conversion_ctas,
                "chat_bootstrap": chat_bootstrap,
                "whatsapp_sandbox": whatsapp_sandbox,
                "education": education_payload,
            },
            "quick_replies": quick_replies,
        }
    )
