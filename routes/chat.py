import sys
import os
import logging
import random
import re
import uuid  # Added for chat_session_id generation
from copy import deepcopy
from urllib.parse import urljoin
from typing import Dict, List, Optional, Tuple
from collections import OrderedDict

# Add project root to sys.path for this routes file
project_root_chat_routes = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_chat_routes not in sys.path:
    sys.path.insert(0, project_root_chat_routes)

from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func, desc
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified # Importado para flag_modified
from models import User, Rubro, Conversacion, db, ChatSessionContext # Added ChatSessionContext
from utils.db_utils import commit_with_retry, ensure_chat_session_context_schema
from socket_service import socketio # Import socketio
from services.logic import (
    RUBROS_PUBLICOS,
    normalizar_rubro,
    es_rubro_publico,
)
from services.live_chat_schedule import build_live_chat_status
from services.demo_registry import load_demo_rubros, demo_rubro_for_token
from utils.auth_helpers import (
    anon_o_token_requerido,
    obtener_token,
    user_from_token,
)
from utils.map_config import get_map_config
from utils.response_utils import normalize_response_payload
from datetime import datetime, timedelta

chat_bp = Blueprint("chat_bp", __name__)

DEMO_ACTION_PREFIX = "demo_select_rubro"
DEMO_MENU_PREFIX = "demo_menu"
DEMO_MENU_BACK_ACTION = f"{DEMO_MENU_PREFIX}:back"
DEMO_MENU_HOME_ACTION = f"{DEMO_MENU_PREFIX}:home"
DEMO_MENU_ROOT_ID = "demo_menu_root"


def _load_demo_rubros() -> List[Dict[str, Optional[str]]]:
    """Recupera la configuración de demos y la enriquece con datos reales."""

    opciones: List[Dict[str, Optional[str]]] = []
    for demo in load_demo_rubros():
        opciones.append(demo.to_internal_dict())
    return opciones


def _extract_demo_key(action_id: Optional[str]) -> Optional[str]:
    if not action_id:
        return None
    value = str(action_id).strip()
    if not value:
        return None

    if ":" in value:
        prefix, candidate = value.split(":", 1)
        if prefix != DEMO_ACTION_PREFIX:
            return None
    elif value.startswith(f"{DEMO_ACTION_PREFIX}_"):
        candidate = value[len(f"{DEMO_ACTION_PREFIX}_"):]
    elif value.startswith(DEMO_ACTION_PREFIX):
        candidate = value[len(DEMO_ACTION_PREFIX):]
        candidate = candidate.lstrip(":_")
    else:
        return None

    candidate = candidate.strip().lower()
    return candidate or None


def _is_init_payload(payload) -> bool:
    if payload is None:
        return True
    if isinstance(payload, str):
        return payload.strip() in ("", "__INIT__")
    if isinstance(payload, dict):
        inner = payload.get("pregunta")
        if inner is None:
            return True
        if isinstance(inner, str) and inner.strip() in ("", "__INIT__"):
            return True
    return False


def _log_widget_request(response, user):
    """Log widget chat requests with tenant and token context."""

    status_code = None
    response_obj = response
    if isinstance(response, tuple):
        response_obj = response[0]
        status_code = response[1] if len(response) > 1 else None
    if status_code is None and hasattr(response_obj, "status_code"):
        status_code = response_obj.status_code

    tenant_slug = getattr(user, "tenant_slug", None) if user else None
    entity_token = getattr(user, "entity_token", None) if user else None

    current_app.logger.info(
        "WIDGET_REQ path=%s user_id=%s tenant=%s entity_token=%s status=%s",
        getattr(request, "path", None),
        getattr(user, "id", None) if user else None,
        tenant_slug,
        entity_token,
        status_code,
    )

    return response


def _build_demo_selector_payload(opciones: List[Dict[str, Optional[str]]]) -> Dict[str, object]:
    mensaje = current_app.config.get(
        "DEMO_WELCOME_MESSAGE",
        "👋 ¡Bienvenido a la demo de Chatboc! Elegí la experiencia que querés probar:",
    )
    botones: List[Dict[str, object]] = []
    for opcion in opciones:
        action_value = f"{DEMO_ACTION_PREFIX}:{opcion['key']}"
        boton = {
            "texto": opcion["label"],
            "action_id": action_value,
            "action": action_value,
            "id": action_value,
        }
        if opcion.get("descripcion"):
            boton["descripcion"] = opcion["descripcion"]
        botones.append(boton)

    message_type = "interactive_list" if len(botones) > 1 else "interactive_buttons"
    return {
        "message_body": mensaje,
        "options_list": botones,
        "botones": botones,
        "message_type": message_type,
        "fuente": "demo_selector",
        "generar_audio": True,
    }


def _activate_demo_session(
    contexto_chat: Dict[str, object],
    demo_payload: Dict[str, object],
    *,
    owner_user: Optional[User] = None,
    rubro_obj: Optional[Rubro] = None,
    reset_counter: bool = False,
) -> bool:
    """Populate the session context with the selected demo metadata."""

    if not isinstance(contexto_chat, dict) or not isinstance(demo_payload, dict):
        return False

    changed = False
    existing_key = contexto_chat.get("demo_key")

    def _set(key: str, value: object) -> None:
        nonlocal changed
        if value is None:
            if key in contexto_chat:
                if contexto_chat.get(key) is not None:
                    changed = True
                contexto_chat.pop(key, None)
        else:
            if contexto_chat.get(key) != value:
                contexto_chat[key] = value
                changed = True

    _set("demo_session", True)

    owner_id = getattr(owner_user, "id", None) or demo_payload.get("owner_user_id")
    _set("demo_owner_user_id", owner_id)

    resolved_rubro = rubro_obj or None
    if not resolved_rubro and demo_payload.get("rubro_id"):
        try:
            resolved_rubro = Rubro.query.get(demo_payload["rubro_id"])
        except Exception:
            resolved_rubro = None

    rubro_id_value = getattr(resolved_rubro, "id", None) or demo_payload.get("rubro_id")
    _set("demo_rubro_id", rubro_id_value)

    rubro_clave_value = (
        getattr(resolved_rubro, "clave", None)
        or demo_payload.get("rubro_clave")
    )
    _set("demo_rubro_clave", rubro_clave_value)

    demo_key = demo_payload.get("key")
    _set("demo_key", demo_key)

    prompt_context = demo_payload.get("prompt_context") or demo_payload.get("descripcion")
    _set("demo_prompt_context", prompt_context)

    _set("demo_display_name", demo_payload.get("label"))
    _set("demo_description", demo_payload.get("descripcion"))
    _set("demo_welcome_message", demo_payload.get("welcome_message"))
    _set("demo_tipo_chat", demo_payload.get("tipo_chat"))

    resources = deepcopy(demo_payload.get("resources") or [])
    if contexto_chat.get("demo_resources") != resources:
        contexto_chat["demo_resources"] = resources
        changed = True

    faq_preview = deepcopy(demo_payload.get("faq_preview") or [])
    if contexto_chat.get("demo_faq_preview") != faq_preview:
        contexto_chat["demo_faq_preview"] = faq_preview
        changed = True

    quick_actions = deepcopy(demo_payload.get("quick_actions") or [])
    if contexto_chat.get("demo_quick_actions") != quick_actions:
        contexto_chat["demo_quick_actions"] = quick_actions
        changed = True

    capabilities = deepcopy(demo_payload.get("capabilities") or [])
    if contexto_chat.get("demo_capabilities") != capabilities:
        contexto_chat["demo_capabilities"] = capabilities
        changed = True

    keywords = deepcopy(demo_payload.get("keywords") or [])
    if contexto_chat.get("demo_keywords") != keywords:
        contexto_chat["demo_keywords"] = keywords
        changed = True

    should_reset_counter = reset_counter or existing_key != demo_key
    if should_reset_counter or "demo_message_count" not in contexto_chat:
        _set("demo_message_count", 0)
    if should_reset_counter:
        _set("demo_intro_sent", False)

    return changed


def _build_demo_limit_response(limite: int) -> Dict[str, object]:
    mensaje = (
        "¡Gracias por probar Chatboc! Llegaste al límite de "
        f"{limite} consultas de la demo interactiva. "
        "Agendá una reunión con nuestro equipo para conocer el potencial completo de la plataforma."
    )
    botones = [
        {
            "texto": "Solicitar Demo Personalizada",
            "action": "open_demo_form",
            "action_id": "open_demo_form",
            "id": "open_demo_form",
        },
        {"texto": "Iniciar Sesión", "action": "login", "action_id": "login", "id": "login"},
        {
            "texto": "Registrarme Gratis",
            "action": "register",
            "action_id": "register",
            "id": "register",
        },
    ]
    return {
        "error": "demo_limit_reached",
        "respuesta": mensaje,
        "message_body": mensaje,
        "options_list": botones,
        "botones": botones,
        "message_type": "interactive_buttons",
        "fuente": "demo_limit",
        "generar_audio": True,
    }


def _absolute_demo_url(path: Optional[str]) -> Optional[str]:
    """Devuelve una URL absoluta para recursos de la demo."""
    if not path:
        return None

    value = str(path).strip()
    if not value:
        return None

    if value.startswith(("http://", "https://", "data:")):
        return value

    base_url = current_app.config.get("BACKEND_URL") or request.host_url
    if not base_url.endswith("/"):
        base_url = f"{base_url}/"

    return urljoin(base_url, value.lstrip("/"))


def _format_demo_resources(
    resources: List[Dict[str, object]] | None,
) -> Tuple[str, List[Dict[str, object]], List[Dict[str, object]]]:
    """Genera texto, botones y adjuntos a partir de la configuración de recursos."""

    if not resources:
        return "", [], []

    icon_map = {
        "pdf": "📄",
        "document": "📄",
        "image": "🖼️",
        "video": "🎬",
        "spreadsheet": "📊",
        "pricing": "💰",
        "link": "🔗",
        "audio": "🎧",
        "presentation": "🗂️",
    }

    lines: List[str] = []
    buttons: List[Dict[str, object]] = []
    attachments: List[Dict[str, object]] = []

    for idx, raw in enumerate(resources):
        if not isinstance(raw, dict):
            continue

        title_raw = raw.get("title") or raw.get("nombre") or raw.get("label")
        description_raw = raw.get("description") or raw.get("descripcion")
        resource_type = str(raw.get("type") or raw.get("tipo") or "document").strip().lower() or "document"
        icon = icon_map.get(resource_type, "📎")

        title = str(title_raw).strip() if title_raw else None
        description = str(description_raw).strip() if description_raw else None
        cta_text_raw = raw.get("cta_text") or raw.get("cta") or title or "Ver recurso"
        cta_text = str(cta_text_raw).strip() if cta_text_raw else "Ver recurso"

        absolute_url = _absolute_demo_url(raw.get("url") or raw.get("href"))
        thumbnail_url = _absolute_demo_url(raw.get("thumbnail") or raw.get("image"))

        price_raw = raw.get("price") or raw.get("precio") or raw.get("price_text")
        price_text = str(price_raw).strip() if price_raw else None
        highlight_raw = raw.get("highlight") or raw.get("badge") or raw.get("tagline")
        highlight_text = str(highlight_raw).strip() if highlight_raw else None
        availability_raw = raw.get("availability") or raw.get("service_level")
        availability_text = str(availability_raw).strip() if availability_raw else None

        label_for_text = title or cta_text or f"Recurso {idx + 1}"
        detail_badges = [part for part in (highlight_text, price_text) if part]
        header_line = f"{icon} {label_for_text}"
        if detail_badges:
            header_line += " · " + " · ".join(detail_badges)
        lines.append(header_line)

        for detail_line in (description, availability_text):
            if detail_line:
                lines.append(f"   {detail_line}")
        lines.append("")

        action_id_raw = raw.get("action_id") or raw.get("id")
        if isinstance(action_id_raw, str) and action_id_raw.strip():
            action_id = action_id_raw.strip()
        else:
            slug_source = f"{label_for_text}-{idx}"
            slug = re.sub(r"[^a-z0-9]+", "_", slug_source.lower()).strip("_")
            action_id = slug or f"demo_resource_{idx}"

        if absolute_url:
            button_entry = {
                "id": action_id,
                "texto": f"{icon} {cta_text}",
                "type": raw.get("button_type") or "url",
                "url": absolute_url,
                "action_id": action_id,
                "action": absolute_url,
            }
            detail_for_button = [
                part
                for part in (
                    highlight_text,
                    description,
                    availability_text,
                    price_text,
                )
                if part
            ]
            if detail_for_button:
                button_entry["description"] = " · ".join(detail_for_button)
            elif description:
                button_entry["description"] = description
            if highlight_text:
                button_entry["badge"] = highlight_text
            buttons.append(button_entry)

        attachment_entry: Dict[str, object] = {
            "titulo": label_for_text,
            "descripcion": description,
            "tipo": resource_type,
        }
        if cta_text:
            attachment_entry["cta"] = cta_text
        if price_text:
            attachment_entry["precio"] = price_text
        if highlight_text:
            attachment_entry["badge"] = highlight_text
        if availability_text:
            attachment_entry["disponibilidad"] = availability_text
        if absolute_url:
            attachment_entry["url"] = absolute_url
        if thumbnail_url:
            attachment_entry["thumbnail"] = thumbnail_url
        attachments.append(attachment_entry)

    formatted_text = "\n".join(line for line in lines if line) if lines else ""
    return formatted_text, buttons, attachments


def _build_demo_menu_registry(
    quick_actions: List[Dict[str, object]] | None,
) -> Tuple[Dict[str, Dict[str, object]], str, List[str]]:
    """Crea una estructura de menús navegables basada en las quick actions configuradas."""

    if not quick_actions:
        return {}, DEMO_MENU_ROOT_ID, []

    registry: Dict[str, Dict[str, object]] = {}
    prompt_examples: List[str] = []
    used_ids: set[str] = set()

    def _next_id(base: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "menu"
        candidate = slug
        counter = 1
        while candidate in used_ids or candidate in registry:
            counter += 1
            candidate = f"{slug}_{counter}"
        used_ids.add(candidate)
        return candidate

    def _process_entries(
        entries: List[Dict[str, object]] | None,
        menu_id: str,
        title: Optional[str],
        description: Optional[str],
        parent: Optional[str],
    ) -> Dict[str, object]:
        groups: "OrderedDict[Optional[str], List[Dict[str, object]]]" = OrderedDict()
        category_order: List[Optional[str]] = []
        items: List[Dict[str, object]] = []

        if not entries:
            return {
                "id": menu_id,
                "title": title or "Menú",
                "description": description,
                "items": items,
                "groups": groups,
                "category_order": category_order,
                "parent": parent,
            }

        for idx, raw in enumerate(entries):
            if not isinstance(raw, dict):
                continue

            emoji = raw.get("emoji") or raw.get("icon")
            label_raw = raw.get("texto") or raw.get("label") or raw.get("title") or raw.get("name")
            if not label_raw:
                label_raw = f"Opción {idx + 1}"
            label = str(label_raw).strip()
            if not label:
                continue

            display_text = f"{emoji} {label}" if emoji and not label.startswith(str(emoji)) else label
            description_raw = raw.get("description") or raw.get("descripcion")
            description_text = str(description_raw).strip() if description_raw else None

            category_raw = raw.get("category") or raw.get("grupo") or raw.get("section")
            category_text = str(category_raw).strip() if category_raw else None
            if category_text not in groups:
                groups[category_text] = []
                category_order.append(category_text)

            prompt_raw = raw.get("prompt") or raw.get("question") or raw.get("payload")
            prompt_text = str(prompt_raw).strip() if prompt_raw else None
            if prompt_text:
                normalized_prompt = prompt_text
                if normalized_prompt not in prompt_examples:
                    prompt_examples.append(normalized_prompt)

            base_id = raw.get("id") or raw.get("action_id") or raw.get("key") or f"{menu_id}_{idx + 1}"
            candidate = str(base_id)
            candidate_id = re.sub(r"[^a-z0-9]+", "_", candidate.lower()).strip("_")
            if not candidate_id:
                candidate_id = f"{menu_id}_{idx + 1}"
            if candidate_id in used_ids:
                candidate_id = _next_id(candidate_id)
            else:
                used_ids.add(candidate_id)

            item_entry: Dict[str, object] = {
                "id": candidate_id,
                "label": label,
                "texto": display_text,
                "description": description_text,
                "category": category_text,
                "emoji": emoji,
            }

            submenu_entries = raw.get("submenu") or raw.get("children") or raw.get("items")
            submenu_title = raw.get("submenu_title")
            submenu_description = raw.get("submenu_description")

            if submenu_entries:
                child_base_id = raw.get("menu_id") or f"{candidate_id}_menu"
                child_slug = re.sub(r"[^a-z0-9]+", "_", str(child_base_id).lower()).strip("_")
                if not child_slug:
                    child_slug = f"{candidate_id}_submenu"
                if child_slug in used_ids or child_slug in registry:
                    child_slug = _next_id(child_slug)
                else:
                    used_ids.add(child_slug)

                child_title = submenu_title or display_text
                child_desc = submenu_description or description_text
                child_menu = _process_entries(submenu_entries, child_slug, child_title, child_desc, menu_id)
                registry[child_slug] = child_menu

                item_entry["type"] = "menu"
                item_entry["menu_id"] = child_slug
                item_entry["action"] = f"{DEMO_MENU_PREFIX}:{child_slug}"
            else:
                action_raw = raw.get("action")
                action_text = str(action_raw).strip() if isinstance(action_raw, str) else None
                if action_text:
                    resolved_action = action_text
                elif prompt_text:
                    resolved_action = prompt_text
                else:
                    resolved_action = label
                item_entry["action"] = resolved_action
                item_entry["type"] = raw.get("type") or "quick_reply"
                if prompt_text:
                    item_entry["prompt"] = prompt_text

            groups[category_text].append(item_entry)
            items.append(item_entry)

        return {
            "id": menu_id,
            "title": title or "Menú",
            "description": description,
            "items": items,
            "groups": groups,
            "category_order": category_order,
            "parent": parent,
        }

    root_menu = _process_entries(quick_actions, DEMO_MENU_ROOT_ID, "Menú principal", None, None)
    registry[DEMO_MENU_ROOT_ID] = root_menu

    return registry, DEMO_MENU_ROOT_ID, prompt_examples


def _format_menu_items(
    menu_entry: Optional[Dict[str, object]],
) -> Tuple[str, str, List[str], List[Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
    """Genera texto y botones para un menú navegable."""

    if not menu_entry:
        return "", "", [], [], []

    groups: "OrderedDict[Optional[str], List[Dict[str, object]]]" = menu_entry.get("groups") or OrderedDict()
    category_order: List[Optional[str]] = menu_entry.get("category_order") or []

    lines: List[str] = []
    for category in category_order:
        items = groups.get(category) or []
        if not items:
            continue
        if category:
            lines.append(str(category))
        for item in items:
            label = str(item.get("texto") or item.get("label") or "").strip()
            if not label:
                continue
            description = str(item.get("description") or "").strip()
            indicator = " ⮕" if str(item.get("type") or "").lower() == "menu" else ""
            lines.append(f"   ◾ {label}{indicator}")
            if description:
                lines.append(f"      {description}")
        lines.append("")

    menu_text = "\n".join(line for line in lines if line).strip()

    prompt_lines: List[str] = []
    seen_prompts: set[str] = set()
    for item in menu_entry.get("items", []):
        prompt = item.get("prompt")
        if prompt and prompt not in seen_prompts:
            prompt_lines.append(f'• "{prompt}"')
            seen_prompts.add(prompt)
    prompt_text = "\n".join(prompt_lines)

    buttons: List[Dict[str, object]] = []
    quick_items: List[Dict[str, object]] = []
    grouped_sections: List[Dict[str, object]] = []

    for idx, item in enumerate(menu_entry.get("items", [])):
        item_id = item.get("id") or f"menu_item_{idx}"
        action_value = item.get("action") or item.get("prompt") or item.get("label")
        button: Dict[str, object] = {
            "texto": item.get("texto") or item.get("label") or "Opción",
            "action": action_value,
            "id": item_id,
            "type": item.get("type") or "quick_reply",
        }
        if item.get("type") == "menu":
            button["action_id"] = action_value
            button["id"] = action_value
        else:
            button["action_id"] = item_id
        description = item.get("description")
        if description:
            button["description"] = description
        buttons.append(button)

        quick_item: Dict[str, object] = {
            "id": item_id,
            "texto": item.get("texto") or item.get("label") or "Opción",
            "description": description,
            "action": action_value,
            "type": item.get("type") or "quick_reply",
        }
        if item.get("prompt"):
            quick_item["prompt"] = item.get("prompt")
        if item.get("emoji"):
            quick_item["emoji"] = item.get("emoji")
        if item.get("category"):
            quick_item["category"] = item.get("category")
        if item.get("type") == "menu" and item.get("menu_id"):
            quick_item["menu_id"] = item.get("menu_id")
        quick_items.append({k: v for k, v in quick_item.items() if v})

    for category in category_order:
        items = groups.get(category) or []
        if not items:
            continue
        group_entry: Dict[str, object] = {
            "title": category or "Opciones disponibles",
            "category": category,
            "items": [],
        }
        for item in items:
            group_item: Dict[str, object] = {
                "id": item.get("id"),
                "texto": item.get("texto") or item.get("label") or "Opción",
                "description": item.get("description"),
                "action": item.get("action") or item.get("prompt") or item.get("label"),
                "type": item.get("type") or "quick_reply",
            }
            if item.get("prompt"):
                group_item["prompt"] = item.get("prompt")
            if item.get("emoji"):
                group_item["emoji"] = item.get("emoji")
            if item.get("category"):
                group_item["category"] = item.get("category")
            if item.get("type") == "menu" and item.get("menu_id"):
                group_item["menu_id"] = item.get("menu_id")
            group_entry["items"].append({k: v for k, v in group_item.items() if v})
        grouped_sections.append(group_entry)

    return menu_text, prompt_text, prompt_lines, buttons, quick_items, grouped_sections


def _interactive_sections_from_quick_items(
    quick_items: List[Dict[str, object]] | None,
) -> List[Dict[str, object]]:
    if not quick_items:
        return []

    rows_by_category: "OrderedDict[str, List[Dict[str, str]]]" = OrderedDict()

    for item in quick_items:
        row_id = item.get("id") or item.get("action")
        title = item.get("texto")
        if not row_id or not title:
            continue
        category = item.get("category") or "Menú principal"
        if category not in rows_by_category:
            rows_by_category[category] = []
        desc_parts: List[str] = []
        if item.get("description"):
            desc_parts.append(str(item["description"]))
        if item.get("prompt"):
            prompt_text = str(item["prompt"])
            if prompt_text and prompt_text not in desc_parts:
                desc_parts.append(prompt_text)
        rows_by_category[category].append({
            "id": row_id,
            "title": str(title),
            "description": " · ".join(desc_parts) if desc_parts else "",
        })

    sections: List[Dict[str, object]] = []
    for category, rows in rows_by_category.items():
        if not rows:
            continue
        sections.append({
            "title": str(category) if category else "Menú principal",
            "rows": rows,
        })

    return sections


def _build_menu_navigation_payload(
    menu_entry: Dict[str, object],
    *,
    menu_registry: Dict[str, Dict[str, object]],
    root_id: str,
    stack: List[str],
) -> Dict[str, object]:
    (
        menu_text,
        prompt_text,
        _prompt_lines,
        buttons,
        quick_items,
        grouped_sections,
    ) = _format_menu_items(menu_entry)

    breadcrumbs: List[str] = []
    for menu_id in stack:
        entry = menu_registry.get(menu_id)
        if not entry:
            continue
        title = str(entry.get("title") or entry.get("id") or "").strip()
        if title:
            breadcrumbs.append(title)
    breadcrumb_text = " > ".join(breadcrumbs)

    title_line = str(menu_entry.get("title") or "Menú").strip()
    description_line = str(menu_entry.get("description") or "").strip()

    segments: List[str] = []
    if breadcrumb_text and breadcrumb_text.lower() != title_line.lower():
        segments.append(f"{title_line}\nRuta: {breadcrumb_text}")
    else:
        segments.append(title_line)
    if description_line:
        segments.append(description_line)
    if menu_text:
        segments.append(f"━━━━━━━━━━━━\nOpciones disponibles\n{menu_text}")
    if prompt_text:
        segments.append(f"💬 Probá decir\n{prompt_text}")
    segments.append("Elegí una opción o usá los botones para navegar.")

    message_text = "\n\n".join(seg for seg in segments if seg)

    payload: Dict[str, object] = {
        "message_body": message_text,
        "respuesta": message_text,
        "fuente": "demo_menu",
    }

    menu_buttons: List[Dict[str, object]] = []
    seen_ids: set[str] = set()
    for button in buttons:
        if not isinstance(button, dict):
            continue
        candidate = deepcopy(button)
        candidate_id = str(
            candidate.get("id")
            or candidate.get("action_id")
            or candidate.get("action")
            or len(menu_buttons)
        )
        if candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        menu_buttons.append(candidate)

    current_menu_id = str(menu_entry.get("id") or "")
    if current_menu_id and current_menu_id != root_id:
        back_button = {
            "texto": "⬅️ Volver al menú anterior",
            "action": DEMO_MENU_BACK_ACTION,
            "action_id": DEMO_MENU_BACK_ACTION,
            "id": DEMO_MENU_BACK_ACTION,
            "type": "menu",
        }
        home_button = {
            "texto": "🏠 Menú principal",
            "action": f"{DEMO_MENU_PREFIX}:{root_id}",
            "action_id": f"{DEMO_MENU_PREFIX}:{root_id}",
            "id": f"{DEMO_MENU_PREFIX}:{root_id}",
            "type": "menu",
        }
        for nav_button in (back_button, home_button):
            nav_id = nav_button["id"]
            if nav_id not in seen_ids:
                menu_buttons.append(nav_button)
                seen_ids.add(nav_id)

    payload["options_list"] = menu_buttons
    payload["botones"] = menu_buttons

    non_url_buttons = [btn for btn in menu_buttons if btn.get("type") != "url"]
    payload["message_type"] = "interactive_list" if len(non_url_buttons) > 3 else "interactive_buttons"

    menu_sections_payload: List[Dict[str, object]] = []
    if quick_items:
        section_payload: Dict[str, object] = {
            "title": menu_entry.get("title") or "Menú",
            "type": "quick_actions",
            "items": quick_items,
        }
        if grouped_sections:
            section_payload["groups"] = grouped_sections
        menu_sections_payload.append(section_payload)
    if menu_sections_payload:
        payload["menu_sections"] = menu_sections_payload

    if quick_items:
        interactive_sections = _interactive_sections_from_quick_items(quick_items)
        if interactive_sections:
            payload["interactive_list_sections"] = interactive_sections
            if payload.get("message_type") == "interactive_list":
                payload.setdefault("interactive_list_button_text", "Ver opciones")
                if len(interactive_sections) == 1:
                    payload.setdefault(
                        "interactive_list_section_title",
                        interactive_sections[0].get("title") or (menu_entry.get("title") or "Menú"),
                    )
                else:
                    payload.setdefault("interactive_list_section_title", menu_entry.get("title") or "Menú")

    payload["generar_audio"] = True
    return payload


def _handle_demo_menu_action(
    contexto_chat: Dict[str, object],
    action_value: Optional[str],
) -> Optional[Dict[str, object]]:
    if not isinstance(action_value, str):
        return None

    normalized = action_value.strip()
    if not normalized:
        return None

    menu_registry = contexto_chat.get("demo_menu_registry")
    if not isinstance(menu_registry, dict) or not menu_registry:
        return None

    root_id = str(contexto_chat.get("demo_menu_root_id") or DEMO_MENU_ROOT_ID)

    stack_raw = contexto_chat.get("demo_menu_stack")
    if isinstance(stack_raw, list) and stack_raw:
        stack = [str(item).strip() for item in stack_raw if str(item).strip()]
    else:
        stack = []
    if not stack:
        stack = [root_id]

    target_id = None

    if normalized == DEMO_MENU_BACK_ACTION:
        if len(stack) > 1:
            stack.pop()
        target_id = stack[-1]
    elif normalized in {DEMO_MENU_HOME_ACTION, f"{DEMO_MENU_PREFIX}:{root_id}"}:
        stack = [root_id]
        target_id = root_id
    elif normalized.startswith(f"{DEMO_MENU_PREFIX}:"):
        candidate = normalized.split(":", 1)[1].strip()
        if not candidate:
            candidate = root_id
        if candidate not in menu_registry:
            return None
        path: List[str] = []
        current = candidate
        guard = 0
        while current and guard < 50:
            path.insert(0, current)
            parent_id = menu_registry.get(current, {}).get("parent")
            if not parent_id:
                break
            current = str(parent_id)
            guard += 1
        if not path or path[0] != root_id:
            path.insert(0, root_id)
        stack = path
        target_id = candidate
    else:
        return None

    if target_id not in menu_registry:
        target_id = root_id
        if target_id not in menu_registry:
            return None

    contexto_chat["demo_menu_stack"] = stack
    menu_entry = menu_registry[target_id]
    return _build_menu_navigation_payload(
        menu_entry,
        menu_registry=menu_registry,
        root_id=root_id,
        stack=stack,
    )

def _format_demo_quick_actions(
    quick_actions: List[Dict[str, object]] | None,
) -> Tuple[
    str,
    str,
    List[Dict[str, object]],
    Dict[str, Dict[str, object]],
    str,
    List[Dict[str, object]],
    List[Dict[str, object]],
]:
    """Render quick access suggestions for the intro message."""

    if not quick_actions:
        return "", "", [], {}, DEMO_MENU_ROOT_ID, [], []

    menu_registry, root_menu_id, prompt_examples = _build_demo_menu_registry(quick_actions)
    root_menu = menu_registry.get(root_menu_id)

    (
        menu_text,
        prompt_text_root,
        prompt_lines_root,
        buttons,
        quick_items,
        grouped_sections,
    ) = _format_menu_items(root_menu)

    combined_prompts: List[str] = []
    seen_prompts: set[str] = set()

    if root_menu:
        for item in root_menu.get("items", []):
            prompt_val = item.get("prompt")
            if prompt_val and prompt_val not in seen_prompts:
                combined_prompts.append(prompt_val)
                seen_prompts.add(prompt_val)

    for prompt in prompt_examples:
        if prompt and prompt not in seen_prompts:
            combined_prompts.append(prompt)
            seen_prompts.add(prompt)

    prompt_examples_text = "\n".join(f'• "{prompt}"' for prompt in combined_prompts) or prompt_text_root

    return (
        menu_text,
        prompt_examples_text,
        buttons,
        menu_registry,
        root_menu_id,
        quick_items,
        grouped_sections,
    )


def _format_demo_keywords(keywords: List[object] | None) -> str:
    if not keywords:
        return ""

    lines: List[str] = []
    seen: set[str] = set()
    for raw in keywords:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        bullet = text if text.startswith("•") else f"• {text}"
        lines.append(bullet)

    return "\n".join(lines)


def _format_demo_capabilities(capabilities: List[object] | None) -> str:
    if not capabilities:
        return ""

    lines: List[str] = []
    seen: set[str] = set()
    for raw in capabilities:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        normalized = text.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        bullet = text if text.startswith("•") else f"• {text}"
        lines.append(bullet)

    return "\n".join(lines)


def _format_demo_faq_preview(faq_preview: List[Dict[str, object]] | None) -> str:
    if not faq_preview:
        return ""

    lines: List[str] = []
    for raw in faq_preview:
        if not isinstance(raw, dict):
            continue

        question = str(raw.get("pregunta") or raw.get("question") or "").strip()
        if not question:
            continue
        answer = str(raw.get("respuesta") or raw.get("answer") or "").strip()
        line = f"• ❓ {question}"
        if answer:
            line += f" → {answer}"
        lines.append(line)

    return "\n".join(lines)

def _parse_request(tipo_chat_fijo: str | None = None):
    def _normalizar_tipo_chat(valor: str | None) -> str | None:
        if not valor:
            return None
        valor = str(valor).strip().lower()
        sinonimos = {
            "pymes": "pyme",
            "pyme": "pyme",
            "municipios": "municipio",
            "municipio": "municipio",
            "muni": "municipio",
        }
        return sinonimos.get(valor)

    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise TypeError("El cuerpo debe ser JSON")

        pregunta = data.get("pregunta")
        attachment_info = data.get("attachmentInfo") or data.get("attachment_info")
        location = data.get("location")
        raw_action = data.get("action") or data.get("action_id")

        # If the question is empty (or not provided) and there's no extra payload
        # (location, attachment or quick action), it's the initial message from the widget.
        if (
            not location
            and not attachment_info
            and not raw_action
            and (pregunta is None or str(pregunta).strip() == "")
        ):
            pregunta = "__INIT__"

        if tipo_chat_fijo:
            tipo_chat = tipo_chat_fijo
        else:
            tipo_chat = _normalizar_tipo_chat(data.get("tipo_chat"))

        contexto_previo = data.get("contexto_previo")
        rubro_id = data.get("rubro_id")
        rubro_clave = data.get("rubro_clave") or data.get("rubro")

        if tipo_chat not in ("pyme", "municipio"):
            rubro_obj_tmp = None
            try:
                if rubro_id:
                    rubro_obj_tmp = Rubro.query.get(int(rubro_id))
                elif rubro_clave:
                    rubro_obj_tmp = Rubro.query.filter(func.lower(Rubro.clave) == func.lower(str(rubro_clave))).first()
            except Exception:
                rubro_obj_tmp = None

            if rubro_obj_tmp:
                tipo_chat = "municipio" if es_rubro_publico(rubro_obj_tmp) else "pyme"
            else:
                raise ValueError("'tipo_chat' debe ser 'pyme' o 'municipio'")

        ticket_id = data.get("ticket_id")
        tipo_ticket = data.get("tipo_ticket")
        profile_name = data.get("nombre_usuario") or data.get("profile_name")
        action_id = raw_action

        if isinstance(pregunta, dict):
            action_id = action_id or pregunta.get("action") or pregunta.get("action_id")


        if attachment_info:
            if not isinstance(attachment_info, dict) or not all(k in attachment_info for k in ['id', 'url']):
                current_app.logger.warning(
                    "attachmentInfo validado de forma laxa. Contenido: %s",
                    str(attachment_info),
                )
                # raise ValueError("El campo 'attachmentInfo' es inválido o le faltan campos requeridos.")

        normalized_location = None
        if location:
            if not isinstance(location, dict):
                raise ValueError("El campo 'location' es inválido.")

            lat_value = location.get("lat")
            lon_value = location.get("lon")
            if lat_value is None:
                lat_value = location.get("latitude")
            if lon_value is None:
                lon_value = location.get("longitude")
            if lon_value is None:
                lon_value = location.get("lng")

            if lat_value is None or lon_value is None:
                raise ValueError("El campo 'location' es inválido.")

            try:
                lat_float = float(lat_value)
                lon_float = float(lon_value)
            except (TypeError, ValueError):
                raise ValueError("El campo 'location' es inválido.")

            normalized_location = {
                "latitude": lat_float,
                "longitude": lon_float,
                "lat": lat_float,
                "lon": lon_float,
            }

            accuracy = location.get("accuracy")
            if accuracy is not None:
                try:
                    normalized_location["accuracy"] = float(accuracy)
                except (TypeError, ValueError):
                    normalized_location["accuracy"] = accuracy

            address = location.get("address") or location.get("label")
            if address:
                normalized_location["address"] = address

            source = location.get("source")
            if source:
                normalized_location["source"] = source

            for extra_key in ("name", "description"):
                if location.get(extra_key):
                    normalized_location[extra_key] = location[extra_key]

            normalized_location.update({
                key: value
                for key, value in location.items()
                if key not in normalized_location and value is not None
            })

            location = normalized_location

        return (
            pregunta,
            contexto_previo,
            tipo_chat,
            rubro_id,
            rubro_clave,
            attachment_info,
            location,
            ticket_id,
            tipo_ticket,
            profile_name,
            action_id,
            None,
        )

    except (TypeError, ValueError) as e:
        current_app.logger.warning(f"Error al parsear /ask: {e}")
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            jsonify({"error": str(e)}),
        )
    except Exception as e:
        current_app.logger.error(f"Error inesperado al parsear /ask: {e}")
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            jsonify({"error": "Formato JSON inválido"}),
        )

def _authenticate_and_get_user():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token:
            return User.query.filter_by(token=token).first()
    return None

def _procesar_chat(
    tipo_chat_fijo: str | None = None,
    current_user=None,
    owner_user=None,
    anon_id: str | None = None,
): 
    from services.logic import responder_chatboc

    channel = "web"  # Define channel for this processing function
    original_user_payload = None
    # --- Session and Context Initialization ---
    chat_session_id_header = request.headers.get("X-Chat-Session-Id")
    if not chat_session_id_header:
        chat_session_id_header = str(uuid.uuid4())
        current_app.logger.warning(f"X-Chat-Session-Id not found. Generated new: {chat_session_id_header}")

    def _emit_socket_payload(payload: object) -> None:
        """Emite un mensaje por Socket.IO si hay una sesión web activa."""

        if channel == "web" and chat_session_id_header:
            normalize_response_payload(payload)

            socketio.emit('message', payload, room=chat_session_id_header)
            current_app.logger.debug(
                "Emitting socket message (early return)",
                extra={"room": chat_session_id_header, "payload": payload},
            )

    actor_principal = current_user

    # Failsafe: make sure schema is aligned even if migrations lag behind
    ensure_chat_session_context_schema(db.session)
    try:
        chat_context_obj = ChatSessionContext.query.filter_by(
            chat_session_id=chat_session_id_header
        ).first()
    except ProgrammingError as exc:
        current_app.logger.warning(
            "[CHAT] tenant_id missing when querying chat_session_context; retrying after safeguard",
            exc_info=exc,
        )
        db.session.rollback()
        ensure_chat_session_context_schema(db.session)
        try:
            chat_context_obj = ChatSessionContext.query.filter_by(
                chat_session_id=chat_session_id_header
            ).first()
        except ProgrammingError as exc_retry:
            current_app.logger.exception(
                "[CHAT] Error accediendo a chat_session_context (schema mismatch)",
                exc_info=exc_retry,
            )
            db.session.rollback()
            return (
                jsonify(
                    {
                        "error": "Estamos ajustando el servicio. Por favor, reintentá en unos minutos.",
                    }
                ),
                200,
            )
    except SQLAlchemyError as exc:
        current_app.logger.exception(
            "[CHAT] Error de base de datos obteniendo el contexto de sesión",
            exc_info=exc,
        )
        db.session.rollback()
        return (
            jsonify({"error": "Hubo un problema momentáneo. Probá de nuevo en breve."}),
            200,
        )

    if not chat_context_obj:
        current_app.logger.info(f"No ChatSessionContext found for {chat_session_id_header}. Creating new one.")
        chat_context_obj = ChatSessionContext(
            chat_session_id=chat_session_id_header,
            user_id=getattr(actor_principal, 'id', None),
            anon_id=anon_id if not actor_principal else None,
            context_data={}
        )
        db.session.add(chat_context_obj)
        try:
            commit_with_retry(db.session)
            current_app.logger.info(
                f"ChatSessionContext inicial guardado para {chat_session_id_header} (commit temprano)."
            )
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(
                f"Error guardando ChatSessionContext inicial para {chat_session_id_header}: {e}",
                exc_info=True,
            )
            return (
                jsonify({"error": "Error de base de datos"}),
                500,
            )

    # --- Request Parsing (Audio or JSON) ---
    if 'audio_file' in request.files:
        audio_file = request.files['audio_file']
        if audio_file.filename != '':
            from services.google_speech_to_text import SpeechToTextService
            import tempfile

            chat_context_obj.context_data['source_is_audio'] = True

            # Use a more unique filename to avoid collisions
            temp_filename = f"{uuid.uuid4()}_{audio_file.filename}"
            temp_path = os.path.join(tempfile.gettempdir(), temp_filename)
            audio_file.save(temp_path)

            stt_service = SpeechToTextService()
            pregunta = stt_service.transcribe_audio_file(file_path=temp_path, mime_type=audio_file.mimetype)

            os.remove(temp_path)

            if not pregunta:
                return jsonify({
                    "message_body": "Lo siento, no pude entender lo que dijiste en el audio. ¿Podrías intentarlo de nuevo o escribir tu consulta?",
                    "message_type": "text",
                    "fuente": "audio_transcription_failed"
                }), 400

            # Set default values for other parameters when processing audio
            contexto_previo = None
            tipo_chat = tipo_chat_fijo or 'municipio'
            rubro_id = request.form.get('rubro_id')
            rubro_clave = request.form.get('rubro_clave')
            uploaded_file_info = None
            archivo_adjunto_id = None
            location = None
            action_id = None
            original_user_payload = pregunta
        else:
            return jsonify({"error": "Audio file is empty."}), 400
    else:
        # chat_context_obj.context_data.pop('source_is_audio', None) # This was moved to after the check
        try:
            (
                pregunta,
                contexto_previo,
                tipo_chat,
                rubro_id,
                rubro_clave,
                attachment_info,
                location,
                ticket_id,
                tipo_ticket,
                profile_name,
                action_id,
                error_response,
            ) = _parse_request(tipo_chat_fijo)
            if error_response:
                return error_response, 400
            original_user_payload = pregunta

            # Fallback to cookies or stored session context if profile name not provided in JSON
            if not profile_name:
                profile_name = request.cookies.get("nombre_usuario") or request.cookies.get("profile_name")
            if not profile_name and chat_context_obj and chat_context_obj.context_data:
                profile_name = chat_context_obj.context_data.get("profile_name")

            # Si la solicitud solo contenía una ubicación, creamos una pregunta sintética para que el backend la procese.
            if not pregunta and location:
                pregunta = "[Ubicación compartida por el usuario]"
        except Exception as e:
            current_app.logger.error(f"Error parsing request in _procesar_chat: {e}", exc_info=True)
            return jsonify({"error": f"Invalid request format: {e}"}), 400

        current_app.logger.debug(
            "Parsed request data",
            extra={
                "pregunta": pregunta,
                "tipo_chat": tipo_chat,
                "rubro_id": rubro_id,
                "rubro_clave": rubro_clave,
                "attachmentInfo": attachment_info,
                "location": location,
                "ticket_id": ticket_id,
                "tipo_ticket": tipo_ticket,
            },
        )

    # --- Intercept messages for active live chats ---
    if ticket_id and tipo_ticket and pregunta:
        from models import MunicipioTicket, PymeTicket
        from services.ticket_service import servicio_tickets

        TicketModel = MunicipioTicket if tipo_ticket == "municipio" else PymeTicket
        # Use with_for_update to lock the row during the check and update
        ticket = db.session.query(TicketModel).filter_by(id=ticket_id).with_for_update().first()

        if ticket and ticket.estado in ["esperando_agente_en_vivo", "en_proceso", "en_vivo"]:
            comentario_data = {
                "comentario": pregunta,
                "user_id": getattr(current_user, "id", None),
                "anon_id": anon_id if not current_user else None,
                "es_admin": False
            }

            if attachment_info:
                archivo_id = attachment_info.get('id')
                comentario_data['archivo_adjunto_id'] = archivo_id
                if not pregunta.strip():
                    comentario_data['comentario'] = f"[Archivo adjunto: {attachment_info.get('name', 'archivo')}]"
                else:
                    comentario_data['comentario'] += f" [Archivo: {attachment_info.get('name', 'archivo')}]"

            nuevo_comentario = servicio_tickets.crear_comentario(
                ticket_id=ticket_id,
                tipo_ticket=tipo_ticket,
                comentario_data=comentario_data
            )

            if nuevo_comentario:
                commit_with_retry(db.session) # Commit the new comment
                room_name = f"ticket_{tipo_ticket}_{ticket_id}"
                socketio.emit('new_chat_message', {
                    'ticket_id': ticket_id,
                    'message': nuevo_comentario.to_dict()
                }, room=room_name)
                current_app.logger.info(f"User message for active ticket {ticket_id} sent to room {room_name}")
                return jsonify({"status": "message_sent_to_live_chat"}), 200
            else:
                db.session.rollback()
                return jsonify({"error": "Failed to save user message for live chat"}), 500

    try:
        # --- User and Role Determination ---
        is_anonymous = not actor_principal
        viewer_obj = current_user # El que mira

        if is_anonymous and not anon_id:
            # This case should ideally not be reached if anon_o_token_requerido is working correctly,
            # as it should have generated an anon_id. This is a safeguard.
            current_app.logger.warning("anon_id no fue provisto a _procesar_chat para un usuario anónimo. El decorador podría no estar funcionando como se espera.")
            return jsonify({"error": "No se pudo identificar la sesión anónima."}), 401

        is_init_request = _is_init_payload(original_user_payload)

        if is_anonymous:
            # Lógica para usuarios anónimos
            max_messages = current_app.config.get("ANONYMOUS_MAX_MESSAGES_PER_SESSION", 10)
            session_timeout_minutes = current_app.config.get("ANONYMOUS_SESSION_TIMEOUT_MINUTES", 15)

            last_message_time = db.session.query(func.max(Conversacion.timestamp)) \
                .filter(Conversacion.session_id == anon_id) \
                .scalar()

            session_expired = False
            if last_message_time:
                if datetime.utcnow() - last_message_time > timedelta(minutes=session_timeout_minutes):
                    session_expired = True
                    current_app.logger.info(f"Sesión anónima {anon_id} expirada. Reiniciando conteo de mensajes.")

            if not session_expired:
                message_count_this_session = Conversacion.query \
                    .filter(Conversacion.session_id == anon_id) \
                    .filter(Conversacion.timestamp >= datetime.utcnow() - timedelta(minutes=session_timeout_minutes)) \
                    .count()

                current_app.logger.info(f"Usuario anónimo {anon_id}: {message_count_this_session} mensajes en la sesión actual (límite: {max_messages}).")

                if message_count_this_session >= max_messages and not is_init_request:
                    return jsonify({
                        "error": "Alcanzaste el límite de mensajes para usuarios invitados.",
                        "respuesta": "Alcanzaste el límite de mensajes para usuarios invitados. Para continuar, por favor inicia sesión o regístrate.",
                        "botones": [
                            {"texto": "Iniciar Sesión", "action": "login"},
                            {"texto": "Registrarme Gratis", "action": "register"}
                        ]
                    }), 403
        else:
            # Lógica para usuarios autenticados
            current_app.logger.info(f"Usuario autenticado: {actor_principal.email} (ID: {actor_principal.id})")
            # No se aplican límites de mensajes para usuarios autenticados
            # Si el usuario está logueado, usar su ubicación guardada si no se proporciona una nueva
            if not location and actor_principal.latitud and actor_principal.longitud:
                location = {"lat": actor_principal.latitud, "lon": actor_principal.longitud}

        rubro_obj_global = None
        owner_del_bot = None
        rubro_para_log = None

        if chat_context_obj and chat_context_obj.context_data is None:
            chat_context_obj.context_data = {}

        contexto_chat = chat_context_obj.context_data if chat_context_obj else {}
        if not isinstance(contexto_chat, dict):
            contexto_chat = {}
            if chat_context_obj:
                chat_context_obj.context_data = contexto_chat

        demo_session_activa = bool(
            isinstance(contexto_chat, dict) and contexto_chat.get("demo_session")
        )

        def _sync_demo_session_flag() -> None:
            """Refresh the local flag after mutating the demo state."""

            nonlocal demo_session_activa
            demo_session_activa = bool(
                isinstance(contexto_chat, dict) and contexto_chat.get("demo_session")
            )

        tipo_chat_normalized = (tipo_chat or "").strip().lower()
        is_municipal_request = tipo_chat_normalized == "municipio"

        def _update_tipo_flags() -> None:
            nonlocal tipo_chat_normalized, is_municipal_request, tipo_chat
            tipo_chat_normalized = (tipo_chat or "").strip().lower()
            is_municipal_request = tipo_chat_normalized == "municipio"
            if tipo_chat and tipo_chat != tipo_chat_normalized:
                tipo_chat = tipo_chat_normalized

        def _set_tipo_chat(value) -> None:
            nonlocal tipo_chat
            if value is None:
                return
            normalized_value = str(value).strip().lower()
            if not normalized_value:
                return
            tipo_chat = normalized_value
            _update_tipo_flags()

        owner_tipo_chat = (getattr(owner_user, "tipo_chat", None) or "").strip().lower()
        if owner_tipo_chat in {"pyme", "municipio"} and owner_tipo_chat != tipo_chat_normalized:
            current_app.logger.info(
                "[CHAT] Ajustando tipo_chat a '%s' basado en owner_user %s (valor previo: '%s')",
                owner_tipo_chat,
                getattr(owner_user, "id", "N/A"),
                tipo_chat_normalized or "",
            )
            _set_tipo_chat(owner_tipo_chat)
        else:
            _update_tipo_flags()

        tenant_slug_hint = (
            request.args.get("tenant_slug")
            or request.args.get("tenant")
            or ""
        ).strip().lower()
        force_demo_selector_flow = (
            not actor_principal
            and tenant_slug_hint in {"municipio", "pyme"}
            and not demo_session_activa
        )

        if is_municipal_request and not force_demo_selector_flow and isinstance(contexto_chat, dict):
            demo_keys_to_clear = (
                "demo_session",
                "demo_owner_user_id",
                "demo_rubro_id",
                "demo_rubro_clave",
                "demo_tipo_chat",
                "demo_key",
                "demo_prompt_context",
                "demo_display_name",
                "demo_description",
                "demo_welcome_message",
                "demo_resources",
                "demo_faq_preview",
                "demo_quick_actions",
                "demo_capabilities",
                "demo_keywords",
                "demo_intro_sent",
                "demo_message_count",
            )
            cleared_demo_state = False
            for key in demo_keys_to_clear:
                if key in contexto_chat:
                    contexto_chat.pop(key, None)
                    cleared_demo_state = True
            if cleared_demo_state and chat_context_obj:
                flag_modified(chat_context_obj, "context_data")
                _sync_demo_session_flag()

        is_demo_selection_event = False
        demo_options: Optional[List[Dict[str, Optional[str]]]] = None

        token_from_request = obtener_token()
        demo_match_from_token = demo_rubro_for_token(token_from_request)
        demo_payload_from_token: Optional[Dict[str, object]] = None

        if not owner_user and demo_match_from_token:
            demo_payload_from_token = demo_match_from_token.to_internal_dict()

            owner_candidate = None
            if demo_match_from_token.owner_user_id:
                owner_candidate = User.query.get(demo_match_from_token.owner_user_id)

            rubro_candidate = None
            if demo_match_from_token.rubro_id:
                rubro_candidate = Rubro.query.get(demo_match_from_token.rubro_id)

            if not rubro_candidate and owner_candidate:
                rubro_candidate = getattr(owner_candidate, "rubro", None)

            if owner_candidate:
                owner_user = owner_candidate
                owner_del_bot = owner_candidate

            if rubro_candidate:
                rubro_obj_global = rubro_candidate
                rubro_para_log = (
                    getattr(rubro_candidate, "nombre", None)
                    or getattr(rubro_candidate, "clave", None)
                    or rubro_para_log
                )
                rubro_id = rubro_candidate.id
                if getattr(rubro_candidate, "clave", None):
                    rubro_clave = rubro_candidate.clave

            if demo_match_from_token.tipo_chat:
                _set_tipo_chat(demo_match_from_token.tipo_chat)

            if demo_payload_from_token:
                changed = _activate_demo_session(
                    contexto_chat,
                    demo_payload_from_token,
                    owner_user=owner_candidate,
                    rubro_obj=rubro_candidate,
                    reset_counter=False,
                )
                if changed and chat_context_obj:
                    flag_modified(chat_context_obj, "context_data")
            _sync_demo_session_flag()

        owner_user_rubro_id = getattr(owner_user, "rubro_id", None)

        if rubro_id:
            rubro_obj_global = Rubro.query.get(rubro_id)
            if rubro_obj_global:
                rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
                if owner_user and owner_user_rubro_id == rubro_obj_global.id:
                    owner_del_bot = owner_user
                else:
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                    if not owner_del_bot:
                        owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()
        elif rubro_clave:
            rubro_obj_global = Rubro.query.filter(func.lower(Rubro.clave) == func.lower(rubro_clave)).first()
            if rubro_obj_global:
                rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
                if owner_user and owner_user_rubro_id == rubro_obj_global.id:
                    owner_del_bot = owner_user
                else:
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                    if not owner_del_bot:
                        owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()
        elif actor_principal and actor_principal.rubro_id:
            rubro_obj_global = Rubro.query.get(actor_principal.rubro_id)
            if rubro_obj_global:
                rubro_para_log = rubro_obj_global.nombre or rubro_obj_global.clave
            if actor_principal.empresa_id is None:
                owner_del_bot = actor_principal
            else:
                if rubro_obj_global:
                    owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, empresa_id=None).first()
                    if not owner_del_bot:
                        owner_del_bot = User.query.filter_by(rubro_id=rubro_obj_global.id, rol='admin').first()

        if not owner_del_bot and owner_user:
            owner_del_bot = owner_user
            if not rubro_obj_global:
                rubro_obj_global = getattr(owner_user, "rubro", None)
                if rubro_obj_global:
                    rubro_para_log = rubro_para_log or getattr(rubro_obj_global, "nombre", None) or getattr(rubro_obj_global, "clave", None)
                    if not rubro_id:
                        rubro_id = rubro_obj_global.id
                    if not rubro_clave and getattr(rubro_obj_global, "clave", None):
                        rubro_clave = rubro_obj_global.clave

        # Recuperar el owner de una demo previamente seleccionada si no vino en la request
        if (
            (not is_municipal_request or force_demo_selector_flow)
            and not owner_del_bot
            and isinstance(contexto_chat, dict)
        ):
            stored_owner_id = contexto_chat.get("demo_owner_user_id")
            if stored_owner_id:
                potencial_owner = User.query.get(stored_owner_id)
                if potencial_owner:
                    owner_del_bot = potencial_owner
                    rubro_obj_global = Rubro.query.get(contexto_chat.get("demo_rubro_id")) or potencial_owner.rubro
                    if rubro_obj_global:
                        rubro_para_log = rubro_para_log or getattr(rubro_obj_global, "nombre", None) or getattr(rubro_obj_global, "clave", None)
                        if not rubro_id:
                            rubro_id = rubro_obj_global.id
                        if not rubro_clave and getattr(rubro_obj_global, "clave", None):
                            rubro_clave = rubro_obj_global.clave
                    _set_tipo_chat(contexto_chat.get("demo_tipo_chat", tipo_chat))

        if owner_del_bot and getattr(owner_del_bot, "token", None):
            demo_match = demo_rubro_for_token(owner_del_bot.token)
            if demo_match:
                demo_payload = demo_match.to_internal_dict()
                if not rubro_obj_global and demo_match.rubro_id:
                    rubro_obj_global = Rubro.query.get(demo_match.rubro_id) or rubro_obj_global
                if rubro_obj_global and not rubro_para_log:
                    rubro_para_log = getattr(rubro_obj_global, "nombre", None) or getattr(rubro_obj_global, "clave", None)
                rubro_id = getattr(rubro_obj_global, "id", rubro_id)
                if demo_payload.get("rubro_clave") and not rubro_clave:
                    rubro_clave = demo_payload.get("rubro_clave")
                if demo_payload.get("tipo_chat"):
                    _set_tipo_chat(demo_payload.get("tipo_chat"))
                changed = _activate_demo_session(
                    contexto_chat,
                    demo_payload,
                    owner_user=owner_del_bot,
                    rubro_obj=rubro_obj_global,
                    reset_counter=False,
                )
                if changed and chat_context_obj:
                    flag_modified(chat_context_obj, "context_data")
                _sync_demo_session_flag()

        if not is_municipal_request or force_demo_selector_flow:
            demo_key = _extract_demo_key(action_id)
            if not demo_key and isinstance(original_user_payload, dict):
                demo_key = _extract_demo_key(original_user_payload.get("action") or original_user_payload.get("action_id"))

            if not demo_key and isinstance(original_user_payload, str):
                user_text = original_user_payload.strip().lower()
                if user_text:
                    demo_options = _load_demo_rubros()
                    for opcion in demo_options:
                        candidatos = {
                            opcion["key"],
                            opcion["label"].strip().lower(),
                            (opcion.get("rubro_clave") or "").strip().lower(),
                        }
                        if user_text in candidatos:
                            demo_key = opcion["key"]
                            break

            if demo_key:
                demo_options = demo_options or _load_demo_rubros()
                selected_demo = next((opt for opt in demo_options if opt["key"] == demo_key), None)
                if not selected_demo:
                    selector_payload = _build_demo_selector_payload(demo_options)
                    selector_payload["message_body"] = (
                        "No pude reconocer esa demo. Elegí una de las opciones disponibles para continuar."
                    )
                    _emit_socket_payload(selector_payload)
                    return jsonify(selector_payload), 200

                owner_del_bot = User.query.get(selected_demo["owner_user_id"])
                rubro_obj_global = Rubro.query.get(selected_demo["rubro_id"]) if selected_demo.get("rubro_id") else None
                if owner_del_bot and not rubro_obj_global:
                    rubro_obj_global = owner_del_bot.rubro

                if not owner_del_bot or not rubro_obj_global:
                    current_app.logger.error(
                        f"[demo] La demo '{demo_key}' no cuenta con usuario o rubro configurado correctamente."
                    )
                    demo_options = demo_options or _load_demo_rubros()
                    selector_payload = _build_demo_selector_payload(demo_options)
                    selector_payload["message_body"] = (
                        "La demo seleccionada no está disponible en este momento. Elegí otra opción para continuar."
                    )
                    _emit_socket_payload(selector_payload)
                    return jsonify(selector_payload), 200

                rubro_para_log = selected_demo["label"]
                _set_tipo_chat(selected_demo.get("tipo_chat"))
                rubro_id = getattr(rubro_obj_global, "id", rubro_id)
                if getattr(rubro_obj_global, "clave", None):
                    rubro_clave = rubro_obj_global.clave

                changed = _activate_demo_session(
                    contexto_chat,
                    selected_demo,
                    owner_user=owner_del_bot,
                    rubro_obj=rubro_obj_global,
                    reset_counter=True,
                )
                if changed and chat_context_obj:
                    flag_modified(chat_context_obj, "context_data")
                _sync_demo_session_flag()

                pregunta = "__INIT__"
                original_user_payload = "__INIT__"
                action_id = None
                is_demo_selection_event = True

            if not owner_del_bot:
                demo_options = demo_options or _load_demo_rubros()
                if demo_options:
                    contexto_chat["demo_session"] = True
                    contexto_chat.pop("demo_owner_user_id", None)
                    contexto_chat.pop("demo_rubro_id", None)
                    contexto_chat.pop("demo_tipo_chat", None)
                    contexto_chat.pop("demo_key", None)
                    contexto_chat.pop("demo_prompt_context", None)
                    contexto_chat.pop("demo_display_name", None)
                    contexto_chat.pop("demo_description", None)
                    contexto_chat.pop("demo_welcome_message", None)
                    contexto_chat.pop("demo_resources", None)
                    contexto_chat.pop("demo_faq_preview", None)
                    contexto_chat.pop("demo_quick_actions", None)
                    contexto_chat.pop("demo_capabilities", None)
                    contexto_chat.pop("demo_keywords", None)
                    contexto_chat.pop("demo_intro_sent", None)
                    flag_modified(chat_context_obj, "context_data")
                    _sync_demo_session_flag()
                    selector_payload = _build_demo_selector_payload(demo_options)
                    try:
                        commit_with_retry(db.session)
                    except Exception as e_commit:
                        db.session.rollback()
                        current_app.logger.error(
                            f"Error guardando la selección de demo para la sesión {chat_session_id_header}: {e_commit}",
                            exc_info=True,
                        )
                    _emit_socket_payload(selector_payload)
                    return jsonify(selector_payload), 200

        if not owner_del_bot and rubro_obj_global:
            current_app.logger.warning(
                f"Rubro ID {rubro_obj_global.id} ('{rubro_para_log}') encontrado pero sin User owner asociado (empresa_id=None o rol=admin). Se continuará sin owner específico si el rubro es público.")

        if rubro_obj_global:
            nombre_rubro_log = rubro_para_log or getattr(rubro_obj_global, "nombre", None) or getattr(rubro_obj_global, "clave", "N/A")
            owner_id_log = getattr(owner_del_bot, "id", "N/A")
            current_app.logger.info(f"Usando Rubro ID {rubro_obj_global.id} ('{nombre_rubro_log}') perteneciente a User ID {owner_id_log} para la lógica del bot.")
        else:
            current_app.logger.info("No se pudo determinar un rubro/owner específico para la lógica del bot. Se usará lógica genérica si aplica (ej. para rubros públicos por defecto).")

        if isinstance(contexto_chat, dict) and contexto_chat.get("demo_key") and not contexto_chat.get("demo_session"):
            contexto_chat["demo_session"] = True
            _sync_demo_session_flag()
            if chat_context_obj:
                flag_modified(chat_context_obj, "context_data")

        demo_flow_active = bool(
            demo_session_activa
            or (isinstance(contexto_chat, dict) and contexto_chat.get("demo_session"))
            or (isinstance(contexto_chat, dict) and contexto_chat.get("demo_key"))
            or demo_payload_from_token
            or demo_match_from_token
        )

        if (
            not demo_flow_active
            and owner_del_bot
            and getattr(owner_del_bot, "token", None)
            and demo_rubro_for_token(owner_del_bot.token)
        ):
            demo_flow_active = True

        if owner_del_bot and not demo_flow_active and not is_init_request:
            from utils.plan_limits import limite_para_usuario
            limite = limite_para_usuario(owner_del_bot)
            if limite is not None and owner_del_bot.preguntas_usadas >= limite:
                return jsonify({
                    "error": f"El bot ha alcanzado el límite de preguntas de su plan ({limite})."
                }), 403

        demo_limit = current_app.config.get("DEMO_MAX_MESSAGES_PER_SESSION", 0)
        incrementar_demo = (
            demo_flow_active
            and demo_limit
            and demo_limit > 0
            and not is_demo_selection_event
            and not _is_init_payload(original_user_payload)
        )

        if incrementar_demo:
            conteo_actual = int(contexto_chat.get("demo_message_count", 0)) + 1
            contexto_chat["demo_message_count"] = conteo_actual
            flag_modified(chat_context_obj, "context_data")
            if conteo_actual > demo_limit:
                respuesta_limite = _build_demo_limit_response(demo_limit)
                try:
                    commit_with_retry(db.session)
                except Exception as e_commit:
                    db.session.rollback()
                    current_app.logger.error(
                        f"Error al guardar el límite de la demo para la sesión {chat_session_id_header}: {e_commit}",
                        exc_info=True,
                    )
                _emit_socket_payload(respuesta_limite)
                return jsonify(respuesta_limite), 403

        # The logic for file analysis has been moved to the upload endpoint.
        # The chat endpoint is only responsible for passing the attachmentInfo.
        analisis_archivo_resultado = None

        # Leer el X-Chat-Session-Id del header
        chat_session_id_header = request.headers.get("X-Chat-Session-Id")

        if not chat_session_id_header:
            # Fallback: Generar un nuevo ID si no viene en el header.
            # Idealmente, el frontend SIEMPRE debería enviarlo.
            chat_session_id_header = str(uuid.uuid4())
            current_app.logger.warning(f"X-Chat-Session-Id no encontrado en headers. Generando uno nuevo: {chat_session_id_header}")

        current_app.logger.info(f"Usando Chat Session ID (from header or generated): {chat_session_id_header}")

        # Cargar o crear el contexto de la base de datos
        chat_context_obj = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id_header).first()

        if not chat_context_obj:
            current_app.logger.info(f"No se encontró ChatSessionContext. Creando uno nuevo.")
            chat_context_obj = ChatSessionContext(
                chat_session_id=chat_session_id_header,
                user_id=getattr(actor_principal, 'id', None), # Asociar con usuario logueado si existe
                anon_id=anon_id if not actor_principal else None, # Asociar con anon_id si no hay usuario logueado
                context_data={} # Inicializar con datos vacíos
            )
            db.session.add(chat_context_obj)
            # No hacer commit aquí todavía, se hará después de procesar el chat
        else:
            current_app.logger.info(f"ChatSessionContext cargado. User_id: {chat_context_obj.user_id}, Anon_id: {chat_context_obj.anon_id}")


            # Detect if user just logged in with this session
            if actor_principal and chat_context_obj.user_id == actor_principal.id and not chat_context_obj.context_data.get("user_was_present_before", False):
                chat_context_obj.context_data["just_logged_in_flag"] = True
                current_app.logger.info(f"User {actor_principal.id} just logged in with session {chat_session_id_header}. Setting just_logged_in_flag.")

            # This flag should be set to True if an authenticated user is present.
            chat_context_obj.context_data["user_was_present_before"] = bool(actor_principal)

        if chat_context_obj and chat_context_obj.context_data.get('just_logged_in_flag'):
            current_app.logger.info(f"User {actor_principal.id} just logged in. Clearing flag.")
            # Welcome back message or other logic can be triggered here.
            # For now, just clearing the flag.
            chat_context_obj.context_data.pop('just_logged_in_flag', None)
            flag_modified(chat_context_obj, "context_data")


        # El objeto `chat_context_obj.context_data` será el que se pase y modifique
        # en lugar de `flask_request_session` para el contexto específico del chat.

        interpretacion_imagen_resultado = None
        # --- Deduplicar mensajes rápidos idénticos ---
        last_msg = chat_context_obj.context_data.get("last_user_message")
        last_time_str = chat_context_obj.context_data.get("last_user_message_time")
        if last_msg == pregunta and last_time_str:
            try:
                last_dt = datetime.fromisoformat(last_time_str)
                if datetime.utcnow() - last_dt < timedelta(seconds=2):
                    current_app.logger.info("Mensaje duplicado detectado; reenviando última respuesta.")
                    last_resp = chat_context_obj.context_data.get("last_bot_response")
                    if last_resp:
                        normalize_response_payload(last_resp)
                        return jsonify(last_resp), 200
            except Exception:
                pass

        demo_metadata = None
        if isinstance(contexto_chat, dict) and contexto_chat.get("demo_session"):
            demo_metadata = {
                "key": contexto_chat.get("demo_key"),
                "prompt_context": contexto_chat.get("demo_prompt_context") or contexto_chat.get("demo_description"),
                "display_name": contexto_chat.get("demo_display_name"),
                "description": contexto_chat.get("demo_description"),
                "welcome_message": contexto_chat.get("demo_welcome_message"),
                "resources": deepcopy(contexto_chat.get("demo_resources") or []),
                "faq_preview": deepcopy(contexto_chat.get("demo_faq_preview") or []),
                "quick_actions": deepcopy(contexto_chat.get("demo_quick_actions") or []),
                "capabilities": deepcopy(contexto_chat.get("demo_capabilities") or []),
                "keywords": deepcopy(contexto_chat.get("demo_keywords") or []),
            }

        menu_action_payload: Optional[Dict[str, object]] = None
        if isinstance(contexto_chat, dict) and contexto_chat.get("demo_session"):
            menu_command: Optional[str] = None
            if isinstance(action_id, str):
                candidate = action_id.strip()
                if candidate:
                    menu_command = candidate
            if not menu_command and isinstance(original_user_payload, dict):
                raw_action = original_user_payload.get("action") or original_user_payload.get("action_id")
                if isinstance(raw_action, str) and raw_action.strip():
                    menu_command = raw_action.strip()
            if not menu_command and isinstance(pregunta, str):
                stripped_question = pregunta.strip()
                if stripped_question.startswith(f"{DEMO_MENU_PREFIX}:") or stripped_question in {
                    DEMO_MENU_BACK_ACTION,
                    DEMO_MENU_HOME_ACTION,
                }:
                    menu_command = stripped_question

            if menu_command and (
                menu_command.startswith(f"{DEMO_MENU_PREFIX}:")
                or menu_command in {DEMO_MENU_BACK_ACTION, DEMO_MENU_HOME_ACTION}
            ):
                menu_action_payload = _handle_demo_menu_action(contexto_chat, menu_command)

        if menu_action_payload:
            contexto_chat.setdefault("demo_intro_sent", True)
            if chat_context_obj:
                flag_modified(chat_context_obj, "context_data")
            try:
                commit_with_retry(db.session)
            except Exception as e_commit:
                db.session.rollback()
                current_app.logger.error(
                    f"Error al guardar el estado del menú demo para la sesión {chat_session_id_header}: {e_commit}",
                    exc_info=True,
                )
            normalize_response_payload(menu_action_payload)
            _emit_socket_payload(menu_action_payload)
            return jsonify(menu_action_payload), 200

        responder_extra_kwargs = {}
        if location:
            responder_extra_kwargs["es_ubicacion"] = True
            responder_extra_kwargs["ubicacion_usuario"] = location

        if attachment_info:
            normalized_uploaded_info = {
                "id": attachment_info.get("id"),
                "url": attachment_info.get("url"),
                "name": attachment_info.get("name"),
                "mime_type": attachment_info.get("mimeType")
                or attachment_info.get("mime_type"),
                "size": attachment_info.get("size"),
                "thumb_url": attachment_info.get("thumbUrl")
                or attachment_info.get("thumbnailUrl"),
                "thumbnail_url": attachment_info.get("thumbnailUrl")
                or attachment_info.get("thumbUrl"),
                "meta": attachment_info.get("meta"),
                "source": attachment_info.get("source") or "web_upload",
            }
            responder_extra_kwargs["uploaded_file_info"] = {
                key: value
                for key, value in normalized_uploaded_info.items()
                if value is not None
            }

        resultado = responder_chatboc(
            pregunta=pregunta,
            owner_user=owner_del_bot,
            current_user=viewer_obj, # El usuario que está viendo/interactuando
            rubro_obj=rubro_obj_global,
            rubro_nombre_frontend=rubro_clave,
            tipo_chat=tipo_chat,
            contexto_previo=contexto_previo, # Este 'contexto_previo' del request original podría necesitar ser integrado o reemplazado por el de la DB
            anon_id=anon_id, # El anon_id de la cabecera, para lógica de límites de mensajes anónimos, etc.
            chat_session_uuid=chat_session_id_header, # El ID de sesión único, ahora desde el header
            chat_db_context=chat_context_obj, # Pasar el objeto de contexto de DB
            channel="web", # Set channel to web
            attachment_info=attachment_info,
            location=location,
            profile_name=profile_name,
            user_data={
                "name": actor_principal.name,
                "email": actor_principal.email,
                "telefono": actor_principal.telefono
            } if actor_principal else None,
            demo_metadata=demo_metadata,
            **responder_extra_kwargs,
        )

        recursos_demo: List[Dict[str, object]] = []
        faq_preview_data: List[Dict[str, object]] = []
        demo_description: Optional[str] = None
        demo_welcome: Optional[str] = None
        quick_actions_raw: List[Dict[str, object]] = []
        capabilities_raw: List[object] = []
        keywords_raw: List[object] = []
        if isinstance(contexto_chat, dict):
            recursos_demo = contexto_chat.get("demo_resources") or []
            faq_preview_data = contexto_chat.get("demo_faq_preview") or []
            demo_description = contexto_chat.get("demo_description")
            demo_welcome = contexto_chat.get("demo_welcome_message")
            quick_actions_raw = contexto_chat.get("demo_quick_actions") or []
            capabilities_raw = contexto_chat.get("demo_capabilities") or []
            keywords_raw = contexto_chat.get("demo_keywords") or []

        has_intro_content = bool(
            recursos_demo
            or faq_preview_data
            or demo_description
            or demo_welcome
            or quick_actions_raw
            or capabilities_raw
            or keywords_raw
        )

        should_apply_intro = (
            demo_session_activa
            and has_intro_content
            and isinstance(resultado, dict)
            and not contexto_chat.get("demo_intro_sent")
            and (is_demo_selection_event or _is_init_payload(original_user_payload))
        )

        if should_apply_intro:
            resources_text, resource_buttons, resource_attachments = _format_demo_resources(recursos_demo)
            (
                menu_text,
                prompt_examples_text,
                quick_action_buttons,
                menu_registry,
                menu_root_id,
                quick_items_struct,
                grouped_quick_sections,
            ) = _format_demo_quick_actions(quick_actions_raw)
            if menu_registry:
                contexto_chat["demo_menu_registry"] = menu_registry
                contexto_chat["demo_menu_root_id"] = menu_root_id
                contexto_chat["demo_menu_stack"] = [menu_root_id]
            quick_actions_list = quick_actions_raw if isinstance(quick_actions_raw, list) else list(quick_actions_raw or [])
            display_name = contexto_chat.get("demo_display_name") or contexto_chat.get("demo_key") or "esta demo"
            faq_preview_text = _format_demo_faq_preview(faq_preview_data)
            keywords_text = _format_demo_keywords(keywords_raw)
            capabilities_text = _format_demo_capabilities(capabilities_raw)

            base_message = (
                contexto_chat.get("demo_welcome_message")
                or resultado.get("message_body")
                or resultado.get("respuesta")
            )
            description = contexto_chat.get("demo_description")
            original_message = resultado.get("message_body")

            segments: List[str] = []

            def _clean_text(value: object) -> str:
                return str(value).strip() if value is not None else ""

            def _append_section(title: str, body: str) -> None:
                body_text = _clean_text(body)
                if body_text:
                    segments.append(f"━━━━━━━━━━━━\n{title}\n{body_text}")

            base_text = _clean_text(base_message)
            if base_text:
                segments.append(base_text)

            if description:
                description_text = _clean_text(description)
                if description_text and description_text not in segments:
                    segments.append(description_text)

            # Removed the huge text dump. We now rely on interactive elements (buttons, lists).
            # The 'menu_text' was causing the "chorizo inentendible".

            # _append_section("📋 Menú principal", menu_text)  <-- DISABLED

            # We keep the welcome message and description, but avoid appending the full menu as text.
            # Interactive sections are added to 'menu_sections' below.

            if original_message:
                original_text = _clean_text(original_message)
                if original_text and original_text not in segments:
                    segments.append(original_text)

            closing_line = "👇 Seleccioná una opción del menú interactivo para comenzar:"
            if closing_line not in segments:
                segments.append(closing_line)

            message_text = "\n\n".join([seg for seg in segments if seg])
            if message_text:
                resultado["message_body"] = message_text
                resultado["respuesta"] = message_text

            combined_buttons: List[Dict[str, object]] = []
            seen_button_ids: set[str] = set()

            def _append_buttons(buttons: List[Dict[str, object]] | None) -> None:
                if not buttons:
                    return
                for button in buttons:
                    if not isinstance(button, dict):
                        continue
                    candidate = deepcopy(button)
                    candidate_id = str(
                        candidate.get("id")
                        or candidate.get("action_id")
                        or candidate.get("texto")
                        or len(combined_buttons)
                    )
                    if candidate_id in seen_button_ids:
                        continue
                    seen_button_ids.add(candidate_id)
                    combined_buttons.append(candidate)

            _append_buttons(quick_action_buttons)
            existing_options = resultado.get("options_list") or resultado.get("botones") or []
            _append_buttons(existing_options)
            if not combined_buttons and resource_buttons:
                _append_buttons(resource_buttons)

            if combined_buttons:
                resultado["options_list"] = combined_buttons
                resultado["botones"] = combined_buttons
                non_url_buttons = [btn for btn in combined_buttons if btn.get("type") != "url"]
                if len(non_url_buttons) > 3:
                    resultado["message_type"] = "interactive_list"
                else:
                    resultado["message_type"] = "interactive_buttons"
            else:
                resultado.setdefault("message_type", resultado.get("message_type") or "text")

            if resource_attachments:
                existing_adjuntos = resultado.get("adjuntos") or []
                resultado["adjuntos"] = existing_adjuntos + resource_attachments

            menu_sections: List[Dict[str, object]] = []

            quick_items: List[Dict[str, object]] = []
            if quick_items_struct:
                for entry in quick_items_struct:
                    item = {
                        "id": entry.get("id") or entry.get("action") or entry.get("texto"),
                        "texto": entry.get("texto"),
                        "description": entry.get("description"),
                        "action": entry.get("action"),
                        "type": entry.get("type"),
                    }
                    if entry.get("prompt"):
                        item["prompt"] = entry.get("prompt")
                    if entry.get("emoji"):
                        item["emoji"] = entry.get("emoji")
                    if entry.get("category"):
                        item["category"] = entry.get("category")
                    if entry.get("menu_id"):
                        item["menu_id"] = entry.get("menu_id")
                    quick_items.append({k: v for k, v in item.items() if v})
            else:
                for idx, button in enumerate(quick_action_buttons or []):
                    texto_btn = _clean_text(button.get("texto"))
                    if not texto_btn:
                        continue
                    item: Dict[str, object] = {
                        "id": button.get("id") or button.get("action_id") or f"quick_{idx}",
                        "texto": texto_btn,
                        "description": _clean_text(button.get("description")) or None,
                        "action": button.get("action"),
                        "type": button.get("type"),
                    }
                    raw_item = quick_actions_list[idx] if idx < len(quick_actions_list) else None
                    if isinstance(raw_item, dict):
                        prompt_val = raw_item.get("prompt") or raw_item.get("question") or raw_item.get("payload")
                        prompt_text = _clean_text(prompt_val)
                        if prompt_text:
                            item["prompt"] = prompt_text
                        emoji_val = raw_item.get("emoji") or raw_item.get("icon")
                        if emoji_val:
                            item["emoji"] = emoji_val
                    quick_items.append({k: v for k, v in item.items() if v})
            if quick_items:
                section_payload: Dict[str, object] = {
                    "title": "Menú principal",
                    "type": "quick_actions",
                    "items": quick_items,
                }
                if grouped_quick_sections:
                    section_payload["groups"] = grouped_quick_sections
                menu_sections.append(section_payload)

            prompt_items: List[str] = []
            for raw in quick_actions_list:
                if not isinstance(raw, dict):
                    continue
                prompt_val = raw.get("prompt") or raw.get("question") or raw.get("payload")
                prompt_text = _clean_text(prompt_val)
                if prompt_text:
                    prompt_items.append(prompt_text)
            if prompt_items:
                menu_sections.append({
                    "title": "Probá decir",
                    "type": "prompt_examples",
                    "items": prompt_items,
                })

            keyword_items: List[str] = []
            seen_keywords: set[str] = set()
            for raw in keywords_raw or []:
                text = _clean_text(raw)
                if not text:
                    continue
                key = text.lower()
                if key in seen_keywords:
                    continue
                seen_keywords.add(key)
                keyword_items.append(text)
            if keyword_items:
                menu_sections.append({
                    "title": "Palabras clave sugeridas",
                    "type": "keywords",
                    "items": keyword_items,
                })

            capability_items: List[str] = []
            seen_capabilities: set[str] = set()
            for raw in capabilities_raw or []:
                text = _clean_text(raw)
                if not text:
                    continue
                key = text.lower()
                if key in seen_capabilities:
                    continue
                seen_capabilities.add(key)
                capability_items.append(text)
            if capability_items:
                menu_sections.append({
                    "title": "Herramientas disponibles",
                    "type": "capabilities",
                    "items": capability_items,
                })

            faq_items: List[Dict[str, str]] = []
            for faq in faq_preview_data or []:
                if not isinstance(faq, dict):
                    continue
                pregunta_text = _clean_text(faq.get("pregunta"))
                respuesta_text = _clean_text(faq.get("respuesta"))
                if not pregunta_text and not respuesta_text:
                    continue
                faq_items.append({
                    "question": pregunta_text,
                    "answer": respuesta_text,
                })
            if faq_items:
                menu_sections.append({
                    "title": "Preguntas frecuentes destacadas",
                    "type": "faqs",
                    "items": faq_items,
                })

            resource_items: List[Dict[str, object]] = []
            for idx, attachment in enumerate(resource_attachments or []):
                if not isinstance(attachment, dict):
                    continue
                item = {
                    "id": attachment.get("url") or f"resource_{idx}",
                    "title": attachment.get("titulo"),
                    "description": attachment.get("descripcion"),
                    "cta": attachment.get("cta"),
                    "price": attachment.get("precio"),
                    "badge": attachment.get("badge"),
                    "availability": attachment.get("disponibilidad"),
                    "url": attachment.get("url"),
                    "thumbnail": attachment.get("thumbnail"),
                    "type": attachment.get("tipo"),
                }
                resource_items.append({k: v for k, v in item.items() if v})
            if resource_items:
                menu_sections.append({
                    "title": f"Material destacado de {display_name}",
                    "type": "resources",
                    "items": resource_items,
                })

            if menu_sections:
                resultado["menu_sections"] = menu_sections

            if quick_items:
                quick_sections_payload = _interactive_sections_from_quick_items(quick_items)
                if quick_sections_payload:
                    resultado["interactive_list_sections"] = quick_sections_payload
                    if resultado.get("message_type") == "interactive_list":
                        resultado.setdefault("interactive_list_button_text", "Ver menú")
                        if len(quick_sections_payload) == 1:
                            resultado.setdefault(
                                "interactive_list_section_title",
                                quick_sections_payload[0].get("title") or "Menú principal",
                            )
                        else:
                            resultado.setdefault("interactive_list_section_title", "Menú principal")

            contexto_chat["demo_intro_sent"] = True
            flag_modified(chat_context_obj, "context_data")

        # Después de que responder_chatboc y sus sub-funciones hayan modificado chat_context_obj.context_data,
        # lo persistimos.
        
        # Marcar explícitamente context_data como modificado para SQLAlchemy
        if chat_context_obj:
            # Importar la función de serialización
            from services.municipio_responder import serializar_enum, CONTEXTO_MUNICIPIO # CONTEXTO_MUNICIPIO for logging clarity

            # Serializar el context_data COMPLETO antes de marcarlo como modificado y hacer commit
            if chat_context_obj.context_data:
                # Log an example of what's in 'estado_conversacion' before and after, if it exists
                # This is for debugging the specific issue observed.
                raw_municipio_context = chat_context_obj.context_data.get(CONTEXTO_MUNICIPIO, {})
                state_before_global_serialization = raw_municipio_context.get("estado_conversacion", "N/A_in_sub_context")

                # Also check top-level estado_conversacion if it exists
                top_level_state_before = chat_context_obj.context_data.get("estado_conversacion", "N/A_top_level")

                chat_context_obj.context_data = serializar_enum(chat_context_obj.context_data)

                # Log after serialization
                serialized_municipio_context = chat_context_obj.context_data.get(CONTEXTO_MUNICIPIO, {})
                state_after_global_serialization = serialized_municipio_context.get("estado_conversacion", "N/A_in_sub_context_after")
                top_level_state_after = chat_context_obj.context_data.get("estado_conversacion", "N/A_top_level_after")

                current_app.logger.info(f"ChatSessionContext Serialization: TopLevelState before='{top_level_state_before}', after='{top_level_state_after}'. SubContextState before='{state_before_global_serialization}', after='{state_after_global_serialization}'.")

            flag_modified(chat_context_obj, "context_data")
            current_app.logger.info(f"ChatSessionContext.context_data (post-serialization) marcado como modificado para {chat_session_id_header}.")

        try:
            commit_with_retry(db.session) # Commit principal para ChatSessionContext y User.preguntas_usadas
            current_app.logger.info(f"ChatSessionContext para {chat_session_id_header} guardado/actualizado en DB (Commit Principal).")
        except Exception as e_commit:
            db.session.rollback()
            current_app.logger.error(f"Error en Commit Principal (ChatSessionContext) para {chat_session_id_header}: {e_commit}", exc_info=True)
            # La respuesta al usuario ya se formó, pero el contexto no se guardó.

        es_publico = es_rubro_publico(rubro_obj_global)
        nombre_rubro_log = getattr(rubro_obj_global, "clave", "N/A") if rubro_obj_global else "N/A"

        current_app.logger.info(
            f"[RUBROS] Rubro efectivo: '{nombre_rubro_log}' (ID: {getattr(rubro_obj_global, 'id', 'N/A')}), esPublico={es_publico}"
        )

        if owner_del_bot and not demo_flow_active:
            owner_del_bot.preguntas_usadas += 1

        if isinstance(resultado, dict):
            message_body = resultado.get("message_body")
            respuesta = resultado.get("respuesta")
            if message_body and not respuesta:
                resultado["respuesta"] = message_body
            elif respuesta and not message_body:
                resultado["message_body"] = respuesta

            resultado["es_publico"] = es_publico
            if owner_del_bot:
                from utils.plan_limits import limite_para_usuario
                resultado["preguntas_usadas"] = owner_del_bot.preguntas_usadas
                resultado["limite_preguntas"] = limite_para_usuario(owner_del_bot)
            if interpretacion_imagen_resultado and not interpretacion_imagen_resultado.get("error"):
                resultado["interpretacion_adjunto"] = interpretacion_imagen_resultado

        # ... (previous commit for ChatSessionContext) ...

        # The 'resultado' dictionary from responder_chatboc is now structured
        # exactly as the LLM specified, which is what the frontend expects.

        if isinstance(resultado, dict):
            normalize_response_payload(resultado)

        # --- Audio Synthesis Step ---
        # If the response indicates that audio should be generated, do it now.
        if isinstance(resultado, dict) and resultado.get("generar_audio"):
            from services.google_text_to_speech import TextToSpeechService
            tts_service = TextToSpeechService()
            # Always synthesize from the normalized message_body.
            text_to_synthesize = resultado.get("message_body")
            if text_to_synthesize:
                try:
                    audio_url = tts_service.synthesize_speech(text_to_synthesize)
                    if audio_url:
                        resultado["audio_url"] = audio_url
                        current_app.logger.info(f"Audio generado y añadido a la respuesta: {audio_url}")
                except Exception as e:
                    # Log the error, but don't crash the main response flow
                    current_app.logger.error(f"Error durante la síntesis de voz: {e}", exc_info=True)

        # We just need to pass it through after adding any necessary metadata.

        if isinstance(resultado, tuple):
            # Handle error cases where responder_chatboc returns a tuple
            error_message, status_code = resultado
            return jsonify(error_message), status_code

        if not isinstance(resultado, dict):
            # Fallback for unexpected response types
            current_app.logger.error(f"Unexpected response type from responder_chatboc: {type(resultado)}")
            resultado = {"message_body": "Ocurrió un error inesperado en el servidor."}


        # Add metadata to the response
        message_body = resultado.get("message_body") if isinstance(resultado, dict) else None
        respuesta = resultado.get("respuesta") if isinstance(resultado, dict) else None
        if message_body and not respuesta:
            resultado["respuesta"] = message_body
        elif respuesta and not message_body:
            resultado["message_body"] = respuesta

        resultado["es_publico"] = es_publico
        if owner_del_bot:
            from utils.plan_limits import limite_para_usuario
            resultado["preguntas_usadas"] = owner_del_bot.preguntas_usadas
            resultado["limite_preguntas"] = limite_para_usuario(owner_del_bot)

        if interpretacion_imagen_resultado and not interpretacion_imagen_resultado.get("error"):
            resultado["interpretacion_adjunto"] = interpretacion_imagen_resultado

        if isinstance(resultado, dict):
            audio_url = resultado.get("audio_url")
            if audio_url and channel == "web" and "audio" not in resultado:
                resultado["audio"] = {"link": audio_url}

        normalize_response_payload(resultado)

        # Si el usuario es anónimo y la acción requiere datos personales, pedirlos
        if is_anonymous and resultado and resultado.get("accion_backend") in ["crear_reclamo", "iniciar_reclamo"] and not (resultado.get("datos_estructura", {}).get("nombre_usuario_detectado") and resultado.get("datos_estructura", {}).get("telefono_detectado") and resultado.get("datos_estructura", {}).get("email_detectado")):
            resultado['pedir_info'] = ["nombre", "telefono", "email"]

        # Guardar datos del último mensaje para evitar duplicados
        if chat_context_obj:
            chat_context_obj.context_data["last_user_message"] = pregunta
            chat_context_obj.context_data["last_user_message_time"] = datetime.utcnow().isoformat()
            chat_context_obj.context_data["last_bot_response"] = resultado
            flag_modified(chat_context_obj, "context_data")

        # This commit is for User.preguntas_usadas and ChatSessionContext primarily
        try:
            commit_with_retry(db.session)
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error during final commit: {e}", exc_info=True)
            return jsonify({"error": "Error interno del servidor al guardar la sesión."}), 500

        # Emit the result via Socket.IO if the channel is web
        if channel == "web" and chat_session_id_header:
            normalize_response_payload(resultado)

            socketio.emit('message', resultado, room=chat_session_id_header)
            current_app.logger.debug(
                "Emitting socket message",
                extra={"room": chat_session_id_header, "payload": resultado},
            )
            current_app.logger.info(
                f"Emitted socket event 'message' to room {chat_session_id_header}"
            )

        current_app.logger.debug(
            "Returning HTTP response", extra={"payload": resultado}
        )
        return jsonify(resultado), 200

    except Exception as e:
        db.session.rollback()
        error_details = {
            "pregunta": pregunta if 'pregunta' in locals() else 'N/A',
            "tipo_chat": tipo_chat if 'tipo_chat' in locals() else 'N/A',
            "rubro_id": rubro_id if 'rubro_id' in locals() else 'N/A',
            "rubro_clave": rubro_clave if 'rubro_clave' in locals() else 'N/A',
            "actor_principal_id": actor_principal.id if 'actor_principal' in locals() and actor_principal else 'N/A',
            "owner_del_bot_id": owner_del_bot.id if 'owner_del_bot' in locals() and owner_del_bot else 'N/A',
            "viewer_obj_id": viewer_obj.id if 'viewer_obj' in locals() and viewer_obj else 'N/A',
            "anon_id": anon_id if 'anon_id' in locals() else 'N/A',
            "archivo_adjunto_id": archivo_adjunto_id if 'archivo_adjunto_id' in locals() else 'N/A',
            "uploaded_file_info": uploaded_file_info if 'uploaded_file_info' in locals() else 'N/A',
            "session_chat_id": session_chat_id if 'session_chat_id' in locals() else 'N/A'
        }
        current_app.logger.error(
            f"❌ Error crítico en _procesar_chat. Details: {error_details}. Exception: {e}",
            exc_info=True
        )
        return jsonify({"error": "Error interno del servidor."}), 500

@chat_bp.route("/ask", methods=["POST", "OPTIONS"])
@anon_o_token_requerido
def ask(current_user=None, anon_id=None, owner_user=None):
    user = owner_user or current_user
    response = _procesar_chat(current_user=current_user, owner_user=user, anon_id=anon_id)
    return _log_widget_request(response, user)

@chat_bp.route("/ask/pyme", methods=["POST", "OPTIONS"])
@anon_o_token_requerido
def ask_pyme(current_user=None, anon_id=None, owner_user=None):
    user = owner_user or current_user
    response = _procesar_chat("pyme", current_user=current_user, owner_user=user, anon_id=anon_id)
    return _log_widget_request(response, user)

@chat_bp.route("/ask/municipio", methods=["POST", "OPTIONS"])
@anon_o_token_requerido
def ask_municipio(current_user=None, anon_id=None, owner_user=None):
    user = owner_user or current_user
    response = _procesar_chat("municipio", current_user=current_user, owner_user=user, anon_id=anon_id)
    return _log_widget_request(response, user)


@chat_bp.route("/profile-name", methods=["POST", "OPTIONS"])
def set_profile_name():
    """Store the visitor's profile name in cookies and session context."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    profile_name = data.get("nombre_usuario") or data.get("profile_name")
    if not profile_name:
        return jsonify({"error": "'nombre_usuario' requerido"}), 400

    resp = jsonify({"profile_name": profile_name})
    resp.set_cookie(
        "nombre_usuario",
        profile_name,
        max_age=60 * 60 * 24 * 30,
        samesite=current_app.config.get("SESSION_COOKIE_SAMESITE", "None"),
        secure=current_app.config.get("SESSION_COOKIE_SECURE", True),
    )

    chat_session_id = request.headers.get("X-Chat-Session-Id")
    if chat_session_id:
        chat_context_obj = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
        if not chat_context_obj:
            chat_context_obj = ChatSessionContext(chat_session_id=chat_session_id, context_data={})
            db.session.add(chat_context_obj)
        if chat_context_obj.context_data is None:
            chat_context_obj.context_data = {}
        chat_context_obj.context_data["profile_name"] = profile_name
        flag_modified(chat_context_obj, "context_data")
        commit_with_retry(db.session)

    return resp, 200

@chat_bp.route("/widget/attention", methods=["GET"])
def widget_attention():
    opciones = current_app.config.get("ATTENTION_BUBBLE_CHOICES")
    default_choices = (
        "¡Hola! ¿Necesitas ayuda?",
        "¿Te ayudo a encontrar algo?",
        "¿Querés que te guíe?",
    )

    if opciones:
        mensaje = random.choice(opciones)
    else:
        texto_unico = current_app.config.get("ATTENTION_BUBBLE_TEXT")
        if texto_unico:
            mensaje = texto_unico
        else:
            mensaje = random.choice(default_choices)
    return jsonify({"mensaje": mensaje})


@chat_bp.route("/widget/config", methods=["GET"])
def widget_config():
    token = obtener_token()
    if not token:
        return jsonify({"error": "Token requerido"}), 400

    user = user_from_token(token)
    if not user:
        user = User.query.filter_by(token=token).first()
    if not user:
        return jsonify({"error": "Token inválido"}), 404

    config = {
        "nombre_empresa": user.nombre_empresa or user.name or "",
        "logo_url": user.logo_url or "",
        "color_primario": user.color_primario or "#000000",
        "color_secundario": user.color_secundario or "#FFFFFF",
        "badge_tipo": user.badge_tipo or "",
        "widget_icon_url": user.widget_icon_url or "",
        "widget_animation": user.widget_animation or "",
    }

    return jsonify(config)

@chat_bp.route("/live-chat/schedule", methods=["GET"])
@chat_bp.route("/api/live-chat/schedule", methods=["GET"])
def live_chat_schedule():
    return jsonify(build_live_chat_status())

@chat_bp.route("/config/google-maps-key", methods=["GET"])
def google_maps_key():
    return jsonify(get_map_config())
