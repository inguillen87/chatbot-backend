from __future__ import annotations

from typing import Any

from services.education_contracts import fold_text, is_education_tenant


LANDING_EXPERIENCE_CONTRACT_VERSION = "public.landing_experience.v1"


def _tenant_kind(tenant: Any = None) -> str:
    if tenant and is_education_tenant(tenant):
        return "educacion"
    tipo = fold_text(getattr(tenant, "tipo", None))
    if tipo == "municipio":
        return "municipio"
    if tipo == "pyme":
        return "pyme"
    return "platform"


def _brand_payload(tenant: Any = None) -> dict[str, Any]:
    tenant_name = getattr(tenant, "nombre", None)
    logo_url = getattr(tenant, "logo_url", None)
    return {
        "product_name": tenant_name or "Chatboc",
        "wordmark": tenant_name or "Chatboc",
        "tagline": "Agentes IA para atencion, ventas y operaciones omnicanal.",
        "logo": {
            "source": "tenant" if logo_url else "generated_mark",
            "url": logo_url,
            "fallback_mark": "rounded-chat-spark",
            "alt": f"{tenant_name or 'Chatboc'} logo",
            "rules": [
                "Use tenant logo when present.",
                "If logo is missing, render text wordmark plus a compact chat mark.",
                "Do not invent municipal or school logos in frontend.",
            ],
        },
        "voice": {
            "tone": "profesional, claro, cercano y confiable",
            "reading_level": "simple",
            "avoid": ["humo generico", "promesas no respaldadas", "copy local hardcodeado"],
        },
    }


def _visual_tokens(tenant: Any = None) -> dict[str, Any]:
    tema = getattr(tenant, "tema", None) if tenant else None
    cfg = getattr(tenant, "configuracion", None) if tenant else None
    tema = tema if isinstance(tema, dict) else {}
    cfg = cfg if isinstance(cfg, dict) else {}

    def pick(*keys: str, default: str) -> str:
        for key in keys:
            if tema.get(key):
                return str(tema[key])
            if cfg.get(key):
                return str(cfg[key])
        return default

    primary = pick("primary", "primaryColor", "color_primario", "primary_color", default="#2563eb")
    accent = pick("accent", "accentColor", "color_secundario", "accent_color", default="#16a34a")
    return {
        "color": {
            "ink": "#182033",
            "muted": "#64748b",
            "surface": "#ffffff",
            "surface_alt": "#f6f8fb",
            "line": "#dbe3ea",
            "primary": primary,
            "accent": accent,
            "warm": "#f59e0b",
            "danger": "#ef4444",
            "success": "#16a34a",
            "info": "#0891b2",
        },
        "typography": {
            "family": "system-ui",
            "headline_weight": 760,
            "body_weight": 450,
            "letter_spacing": 0,
            "scale": {
                "hero": {"min_px": 44, "max_px": 68},
                "section_title": {"min_px": 28, "max_px": 40},
                "body": {"px": 16},
                "caption": {"px": 13},
            },
        },
        "radius": {"card": 8, "button": 8, "media": 12, "pill": 999},
        "shadow": {
            "sm": "0 1px 2px rgba(15, 23, 42, 0.08)",
            "md": "0 12px 32px rgba(15, 23, 42, 0.12)",
            "focus": "0 0 0 3px rgba(37, 99, 235, 0.22)",
        },
        "layout": {
            "max_width": 1180,
            "section_y": {"desktop": 96, "mobile": 56},
            "hero_min_height": "min(760px, 92vh)",
            "show_next_section_hint": True,
        },
    }


def _motion_tokens() -> dict[str, Any]:
    return {
        "contract_version": "landing.motion.v1",
        "motion_level": "balanced",
        "respect_reduced_motion": True,
        "durations_ms": {"micro": 120, "enter": 260, "panel": 420, "hero": 700},
        "easing": {
            "standard": "cubic-bezier(.2,.8,.2,1)",
            "gentle": "cubic-bezier(.16,1,.3,1)",
        },
        "components": {
            "nav": {"enter": "fade-down", "sticky": "border-soften"},
            "hero_media": {"enter": "product-reveal", "idle": "subtle-parallax"},
            "chat_preview": {"enter": "message-cascade", "typing": "wave-dots"},
            "cards": {"enter": "stagger-up", "hover": "lift-2"},
            "cta": {"hover": "brighten", "success": "checkmark-pop"},
            "logos": {"enter": "fade-in", "hover": "none"},
        },
        "mobile_rules": {
            "disable_cursor_trails": True,
            "disable_particles": True,
            "max_parallel_animations": 2,
        },
    }


def _hero_for_kind(kind: str) -> dict[str, Any]:
    titles = {
        "platform": "Chatboc",
        "municipio": "Atencion ciudadana con IA",
        "pyme": "Ventas y soporte con IA",
        "educacion": "Asistente escolar omnicanal",
    }
    subtitles = {
        "platform": "Un agente IA que atiende, vende, crea tickets, entiende audios, imagenes y ubicaciones, y deja datos listos para operar.",
        "municipio": "Recibi reclamos, tramites, consultas y ubicaciones con seguimiento, mapas y trazabilidad.",
        "pyme": "Acompana consultas, catalogos, pedidos, pagos y derivaciones humanas desde web y WhatsApp.",
        "educacion": "Ayuda a familias con asistencia, comunicados, secretaria, adjuntos y casos sensibles con derivacion cuidada.",
    }
    hero = {
        "eyebrow": "SaaS omnicanal con agentes IA",
        "headline": "Converti conversaciones en casos, pedidos y decisiones operativas",
        "h1": titles.get(kind, titles["platform"]),
        "subheadline": "Texto, audio, imagenes, archivos y ubicaciones entran por web o WhatsApp; Chatboc entiende, acciona y deja seguimiento.",
        "subtitle": subtitles.get(kind, subtitles["platform"]),
        "conversation_title": "Demo real de atencion",
        "conversation_subtitle": "Elegi un caso y mira que accion deja en el panel.",
        "primary_cta": {"label": "Probar demo", "href": "/demo", "intent": "start_demo"},
        "secondary_cta": {"label": "Ver casos de uso", "href": "/casos", "intent": "view_use_cases"},
        "tertiary_cta": {"label": "Hablar con ventas", "href": "/contacto", "intent": "sales_contact"},
        "trust_line": "Widget, WhatsApp, tickets, encuestas, mapas y analiticas trabajando como una sola operacion.",
        "media": {
            "type": "product_ui_composite",
            "rule": "Use real product screenshots, dashboard previews or generated bitmap product mockups; avoid abstract gradient-only hero.",
            "assets": [
                {"id": "municipio_dashboard", "url": "/static/demo/municipio/dashboard-preview.svg", "alt": "Dashboard municipal con reclamos y mapas"},
                {"id": "pyme_dashboard", "url": "/static/demo/bodega/dashboard-preview.svg", "alt": "Dashboard comercial con pedidos y catalogo"},
                {"id": "surveys_public", "url": "/static/encuestas/participacion_ciudadana.png", "alt": "Participacion ciudadana y encuestas"},
            ],
            "chat_preview": [
                {"role": "user", "text": "Quiero hacer un reclamo y mandar ubicacion."},
                {"role": "assistant", "text": "Te ayudo. Recibi la ubicacion, clasifico el caso y lo dejo con seguimiento."},
                {"role": "user", "text": "Tambien te mando una foto."},
                {"role": "assistant", "text": "Perfecto. La adjunto al ticket y aviso al equipo correspondiente."},
            ],
        },
    }
    hero["conversation_demo"] = _hero_conversation_demo(kind)
    hero["demo_conversation"] = hero["conversation_demo"]
    hero["workflow_steps"] = ["Mensaje entendido", "Datos accionables", "Caso visible en panel"]
    return hero


def _hero_conversation_demo(kind: str) -> dict[str, Any]:
    flows = [
        {
            "id": "gobierno-reclamo-ubicacion",
            "label": "Gobiernos",
            "sector": "gobierno",
            "user_message": "Te mando foto, audio y ubicacion de un semaforo caido.",
            "agent_message": "Recibi la evidencia, clasifique el reclamo, marque la zona y lo deje listo para seguimiento.",
            "inputs": [
                {
                    "kind": "image",
                    "label": "Foto",
                    "preview_url": "/static/demo/municipio/reclamo-semaforo.jpg",
                    "image_url": "/static/demo/municipio/reclamo-semaforo.jpg",
                    "detail": "Evidencia visual adjunta al reclamo.",
                },
                {
                    "kind": "audio",
                    "label": "Nota de voz",
                    "detail": "Transcripcion resumida por IA para clasificar el reclamo.",
                },
                {
                    "kind": "location",
                    "label": "Ubicacion",
                    "address": "Av. San Martin y Rivadavia",
                    "lat": -34.6083,
                    "lng": -58.3712,
                    "detail": "Punto operativo para mapa y derivacion.",
                },
            ],
            "action": {
                "label": "Reclamo creado",
                "detail": "Ticket con categoria, prioridad, zona, evidencia y equipo sugerido.",
                "status": "Listo para operar",
                "creates": "ticket",
                "fields": [
                    {"label": "Categoria", "value": "Semaforo"},
                    {"label": "Prioridad", "value": "Alta"},
                    {"label": "Equipo sugerido", "value": "Transito"},
                    {"label": "Seguimiento", "value": "Codigo y PIN"},
                ],
                "metadata": {
                    "requires_location": True,
                    "supports_media": ["image", "audio", "location"],
                    "traceable_target": "ticket",
                },
                "summary_items": ["categoria", "prioridad", "zona", "evidencia", "equipo_sugerido"],
                "facts": [
                    {"label": "Entrada", "value": "Foto, audio y ubicacion"},
                    {"label": "Salida operativa", "value": "Ticket con equipo sugerido"},
                ],
                "details": {
                    "operational_summary": "Reclamo municipal trazable con evidencia, zona y prioridad.",
                    "panel_target": "inbox",
                },
                "attributes": {
                    "vertical": "gobierno",
                    "workflow": "reclamo_con_evidencia",
                    "traceable": True,
                },
            },
            "result": {
                "kind": "ticket",
                "traceable": True,
                "panel": "inbox",
                "tracking": "codigo_y_pin",
            },
            "highlights": ["mapa operativo", "asignacion sugerida", "seguimiento ciudadano"],
            "workflow_steps": ["Entiende texto y adjuntos", "Crea ticket real", "Sugiere equipo", "Deja seguimiento"],
            "cta": {"label": "Probar reclamo real", "href": "/demo?sector=gobierno"},
        },
        {
            "id": "pyme-pedido-carrito",
            "label": "PyMEs",
            "sector": "empresas",
            "user_message": "Te mando una foto del producto y quiero comprar dos unidades.",
            "agent_message": "Detecte el producto, prepare el carrito invitado y deje el pedido listo para continuar.",
            "inputs": [
                {
                    "kind": "image",
                    "label": "Foto",
                    "preview_url": "/static/demo/bodega/producto-detectado.jpg",
                    "image_url": "/static/demo/bodega/producto-detectado.jpg",
                    "detail": "Imagen usada para sugerir producto del catalogo real.",
                },
                {"kind": "text", "label": "Cantidad", "detail": "Dos unidades solicitadas por el comprador."},
            ],
            "action": {
                "label": "Pedido iniciado",
                "detail": "Carrito invitado con producto, cantidad, contacto pendiente y checkout cuando el tenant lo habilita.",
                "status": "Listo para vender",
                "creates": "order_or_lead",
                "fields": [
                    {"label": "Producto", "value": "Detectado desde catalogo"},
                    {"label": "Cantidad", "value": "2 unidades"},
                    {"label": "Carrito", "value": "Invitado"},
                    {"label": "Checkout", "value": "Segun tenant"},
                ],
                "metadata": {
                    "requires_catalog": True,
                    "allows_guest_cart": True,
                    "traceable_target": "order_or_lead",
                },
                "summary_items": ["producto", "cantidad", "carrito", "checkout"],
                "facts": [
                    {"label": "Entrada", "value": "Foto y cantidad"},
                    {"label": "Salida operativa", "value": "Carrito invitado o lead comercial"},
                ],
                "details": {
                    "operational_summary": "Consulta comercial convertida en carrito, pedido o lead segun capacidades del tenant.",
                    "panel_target": "marketplace",
                },
                "attributes": {
                    "vertical": "empresas",
                    "workflow": "catalogo_carrito_checkout",
                    "traceable": True,
                },
            },
            "result": {
                "kind": "order",
                "traceable": True,
                "panel": "marketplace",
                "tracking": "pedido_y_carrito",
            },
            "highlights": ["catalogo real", "carrito invitado", "seguimiento comercial"],
            "workflow_steps": ["Detecta producto", "Arma carrito", "Pide contacto", "Deja pedido o lead"],
            "cta": {"label": "Probar venta real", "href": "/demo?sector=empresas"},
        },
        {
            "id": "colegio-certificado-caso",
            "label": "Colegios",
            "sector": "educacion",
            "user_message": "Necesito un certificado de alumno regular y adjunto el DNI.",
            "agent_message": "Identifique el tramite, guarde el adjunto y genere el caso para el equipo administrativo.",
            "inputs": [
                {
                    "kind": "file",
                    "label": "Adjunto",
                    "thumbnail_url": "/static/demo/colegio/documento-adjunto.png",
                    "detail": "Documento asociado al tramite escolar.",
                },
                {"kind": "text", "label": "Solicitud", "detail": "Certificado de alumno regular."},
            ],
            "action": {
                "label": "Caso escolar creado",
                "detail": "Caso con familia, tramite, documentacion y derivacion al equipo correspondiente.",
                "status": "Listo para gestionar",
                "creates": "school_case",
                "fields": [
                    {"label": "Tramite", "value": "Certificado"},
                    {"label": "Equipo", "value": "Secretaria"},
                    {"label": "Adjunto", "value": "DNI recibido"},
                    {"label": "Derivacion", "value": "Administrativa"},
                ],
                "metadata": {
                    "requires_attachment": True,
                    "sensitive_escalation": False,
                    "traceable_target": "school_case",
                },
                "summary_items": ["tramite", "familia", "adjunto", "equipo"],
                "facts": [
                    {"label": "Entrada", "value": "Solicitud y adjunto"},
                    {"label": "Salida operativa", "value": "Caso escolar derivado"},
                ],
                "details": {
                    "operational_summary": "Tramite escolar con documentacion y derivacion administrativa.",
                    "panel_target": "education",
                },
                "attributes": {
                    "vertical": "educacion",
                    "workflow": "tramite_escolar_con_adjunto",
                    "traceable": True,
                },
            },
            "result": {
                "kind": "case",
                "traceable": True,
                "panel": "education",
                "tracking": "caso_escolar",
            },
            "highlights": ["adjuntos procesados", "derivacion cuidada", "historial escolar"],
            "workflow_steps": ["Reconoce tramite", "Valida adjunto", "Crea caso", "Deriva a secretaria"],
            "cta": {"label": "Probar caso escolar", "href": "/demo?sector=educacion"},
        },
        {
            "id": "encuestas-votacion-en-vivo",
            "label": "Encuestas",
            "sector": "participacion",
            "user_message": "Quiero votar una prioridad del barrio y dejar un comentario.",
            "agent_message": "Registre la participacion, actualice resultados en vivo y deje el comentario disponible para analisis.",
            "inputs": [
                {"kind": "choice", "label": "Voto"},
                {"kind": "text", "label": "Comentario"},
                {
                    "kind": "location",
                    "label": "Zona",
                    "address": "Barrio Centro",
                    "lat": -34.6037,
                    "lng": -58.3816,
                    "detail": "Segmento territorial para resultados y comentarios.",
                },
            ],
            "action": {
                "label": "Participacion registrada",
                "detail": "Respuesta con resultados en vivo, comentario moderable y segmento territorial cuando hay ubicacion.",
                "status": "Listo para analizar",
                "creates": "survey_response",
                "fields": [
                    {"label": "Participacion", "value": "Voto y comentario"},
                    {"label": "Segmento", "value": "Zona"},
                    {"label": "Resultados", "value": "En vivo si esta habilitado"},
                    {"label": "Moderacion", "value": "Comentario revisable"},
                ],
                "metadata": {
                    "requires_survey": True,
                    "supports_live_results": True,
                    "traceable_target": "survey_response",
                },
                "summary_items": ["voto", "comentario", "segmento", "resultados"],
                "facts": [
                    {"label": "Entrada", "value": "Voto, comentario y zona"},
                    {"label": "Salida operativa", "value": "Participacion segmentada"},
                ],
                "details": {
                    "operational_summary": "Participacion ciudadana con resultados, comentarios y segmento territorial.",
                    "panel_target": "surveys",
                },
                "attributes": {
                    "vertical": "participacion",
                    "workflow": "votacion_en_vivo",
                    "traceable": True,
                },
            },
            "result": {
                "kind": "survey_response",
                "traceable": True,
                "panel": "surveys",
                "tracking": "resultados_en_vivo",
            },
            "highlights": ["resultados en vivo", "comentarios", "segmentos"],
            "workflow_steps": ["Registra voto", "Guarda comentario", "Segmenta respuesta", "Actualiza resultados"],
            "cta": {"label": "Probar votacion", "href": "/demo?sector=gobierno&flow=encuestas"},
        },
    ]
    preferred_by_kind = {
        "municipio": "gobierno-reclamo-ubicacion",
        "pyme": "pyme-pedido-carrito",
        "educacion": "colegio-certificado-caso",
    }
    preferred = preferred_by_kind.get(kind)
    if preferred:
        flows.sort(key=lambda item: 0 if item["id"] == preferred else 1)
    return {
        "contract_version": "landing.hero_conversation_demo.v1",
        "flows": flows,
        "rules": {
            "traceable_actions_only": True,
            "hide_result_without_action": True,
            "hide_steps_without_workflow_steps": True,
        },
    }


def _sections_for_kind(kind: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "choose_and_chat",
            "label": "Elegir rubro y empezar",
            "title": "La demo tiene que sentirse viva desde el primer click.",
            "body": "El usuario elige municipio, pyme o colegio y entra a un chat con acciones listas, respuestas rapidas y multimedia para probar.",
            "visual": "demo_selector_plus_chat",
            "cta": {"label": "Abrir selector", "href": "/demo"},
        },
        {
            "id": "media_intelligence",
            "label": "Texto, audio, imagen y ubicacion",
            "title": "El agente entiende lo que la gente realmente manda.",
            "body": "Notas de voz, fotos, archivos, certificados, comprobantes y ubicaciones se convierten en contexto accionable para tickets, pedidos o handoff.",
            "visual": "multimodal_chat_thread",
            "metrics": ["audio", "image", "file", "location"],
        },
        {
            "id": "operations",
            "label": "Operacion real",
            "title": "No es solo chat: queda gestionable.",
            "body": "Cada conversacion puede terminar en ticket, lead, encuesta, pedido, mapa, estadistica o derivacion humana con trazabilidad.",
            "visual": "dashboard_kpi_map",
            "cta": {"label": "Ver analytics", "href": "/analytics"},
        },
        {
            "id": "verticals",
            "label": "Verticales",
            "title": "Pymes, gobiernos y colegios, con el mismo motor.",
            "body": "La experiencia se adapta a cada organizacion sin duplicar pantallas ni desordenar la operacion.",
            "visual": "vertical_cards",
            "cards": [
                {"id": "pymes", "title": "Pymes", "href": "/pymes", "icon": "store"},
                {"id": "municipios", "title": "Gobiernos", "href": "/municipios", "icon": "landmark"},
                {"id": "colegios", "title": "Colegios", "href": "/colegios", "icon": "school"},
            ],
        },
    ]


def _adjacent_pages() -> list[dict[str, Any]]:
    return [
        {
            "id": "demo",
            "path": "/demo",
            "title": "Demo interactiva",
            "purpose": "Elegir sector o rubro y entrar a una experiencia guiada de chat.",
            "primary_components": ["sector_selector", "rubro_grid", "chat_preview", "lead_capture"],
            "source_endpoints": ["/api/v2/demo/catalog", "/api/v2/demo/session"],
        },
        {
            "id": "pymes",
            "path": "/pymes",
            "title": "Pymes",
            "purpose": "Mostrar ventas, catalogo, pedidos, pagos, WhatsApp y soporte.",
            "primary_components": ["use_case_cards", "catalog_chat_demo", "checkout_preview", "proof_bar"],
        },
        {
            "id": "municipios",
            "path": "/municipios",
            "title": "Gobiernos",
            "purpose": "Mostrar reclamos, tramites, mapas, encuestas y atencion ciudadana.",
            "primary_components": ["claim_flow", "heatmap_preview", "survey_preview", "ticket_tracking"],
        },
        {
            "id": "colegios",
            "path": "/colegios",
            "title": "Colegios",
            "purpose": "Mostrar asistencia, comunicados, secretaria, certificados y WhatsApp escolar.",
            "primary_components": ["school_quick_menu", "family_chat_demo", "case_dashboard", "whatsapp_playbook"],
            "source_endpoints": ["/api/v1/education/admin/menu", "/api/v1/education/operations/summary"],
        },
        {
            "id": "encuestas",
            "path": "/encuestas",
            "title": "Encuestas y votaciones",
            "purpose": "Mostrar participacion publica, resultados, estados y offline UX.",
            "primary_components": ["survey_cards", "live_results", "vote_flow", "empty_states"],
        },
        {
            "id": "widget",
            "path": "/widget",
            "title": "Widget omnicanal",
            "purpose": "Mostrar embed, configuracion, quick menu, media y soporte humano.",
            "primary_components": ["widget_preview", "theme_controls", "quick_menu_preview", "embed_code"],
            "source_endpoints": ["/api/public/widget-config"],
        },
    ]


def _proof_and_pricing() -> dict[str, Any]:
    return {
        "proof_bar": [
            {"id": "omnichannel", "label": "Web + WhatsApp", "detail": "Un mismo contexto para multiples canales."},
            {"id": "traceability", "label": "Tickets y leads", "detail": "Cada contacto queda operativo."},
            {"id": "analytics", "label": "Mapas y metricas", "detail": "Datos listos para decisiones."},
            {"id": "white_label", "label": "White label", "detail": "Tenant-aware sin copy hardcodeado."},
        ],
        "pricing_teaser": {
            "title": "Planes simples, implementacion acompaniada.",
            "body": "Cuando el precio depende del caso, guiamos a una propuesta o una demo personalizada.",
            "cta": {"label": "Solicitar propuesta", "href": "/contacto"},
        },
        "faq": [
            {"q": "Se puede probar sin configurar todo?", "a": "Si. La demo usa rubros de prueba y una experiencia lista para conversar."},
            {"q": "Funciona con WhatsApp?", "a": "Si. La plataforma soporta texto, audio, imagenes, archivos y ubicacion."},
            {"q": "Sirve para colegios y municipios?", "a": "Si. Son verticales sobre la misma plataforma."},
        ],
    }


def build_landing_experience_contract(tenant: Any = None, *, page: str | None = None) -> dict[str, Any]:
    kind = _tenant_kind(tenant)
    selected_page = fold_text(page).replace(" ", "_") or "home"
    return {
        "contract_version": LANDING_EXPERIENCE_CONTRACT_VERSION,
        "tenant": {
            "slug": getattr(tenant, "slug", None),
            "tipo": getattr(tenant, "tipo", None),
            "vertical": getattr(tenant, "vertical", None),
            "white_label": bool(tenant),
        },
        "selected_page": selected_page,
        "experience_kind": kind,
        "brand": _brand_payload(tenant),
        "design_tokens": _visual_tokens(tenant),
        "motion": _motion_tokens(),
        "navigation": {
            "primary": [
                {"label": "Demo", "href": "/demo"},
                {"label": "Pymes", "href": "/pymes"},
                {"label": "Gobiernos", "href": "/municipios"},
                {"label": "Colegios", "href": "/colegios"},
                {"label": "Encuestas", "href": "/encuestas"},
            ],
            "cta": {"label": "Probar ahora", "href": "/demo"},
        },
        "hero": _hero_for_kind(kind),
        "sections": _sections_for_kind(kind),
        "adjacent_pages": _adjacent_pages(),
        "conversion": {
            "lead_capture_endpoint": "/api/public/lead-capture",
            "demo_catalog_endpoint": "/api/v2/demo/catalog",
            "demo_session_endpoint": "/api/v2/demo/session",
            "primary_intents": ["start_demo", "sales_contact", "lead_capture"],
        },
        "content_rules": [
            "Use this payload as copy source; keep municipality, pyme and school text configurable.",
            "Make the first viewport signal Chatboc or the tenant brand clearly.",
            "Use real product UI, dashboard previews or generated bitmap product mockups for hero media.",
            "Avoid one-color palettes; combine primary, accent and warm tokens with neutral surfaces.",
            "Respect reduced motion and keep mobile animations light.",
            "Keep cards at 8px radius unless an existing design token says otherwise.",
        ],
        **_proof_and_pricing(),
    }
