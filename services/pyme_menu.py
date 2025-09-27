"""Utilities for building structured PYME menus across channels."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List

from services.config_loader import cargar_configuracion_pyme

logger = logging.getLogger(__name__)


PYME_MENU_DISPLAY_ORDER = [
    "pyme_productos_stock",
    "pyme_promociones",
    "pyme_hacer_pedido",
    "pyme_estado_pedido",
    "pyme_hablar_agente",
    "ver_carrito_pyme",
    "limpiar_y_nuevo_pedido_saludo_pyme",
]


def _slugify(value: str | None) -> str:
    if not value:
        return "default"
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "default"


def _channels_allow(option: Dict[str, Any], channel: str) -> bool:
    channels = option.get("channels")
    if not channels:
        return True
    if isinstance(channels, str):
        channels = [channels]
    normalized = {str(ch).lower() for ch in channels}
    return channel.lower() in normalized or "all" in normalized


def _dedupe_options(options: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_ids: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for opt in options:
        option_id = str(opt.get("id") or opt.get("action_id") or opt.get("texto") or "").strip()
        if not option_id:
            continue
        if option_id in seen_ids:
            continue
        seen_ids.add(option_id)
        deduped.append(opt)
    return deduped


def get_pyme_menu_payload(context: Dict[str, Any], channel: str = "web") -> Dict[str, Any]:
    """Return a structured menu payload tailored for the active PYME rubro."""

    rubro_slug = _slugify(
        context.get("rubro_slug")
        or context.get("rubro_nombre")
        or context.get("rubro_clave")
    )

    menu_config = cargar_configuracion_pyme(rubro_slug, "menu.json")
    if not menu_config:
        menu_config = cargar_configuracion_pyme("default", "menu.json")

    nombre_pyme = context.get("nombre_pyme") or menu_config.get("nombre_pyme")
    greeting = menu_config.get("greeting") or (
        f"¡Hola! Soy tu asistente virtual para {nombre_pyme or 'la empresa'}."
    )
    help_text = menu_config.get("help_text")
    message_body = greeting.strip()
    if help_text:
        message_body += "\n\n" + help_text.strip()

    sections = menu_config.get("sections", [])
    structured_sections: List[Dict[str, Any]] = []
    flat_options: List[Dict[str, Any]] = []

    for section in sections:
        if not isinstance(section, dict):
            continue
        section_title = section.get("title") or section.get("nombre")
        rows: List[Dict[str, Any]] = []
        for option in section.get("options", []):
            if not isinstance(option, dict):
                continue
            if not _channels_allow(option, channel):
                continue

            action_id = option.get("action_id") or option.get("id")
            label = option.get("label") or option.get("texto") or option.get("title")
            description = option.get("description") or option.get("subtitle")
            prompt = option.get("prompt")

            if label:
                row_payload: Dict[str, Any] = {
                    "id": action_id or option.get("id") or label,
                    "title": label,
                }
                if description:
                    row_payload["description"] = description
                if prompt:
                    row_payload["prompt"] = prompt
                rows.append(row_payload)

            if action_id and label:
                option_entry = {"id": action_id, "texto": label}
                if description:
                    option_entry["descripcion"] = description
                flat_options.append(option_entry)

        if rows:
            structured_section = {"rows": rows}
            if section_title:
                structured_section["title"] = section_title
            if section.get("description"):
                structured_section["description"] = section.get("description")
            structured_sections.append(structured_section)

    quick_actions = menu_config.get("quick_actions", [])
    for quick_action in quick_actions:
        if not isinstance(quick_action, dict):
            continue
        if not _channels_allow(quick_action, channel):
            continue
        action_id = quick_action.get("action_id") or quick_action.get("id")
        label = quick_action.get("label") or quick_action.get("texto")
        description = quick_action.get("description")
        prompt = quick_action.get("prompt")

        if label:
            qa_row: Dict[str, Any] = {
                "id": action_id or quick_action.get("id") or label,
                "title": label,
            }
            if description:
                qa_row["description"] = description
            if prompt:
                qa_row["prompt"] = prompt
            structured_sections.append({
                "title": quick_action.get("category") or quick_action.get("title", "Accesos rápidos"),
                "rows": [qa_row],
            })

        if action_id and label:
            option_entry = {"id": action_id, "texto": label}
            if description:
                option_entry["descripcion"] = description
            flat_options.append(option_entry)

    if not flat_options:
        flat_options.extend(
            [
                {"id": "pyme_productos_stock", "texto": "Ver productos y stock"},
                {"id": "pyme_promociones", "texto": "Promociones vigentes"},
                {"id": "pyme_hacer_pedido", "texto": "Hacer un pedido"},
                {"id": "pyme_estado_pedido", "texto": "Estado de mi pedido"},
                {"id": "pyme_hablar_agente", "texto": "Hablar con un asesor"},
            ]
        )

    deduped_options = _dedupe_options(flat_options)
    indexed_options = list(enumerate(deduped_options))
    order_map = {action: idx for idx, action in enumerate(PYME_MENU_DISPLAY_ORDER)}
    indexed_options.sort(
        key=lambda item: (
            order_map.get(item[1].get("id") or item[1].get("action_id"), len(order_map)),
            item[0],
        )
    )
    flat_options = [item[1] for item in indexed_options][:10]

    payload: Dict[str, Any] = {
        "message_body": message_body,
        "options_list": flat_options,
        "message_type": "interactive_menu",
        "fuente": menu_config.get("fuente", "pyme_menu_estructurado_v1"),
        "data": {
            "title": menu_config.get("title") or "Menú principal",
            "sections": structured_sections,
        },
    }

    if channel.lower() == "whatsapp":
        assistant_name = menu_config.get("assistant_name") or "ACA WinRey"
        brand_name = nombre_pyme or menu_config.get("nombre_pyme") or "la bodega"
        whatsapp_lines = [
            f"🍷 ¡Hola! Soy *{assistant_name}*, tu asistente virtual de {brand_name}.",
        ]
        if help_text:
            whatsapp_lines.append(help_text.strip())
        whatsapp_lines.append("Elegí una opción para comenzar o escribime tu consulta.")
        payload["message_body"] = "\n\n".join([line for line in whatsapp_lines if line]).strip()
        payload["message_type"] = "text"
        payload["categorias"] = [
            {
                "titulo": menu_config.get("title") or "Opciones principales",
                "botones": [
                    {
                        "texto": option.get("texto"),
                        "action_id": option.get("id") or option.get("action_id"),
                    }
                    for option in flat_options
                    if option.get("texto")
                ],
            }
        ]

    if menu_config.get("footer"):
        payload["data"]["footer"] = menu_config["footer"]
    if menu_config.get("highlights"):
        payload["data"]["highlights"] = menu_config["highlights"]

    return payload
