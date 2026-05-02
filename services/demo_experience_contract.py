from __future__ import annotations

from typing import Any


def _quick_actions_for_tipo(tipo: str) -> list[dict[str, Any]]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        return [
            {
                "id": "qa_reclamo",
                "label": "Crear reclamo",
                "description": "Abrir un caso con categoria, ubicacion y seguimiento.",
                "intent": "iniciar_reclamo",
                "icon": "alert-triangle",
                "cta_label": "Probar reclamo",
            },
            {
                "id": "qa_sugerencia",
                "label": "Enviar sugerencia",
                "description": "Capturar ideas ciudadanas sin perder trazabilidad.",
                "intent": "enviar_sugerencia",
                "icon": "lightbulb",
                "cta_label": "Enviar idea",
            },
            {
                "id": "qa_ticket",
                "label": "Estado ticket",
                "description": "Consultar avance y proximos pasos con lenguaje simple.",
                "intent": "consultar_ticket",
                "icon": "clipboard-check",
                "cta_label": "Ver estado",
            },
            {
                "id": "qa_heatmap",
                "label": "Mapa operativo",
                "description": "Visualizar demanda por zona, categoria y prioridad.",
                "intent": "analytics_heatmap",
                "icon": "map",
                "cta_label": "Ver mapa",
            },
        ]
    return [
        {
            "id": "qa_catalogo",
            "label": "Ver catalogo",
            "description": "Mostrar productos, precios y variantes sin salir del chat.",
            "intent": "ver_catalogo",
            "icon": "book-open",
            "cta_label": "Abrir catalogo",
        },
        {
            "id": "qa_pedido",
            "label": "Crear pedido",
            "description": "Armar carrito, validar contacto y avanzar al checkout.",
            "intent": "crear_pedido",
            "icon": "shopping-cart",
            "cta_label": "Crear pedido",
        },
        {
            "id": "qa_estado_pedido",
            "label": "Estado pedido",
            "description": "Responder seguimiento con estado comercial claro.",
            "intent": "estado_pedido",
            "icon": "truck",
            "cta_label": "Consultar",
        },
        {
            "id": "qa_subir_pdf",
            "label": "Subir PDF",
            "description": "Convertir catalogos y listas en conocimiento del agente.",
            "intent": "subir_catalogo_pdf",
            "icon": "file-text",
            "cta_label": "Probar PDF",
        },
        {
            "id": "qa_subir_excel",
            "label": "Subir Excel",
            "description": "Preparar carga masiva de productos y precios.",
            "intent": "subir_catalogo_excel",
            "icon": "table",
            "cta_label": "Probar Excel",
        },
    ]


def _starter_prompts_for_tipo(tipo: str) -> list[str]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        return [
            "Quiero iniciar un reclamo por alumbrado publico.",
            "Necesito saber como sacar un turno para licencia.",
            "Donde reporto baches con ubicacion?",
        ]
    return [
        "Quiero ver el catalogo y precios mayoristas.",
        "Necesito crear un pedido con envio en el dia.",
        "Que promociones tienen esta semana?",
    ]


def _agent_persona_for_tipo(tipo: str, rubro: str) -> dict[str, Any]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        return {
            "name": "Asistente ciudadano",
            "tone": "claro, resolutivo y empatico",
            "promise": "Te ayuda a resolver tramites, reclamos y consultas sin vueltas.",
            "do": ["pedir datos de a uno", "confirmar antes de crear casos", "ofrecer seguimiento"],
            "avoid": ["pedir datos sensibles si no hacen falta", "prometer plazos no configurados"],
        }
    return {
        "name": f"Asistente comercial {rubro}",
        "tone": "profesional, cercano y vendedor sin presionar",
        "promise": "Acompana al cliente desde la pregunta hasta el pedido o la compra.",
        "do": ["mostrar opciones concretas", "recuperar carrito", "pasar a humano cuando hay intencion alta"],
        "avoid": ["inventar stock", "inventar descuentos", "ocultar costos o pasos"],
    }


def _first_visit_for_tipo(tipo: str) -> dict[str, Any]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        return {
            "headline": "Hola, soy tu mesa de ayuda inteligente.",
            "subheadline": "Puedo orientarte, crear casos y dejar todo listo para seguimiento.",
            "primary_action": {"id": "start_reclamo", "label": "Crear un reclamo", "intent": "iniciar_reclamo"},
            "secondary_action": {"id": "ask_question", "label": "Hacer una consulta", "intent": "consultas_generales"},
            "steps": [
                {"id": "choose", "label": "Elegi una accion"},
                {"id": "details", "label": "Te pido solo lo necesario"},
                {"id": "tracking", "label": "Queda registrado con seguimiento"},
            ],
        }
    return {
        "headline": "Hola, soy tu asistente comercial.",
        "subheadline": "Puedo mostrar productos, responder dudas y ayudarte a comprar.",
        "primary_action": {"id": "browse_catalog", "label": "Ver catalogo", "intent": "ver_catalogo"},
        "secondary_action": {"id": "talk_sales", "label": "Hablar con ventas", "intent": "derivar_humano"},
        "steps": [
            {"id": "discover", "label": "Contame que estas buscando"},
            {"id": "compare", "label": "Te muestro opciones utiles"},
            {"id": "checkout", "label": "Avanzamos a pedido o asesor"},
        ],
    }


def _sample_conversations_for_tipo(tipo: str) -> list[dict[str, Any]]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        return [
            {
                "id": "sample_reclamo",
                "title": "Reclamo con ubicacion",
                "user_message": "Hay una luminaria rota en mi cuadra.",
                "assistant_goal": "Pedir direccion, categoria y crear ticket con seguimiento.",
                "intent": "iniciar_reclamo",
            },
            {
                "id": "sample_tramite",
                "title": "Consulta de tramite",
                "user_message": "Que necesito para renovar licencia?",
                "assistant_goal": "Responder requisitos y ofrecer turno si corresponde.",
                "intent": "info_tramite",
            },
            {
                "id": "sample_handoff",
                "title": "Derivacion humana",
                "user_message": "Necesito hablar con alguien ahora.",
                "assistant_goal": "Explicar canales disponibles y abrir handoff trazable.",
                "intent": "derivar_humano",
            },
        ]
    return [
        {
            "id": "sample_catalog",
            "title": "Busqueda de producto",
            "user_message": "Busco packs mayoristas para esta semana.",
            "assistant_goal": "Mostrar opciones del catalogo y sugerir siguiente paso.",
            "intent": "ver_catalogo",
        },
        {
            "id": "sample_order",
            "title": "Pedido guiado",
            "user_message": "Quiero comprar 3 unidades y coordinar envio.",
            "assistant_goal": "Armar pedido, validar contacto y preparar checkout.",
            "intent": "crear_pedido",
        },
        {
            "id": "sample_sales_handoff",
            "title": "Lead caliente",
            "user_message": "Me interesa contratarlo para mi empresa.",
            "assistant_goal": "Capturar datos y derivar a ventas con contexto.",
            "intent": "derivar_humano",
        },
    ]


def _trust_signals_for_tipo(tipo: str) -> list[dict[str, str]]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        return [
            {"id": "traceable", "label": "Casos trazables", "detail": "Cada reclamo queda con estado y contexto."},
            {"id": "privacy", "label": "Privacidad primero", "detail": "No muestra datos sensibles en widget publico."},
            {"id": "omnichannel", "label": "Un solo historial", "detail": "Widget, WhatsApp y panel comparten contexto."},
        ]
    return [
        {"id": "sales_ready", "label": "Listo para vender", "detail": "Catalogo, pedidos y pagos pueden convivir en el chat."},
        {"id": "human_backup", "label": "Humano disponible", "detail": "El agente sabe cuando derivar a ventas o soporte."},
        {"id": "tenant_brand", "label": "White label", "detail": "La experiencia se adapta al tenant sin hardcodear React."},
    ]


def _lead_capture_for_tipo(tipo: str) -> dict[str, Any]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        title = "Dejar contacto para seguimiento"
        trigger_intents = ["derivar_humano", "iniciar_reclamo", "consulta_estado_ticket"]
    else:
        title = "Recibir propuesta o continuar compra"
        trigger_intents = ["crear_pedido", "derivar_humano", "checkout_intent"]
    return {
        "enabled": True,
        "title": title,
        "fields": [
            {"id": "name", "label": "Nombre", "required": True, "type": "text"},
            {"id": "email", "label": "Email", "required": False, "type": "email"},
            {"id": "phone", "label": "Telefono", "required": False, "type": "tel"},
            {"id": "interest", "label": "Interes", "required": False, "type": "text"},
        ],
        "trigger_intents": trigger_intents,
        "endpoint": "/api/public/lead-capture",
        "success_message": "Listo, dejamos tu consulta preparada para seguimiento.",
    }


def _media_capabilities_for_tipo(tipo: str) -> dict[str, Any]:
    normalized = (tipo or "").strip().lower()
    primary_business_action = "ticket" if normalized == "municipio" else "pedido"
    location_intents = ["iniciar_reclamo", "reclamo_con_ubicacion"] if normalized == "municipio" else ["crear_pedido", "coordinar_envio"]
    image_intents = ["reclamo_con_foto", "evidencia_visual"] if normalized == "municipio" else ["consulta_producto", "soporte_visual"]

    return {
        "version": "media.capabilities.v1",
        "primary_business_action": primary_business_action,
        "composer": {
            "placeholder": "Escribi, habla o adjunta algo para que el agente te ayude.",
            "actions": [
                {"id": "attach_image", "type": "image", "icon": "image", "label": "Imagen"},
                {"id": "record_audio", "type": "audio", "icon": "mic", "label": "Audio"},
                {"id": "share_location", "type": "location", "icon": "map-pin", "label": "Ubicacion"},
                {"id": "attach_file", "type": "file", "icon": "paperclip", "label": "Archivo"},
            ],
            "states": ["idle", "recording", "uploading", "transcribing", "thinking", "needs_confirmation", "success", "handoff"],
        },
        "input_modes": {
            "text": {
                "enabled": True,
                "chat_endpoint": "/ask",
                "payload_key": "pregunta",
                "max_length": 4000,
            },
            "image": {
                "enabled": True,
                "upload_endpoint": "/archivos/upload/chat_attachment",
                "upload_response_key": "attachmentInfo",
                "chat_payload_key": "attachmentInfo",
                "accept": ["image/jpeg", "image/png", "image/webp"],
                "intents": image_intents,
                "expected_bot_behavior": "describir contexto visual, pedir datos faltantes y proponer accion de negocio.",
            },
            "audio": {
                "enabled": True,
                "chat_endpoint": "/ask",
                "multipart_field": "audio_file",
                "accept": ["audio/webm", "audio/ogg", "audio/mpeg", "audio/wav", "audio/mp4"],
                "max_seconds": 120,
                "intents": ["nota_de_voz", "consulta_rapida", "derivar_humano"],
                "expected_bot_behavior": "transcribir, responder sobre la transcripcion y conservar contexto de audio.",
            },
            "location": {
                "enabled": True,
                "chat_endpoint": "/ask",
                "payload_key": "location",
                "fields": ["lat", "lon", "latitude", "longitude", "lng", "address", "accuracy"],
                "intents": location_intents,
                "expected_bot_behavior": "confirmar zona/direccion y usarla para ticket, envio o derivacion.",
            },
            "file": {
                "enabled": True,
                "upload_endpoint": "/archivos/upload/chat_attachment",
                "upload_response_key": "attachmentInfo",
                "chat_payload_key": "attachmentInfo",
                "accept": [
                    "application/pdf",
                    "text/csv",
                    "application/vnd.ms-excel",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "application/msword",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ],
                "intents": ["subir_catalogo", "soporte_documental", "adjuntar_evidencia"],
                "expected_bot_behavior": "usar metadata del archivo, pedir confirmacion y ofrecer siguiente accion.",
            },
        },
        "output_modes": {
            "text": True,
            "quick_replies": True,
            "cards": True,
            "attachments": True,
            "ticket_cta": normalized == "municipio",
            "order_cta": normalized != "municipio",
            "lead_cta": True,
            "handoff_cta": True,
        },
        "runtime_notes": {
            "chat_payload_keys": ["pregunta", "attachmentInfo", "location", "action_id"],
            "requires_chat_session_id": True,
            "request_headers": ["X-Chat-Session-Id", "X-Tenant-Slug", "X-Anon-Id"],
            "degrade_when_unsupported": "hide_action_keep_text_input",
        },
        "channels": {
            "widget": {"text": True, "image": True, "audio": True, "location": True, "file": True},
            "whatsapp": {"text": True, "image": True, "audio": True, "location": True, "file": True},
            "inbox": {"text": True, "image": True, "audio": True, "location": True, "file": True},
        },
    }


def _conversion_ctas_for_tipo(tipo: str) -> dict[str, Any]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        actions = [
            {
                "id": "create_ticket",
                "label": "Crear ticket",
                "intent": "iniciar_reclamo",
                "endpoint": "/ask",
                "show_when": ["location_ready", "image_attached", "high_intent"],
                "style": "primary",
            },
            {
                "id": "check_ticket",
                "label": "Consultar estado",
                "intent": "consulta_estado_ticket",
                "endpoint": "/ask",
                "show_when": ["ticket_id_detected", "returning_user"],
                "style": "secondary",
            },
            {
                "id": "talk_human",
                "label": "Hablar con una persona",
                "intent": "derivar_humano",
                "endpoint": "/api/v2/inbox/omnichannel/actions",
                "show_when": ["frustration", "handoff_requested", "demo_limit"],
                "style": "secondary",
            },
            {
                "id": "capture_lead",
                "label": "Dejar contacto",
                "intent": "lead_capture",
                "endpoint": "/api/public/lead-capture",
                "show_when": ["demo_interest", "support_followup"],
                "style": "accent",
            },
        ]
    else:
        actions = [
            {
                "id": "create_order",
                "label": "Crear pedido",
                "intent": "crear_pedido",
                "endpoint": "/ask",
                "show_when": ["catalog_item_selected", "price_question", "high_intent"],
                "style": "primary",
            },
            {
                "id": "prepare_checkout",
                "label": "Preparar checkout",
                "intent": "checkout_intent",
                "endpoint": "/api/v2/payments/checkout-preview",
                "show_when": ["cart_ready", "payment_ready"],
                "style": "primary",
            },
            {
                "id": "talk_sales",
                "label": "Hablar con ventas",
                "intent": "derivar_humano",
                "endpoint": "/api/v2/inbox/omnichannel/actions",
                "show_when": ["complex_question", "bulk_purchase", "handoff_requested"],
                "style": "secondary",
            },
            {
                "id": "capture_lead",
                "label": "Recibir propuesta",
                "intent": "lead_capture",
                "endpoint": "/api/public/lead-capture",
                "show_when": ["demo_interest", "pricing_interest", "abandoned_checkout"],
                "style": "accent",
            },
        ]

    return {
        "version": "conversion.ctas.v1",
        "actions": actions,
        "rules": {
            "max_visible": 3,
            "prefer_backend_labels": True,
            "fallback_behavior": "hide_missing_actions",
            "preserve_context_on_click": True,
        },
    }


def _animation_tokens() -> dict[str, Any]:
    return {
        "version": "chat.motion.v1",
        "respect_reduced_motion": True,
        "motion_level": "polished",
        "events": [
            {"id": "launcher_open", "trigger": "widget.open", "pattern": "spring_scale", "duration_ms": 180},
            {"id": "message_in", "trigger": "message.received", "pattern": "slide_fade", "duration_ms": 160},
            {"id": "quick_reply_tap", "trigger": "quick_reply.clicked", "pattern": "press_ripple", "duration_ms": 120},
            {"id": "recording", "trigger": "audio.recording", "pattern": "level_meter", "duration_ms": 0},
            {"id": "uploading_media", "trigger": "media.uploading", "pattern": "progress_shimmer", "duration_ms": 0},
            {"id": "location_shared", "trigger": "location.shared", "pattern": "pin_drop", "duration_ms": 220},
            {"id": "handoff_ready", "trigger": "handoff.ready", "pattern": "soft_pulse", "duration_ms": 700},
            {"id": "lead_success", "trigger": "lead.created", "pattern": "success_check", "duration_ms": 260},
        ],
        "surfaces": {
            "launcher": ["launcher_open"],
            "composer": ["recording", "uploading_media", "quick_reply_tap"],
            "message_list": ["message_in", "location_shared", "lead_success"],
            "handoff": ["handoff_ready"],
        },
    }


def _empty_states_for_tipo(tipo: str) -> dict[str, dict[str, str]]:
    return {
        "no_messages": {
            "title": "Empeza con una accion rapida",
            "body": "Elegi un ejemplo o escribi como si hablaras por WhatsApp.",
        },
        "offline": {
            "title": "Modo sin conexion",
            "body": "Guardamos el borrador y lo sincronizamos cuando vuelva internet.",
        },
        "handoff_waiting": {
            "title": "Te conectamos con una persona",
            "body": "El asistente conserva el contexto para que no tengas que repetir todo.",
        },
        "limit_reached": {
            "title": "Demo completada",
            "body": "Ya viste el flujo principal. El plan Full habilita mas mensajes y automatizaciones.",
        },
    }


def _agent_copilot_for_tipo(tipo: str) -> dict[str, Any]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        suggestions = ["Responder con proximo paso", "Asignar area", "Pedir ubicacion", "Cerrar caso"]
    else:
        suggestions = ["Sugerir producto", "Preparar checkout", "Ofrecer descuento configurado", "Derivar a ventas"]
    return {
        "enabled": True,
        "suggestions": suggestions,
        "feedback_endpoint": "/api/v2/inbox/omnichannel/actions",
        "human_in_the_loop": True,
    }


def build_demo_experience_contract(
    *,
    tenant_type: str,
    rubro_label: str | None = None,
    max_messages: int = 10,
) -> dict[str, Any]:
    tipo = (tenant_type or "").strip().lower() or "pyme"
    rubro = (rubro_label or tipo.title()).strip()
    first_visit = _first_visit_for_tipo(tipo)
    starter_prompts = _starter_prompts_for_tipo(tipo)

    return {
        "version": "2026-05-agent-experience-v2",
        "tenant_type": tipo,
        "agent_persona": _agent_persona_for_tipo(tipo, rubro),
        "first_visit": first_visit,
        "guided_onboarding": {
            "autostart_chat": True,
            "open_widget": True,
            "entry_prompt": first_visit.get("subheadline") or "Que queres probar primero?",
            "starter_prompts": starter_prompts,
            "suggested_workflows": [
                {"id": "wf_guided_chat", "label": "Chat guiado con botones"},
                {"id": "wf_ticket_or_order", "label": "Crear y seguir ticket/pedido"},
                {"id": "wf_feedback_loop", "label": "Recolectar feedback de satisfaccion"},
            ],
        },
        "hero": {
            "title": f"Demo IA para {rubro}",
            "subtitle": "Proba en tiempo real WhatsApp, widget y automatizaciones sin configurar nada.",
            "badge": f"Demo {max_messages} mensajes",
        },
        "quick_actions": _quick_actions_for_tipo(tipo),
        "sample_conversations": _sample_conversations_for_tipo(tipo),
        "trust_signals": _trust_signals_for_tipo(tipo),
        "lead_capture": _lead_capture_for_tipo(tipo),
        "media_capabilities": _media_capabilities_for_tipo(tipo),
        "conversion_ctas": _conversion_ctas_for_tipo(tipo),
        "animation_tokens": _animation_tokens(),
        "empty_states": _empty_states_for_tipo(tipo),
        "agent_copilot": _agent_copilot_for_tipo(tipo),
        "journeys": [
            {
                "id": "journey_whatsapp_activation",
                "title": "Activar WhatsApp demo",
                "steps": ["Abrir WhatsApp", "Enviar join brief-yesterday", "Volver al panel y probar flujos"],
            },
            {
                "id": "journey_multimodal",
                "title": "Probar chat multimodal",
                "steps": ["Enviar texto", "Enviar audio", "Enviar imagen", "Recibir respuesta IA con contexto"],
            },
            {
                "id": "journey_business_action",
                "title": "Ejecutar accion de negocio",
                "steps": ["Crear reclamo/pedido", "Consultar estado", "Medir resultados en tablero"],
            },
        ],
        "upsell_wall": {
            "trigger_after_messages": max_messages,
            "copy": "Llegaste al limite de demo. Activa plan Full para escalar.",
            "locked_features": ["qdrant_catalogo_completo", "automatizaciones_enterprise"],
            "cta_label": "Quiero plan Full",
        },
        "channel_playbooks": {
            "whatsapp": {
                "activation_phrase": "join brief-yesterday",
                "starter_messages": [
                    "Quiero crear un reclamo",
                    "Te mando una foto del problema",
                    "Necesito precio por mayor",
                    "Quiero subir mi catalogo en PDF",
                ],
                "media_checks": ["text", "audio", "image", "location", "file"],
            },
            "widget_chat": {
                "starter_messages": [
                    "Mostrame promociones vigentes",
                    "Necesito un pedido rapido",
                    "Quiero cargar un catalogo en Excel",
                    "Quiero soporte con ubicacion",
                ],
                "media_checks": ["text", "audio", "image", "location", "file"],
            },
        },
        "conversion_pitch": {
            "title": "Listo para vender en minutos",
            "bullets": [
                "Menu inteligente por rubro",
                "Automatizacion de reclamos, pedidos y sugerencias",
                "Soporte multimodal: audio, imagen, ubicacion y archivos",
            ],
            "cta_label": "Hablar con ventas",
        },
        "analytics_kpis": [
            "tiempo_respuesta_promedio",
            "tasa_resolucion",
            "satisfaccion_usuario",
            "conversion_ticket_o_pedido",
        ],
        "integrations": {
            "webhooks": {
                "supported": True,
                "events": ["ticket.created", "ticket.updated", "order.created", "survey.completed"],
            },
            "crm_connectors": ["hubspot", "salesforce"],
            "erp_connectors": ["tango", "colppy"],
        },
        "component_pack": {
            "layout": "stacked_cards",
            "sections": [
                {"id": "hero", "component": "DemoHeroCard", "props": {"show_badge": True, "show_cta": True}},
                {"id": "first_visit", "component": "FirstVisitPanel", "props": {"show_steps": True, "show_primary_action": True}},
                {"id": "quick_actions", "component": "QuickActionGrid", "props": {"columns_mobile": 2, "columns_desktop": 4, "show_icons": True}},
                {"id": "sample_conversations", "component": "ConversationExamples", "props": {"max_items": 3, "allow_prefill": True}},
                {"id": "media_composer", "component": "ChatComposerCapabilities", "props": {"show_audio": True, "show_location": True, "show_upload": True}},
                {"id": "channels", "component": "ChannelPlaybookTabs", "props": {"default_tab": "whatsapp", "show_media_checks": True}},
                {"id": "conversion_ctas", "component": "ConversionActionBar", "props": {"max_visible": 3, "source": "backend"}},
                {"id": "trust_signals", "component": "TrustSignalRow", "props": {"compact": True}},
                {"id": "lead_capture", "component": "LeadCaptureInline", "props": {"trigger": "high_intent"}},
                {"id": "upsell", "component": "UpgradeWallCard", "props": {"style": "enterprise", "highlight_limit": True}},
            ],
        },
    }
