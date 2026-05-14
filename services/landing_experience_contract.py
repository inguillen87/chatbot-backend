from __future__ import annotations

from typing import Any

from services.education_contracts import fold_text, is_education_tenant


LANDING_EXPERIENCE_CONTRACT_VERSION = "public.landing_experience.v1"
LANDING_OPERATIONAL_CONTRACT_VERSION = "landing.operational_contract.v1"


def _lead_capture_contract() -> dict[str, Any]:
    return {
        "contract_version": "public.lead_capture.form.v1",
        "enabled": True,
        "endpoint": "/api/public/lead-capture",
        "fields": [
            {"id": "name", "label": "Nombre", "type": "text", "required": True},
            {"id": "phone", "label": "Telefono", "type": "tel", "required": False},
            {"id": "email", "label": "Email", "type": "email", "required": False},
            {"id": "message", "label": "Mensaje", "type": "textarea", "required": False},
        ],
        "required_fields": ["name"],
        "required_any_of": [["phone", "email"]],
        "submit_contract": "public.lead_capture.v1",
    }


def _tenant_kind(tenant: Any = None) -> str:
    if tenant and is_education_tenant(tenant):
        return "educacion"
    tipo = fold_text(getattr(tenant, "tipo", None))
    if tipo == "municipio":
        return "municipio"
    if tipo == "pyme":
        return "pyme"
    return "platform"


def _hero_for_kind(kind: str) -> dict[str, Any]:
    hero = {
        "contract_scope": "operational_demo_data",
        "headline": "Converti conversaciones en operaciones reales",
        "subheadline": "Chatboc atiende por web o WhatsApp, pide los datos justos y deja casos, pedidos o leads listos para operar.",
        "conversation_title": "WhatsApp operativo",
        "conversation_subtitle": "Un caso entra, el agente pide datos y deja una accion trazable.",
        "primary_cta": {"label": "Probar una conversacion real", "href": "/demo", "intent": "start_demo"},
        "secondary_cta": {"label": "Hablar con ventas", "href": "/contacto", "intent": "sales_contact"},
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
            "lead_capture": {
                "enabled": True,
                "endpoint": "/api/public/lead-capture",
                "required_fields": ["nombre", "telefono"],
            },
            "admin_preview_endpoint": "/api/v2/demo/admin-preview?sector=gobierno&tenant_slug=municipio",
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
            "lead_capture": {
                "enabled": True,
                "endpoint": "/api/public/lead-capture",
                "required_fields": ["nombre", "telefono"],
            },
            "admin_preview_endpoint": "/api/v2/demo/admin-preview?sector=empresas&tenant_slug=bodega",
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
            "lead_capture": {
                "enabled": True,
                "endpoint": "/api/public/lead-capture",
                "required_fields": ["nombre", "telefono"],
            },
            "admin_preview_endpoint": "/api/v2/demo/admin-preview?sector=educacion&tenant_slug=colegio-demo",
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
            "survey_voting": {
                "enabled": True,
                "respond_endpoint": "/api/public/encuestas/v1",
                "results_endpoint": "/api/public/encuestas/v1",
            },
            "admin_preview_endpoint": "/api/v2/demo/admin-preview?sector=gobierno&tenant_slug=municipio",
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
        "contract_scope": "operational_demo_data",
        "flows": flows,
        "rules": {
            "traceable_actions_only": True,
            "hide_result_without_action": True,
            "hide_steps_without_workflow_steps": True,
            "frontend_owns_visual_design": True,
            "backend_owns_visible_copy": True,
        },
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
        "hero": _hero_for_kind(kind),
        "conversion": {
            "contract_version": LANDING_OPERATIONAL_CONTRACT_VERSION,
            "lead_capture_endpoint": "/api/public/lead-capture",
            "lead_capture": _lead_capture_contract(),
            "demo_catalog_endpoint": "/api/v2/demo/catalog",
            "demo_session_endpoint": "/api/v2/demo/session",
            "admin_preview_endpoint": "/api/v2/demo/admin-preview",
            "primary_intents": ["start_demo", "lead_capture"],
        },
        "runtime_rules": {
            "frontend_owns_visual_design": True,
            "backend_owns_visible_copy": True,
            "backend_owns_sessions_actions_and_traceability": True,
            "do_not_publish_frontend_mock_data": True,
        },
    }
