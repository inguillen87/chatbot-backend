from __future__ import annotations

from typing import Any


def _quick_actions_for_tipo(tipo: str) -> list[dict[str, Any]]:
    normalized = (tipo or "").strip().lower()
    if normalized == "municipio":
        return [
            {"id": "qa_reclamo", "label": "Crear reclamo", "intent": "iniciar_reclamo", "icon": "alert-triangle"},
            {"id": "qa_sugerencia", "label": "Enviar sugerencia", "intent": "enviar_sugerencia", "icon": "lightbulb"},
            {"id": "qa_ticket", "label": "Estado ticket", "intent": "consultar_ticket", "icon": "clipboard-check"},
            {"id": "qa_heatmap", "label": "Mapa de calor", "intent": "analytics_heatmap", "icon": "map"},
        ]
    return [
        {"id": "qa_catalogo", "label": "Ver catálogo", "intent": "ver_catalogo", "icon": "book-open"},
        {"id": "qa_pedido", "label": "Crear pedido", "intent": "crear_pedido", "icon": "shopping-cart"},
        {"id": "qa_estado_pedido", "label": "Estado pedido", "intent": "estado_pedido", "icon": "truck"},
        {"id": "qa_subir_pdf", "label": "Subir PDF", "intent": "subir_catalogo_pdf", "icon": "file-text"},
        {"id": "qa_subir_excel", "label": "Subir Excel", "intent": "subir_catalogo_excel", "icon": "table"},
    ]


def build_demo_experience_contract(
    *,
    tenant_type: str,
    rubro_label: str | None = None,
    max_messages: int = 10,
) -> dict[str, Any]:
    tipo = (tenant_type or "").strip().lower() or "pyme"
    rubro = (rubro_label or tipo.title()).strip()
    actions = _quick_actions_for_tipo(tipo)
    return {
        "version": "2026-04-demo-experience-v1",
        "tenant_type": tipo,
        "hero": {
            "title": f"Demo IA para {rubro}",
            "subtitle": "Probá en tiempo real WhatsApp + Widget + automatizaciones en minutos.",
            "badge": f"Demo {max_messages} mensajes",
        },
        "quick_actions": actions,
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
                "title": "Ejecutar acción de negocio",
                "steps": ["Crear reclamo/pedido", "Consultar estado", "Medir resultados en tablero"],
            },
        ],
        "upsell_wall": {
            "trigger_after_messages": max_messages,
            "copy": "Llegaste al límite de demo. Activá plan Full para escalar.",
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
                    "Quiero subir mi catálogo en PDF",
                ],
                "media_checks": ["text", "audio", "image", "location", "file"],
            },
            "widget_chat": {
                "starter_messages": [
                    "Mostrame promociones vigentes",
                    "Necesito un pedido rápido",
                    "Quiero cargar un catálogo en Excel",
                    "Quiero soporte con ubicación",
                ],
                "media_checks": ["text", "audio", "image", "location", "file"],
            },
        },
        "conversion_pitch": {
            "title": "Listo para vender en minutos",
            "bullets": [
                "Menú inteligente por rubro",
                "Automatización de reclamos, pedidos y sugerencias",
                "Soporte multimodal: audio, imagen, ubicación y archivos",
            ],
            "cta_label": "Hablar con ventas",
        },
        "component_pack": {
            "layout": "stacked_cards",
            "sections": [
                {
                    "id": "hero",
                    "component": "DemoHeroCard",
                    "props": {
                        "show_badge": True,
                        "show_cta": True,
                    },
                },
                {
                    "id": "quick_actions",
                    "component": "QuickActionGrid",
                    "props": {
                        "columns_mobile": 2,
                        "columns_desktop": 4,
                        "show_icons": True,
                    },
                },
                {
                    "id": "channels",
                    "component": "ChannelPlaybookTabs",
                    "props": {
                        "default_tab": "whatsapp",
                        "show_media_checks": True,
                    },
                },
                {
                    "id": "upsell",
                    "component": "UpgradeWallCard",
                    "props": {
                        "style": "enterprise",
                        "highlight_limit": True,
                    },
                },
            ],
        },
    }
