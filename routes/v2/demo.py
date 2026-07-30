from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
import hashlib
import json
import uuid

from flask import Blueprint, abort, current_app, jsonify, request, send_from_directory

from models import MunicipioTicket, TenantProfile, WhatsappNumero
from routes.auth import (
    _first_active_tenant_for_demo,
    _resolve_demo_tenant_slug,
    demo_catalog as legacy_demo_catalog,
)
from services.tenant_resolver import resolve_tenant_only
from services.tenant_ticket_scope import scoped_municipio_ticket_query
from services.demo_experience_contract import build_demo_experience_contract
from services.demo_registry import load_demo_rubros
from services.demo_pillar_catalog import (
    DEMO_PILLAR_CONTRACT_VERSION,
    catalog_resources_for_rubro,
    category_for_rubro,
    curated_demo_rubros,
    default_rubro_for_sector,
    demo_pillars,
    demo_pillar_keys,
    normalize_demo_sector,
    sector_for_rubro,
)
from services.demo_sandbox_contract import build_demo_whatsapp_sandbox_contract
from services.demo_surveys import build_demo_surveys_votings_contract
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
        "Idempotency-Key,X-Request-Id,X-Contact-Key,X-Conversation-Id"
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


def _truthy_payload_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "si", "compact", "widget"}


def _demo_session_response_profile(data: dict[str, Any]) -> str:
    explicit = (
        data.get("response_profile")
        or data.get("response_mode")
        or data.get("render_profile")
        or data.get("surface")
        or data.get("mode")
        or data.get("source")
    )
    profile = _payload_slug(explicit)
    if profile in {
        "widget",
        "compact",
        "selector",
        "widget_selector",
        "platform_sector_selector",
        "landing_widget_selector",
        "public_widget_selector",
        "public_widget",
        "widget_onboarding",
    }:
        return "widget_compact"
    if _truthy_payload_flag(data.get("compact")) or _truthy_payload_flag(data.get("compact_response")):
        return "widget_compact"
    return "full"


def _payload_search_text(value: Any) -> str:
    if isinstance(value, dict):
        parts = [value.get(key) for key in ("sector", "rubro", "slug", "key", "id", "label", "title", "name", "text")]
        return " ".join(fold_text(part) for part in parts if part)
    if isinstance(value, (list, tuple)):
        return " ".join(_payload_search_text(item) for item in value)
    return fold_text(value)


def _infer_demo_sector_from_payload(
    data: dict[str, Any],
    *,
    rubro: str = "",
    tenant_slug: str = "",
    current_sector: str = "",
) -> str:
    candidates = [
        current_sector,
        rubro,
        tenant_slug,
        data.get("sector"),
        data.get("pilar"),
        data.get("pillar"),
        data.get("selected_sector"),
        data.get("selected_pillar"),
        data.get("segment"),
        data.get("vertical"),
        data.get("categoria"),
        data.get("category"),
        data.get("label"),
        data.get("title"),
        data.get("name"),
        data.get("id"),
        data.get("key"),
        data.get("button_label"),
        data.get("cta_label"),
        data.get("text"),
    ]
    for candidate in candidates:
        normalized = normalize_demo_sector(candidate)
        if normalized in set(demo_pillar_keys()):
            return normalized
        inferred = sector_for_rubro(candidate)
        if inferred:
            return inferred

    haystack = " ".join(_payload_search_text(candidate) for candidate in candidates if candidate)
    if any(keyword in haystack for keyword in ("coleg", "escuel", "educacion", "instituto", "jardin", "alumno", "familia", "admisiones")):
        return "educacion"
    if any(keyword in haystack for keyword in ("gobierno", "municip", "reclamo", "tramite", "ciudadan", "bache", "alumbrado", "licencia")):
        return "gobierno"
    if any(keyword in haystack for keyword in ("empresa", "pyme", "bodega", "ferreter", "catalog", "pedido", "precio", "comercio", "tienda")):
        return "empresas"
    return ""


def _infer_demo_rubro_from_payload(data: dict[str, Any], *, sector: str = "") -> str:
    explicit_candidates = [
        data.get("rubro"),
        data.get("rubro_slug"),
        data.get("rubro_key"),
        data.get("rubro_clave"),
        data.get("categoria"),
        data.get("categoria_slug"),
        data.get("category"),
        data.get("category_slug"),
        data.get("category_key"),
        data.get("demo_rubro"),
        data.get("subvertical"),
    ]
    normalized_sector = normalize_demo_sector(sector)
    pillar_keys = set(demo_pillar_keys())

    for candidate in explicit_candidates:
        slug = _payload_slug(candidate)
        if not slug or slug in pillar_keys:
            continue
        category = category_for_rubro(slug)
        if category and (not normalized_sector or category.get("sector") == normalized_sector):
            return str(category.get("slug") or slug)

    text_candidates = [
        data.get("label"),
        data.get("title"),
        data.get("name"),
        data.get("id"),
        data.get("key"),
        data.get("button_label"),
        data.get("cta_label"),
        data.get("text"),
    ]
    haystack = " ".join(_payload_search_text(candidate) for candidate in text_candidates if candidate)
    if not haystack:
        return ""

    for pillar in demo_pillars():
        if normalized_sector and pillar.get("key") != normalized_sector:
            continue
        for category in pillar.get("categories") or []:
            slug = _payload_slug(category.get("slug"))
            if not slug or slug in pillar_keys:
                continue
            label = fold_text(category.get("label") or "")
            slug_words = fold_text(slug.replace("_", " "))
            if (label and label in haystack) or (slug_words and slug_words in haystack):
                return str(category.get("slug") or slug)
    return ""


def _demo_rubro_selector_contract(
    *,
    sector: str,
    selected_rubro: str,
    response_profile: str,
) -> dict[str, Any]:
    categories: list[dict[str, Any]] = []
    for pillar in demo_pillars():
        if pillar.get("key") != sector:
            continue
        for category in pillar.get("categories") or []:
            slug = _payload_slug(category.get("slug"))
            if not slug:
                continue
            categories.append(
                {
                    "slug": slug,
                    "label": category.get("label") or slug.replace("_", " ").title(),
                    "sector": sector,
                    "tipo_chat": category.get("tipo_chat"),
                    "vertical": category.get("vertical"),
                    "subvertical": category.get("subvertical"),
                    "sample_prompts": category.get("sample_prompts") or [],
                    "resources": category.get("resources") or [],
                    "selected": slug == _payload_slug(selected_rubro),
                    "action_payload": {
                        "surface": "widget",
                        "source": "landing_widget_rubro_selector",
                        "sector": sector,
                        "rubro": slug,
                        "label": category.get("label") or slug.replace("_", " ").title(),
                    },
                }
            )
        break

    return {
        "contract_version": "demo.rubro_selector.v1",
        "render_as": "rubro_selector",
        "response_profile": response_profile,
        "sector": sector,
        "selected_rubro": selected_rubro,
        "requires_selection": sector == "empresas",
        "open_chat_after_selection": True,
        "post_endpoint": "/api/v2/demo/session",
        "categories": categories,
        "frontend_rule": "Si requires_selection=true, renderizar estas categorias y no enviar __INIT__ todavia.",
    }


def _demo_rubro_context(*, sector: str, rubro: str, tenant: TenantProfile) -> dict[str, Any]:
    category = category_for_rubro(rubro) or {}
    slug = _payload_slug(category.get("slug") or rubro or tenant.slug)
    label = str(category.get("label") or tenant.nombre or slug.replace("_", " ").title()).strip()
    base = {
        "contract_version": "demo.rubro_context.v1",
        "sector": sector,
        "slug": slug,
        "label": label,
        "description": category.get("description") or "",
        "vertical": category.get("vertical") or tenant.vertical,
        "subvertical": category.get("subvertical") or tenant.subvertical,
        "sample_prompts": category.get("sample_prompts") or [],
        "resources": category.get("resources") or [],
    }

    pyme_profiles = {
        "bodega": {
            "display_name": "Bodega",
            "description": "Venta de vinos, cajas, promociones, maridajes y pedidos mayoristas.",
            "prompt_context": (
                "Demo PYME rubro bodega. Responde como asistente comercial de una bodega: "
                "habla de vinos, varietales, cajas, maridajes, stock, promociones, envio y retiro. "
                "No respondas como ferreteria ni comercio generico."
            ),
            "quick_actions": [
                {"id": "ver_vinos", "label": "Ver vinos", "intent": "ver_catalogo_vinos", "description": "Mostrar catalogo de vinos y precios."},
                {"id": "armar_caja", "label": "Armar caja", "intent": "armar_caja_vinos", "description": "Combinar botellas por gusto y presupuesto."},
                {"id": "maridaje", "label": "Sugerir maridaje", "intent": "sugerir_maridaje", "description": "Recomendar vino segun comida u ocasion."},
                {"id": "pedido_mayorista", "label": "Pedido mayorista", "intent": "pedido_mayorista", "description": "Tomar datos para compra mayorista."},
            ],
        },
        "ferreteria": {
            "display_name": "Ferreteria",
            "description": "Venta de herramientas, materiales, presupuesto, stock y envios.",
            "prompt_context": (
                "Demo PYME rubro ferreteria. Responde como asistente de ferreteria: "
                "ayuda con herramientas, materiales, medidas, cantidades, presupuestos, stock, envio y retiro. "
                "No recomiendes vinos ni uses lenguaje de bodega."
            ),
            "quick_actions": [
                {"id": "buscar_producto", "label": "Buscar producto", "intent": "buscar_producto_ferreteria", "description": "Encontrar herramientas o materiales."},
                {"id": "calcular_materiales", "label": "Calcular materiales", "intent": "calcular_materiales", "description": "Estimar cantidades por medida u obra."},
                {"id": "armar_presupuesto", "label": "Armar presupuesto", "intent": "armar_presupuesto_ferreteria", "description": "Preparar pedido con precios y stock."},
                {"id": "coordinar_envio", "label": "Coordinar envio", "intent": "coordinar_envio_retiro", "description": "Resolver entrega, retiro o consulta de sucursal."},
            ],
        },
        "inmobiliaria": {
            "display_name": "Inmobiliaria",
            "description": "Consultas por propiedades, requisitos, visitas y tasaciones.",
            "prompt_context": "Demo PYME rubro inmobiliaria. Prioriza propiedades, visitas, requisitos, tasaciones y seguimiento comercial.",
            "quick_actions": [
                {"id": "buscar_propiedad", "label": "Buscar propiedad", "intent": "buscar_propiedad"},
                {"id": "agendar_visita", "label": "Agendar visita", "intent": "agendar_visita"},
                {"id": "requisitos", "label": "Consultar requisitos", "intent": "consultar_requisitos"},
                {"id": "tasacion", "label": "Pedir tasacion", "intent": "pedir_tasacion"},
            ],
        },
        "medico_general": {
            "display_name": "Clinica o consultorio",
            "description": "Turnos, consultas, estudios y seguimiento administrativo.",
            "prompt_context": "Demo PYME rubro salud. Prioriza turnos, estudios, cobertura, preparacion y derivacion administrativa.",
            "quick_actions": [
                {"id": "pedir_turno", "label": "Pedir turno", "intent": "pedir_turno"},
                {"id": "consultar_estudios", "label": "Consultar estudios", "intent": "consultar_estudios"},
                {"id": "cobertura", "label": "Cobertura", "intent": "consultar_cobertura"},
                {"id": "hablar_admin", "label": "Hablar con administracion", "intent": "derivar_administracion"},
            ],
        },
        "seguros": {
            "display_name": "Seguros",
            "description": "Cotizaciones, polizas, siniestros y documentacion.",
            "prompt_context": "Demo PYME rubro seguros. Prioriza cotizaciones, coberturas, polizas, siniestros y documentacion.",
            "quick_actions": [
                {"id": "cotizar", "label": "Cotizar seguro", "intent": "cotizar_seguro"},
                {"id": "denunciar_siniestro", "label": "Denunciar siniestro", "intent": "denunciar_siniestro"},
                {"id": "ver_poliza", "label": "Ver poliza", "intent": "consultar_poliza"},
                {"id": "documentacion", "label": "Documentacion", "intent": "consultar_documentacion"},
            ],
        },
        "logistica": {
            "display_name": "Logistica",
            "description": "Envios, seguimiento, retiros, tarifas y coordinacion operativa.",
            "prompt_context": "Demo PYME rubro logistica. Prioriza seguimiento, retiros, tarifas, zonas, horarios y novedades de envio.",
            "quick_actions": [
                {"id": "cotizar_envio", "label": "Cotizar envio", "intent": "cotizar_envio"},
                {"id": "seguir_envio", "label": "Seguir envio", "intent": "seguir_envio"},
                {"id": "coordinar_retiro", "label": "Coordinar retiro", "intent": "coordinar_retiro"},
                {"id": "zonas", "label": "Ver zonas", "intent": "consultar_zonas"},
            ],
        },
    }
    generic_pyme = {
        "display_name": "Comercio general",
        "description": "Catalogo, pedidos, stock, promociones, envios y seguimiento comercial.",
        "prompt_context": (
            "Demo PYME generica. Responde como asistente comercial adaptable: pide rubro o producto si falta contexto, "
            "ayuda con catalogo, precios, stock, pedido, envio y derivacion a una persona."
        ),
        "quick_actions": [
            {"id": "ver_catalogo", "label": "Ver catalogo", "intent": "ver_catalogo", "description": "Mostrar productos y precios."},
            {"id": "crear_pedido", "label": "Crear pedido", "intent": "crear_pedido", "description": "Tomar productos, cantidades y contacto."},
            {"id": "consultar_stock", "label": "Consultar stock", "intent": "consultar_stock", "description": "Validar disponibilidad."},
            {"id": "hablar_asesor", "label": "Hablar con asesor", "intent": "derivar_humano", "description": "Derivar a una persona."},
        ],
    }
    profile = pyme_profiles.get(slug) if sector == "empresas" else None
    profile = profile or generic_pyme if sector == "empresas" else {
        "display_name": label,
        "description": base["description"],
        "prompt_context": "",
        "quick_actions": [],
    }
    base.update(profile)
    base["label"] = profile.get("display_name") or base["label"]
    return base


def _safe_demo_json(path: Path) -> Any:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        current_app.logger.warning("No se pudo leer JSON demo %s: %s", path, exc)
    return None


def _non_empty(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (list, dict)) and not value:
        return None
    return value


def _tenant_owner_for_tools(tenant: TenantProfile) -> Any:
    return getattr(tenant, "municipio", None) or getattr(tenant, "pyme", None)


def _tenant_runtime_config_for_tools(tenant: TenantProfile) -> dict[str, Any]:
    cfg = tenant.configuracion if isinstance(tenant.configuracion, dict) else {}
    owner = _tenant_owner_for_tools(tenant)

    address = (
        _non_empty(cfg.get("direccion"))
        or _non_empty(cfg.get("address"))
        or _non_empty(cfg.get("domicilio"))
        or _non_empty(getattr(owner, "direccion", None))
    )
    city = _non_empty(cfg.get("ciudad")) or _non_empty(getattr(owner, "ciudad", None))
    lat = (
        _non_empty(cfg.get("lat"))
        or _non_empty(cfg.get("latitud"))
        or _non_empty(getattr(owner, "latitud", None))
    )
    lng = (
        _non_empty(cfg.get("lng"))
        or _non_empty(cfg.get("lon"))
        or _non_empty(cfg.get("longitud"))
        or _non_empty(getattr(owner, "longitud", None))
    )
    phone = (
        _non_empty(cfg.get("telefono"))
        or _non_empty(cfg.get("phone"))
        or _non_empty(tenant.dispatch_phone)
        or _non_empty(getattr(owner, "telefono", None))
    )
    whatsapp = (
        _non_empty((cfg.get("whatsapp") or {}).get("numero") if isinstance(cfg.get("whatsapp"), dict) else None)
        or _non_empty(cfg.get("numero_whatsapp"))
        or _non_empty(cfg.get("whatsapp_number"))
        or _non_empty(tenant.whatsapp_sender_id)
    )
    email = (
        _non_empty((cfg.get("contacto") or {}).get("email") if isinstance(cfg.get("contacto"), dict) else None)
        or _non_empty(cfg.get("public_email"))
        or _non_empty(cfg.get("email_contacto"))
        or _non_empty(tenant.dispatch_email)
    )
    website = (
        _non_empty(cfg.get("web_url"))
        or _non_empty(cfg.get("website"))
        or _non_empty(cfg.get("link_web"))
        or _non_empty(getattr(owner, "link_web", None))
        or _non_empty(tenant.dominio)
    )

    runtime: dict[str, Any] = {}
    if address:
        runtime["direccion"] = address
    if city:
        runtime["ciudad"] = city
    if lat is not None:
        runtime["lat"] = lat
    if lng is not None:
        runtime["lng"] = lng
    if address or (lat is not None and lng is not None):
        runtime["ubicaciones"] = [
            {
                "id": "tenant_main_location",
                "label": tenant.nombre or "Ubicacion principal",
                "direccion": address or "",
                "lat": lat,
                "lng": lng,
            }
        ]
    if phone or email or website:
        runtime["contacto"] = {
            "telefono": phone or "",
            "email": email or "",
            "web": website or "",
        }
    if whatsapp:
        runtime["whatsapp"] = {"numero": str(whatsapp).replace("whatsapp:", "", 1)}
    if _non_empty(cfg.get("horarios")) or _non_empty(cfg.get("hours")) or _non_empty(cfg.get("horario_atencion")):
        runtime["horarios"] = cfg.get("horarios") or cfg.get("hours") or cfg.get("horario_atencion")
    if isinstance(cfg.get("resources"), list):
        runtime["resources"] = cfg.get("resources")
    return runtime


def _merge_demo_tool_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (overlay or {}).items():
        value = _non_empty(value)
        if value is None:
            continue
        if key == "resources" and isinstance(value, list):
            base_resources = merged.get("resources") if isinstance(merged.get("resources"), list) else []
            merged["resources"] = [*base_resources, *value]
        elif key == "ubicaciones" and isinstance(value, list):
            base_locations = merged.get("ubicaciones") if isinstance(merged.get("ubicaciones"), list) else []
            merged["ubicaciones"] = [*value, *base_locations]
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update({nested_key: nested_value for nested_key, nested_value in value.items() if _non_empty(nested_value) is not None})
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _demo_pyme_file_candidates(rubro: str, tenant: TenantProfile, filename: str) -> list[Path]:
    root = Path(current_app.root_path) / "data" / "pyme" / "rubros"
    slug = _payload_slug(rubro or tenant.slug)
    tenant_slug = _payload_slug(tenant.slug)
    candidates: list[Path] = []

    if slug:
        if tenant_slug:
            candidates.append(root / slug / tenant_slug / filename)
        candidates.append(root / slug / filename)
        rubro_dir = root / slug
        if rubro_dir.is_dir():
            for child in sorted(rubro_dir.iterdir(), key=lambda item: item.name):
                if child.is_dir():
                    candidates.append(child / filename)

    candidates.append(root / "default" / filename)

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _demo_static_config_for_rubro(*, sector: str, rubro: str, tenant: TenantProfile) -> dict[str, Any]:
    tenant_config = _tenant_runtime_config_for_tools(tenant)
    if sector == "gobierno":
        raw = _safe_demo_json(Path(current_app.root_path) / "data" / "municipios" / "default" / "config.json")
        static_config = raw if isinstance(raw, dict) else {}
        return _merge_demo_tool_config(static_config, tenant_config)

    for candidate in _demo_pyme_file_candidates(rubro, tenant, "config.json"):
        raw = _safe_demo_json(candidate)
        if isinstance(raw, dict) and raw:
            return _merge_demo_tool_config(raw, tenant_config)
    return tenant_config


def _demo_static_faq_for_rubro(*, rubro: str, tenant: TenantProfile) -> list[dict[str, str]]:
    raw_items: Any = None
    for candidate in _demo_pyme_file_candidates(rubro, tenant, "faq.json"):
        raw_items = _safe_demo_json(candidate)
        if raw_items:
            break

    if isinstance(raw_items, dict):
        raw_items = raw_items.get("items") or raw_items.get("faqs") or raw_items.get("questions") or []
    if not isinstance(raw_items, list):
        return []

    normalized: list[dict[str, str]] = []
    for idx, entry in enumerate(raw_items[:8], start=1):
        if isinstance(entry, str):
            question = entry.strip()
            answer = ""
            extended_answer = ""
        elif isinstance(entry, dict):
            question = str(entry.get("question") or entry.get("pregunta") or entry.get("label") or "").strip()
            answer = str(entry.get("answer") or entry.get("respuesta") or "").strip()
            extended_answer = str(entry.get("extended_answer") or entry.get("detalle") or "").strip()
        else:
            continue
        if not question:
            continue
        normalized.append(
            {
                "id": f"faq_{idx}",
                "question": question,
                "answer": answer,
                "extended_answer": extended_answer,
            }
        )
    return normalized


def _normalize_demo_resource(resource: dict[str, Any], index: int) -> dict[str, Any] | None:
    label = str(
        resource.get("label")
        or resource.get("title")
        or resource.get("name")
        or resource.get("cta_text")
        or ""
    ).strip()
    url = str(resource.get("url") or resource.get("href") or resource.get("link") or "").strip()
    if not label or not url:
        return None
    kind = str(resource.get("kind") or resource.get("type") or "link").strip().lower()
    item_id = str(resource.get("id") or _payload_slug(label) or f"resource_{index}").strip()
    return {
        "id": item_id,
        "label": label,
        "kind": kind,
        "url": url,
        "description": str(resource.get("description") or resource.get("detail") or "").strip(),
        "cta_label": str(resource.get("cta_label") or resource.get("cta_text") or "Abrir").strip(),
        "highlight": resource.get("highlight"),
        "availability": resource.get("availability"),
        "thumbnail": resource.get("thumbnail") or resource.get("thumbnail_url"),
    }


def _merge_demo_resources(*groups: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for raw in group:
            if not isinstance(raw, dict):
                continue
            item = _normalize_demo_resource(raw, len(merged) + 1)
            if not item:
                continue
            key = item.get("url") or item.get("id")
            if key in seen:
                continue
            seen.add(str(key))
            merged.append(item)
    return merged


def _google_maps_url(*, address: str | None = None, lat: Any = None, lng: Any = None) -> str | None:
    lat_value = str(lat or "").strip()
    lng_value = str(lng or "").strip()
    if lat_value and lng_value:
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(lat_value + ',' + lng_value)}"
    address_value = str(address or "").strip()
    if address_value:
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address_value)}"
    return None


def _extract_demo_locations(config: dict[str, Any], tenant: TenantProfile) -> list[dict[str, Any]]:
    raw_locations = config.get("ubicaciones") or config.get("locations") or config.get("sucursales") or []
    if isinstance(raw_locations, dict):
        raw_locations = list(raw_locations.values())
    if not isinstance(raw_locations, list):
        raw_locations = []

    locations: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_locations, start=1):
        if not isinstance(raw, dict):
            continue
        address = str(raw.get("address") or raw.get("direccion") or raw.get("domicilio") or "").strip()
        lat = raw.get("lat") or raw.get("latitude")
        lng = raw.get("lng") or raw.get("lon") or raw.get("longitude")
        maps_url = _google_maps_url(address=address, lat=lat, lng=lng)
        if not maps_url:
            continue
        locations.append(
            {
                "id": str(raw.get("id") or f"location_{index}"),
                "label": str(raw.get("label") or raw.get("nombre") or tenant.nombre or "Ubicacion").strip(),
                "address": address,
                "lat": lat,
                "lng": lng,
                "maps_url": maps_url,
            }
        )

    address = str(config.get("direccion") or config.get("address") or config.get("domicilio") or "").strip()
    if address and not any(item.get("address") == address for item in locations):
        maps_url = _google_maps_url(address=address)
        if maps_url:
            locations.append(
                {
                    "id": "main_location",
                    "label": str(config.get("ciudad") or tenant.nombre or "Ubicacion principal").strip(),
                    "address": address,
                    "lat": None,
                    "lng": None,
                    "maps_url": maps_url,
                }
            )
    return locations


def _extract_demo_contact(config: dict[str, Any], tenant: TenantProfile) -> dict[str, Any]:
    contacto = config.get("contacto") if isinstance(config.get("contacto"), dict) else {}
    whatsapp = config.get("whatsapp") if isinstance(config.get("whatsapp"), dict) else {}
    phone = (
        contacto.get("telefono")
        or contacto.get("phone")
        or config.get("telefono")
        or config.get("phone")
        or whatsapp.get("numero")
        or _demo_whatsapp_number_for_tenant(tenant)
    )
    website = (
        contacto.get("web")
        or contacto.get("website")
        or config.get("web_url")
        or config.get("website")
        or config.get("link_web")
    )
    return {
        "phone": str(phone or "").strip(),
        "whatsapp": str(whatsapp.get("numero") or phone or "").strip(),
        "email": str(contacto.get("email") or config.get("email") or "").strip(),
        "website": str(website or "").strip(),
    }


def _extract_demo_hours(config: dict[str, Any]) -> Any:
    return (
        config.get("horarios")
        or config.get("hours")
        or config.get("horario_atencion")
        or config.get("opening_hours")
    )


def _demo_tool_contract(
    *,
    key: str,
    label: str,
    description: str,
    enabled: bool,
    items: list[dict[str, Any]] | None = None,
    data: Any = None,
    intent: str | None = None,
    action_id: str | None = None,
    action_label: str | None = None,
    action_url: str | None = None,
    tool_mode: str | None = None,
    fields: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_action = action_id or intent or f"tool_{key}"
    resolved_mode = tool_mode or ("downloadable" if action_url else "chat_action")
    return {
        "id": key,
        "kind": "rubro_tool",
        "tool_mode": resolved_mode,
        "label": label,
        "intent": resolved_action,
        "action_id": resolved_action if resolved_mode != "downloadable" else None,
        "description": description,
        "enabled": bool(enabled),
        "items": items or [],
        "data": data,
        "fields": fields or [],
        "action_label": action_label,
        "action_url": action_url,
    }


def _first_demo_item_url(items: list[dict[str, Any]], *keys: str) -> str | None:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for key in keys:
            value = str(item.get(key) or "").strip()
            if value:
                return value
    return None


def _contact_action_url(contact: dict[str, Any]) -> str | None:
    whatsapp = str(contact.get("whatsapp") or contact.get("phone") or "").strip()
    if whatsapp:
        digits = "".join(ch for ch in whatsapp if ch.isdigit())
        if digits:
            return f"https://wa.me/{digits}"
    email = str(contact.get("email") or "").strip()
    if email:
        return f"mailto:{email}"
    website = str(contact.get("website") or "").strip()
    return website or None


def _demo_rubro_tools_contract(
    *,
    sector: str,
    rubro: str,
    tenant: TenantProfile,
    rubro_context: dict[str, Any],
    catalog_resources: list[dict[str, Any]],
) -> dict[str, Any]:
    config = _demo_static_config_for_rubro(sector=sector, rubro=rubro, tenant=tenant)
    faq_preview = [] if sector == "gobierno" else _demo_static_faq_for_rubro(rubro=rubro, tenant=tenant)

    config_resources = config.get("resources") if isinstance(config.get("resources"), list) else []
    resources = _merge_demo_resources(catalog_resources, rubro_context.get("resources"), config_resources)
    government_links = []
    if sector == "gobierno":
        if config.get("tramites_web_url"):
            government_links.append(
                {
                    "id": "tramites_web",
                    "label": "Tramites online",
                    "kind": "link",
                    "url": str(config.get("tramites_web_url")),
                    "description": "Portal publico de tramites.",
                    "cta_label": "Abrir tramites",
                }
            )
        if config.get("web_url"):
            government_links.append(
                {
                    "id": "sitio_oficial",
                    "label": "Sitio oficial",
                    "kind": "link",
                    "url": str(config.get("web_url")),
                    "description": "Sitio publico del organismo.",
                    "cta_label": "Abrir sitio",
                }
            )
    resources = _merge_demo_resources(resources, government_links)

    price_terms = ("precio", "price", "lista", "stock", "excel", "xlsx", "spreadsheet")
    price_resources = [
        item
        for item in resources
        if any(
            term
            in " ".join(
                str(item.get(field) or "").lower()
                for field in ("id", "label", "kind", "url", "description", "cta_label")
            )
            for term in price_terms
        )
    ]
    locations = _extract_demo_locations(config, tenant)
    contact = _extract_demo_contact(config, tenant)
    contact_enabled = any(contact.get(key) for key in ("phone", "whatsapp", "email", "website"))
    hours = _extract_demo_hours(config)

    tools = [
        _demo_tool_contract(
            key="catalog",
            label="Catalogo",
            description="Recursos publicados para productos, servicios o tramites.",
            enabled=bool(resources),
            items=resources,
            intent="ver_catalogo",
            action_label="Abrir catalogo" if resources else None,
            action_url=_first_demo_item_url(resources, "url", "href"),
            tool_mode="downloadable",
            fields=[{"label": "Recursos", "value": len(resources)}] if resources else [],
        ),
        _demo_tool_contract(
            key="price_list",
            label="Lista de precios",
            description="Precios, stock o lista descargable cuando el rubro la publica.",
            enabled=bool(price_resources),
            items=price_resources,
            intent="consultar_precios",
            action_label="Ver lista de precios" if price_resources else None,
            action_url=_first_demo_item_url(price_resources, "url", "href"),
            tool_mode="downloadable",
            fields=[{"label": "Listas", "value": len(price_resources)}] if price_resources else [],
        ),
        _demo_tool_contract(
            key="location",
            label="Ubicacion",
            description="Direcciones publicadas por el rubro; las nuevas ubicaciones se envian dentro del chat.",
            enabled=bool(locations),
            items=locations,
            intent="consultar_ubicacion",
            action_label="Consultar ubicacion" if locations else None,
            tool_mode="chat_action",
            fields=[{"label": "Ubicaciones", "value": len(locations)}] if locations else [],
        ),
        _demo_tool_contract(
            key="contact",
            label="Telefono y contacto",
            description="Canales reales o configurados para contacto.",
            enabled=contact_enabled,
            data=contact if contact_enabled else None,
            intent="consultar_contacto",
            action_label="Contactar" if contact_enabled else None,
            tool_mode="chat_action",
            fields=[
                {"label": "Telefono", "value": contact.get("phone")},
                {"label": "WhatsApp", "value": contact.get("whatsapp")},
                {"label": "Email", "value": contact.get("email")},
                {"label": "Web", "value": contact.get("website")},
            ] if contact_enabled else [],
        ),
        _demo_tool_contract(
            key="hours",
            label="Horarios",
            description="Horarios de atencion publicados por el rubro.",
            enabled=bool(hours),
            data=hours if hours else None,
            intent="consultar_horarios",
            action_label="Consultar horarios" if hours else None,
            tool_mode="chat_action",
            fields=[{"label": "Horarios", "value": hours}] if isinstance(hours, str) else [],
        ),
        _demo_tool_contract(
            key="faq",
            label="Consultas frecuentes",
            description="Preguntas frecuentes trazables del rubro.",
            enabled=bool(faq_preview),
            items=faq_preview,
            intent="consultar_faq",
            action_label="Consultar" if faq_preview else None,
            tool_mode="chat_action",
            fields=[{"label": "Preguntas", "value": len(faq_preview)}] if faq_preview else [],
        ),
    ]

    enabled_tools = [tool for tool in tools if tool.get("enabled")]
    return {
        "contract_version": "demo.rubro_tools.v1",
        "sector": sector,
        "rubro": rubro,
        "tenant_slug": tenant.slug,
        "display_name": rubro_context.get("label") or tenant.nombre,
        "tools": tools,
        "enabled_tools": enabled_tools,
        "resources": resources,
        "price_resources": price_resources,
        "locations": locations,
        "contact": contact if contact_enabled else {},
        "hours": hours,
        "faq_preview": faq_preview,
        "frontend_contract": {
            "render_as": "tool_tray",
            "source_path": "workspace.rubro_tools.enabled_tools",
            "hide_disabled_tools": True,
            "open_maps_with": "items[].maps_url",
            "do_not_invent_missing_tools": True,
        },
        "llm_context": {
            "resources": resources[:8],
            "price_resources": price_resources[:5],
            "locations": locations[:5],
            "contact": contact if contact_enabled else {},
            "hours": hours,
            "faq_preview": faq_preview[:6],
        },
    }


def _compact_rubro_tools_contract(rubro_tools: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rubro_tools, dict):
        return {}
    enabled_tools = rubro_tools.get("enabled_tools") if isinstance(rubro_tools.get("enabled_tools"), list) else []
    return {
        "contract_version": rubro_tools.get("contract_version") or "demo.rubro_tools.v1",
        "sector": rubro_tools.get("sector"),
        "rubro": rubro_tools.get("rubro"),
        "tenant_slug": rubro_tools.get("tenant_slug"),
        "display_name": rubro_tools.get("display_name"),
        "enabled_tools": enabled_tools,
        "resources": rubro_tools.get("resources") or [],
        "price_resources": rubro_tools.get("price_resources") or [],
        "locations": rubro_tools.get("locations") or [],
        "contact": rubro_tools.get("contact") or {},
        "hours": rubro_tools.get("hours"),
        "faq_preview": rubro_tools.get("faq_preview") or [],
        "frontend_contract": rubro_tools.get("frontend_contract") or {},
    }


def _compact_survey_item(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    return {
        "contract_version": item.get("contract_version") or "demo.survey_item.v1",
        "id": item.get("id"),
        "slug": item.get("slug"),
        "slug_publico": item.get("slug_publico"),
        "tenant_slug": item.get("tenant_slug"),
        "sector": item.get("sector"),
        "tipo": item.get("tipo"),
        "titulo": item.get("titulo"),
        "title": item.get("title") or item.get("titulo"),
        "descripcion": item.get("descripcion"),
        "description": item.get("description") or item.get("descripcion"),
        "demo_mode": bool(item.get("demo_mode")),
        "es_votacion_envivo": bool(item.get("es_votacion_envivo")),
        "mostrar_resultados_envivo": bool(item.get("mostrar_resultados_envivo")),
        "seed": item.get("seed") or {},
        "analytics_summary": item.get("analytics_summary") or {},
        "url_publica": item.get("url_publica"),
        "public_url": item.get("public_url"),
        "share_url": item.get("share_url"),
        "whatsapp_share_url": item.get("whatsapp_share_url"),
        "share_whatsapp_url": item.get("share_whatsapp_url"),
        "public_api_endpoint": item.get("public_api_endpoint"),
        "respond_endpoint": item.get("respond_endpoint"),
        "results_endpoint": item.get("results_endpoint"),
    }


def _compact_survey_voting_contract(contract: dict[str, Any], *, include_items: bool = False) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    compact = {
        "contract_version": contract.get("contract_version") or "demo.survey_voting.v1",
        "enabled": bool(contract.get("enabled")),
        "demo_mode": bool(contract.get("demo_mode")),
        "tenant_slug": contract.get("tenant_slug"),
        "sector": contract.get("sector"),
        "rubro": contract.get("rubro"),
        "label": contract.get("label"),
        "description": contract.get("description"),
        "availability_rule": contract.get("availability_rule"),
        "primary_action_enabled": bool(contract.get("primary_action_enabled")),
        "page": contract.get("page"),
        "page_size": contract.get("page_size"),
        "total_available": contract.get("total_available"),
        "has_more": bool(contract.get("has_more")),
        "next_action_id": contract.get("next_action_id"),
        "previous_action_id": contract.get("previous_action_id"),
        "seed_policy": contract.get("seed_policy") or {},
        "primary_action": contract.get("primary_action") or {},
        "public_list_endpoint": contract.get("public_list_endpoint"),
        "respond_endpoint_template": contract.get("respond_endpoint_template") or contract.get("public_response_endpoint_template"),
        "live_results_endpoint_template": contract.get("live_results_endpoint_template") or contract.get("results_endpoint"),
        "public_detail_endpoint_template": contract.get("public_detail_endpoint_template"),
        "frontend_contract": contract.get("frontend_contract") or {},
    }
    if include_items:
        compact["items"] = [
            compact_item
            for compact_item in (_compact_survey_item(item) for item in contract.get("items") or [])
            if compact_item
        ]
    return compact


def _compact_whatsapp_sandbox_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    compact = dict(contract)
    if isinstance(compact.get("surveys_votings"), dict):
        compact["surveys_votings"] = _compact_survey_voting_contract(compact["surveys_votings"], include_items=False)
    return compact


def _demo_chat_metadata(
    *,
    sector: str,
    rubro: str,
    tenant: TenantProfile,
    rubro_context: dict[str, Any],
    default_menu: dict[str, Any] | None = None,
    rubro_tools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool_context = rubro_tools or {}
    return {
        "contract_version": "demo.chat_metadata.v1",
        "key": rubro,
        "rubro_clave": rubro,
        "sector": sector,
        "tenant_slug": tenant.slug,
        "display_name": rubro_context.get("label") or tenant.nombre,
        "description": rubro_context.get("description") or "",
        "prompt_context": rubro_context.get("prompt_context") or "",
        "quick_actions": (default_menu or {}).get("items") or rubro_context.get("quick_actions") or [],
        "resources": tool_context.get("resources") or rubro_context.get("resources") or [],
        "faq_preview": tool_context.get("faq_preview") or rubro_context.get("sample_prompts") or [],
        "enabled_tool_ids": [
            str(tool.get("id") or "")
            for tool in tool_context.get("enabled_tools") or []
            if isinstance(tool, dict) and tool.get("id")
        ],
        "tool_summary": tool_context.get("llm_context") or {},
    }


def _error_response(message: str, status_code: int, reason_code: str, action_hint: str):
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "ok": False,
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
        "catalogo-demo-colegios.pdf": ("colegios", "catalogo-demo-colegios.pdf"),
        "municipio-demo.pdf": ("gobiernos", "catalogo-demo-gobiernos.pdf"),
        "gobierno-demo.pdf": ("gobiernos", "catalogo-demo-gobiernos.pdf"),
        "catalogo-demo-gobiernos.pdf": ("gobiernos", "catalogo-demo-gobiernos.pdf"),
        "empresa-demo.pdf": ("empresas", "catalogo-demo-empresas.pdf"),
        "empresas-demo.pdf": ("empresas", "catalogo-demo-empresas.pdf"),
        "catalogo-demo-empresas.pdf": ("empresas", "catalogo-demo-empresas.pdf"),
    }
    allowed_folders = {"colegios", "gobiernos", "empresas"}
    raw = str(filename or "").strip().replace("\\", "/").strip("/")
    parts = [part for part in raw.split("/") if part]

    folder = ""
    asset_name = ""
    if len(parts) >= 2:
        folder_candidate = parts[-2].lower()
        asset_candidate = parts[-1]
        if folder_candidate in allowed_folders:
            folder, asset_name = folder_candidate, asset_candidate
    if not asset_name and parts:
        clean = parts[-1]
        folder, asset_name = aliases.get(clean, ("", clean))

    if (
        not folder
        or folder not in allowed_folders
        or not asset_name
        or ".." in asset_name
        or "/" in asset_name
        or "\\" in asset_name
        or not asset_name.lower().endswith(".pdf")
    ):
        abort(404)

    base_dir = Path(current_app.root_path) / "data" / "demo_catalogs"
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
        return "/api/ask/municipio"
    if normalized == "pyme":
        return "/api/ask/pyme"
    return "/api/ask"


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
            endpoint = str(source.get("endpoint") or "/api/ask").strip()
            if endpoint.startswith("/ask"):
                endpoint = f"/api{endpoint}"
            actions.append(
                {
                    "id": str(source.get("id") or action_id),
                    "intent": str(action_id),
                    "label": source.get("label") or source.get("title") or str(action_id),
                    "endpoint": endpoint,
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


def _operational_action(
    *,
    label: str,
    action_id: str,
    description: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "title": label,
        "texto": label,
        "description": description,
        "action_id": action_id,
        "intent": action_id,
        "action": action_id,
        "type": "quick_reply",
        "kind": "operational_action",
        "enabled": True,
        "payload": payload or {},
    }


def _short_operational_menu_contract(
    *,
    sector: str,
    tenant: TenantProfile,
    education_payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_demo_sector(sector)
    survey_action = _operational_action(
        label="Encuestas y votaciones",
        description="Lista encuestas demo con links para participar y compartir por WhatsApp.",
        action_id="mostrar_menu_encuestas",
        payload={"demo_mode": True, "sector": normalized, "tenant_slug": tenant.slug},
    )
    if normalized == "educacion":
        configured = []
        if isinstance(education_payload, dict):
            configured.extend(education_payload.get("primary_actions") or [])
            configured.extend(education_payload.get("quick_menu") or [])
        if configured:
            actions: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in configured:
                if not isinstance(item, dict):
                    continue
                action_id = str(item.get("action_id") or item.get("intent") or item.get("action") or item.get("id") or "").strip()
                label = str(item.get("label") or item.get("texto") or item.get("title") or "").strip()
                if not action_id or not label or action_id in seen:
                    continue
                seen.add(action_id)
                actions.append(
                    _operational_action(
                        label=label,
                        action_id=action_id,
                        description=str(item.get("description") or item.get("descripcion") or ""),
                        payload=item.get("payload") if isinstance(item.get("payload"), dict) else {},
                    )
                )
                if len(actions) >= 4:
                    break
            if survey_action["action_id"] not in seen:
                actions.append(survey_action)
            if actions:
                return actions[:5]
        return [
            _operational_action(
                label="Crear caso escolar",
                description="Conta que paso y adjunta foto, audio o PDF si hace falta.",
                action_id="create_school_case",
                payload={"education_context": {"is_education": True, "tenant_slug": tenant.slug}},
            ),
            _operational_action(
                label="Justificar inasistencia",
                description="Carga alumno, curso, fecha, motivo y certificado si existe.",
                action_id="justify_absence",
                payload={"education_context": {"is_education": True, "tenant_slug": tenant.slug}},
            ),
            _operational_action(
                label="Hablar con secretaria",
                description="Crea espera para secretaria o muestra horario real.",
                action_id="talk_secretary",
                payload={"education_context": {"is_education": True, "tenant_slug": tenant.slug}},
            ),
            survey_action,
        ]
    if normalized == "gobierno":
        return [
            _operational_action(
                label="Crear reclamo",
                description="Contame que paso. Podes adjuntar foto, audio o ubicacion.",
                action_id="crear_reclamo",
                payload={"vertical": "municipio"},
            ),
            _operational_action(
                label="Consultar estado",
                description="Busca un reclamo por numero o datos de contacto.",
                action_id="consultar_estado_reclamo",
            ),
            _operational_action(
                label="Consultar tramite",
                description="Responde desde tramites publicados por el municipio.",
                action_id="consultar_tramite",
            ),
            _operational_action(
                label="Hablar con una persona",
                description="Deriva a mesa de atencion si esta disponible.",
                action_id="derivar_humano",
            ),
            survey_action,
        ]
    if normalized == "empresas":
        return [
            _operational_action(
                label="Consultar producto",
                description="Busca precio, disponibilidad o detalle publicado por el comercio.",
                action_id="consultar_producto",
            ),
            _operational_action(
                label="Crear pedido",
                description="Pide datos y confirma el pedido solo con respuesta del backend.",
                action_id="crear_pedido",
            ),
            _operational_action(
                label="Cotizar envio",
                description="Usa direccion o ubicacion enviada por el usuario.",
                action_id="cotizar_envio",
            ),
            _operational_action(
                label="Hablar con ventas",
                description="Deriva o registra lead comercial.",
                action_id="capturar_lead_comercial",
            ),
            survey_action,
        ]
    return [
        _operational_action(
            label="Registrar consulta",
            description="Deja una solicitud trazable para el equipo.",
            action_id="registrar_solicitud_operativa",
            payload={"tipo_solicitud": "consulta"},
        ),
        _operational_action(
            label="Hablar con una persona",
            description="Pide datos de contacto y deriva al equipo.",
            action_id="derivar_humano",
        ),
    ]


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
    demo_contract = build_demo_surveys_votings_contract(
        sector=normalized,
        tenant_slug=tenant_slug,
    )
    return {
        **demo_contract,
        "contract_version": "demo.survey_voting.v1",
        "enabled": normalized in {"educacion", "gobierno", "empresas"},
        "primary_action_enabled": True,
        "availability_rule": "always_visible_in_demo",
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
            **(demo_contract.get("frontend_contract") or {}),
            "render_as": "survey_voting_module",
            "show_only_when_enabled": False,
            "empty_state_behavior": "render_demo_seeded_surveys",
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
    rubro_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    endpoint = _chat_endpoint_for_tenant_type(tenant_type)
    canonical_rubro = (rubro or tenant.slug or tenant_type or "").strip().lower()
    first_prompt = next((item.get("payload") or item.get("label") for item in quick_replies if item.get("label")), "")
    anon_id = (
        request.headers.get("X-Anon-Id")
        or request.args.get("anon_id")
        or ((request.get_json(silent=True) or {}).get("anon_id") if request.is_json else None)
        or ""
    )
    chat_session_id = _stable_demo_chat_session_id(demo_session_id)
    demo_metadata = _demo_chat_metadata(
        sector=sector,
        rubro=canonical_rubro,
        tenant=tenant,
        rubro_context=rubro_context or {},
    )

    return {
        "contract_version": "demo.chat_bootstrap.v1",
        "endpoint": endpoint,
        "same_origin_endpoint": endpoint,
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
            "X-Anon-Id": anon_id,
        },
        "query": {
            "tenant_slug": tenant.slug,
            "tenant": tenant.slug,
        },
        "payload": {
            "pregunta": "",
            "tipo_chat": tenant_type,
            "tenant_slug": tenant.slug,
            "tenant": tenant.slug,
            "rubro": canonical_rubro,
            "rubro_clave": canonical_rubro,
            "vertical": vertical,
            "education_profile": education_profile,
            "education_context": education_profile,
            "rubro_context": rubro_context,
            "demo_metadata": demo_metadata,
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
            "education_context": education_profile,
            "rubro_context": rubro_context,
            "demo_metadata": demo_metadata,
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


def _demo_session_selection_contract(
    *,
    sector: str,
    rubro: str,
    tenant: TenantProfile,
    chat_bootstrap: dict[str, Any],
    quick_replies: list[dict[str, str]],
    admin_preview_endpoint: str,
    response_profile: str,
) -> dict[str, Any]:
    chat_session_id = (chat_bootstrap.get("session") or {}).get("chat_session_id")
    demo_session_id = (chat_bootstrap.get("session") or {}).get("demo_session_id")
    return {
        "contract_version": "demo.widget_onboarding_result.v1",
        "ok": True,
        "status": "ready",
        "state": "ready",
        "ready": True,
        "response_profile": response_profile,
        "selected_sector": sector,
        "selected_rubro": rubro or tenant.slug,
        "tenant_slug": tenant.slug,
        "tenant_tipo": tenant.tipo,
        "chat_session_id": chat_session_id,
        "demo_session_id": demo_session_id,
        "chat_bootstrap_path": "workspace.chat_bootstrap",
        "open_chat": True,
        "close_selector": True,
        "autostart_chat": True,
        "send_init_once": True,
        "admin_preview_endpoint": admin_preview_endpoint,
        "initial_prompt": chat_bootstrap.get("initial_prompt") or "",
        "quick_replies": quick_replies[:3],
        "error_message": None,
    }


def _demo_session_frontend_contract(
    *,
    sector: str,
    rubro: str,
    tenant: TenantProfile,
    response_profile: str,
) -> dict[str, Any]:
    return {
        "contract_version": "demo.session_frontend.v1",
        "render_as": "demo_session_ready",
        "success_condition": "http_200_and_ok_true",
        "response_profile": response_profile,
        "selected_sector": sector,
        "selected_rubro": rubro or tenant.slug,
        "tenant_slug": tenant.slug,
        "use_chat_bootstrap_from": "workspace.chat_bootstrap",
        "preserve_session_headers": True,
        "show_error_only_when_ok_false": True,
        "do_not_infer_endpoint_locally": True,
    }


def _demo_default_menu_contract(
    *,
    sector: str,
    tenant: TenantProfile,
    quick_replies: list[dict[str, str]],
    value_cards: list[dict[str, Any]],
    allowed_actions: list[dict[str, Any]],
    rubro_context: dict[str, Any] | None = None,
    rubro_tools: dict[str, Any] | None = None,
    education_payload: dict[str, Any] | None = None,
    primary_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    def add_item(source: dict[str, Any], *, kind: str = "action") -> None:
        item_id = str(source.get("id") or source.get("key") or source.get("action_id") or source.get("intent") or source.get("label") or source.get("texto") or "").strip()
        label = str(source.get("label") or source.get("texto") or source.get("title") or source.get("cta_label") or "").strip()
        intent = str(source.get("action_id") or source.get("intent") or source.get("action") or source.get("payload") or item_id).strip()
        if not item_id or not label:
            return
        if any(existing.get("id") == item_id or existing.get("intent") == intent for existing in items):
            return
        items.append(
            {
                "id": item_id,
                "label": label,
                "intent": intent,
                "action_id": intent,
                "description": source.get("description") or source.get("detail") or "",
                "icon": source.get("icon"),
                "kind": kind,
                "enabled": bool(source.get("enabled", True)),
            }
        )

    education_menu = (education_payload or {}).get("quick_menu") if isinstance(education_payload, dict) else []
    education_primary = (education_payload or {}).get("primary_actions") if isinstance(education_payload, dict) else []
    rubro_menu = (rubro_context or {}).get("quick_actions") if isinstance(rubro_context, dict) else []
    enabled_tools = (rubro_tools or {}).get("enabled_tools") if isinstance(rubro_tools, dict) else []
    primary_regular: list[dict[str, Any]] = []
    primary_surveys: list[dict[str, Any]] = []
    for item in primary_actions or []:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or item.get("intent") or item.get("action") or item.get("id") or "").strip()
        if action_id == "mostrar_menu_encuestas":
            primary_surveys.append(item)
        else:
            primary_regular.append(item)
    for item in primary_regular[:3]:
        if isinstance(item, dict):
            add_item(item, kind="operational_action")
    for item in education_primary or []:
        if isinstance(item, dict):
            add_item(item, kind="education_primary_action")
    for item in rubro_menu or []:
        if isinstance(item, dict):
            add_item(item, kind="rubro_action")
    for item in primary_surveys:
        if isinstance(item, dict):
            add_item(item, kind="operational_action")
    for item in primary_regular[3:]:
        if isinstance(item, dict):
            add_item(item, kind="operational_action")
    for item in enabled_tools or []:
        if isinstance(item, dict):
            add_item(item, kind="rubro_tool")
    for item in education_menu or []:
        if isinstance(item, dict):
            add_item(item, kind="education_action")
    for item in value_cards or []:
        if isinstance(item, dict):
            add_item(item, kind="action")
    for item in allowed_actions or []:
        if isinstance(item, dict):
            add_item(item, kind="backend_action")
    if not items:
        for item in quick_replies or []:
            if isinstance(item, dict):
                add_item(item, kind="message")

    return {
        "contract_version": "demo.default_menu.v1",
        "render_as": "quick_menu",
        "source": "backend_demo_session",
        "selected_sector": sector,
        "selected_rubro": (rubro_context or {}).get("slug") or tenant.slug,
        "tenant_slug": tenant.slug,
        "rubro_context": rubro_context,
        "max_visible_items": 5,
        "collapse_extra_items": True,
        "items": items[:5],
        "starter_prompts": quick_replies,
        "empty_state": None if items else "Sin menu predeterminado disponible para este rubro.",
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


def _demo_ticket_details(ticket: MunicipioTicket) -> dict[str, Any]:
    try:
        parsed = json.loads(ticket.detalles or "{}")
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _is_runtime_demo_ticket(ticket: MunicipioTicket) -> bool:
    details = _demo_ticket_details(ticket)
    return bool(
        details.get("demo_runtime")
        or details.get("source") == "demo_municipio_runtime"
        or str(ticket.canal_ingreso or "") == "web_demo_widget"
    ) and str(ticket.categoria or "") != "lead_demo_prospecto"


def _resolve_preview_tenant(tenant_slug: str) -> TenantProfile | None:
    slug = str(tenant_slug or "").strip().lower()
    if not slug:
        return None
    try:
        return resolve_tenant_only(tenant_slug=slug, require_explicit_slug=True)
    except Exception:
        return None


def _recent_demo_municipio_tickets(tenant_slug: str, chat_session_id: str = "") -> list[MunicipioTicket]:
    tenant = _resolve_preview_tenant(tenant_slug)
    if tenant is None:
        return []
    session_filter = str(chat_session_id or "").strip()
    query = (
        scoped_municipio_ticket_query(tenant)
        .order_by(MunicipioTicket.fecha.desc())
        .limit(80)
    )
    tickets: list[MunicipioTicket] = []
    for ticket in query.all():
        if _is_runtime_demo_ticket(ticket):
            if session_filter:
                details = _demo_ticket_details(ticket)
                if str(details.get("chat_session_id") or "") != session_filter:
                    continue
            tickets.append(ticket)
    return tickets[:10]


def _apply_gobierno_session_activity(preset: dict[str, Any], tenant_slug: str, chat_session_id: str = "") -> dict[str, Any]:
    tickets = _recent_demo_municipio_tickets(tenant_slug, chat_session_id=chat_session_id)
    if not tickets:
        return {
            "cards": preset["cards"],
            "modules": preset["modules"],
            "timeline": preset["timeline"],
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
        }

    points: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    comments_count = 0
    for ticket in tickets:
        details = _demo_ticket_details(ticket)
        try:
            comments_count += ticket.comentarios.count()
        except Exception:
            comments_count += len(details.get("events") or [])
        if ticket.latitud is not None and ticket.longitud is not None:
            points.append(
                {
                    "id": f"ticket-{ticket.id}",
                    "lat": ticket.latitud,
                    "lng": ticket.longitud,
                    "label": ticket.categoria or "Reclamo",
                    "status": ticket.estado,
                    "ticket_id": ticket.id,
                    "ticket_code": ticket.nro_ticket,
                    "address": ticket.direccion,
                    "weight": 1,
                }
            )
        items.append(
            {
                "id": f"ticket-{ticket.id}",
                "type": "ticket",
                "label": ticket.asunto or ticket.categoria or "Reclamo ciudadano",
                "status": ticket.estado,
                "category": ticket.categoria,
                "ticket_id": ticket.id,
                "ticket_code": ticket.nro_ticket,
                "detail_endpoint": f"/api/v2/inbox/omnichannel/{ticket.id}",
                "has_location": bool(ticket.latitud is not None and ticket.longitud is not None),
                "media_count": len(details.get("media") or []),
            }
        )

    modules = []
    for module in preset["modules"]:
        cloned = dict(module)
        if cloned.get("id") == "heatmap":
            cloned["enabled"] = bool(points)
            if points:
                cloned.pop("empty_state", None)
        modules.append(cloned)

    cards = [
        {
            "label": "Reclamos creados en esta sesion",
            "value": str(len(tickets)),
            "detail": "tickets reales generados por el runtime demo",
        },
        {
            "label": "Ubicaciones capturadas",
            "value": str(len(points)),
            "detail": "puntos reales listos para mapa operativo",
        },
        {
            "label": "Mensajes y evidencias",
            "value": str(comments_count),
            "detail": "turnos de chat, adjuntos o ubicaciones guardadas",
        },
    ]
    timeline = [
        {"label": "Vecino envia reclamo o evidencia", "status": "done"},
        {"label": "Backend crea ticket trazable", "status": "done"},
        {"label": "Equipo ve bandeja y mapa si hay coordenadas", "status": "done" if points else "waiting_for_location"},
    ]
    return {
        "cards": cards,
        "modules": modules,
        "timeline": timeline,
        "map": {
            "enabled": bool(points),
            "points": points,
            "empty_state": None if points else "Disponible cuando la sesion genere ubicaciones reales.",
        },
        "session_activity": {
            "contract_version": "demo.session_activity.v1",
            "source": "session_generated_events",
            "has_session_data": True,
            "items": items,
        },
    }


def _admin_preview_for_sector(sector: str, tenant_slug: str = "", chat_session_id: str = "") -> dict[str, Any]:
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
            "subtitle": "Gobiernos y municipios",
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
    real_activity = (
        _apply_gobierno_session_activity(preset, resolved_tenant_slug, chat_session_id=chat_session_id)
        if normalized == "gobierno"
        else {
            "cards": preset["cards"],
            "modules": preset["modules"],
            "timeline": preset["timeline"],
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
        }
    )
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
        "modules": real_activity["modules"],
        "cards": real_activity["cards"],
        "timeline": real_activity["timeline"],
        "metrics": [],
        "map": real_activity["map"],
        "session_activity": real_activity["session_activity"],
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
    chat_session_id = request.args.get("chat_session_id") or request.headers.get("X-Chat-Session-Id") or ""
    if not sector:
        sector = sector_for_rubro(tenant_slug) or tenant_slug or "empresas"
    return _json_response(
        _admin_preview_for_sector(
            sector,
            tenant_slug=str(tenant_slug or "").strip().lower(),
            chat_session_id=str(chat_session_id or "").strip(),
        )
    )


@v2_demo_bp.route("/catalog-assets/<path:filename>", methods=["GET", "OPTIONS"])
def demo_catalog_asset_v2(filename: str):
    if request.method == "OPTIONS":
        return _options_response()
    return _demo_catalog_asset_response(filename)


@demo_compat_bp.route("/media/demo_catalogs/<path:filename>", methods=["GET", "OPTIONS"])
@demo_compat_bp.route("/media/demo-catalogs/<path:filename>", methods=["GET", "OPTIONS"])
def demo_catalog_asset_legacy_media(filename: str):
    """Serve legacy demo PDF paths used by older frontend bundles.

    Current contracts publish /api/v2/demo/catalog-assets/...; this compatibility
    route prevents deployed stale links from landing in the SPA 404 screen.
    """

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
    education_profile = build_education_profile(tenant)
    education_whatsapp_playbook = build_education_whatsapp_playbook(tenant) if education_profile.get("is_education") else None
    education_payload = None
    if education_profile.get("is_education"):
        education_payload = {
            "profile": education_profile,
            "education_profile": education_profile,
            "whatsapp_playbook": education_whatsapp_playbook,
            "quick_menu": (education_whatsapp_playbook or {}).get("quick_menu") or [],
            "primary_actions": (education_whatsapp_playbook or {}).get("primary_actions") or [],
        }
    whatsapp_sandbox = build_demo_whatsapp_sandbox_contract(
        tenant_slug=tenant.slug,
        sector=sector,
        rubro=rubro or tenant.slug,
        sandbox_number=demo_whatsapp_number or _twilio_sandbox_number(),
        join_phrase=join_phrase,
        source=str(payload.get("source") or "public_demo_profile"),
        provider="twilio_whatsapp_number" if demo_whatsapp_number else "twilio_sandbox",
        whatsapp_playbook=education_whatsapp_playbook,
        education=education_payload,
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
            "education": education_payload,
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
    response_profile = _demo_session_response_profile(data)
    sector = normalize_demo_sector(
        data.get("sector")
        or data.get("pilar")
        or data.get("pillar")
        or data.get("pilar_key")
        or data.get("segment")
        or data.get("vertical")
        or data.get("selected_sector")
        or data.get("selected_pillar")
        or data.get("label")
        or data.get("title")
        or data.get("name")
        or data.get("id")
        or data.get("key")
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
    requested_rubro = rubro

    inferred_payload_sector = _infer_demo_sector_from_payload(data, rubro=rubro, tenant_slug=tenant_slug, current_sector=sector)
    if inferred_payload_sector:
        sector = inferred_payload_sector
    if not sector:
        sector = sector_for_rubro(rubro) or "empresas"
    if sector not in set(demo_pillar_keys()):
        inferred_sector = sector_for_rubro(sector) or sector_for_rubro(rubro)
        sector = inferred_sector or sector

    if sector not in {"gobierno", "empresas", "educacion"}:
        return _error_response("sector debe ser 'gobierno', 'empresas' o 'educacion'", 400, "validation_error", "send_valid_sector")

    inferred_rubro = _infer_demo_rubro_from_payload(data, sector=sector)
    if inferred_rubro and (
        not rubro
        or rubro in set(demo_pillar_keys())
        or sector_for_rubro(rubro) != sector
    ):
        rubro = inferred_rubro

    rubro_was_explicit = bool(
        inferred_rubro
        or (
            requested_rubro
            and requested_rubro not in set(demo_pillar_keys())
            and sector_for_rubro(requested_rubro) == sector
        )
        or (
            tenant_slug
            and category_for_rubro(tenant_slug) is not None
            and sector_for_rubro(tenant_slug) == sector
        )
    )

    if not rubro and not tenant_slug:
        rubro = default_rubro_for_sector(sector)

    if sector == "empresas" and response_profile == "widget_compact" and not rubro_was_explicit:
        selector = _demo_rubro_selector_contract(
            sector=sector,
            selected_rubro=rubro,
            response_profile=response_profile,
        )
        selector_menu = [
            {
                "id": f"select_{item.get('slug')}",
                "label": item.get("label"),
                "intent": f"select_rubro:{item.get('slug')}",
                "kind": "rubro_selector",
                "enabled": True,
                "action_payload": item.get("action_payload"),
            }
            for item in selector.get("categories") or []
        ]
        default_menu = {
            "contract_version": "demo.default_menu.v1",
            "render_as": "rubro_selector",
            "source": "backend_demo_session",
            "selected_sector": sector,
            "selected_rubro": rubro,
            "tenant_slug": None,
            "max_visible_items": 6,
            "collapse_extra_items": True,
            "items": selector_menu,
            "starter_prompts": [],
            "empty_state": None if selector_menu else "Sin rubros disponibles para Empresas.",
        }
        widget_onboarding = {
            "contract_version": "demo.widget_onboarding_result.v1",
            "ok": True,
            "status": "select_rubro",
            "state": "select_rubro",
            "ready": True,
            "response_profile": response_profile,
            "selected_sector": sector,
            "selected_rubro": rubro,
            "tenant_slug": None,
            "chat_session_id": None,
            "demo_session_id": None,
            "chat_bootstrap_path": None,
            "required_next_step": "select_rubro",
            "requires_rubro_selection": True,
            "open_chat": False,
            "close_selector": False,
            "autostart_chat": False,
            "send_init_once": False,
            "default_menu": default_menu,
            "rubro_selector": selector,
            "error_message": None,
        }
        frontend_contract = {
            "contract_version": "demo.session_frontend.v1",
            "render_as": "demo_rubro_selector",
            "success_condition": "http_200_and_ok_true",
            "response_profile": response_profile,
            "selected_sector": sector,
            "selected_rubro": rubro,
            "tenant_slug": None,
            "next_step": "select_rubro",
            "requires_rubro_selection": True,
            "open_chat": False,
            "rubro_selector_path": "workspace.rubro_selector",
            "preserve_session_headers": False,
            "show_error_only_when_ok_false": True,
            "do_not_infer_endpoint_locally": True,
        }
        workspace = {
            "title": "Empresas",
            "subtitle": "Elegir rubro comercial para probar ventas y soporte con IA.",
            "welcome_message": "Que rubro de empresa queres simular?",
            "rubro_selector": selector,
            "default_menu": default_menu,
            "quick_menu": selector_menu,
            "widget_onboarding": widget_onboarding,
            "frontend_contract": frontend_contract,
            "runtime_contract": {
                "demo_data_source": "backend_demo_session",
                "local_mock_allowed": False,
                "requires_rubro_before_chat": True,
                "chat_response_contract": "chat.response.v1",
            },
        }
        return _json_response(
            {
                "contract_version": "demo.session.v2",
                "contract_aliases": ["demo.session.v1"],
                "ok": True,
                "ready": True,
                "status": "ready",
                "next_step": "select_rubro",
                "requires_rubro_selection": True,
                "response_profile": response_profile,
                "selected_sector": sector,
                "selected_rubro": rubro,
                "workspace": workspace,
                "rubro_selector": selector,
                "widget_onboarding": widget_onboarding,
                "frontend_contract": frontend_contract,
                "default_menu": default_menu,
                "quick_menu": selector_menu,
            }
        )

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
    if sector == "gobierno":
        tenant_type = "municipio"
    elif sector in {"educacion", "empresas"}:
        tenant_type = "pyme"
    if rubro and tenant.slug and rubro.replace("_", "-") == tenant.slug:
        rubro = tenant.slug
    effective_rubro = rubro or tenant.slug
    rubro_context = _demo_rubro_context(sector=sector, rubro=effective_rubro, tenant=tenant)
    experience_rubro_label = rubro_context.get("label") or tenant.nombre
    education_profile = build_education_profile(tenant, rubro_label=experience_rubro_label)
    vertical = "educacion" if sector == "educacion" or education_profile.get("is_education") else tenant.vertical
    experience = build_demo_experience_contract(
        tenant_type=tenant_type,
        rubro_label=experience_rubro_label,
        vertical=vertical,
        subvertical=tenant.subvertical,
        education_profile=education_profile if education_profile.get("is_education") else None,
    )
    education_public_profile = None
    if education_profile.get("is_education"):
        education_public_profile = {
            **education_profile,
            "primary_actions": experience.get("education_primary_actions") or [],
            "quick_menu": experience.get("education_quick_menu") or [],
        }
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

    demo_session_id = create_demo_session_token(tenant_slug=tenant.slug, sector=sector, rubro=effective_rubro)
    chat_session_id = _stable_demo_chat_session_id(demo_session_id)
    chat_bootstrap = _chat_bootstrap(
        tenant=tenant,
        tenant_type=tenant_type,
        sector=sector,
        rubro=effective_rubro,
        demo_session_id=demo_session_id,
        quick_replies=quick_replies,
        media_capabilities=media_capabilities,
        vertical=vertical,
        education_profile=education_profile if education_profile.get("is_education") else None,
        rubro_context=rubro_context,
    )
    demo_whatsapp_number = _demo_whatsapp_number_for_tenant(tenant)
    education_whatsapp_playbook = build_education_whatsapp_playbook(tenant) if education_profile.get("is_education") else None
    whatsapp_sandbox = build_demo_whatsapp_sandbox_contract(
        tenant_slug=tenant.slug,
        sector=sector,
        rubro=effective_rubro,
        sandbox_number=demo_whatsapp_number or _twilio_sandbox_number(),
        join_phrase=_twilio_sandbox_join_phrase(),
        source="public_demo_session",
        provider="twilio_whatsapp_number" if demo_whatsapp_number else "twilio_sandbox",
        whatsapp_playbook=education_whatsapp_playbook,
    )
    education_payload = None
    if education_profile.get("is_education"):
        education_payload = {
            "profile": education_public_profile,
            "education_profile": education_public_profile,
            "whatsapp_playbook": education_whatsapp_playbook,
            "admin_menu": build_education_admin_menu(tenant),
            "quick_menu": experience.get("education_quick_menu") or [],
            "primary_actions": experience.get("education_primary_actions") or [],
        }
        whatsapp_sandbox["education"] = education_payload

    catalog_resources = catalog_resources_for_rubro(effective_rubro, sector)
    rubro_tools = _demo_rubro_tools_contract(
        sector=sector,
        rubro=effective_rubro,
        tenant=tenant,
        rubro_context=rubro_context,
        catalog_resources=catalog_resources,
    )
    primary_actions = _short_operational_menu_contract(
        sector=sector,
        tenant=tenant,
        education_payload=education_payload,
    )
    if isinstance(education_payload, dict):
        education_payload["primary_actions"] = primary_actions
        education_payload["quick_menu"] = primary_actions
    if isinstance(education_public_profile, dict) and education_public_profile.get("is_education"):
        education_public_profile["primary_actions"] = primary_actions
        education_public_profile["quick_menu"] = primary_actions
    operational_menu = {
        "contract_version": "demo.operational_menu.v1",
        "source": "backend_demo_session",
        "selected_sector": sector,
        "selected_rubro": effective_rubro,
        "tenant_slug": tenant.slug,
        "primary_actions": primary_actions,
        "quick_menu": primary_actions,
        "max_visible_items": 5,
    }
    vertical_key = "education" if sector == "educacion" else "government" if sector == "gobierno" else "business" if sector == "empresas" else "general"
    vertical_aliases = {
        vertical_key: {
            "contract_version": "demo.vertical_menu.v1",
            "actions": primary_actions,
            "primary_actions": primary_actions,
            "quick_menu": primary_actions,
        }
    }
    if sector == "educacion":
        vertical_aliases.setdefault("educacion", vertical_aliases[vertical_key])
    elif sector == "gobierno":
        vertical_aliases.setdefault("gobierno", vertical_aliases[vertical_key])
        vertical_aliases.setdefault("municipio", vertical_aliases[vertical_key])
    elif sector == "empresas":
        vertical_aliases.setdefault("pyme", vertical_aliases[vertical_key])
        vertical_aliases.setdefault("commerce", vertical_aliases[vertical_key])

    workspace = {
        "title": tenant.nombre or "Demo Chatboc",
        "subtitle": (experience.get("hero") or {}).get("subtitle"),
        "welcome_message": onboarding.get("entry_prompt") or "Que queres probar primero?",
        "quick_replies": quick_replies,
        "primary_actions": primary_actions,
        "quick_menu": primary_actions,
        "operational_menu": operational_menu,
        "verticals": vertical_aliases,
        "rubro_context": rubro_context,
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
        "education_profile": education_public_profile,
        "pillar_selector": {
            "contract_version": DEMO_PILLAR_CONTRACT_VERSION,
            "selected_sector": sector,
            "selected_rubro": effective_rubro,
            "pillars": demo_pillars(),
        },
        "rubro_selector": _demo_rubro_selector_contract(
            sector=sector,
            selected_rubro=effective_rubro,
            response_profile=response_profile,
        ),
        "catalog_resources": catalog_resources,
        "rubro_tools": rubro_tools,
        "runtime_contract": {
            "demo_data_source": "backend_tenant_contracts",
            "local_mock_allowed": False,
            "requires_demo_session_id": True,
            "chat_response_contract": "chat.response.v1",
        },
    }

    if sector == "educacion":
        workspace["education"] = {
            **(workspace.get("education") or {}),
            "primary_actions": primary_actions,
            "quick_menu": primary_actions,
        }
        workspace["education_profile"] = {
            **(workspace.get("education_profile") or {}),
            "primary_actions": primary_actions,
            "quick_menu": primary_actions,
        }
    elif sector == "gobierno":
        government_menu = {
            "contract_version": "demo.government_menu.v1",
            "primary_actions": primary_actions,
            "quick_menu": primary_actions,
        }
        workspace["government"] = government_menu
        workspace["gobierno"] = government_menu
        workspace["municipio"] = government_menu
    elif sector == "empresas":
        business_menu = {
            "contract_version": "demo.business_menu.v1",
            "primary_actions": primary_actions,
            "quick_menu": primary_actions,
        }
        workspace["pyme"] = business_menu
        workspace["business"] = business_menu
        workspace["commerce"] = business_menu

    default_menu = _demo_default_menu_contract(
        sector=sector,
        tenant=tenant,
        quick_replies=quick_replies,
        value_cards=workspace["value_cards"],
        allowed_actions=allowed_actions,
        rubro_context=rubro_context,
        rubro_tools=rubro_tools,
        education_payload=education_payload,
        primary_actions=primary_actions,
    )
    demo_metadata = _demo_chat_metadata(
        sector=sector,
        rubro=effective_rubro,
        tenant=tenant,
        rubro_context=rubro_context,
        default_menu=default_menu,
        rubro_tools=rubro_tools,
    )
    chat_bootstrap["payload"]["demo_metadata"] = demo_metadata
    chat_bootstrap["payload"]["rubro_context"] = rubro_context
    chat_bootstrap["payload"]["rubro_tool_summary"] = rubro_tools.get("llm_context") or {}
    chat_bootstrap["context"] = {
        "sector": sector,
        "rubro": effective_rubro,
        "tenant_slug": tenant.slug,
        "tenant_tipo": tenant_type,
        "vertical": vertical,
        "education_context": education_profile if education_profile.get("is_education") else None,
        "demo_session_id": demo_session_id,
        "chat_session_id": chat_session_id,
        "rubro_tool_summary": rubro_tools.get("llm_context") or {},
    }
    chat_bootstrap["default_menu"] = {
        "contract_version": "demo.default_menu.v1",
        "items": default_menu.get("items") or [],
    }
    chat_bootstrap["default_menu_path"] = "workspace.default_menu"
    chat_bootstrap["demo_metadata_path"] = "workspace.demo_metadata"
    chat_bootstrap["rubro_context_path"] = "workspace.rubro_context"
    chat_bootstrap["rubro_tools_path"] = "workspace.rubro_tools"
    workspace["demo_metadata"] = demo_metadata
    workspace["default_menu"] = default_menu
    workspace["quick_menu"] = default_menu["items"]

    session_contract = {
        "demo_session_id": demo_session_id,
        "chat_session_id": chat_session_id,
        "session_id": chat_session_id,
        "tenant_slug": tenant.slug,
        "sector": sector,
        "rubro": effective_rubro,
        "max_messages": (whatsapp_sandbox.get("trial_policy") or {}).get("max_messages"),
    }
    widget_onboarding = _demo_session_selection_contract(
        sector=sector,
        rubro=effective_rubro,
        tenant=tenant,
        chat_bootstrap=chat_bootstrap,
        quick_replies=quick_replies,
        admin_preview_endpoint=admin_preview_endpoint,
        response_profile=response_profile,
    )
    widget_onboarding["default_menu"] = default_menu
    frontend_contract = _demo_session_frontend_contract(
        sector=sector,
        rubro=effective_rubro,
        tenant=tenant,
        response_profile=response_profile,
    )
    requires_rubro_selection = sector == "empresas" and not rubro_was_explicit
    if requires_rubro_selection:
        widget_onboarding.update(
            {
                "status": "select_rubro",
                "state": "select_rubro",
                "required_next_step": "select_rubro",
                "requires_rubro_selection": True,
                "open_chat": False,
                "close_selector": False,
                "autostart_chat": False,
                "send_init_once": False,
                "rubro_selector": workspace["rubro_selector"],
            }
        )
        frontend_contract.update(
            {
                "render_as": "demo_rubro_selector",
                "next_step": "select_rubro",
                "requires_rubro_selection": True,
                "open_chat": False,
                "rubro_selector_path": "workspace.rubro_selector",
            }
        )
    workspace["session"] = session_contract
    workspace["widget_onboarding"] = widget_onboarding
    workspace["frontend_contract"] = frontend_contract

    if response_profile == "widget_compact":
        compact_rubro_tools = _compact_rubro_tools_contract(rubro_tools)
        compact_survey_voting = _compact_survey_voting_contract(commercial["survey_voting"])
        compact_whatsapp_sandbox = _compact_whatsapp_sandbox_contract(whatsapp_sandbox)
        compact_vertical_aliases = {
            vertical_key: {
                "contract_version": "demo.vertical_menu.v1",
                "actions_path": "workspace.primary_actions",
                "quick_menu_path": "workspace.quick_menu",
            }
        }
        if sector == "educacion":
            compact_vertical_aliases["educacion"] = {"alias_of": vertical_key, "actions_path": "workspace.primary_actions"}
        elif sector == "gobierno":
            compact_vertical_aliases["gobierno"] = {"alias_of": vertical_key, "actions_path": "workspace.primary_actions"}
            compact_vertical_aliases["municipio"] = {"alias_of": vertical_key, "actions_path": "workspace.primary_actions"}
        elif sector == "empresas":
            compact_vertical_aliases["pyme"] = {"alias_of": vertical_key, "actions_path": "workspace.primary_actions"}
            compact_vertical_aliases["commerce"] = {"alias_of": vertical_key, "actions_path": "workspace.primary_actions"}
        compact_workspace = {
            "title": workspace["title"],
            "subtitle": workspace["subtitle"],
            "welcome_message": workspace["welcome_message"],
            "quick_replies": quick_replies,
            "quick_menu": default_menu["items"],
            "primary_actions": primary_actions,
            "operational_menu": operational_menu,
            "verticals": compact_vertical_aliases,
            "default_menu": default_menu,
            "lead_capture": workspace["lead_capture"],
            "admin_preview_endpoint": admin_preview_endpoint,
            "media_capabilities": media_capabilities,
            "conversion_ctas": conversion_ctas,
            "survey_voting": compact_survey_voting,
            "chat_bootstrap": chat_bootstrap,
            "empty_states": workspace["empty_states"],
            "pillar_selector": workspace["pillar_selector"],
            "rubro_selector": workspace["rubro_selector"],
            "rubro_context": rubro_context,
            "catalog_resources": workspace["catalog_resources"],
            "rubro_tools": compact_rubro_tools,
            "whatsapp_sandbox": compact_whatsapp_sandbox,
            "education": education_payload,
            "session": session_contract,
            "widget_onboarding": widget_onboarding,
            "frontend_contract": frontend_contract,
            "runtime_contract": workspace["runtime_contract"],
        }
        if sector == "educacion":
            compact_workspace["education_profile"] = workspace.get("education_profile")
        elif sector == "gobierno":
            compact_workspace["government"] = workspace.get("government")
            compact_workspace["gobierno"] = {"alias_of": "government", "actions_path": "workspace.government.primary_actions"}
            compact_workspace["municipio"] = {"alias_of": "government", "actions_path": "workspace.government.primary_actions"}
        elif sector == "empresas":
            compact_workspace["pyme"] = workspace.get("pyme")
            compact_workspace["business"] = {"alias_of": "pyme", "actions_path": "workspace.pyme.primary_actions"}
            compact_workspace["commerce"] = {"alias_of": "pyme", "actions_path": "workspace.pyme.primary_actions"}
        return _json_response(
            {
                "contract_version": "demo.session.v2",
                "contract_aliases": ["demo.session.v1"],
                "ok": True,
                "ready": True,
                "status": "ready",
                "next_step": "select_rubro" if requires_rubro_selection else "open_chat",
                "requires_rubro_selection": requires_rubro_selection,
                "response_profile": response_profile,
                "demo_session_id": demo_session_id,
                "session_id": chat_session_id,
                "chat_session_id": chat_session_id,
                "session": session_contract,
                "tenant_slug": tenant.slug,
                "tenant": _tenant_dict(tenant, sector=sector),
                "workspace": compact_workspace,
                "widget_onboarding": widget_onboarding,
                "frontend_contract": frontend_contract,
                "default_menu": default_menu,
                "primary_actions": primary_actions,
                "quick_menu": default_menu["items"],
                "quick_replies": quick_replies,
            }
        )

    return _json_response(
        {
            "contract_version": "demo.session.v2",
            "contract_aliases": ["demo.session.v1"],
            "ok": True,
            "ready": True,
            "status": "ready",
            "next_step": "select_rubro" if requires_rubro_selection else "open_chat",
            "requires_rubro_selection": requires_rubro_selection,
            "response_profile": response_profile,
            "demo_session_id": demo_session_id,
            "session_id": chat_session_id,
            "chat_session_id": chat_session_id,
            "session": session_contract,
            "tenant_slug": tenant.slug,
            "tenant": _tenant_dict(tenant, sector=sector),
            "workspace": workspace,
            "pillar_selector": workspace["pillar_selector"],
            "rubro_selector": workspace["rubro_selector"],
            "rubro_context": rubro_context,
            "demo_metadata": demo_metadata,
            "catalog_resources": workspace["catalog_resources"],
            "rubro_tools": rubro_tools,
            "chat_bootstrap": chat_bootstrap,
            "widget_onboarding": widget_onboarding,
            "frontend_contract": frontend_contract,
            "default_menu": default_menu,
            "primary_actions": primary_actions,
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
                "default_menu": default_menu,
                "quick_menu": default_menu["items"],
                "rubro_tools": rubro_tools,
            },
            "quick_menu": default_menu["items"],
            "quick_replies": quick_replies,
        }
    )
