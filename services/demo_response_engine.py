"""Offline demo responses to avoid calling LLMs for public demos."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_DEMO_ACTION_PREFIX = "demo_action"
_ICON_MAP = {
    "pdf": "📄",
    "document": "📄",
    "image": "🖼️",
    "video": "🎬",
    "spreadsheet": "📊",
    "pricing": "💰",
    "link": "🔗",
}
_WORD_SEP_RE = re.compile(r"[^a-z0-9]+")
_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "demo_scripts"


class _SafeFormatDict(dict):
    """Return placeholders unchanged when the key is missing."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - defensive
        return "{" + key + "}"


def _normalize_key(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]+", "", text)
    return text or None


def _normalize_text(value: Any) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = _WORD_SEP_RE.sub(" ", text)
    return " ".join(text.split())


def _extract_action_target(action_id: Optional[str], script_key: Optional[str]) -> Optional[str]:
    if not action_id:
        return None
    candidate = str(action_id).strip().lower()
    if not candidate:
        return None
    if candidate.startswith(f"{_DEMO_ACTION_PREFIX}:"):
        parts = candidate.split(":")
        if len(parts) >= 3:
            return _normalize_key(parts[-1])
        if len(parts) == 2:
            return _normalize_key(parts[1])
    if script_key and candidate.startswith(f"{script_key}:"):
        return _normalize_key(candidate.split(":", 1)[1])
    return _normalize_key(candidate)


@lru_cache(maxsize=32)
def _load_script_data(script_key: str) -> Optional[Dict[str, Any]]:
    path = _DATA_DIR / f"{script_key}.json"
    if not path.is_file():
        logger.debug("[demo_offline] Script '%s' not found at %s", script_key, path)
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:  # pragma: no cover - should not happen
        logger.error("[demo_offline] Invalid JSON in %s: %s", path, exc)
        return None


def _iter_script_entries(script: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    entries = script.get("responses")
    if isinstance(entries, list):
        for raw in entries:
            if isinstance(raw, dict):
                yield raw


def _entry_matches(
    entry: Dict[str, Any],
    normalized_text: str,
    action_target: Optional[str],
) -> bool:
    entry_id = _normalize_key(entry.get("id"))
    if action_target and entry_id and action_target == entry_id:
        return True

    extra_actions = entry.get("actions") or []
    normalized_actions = {
        _normalize_key(val) for val in extra_actions if _normalize_key(val)
    }
    if action_target and action_target in normalized_actions:
        return True

    if entry.get("match_always"):
        return True

    keywords_any = entry.get("keywords_any") or entry.get("keywords") or []
    keywords_all = entry.get("keywords_all") or []

    normalized_any = [_normalize_text(val) for val in keywords_any if val]
    normalized_all = [_normalize_text(val) for val in keywords_all if val]

    matched = True
    if normalized_any:
        matched = any(term and term in normalized_text for term in normalized_any)
    if matched and normalized_all:
        matched = all(term and term in normalized_text for term in normalized_all)

    if matched and (normalized_any or normalized_all):
        return True

    return False


def _format_text(template: str, context: Dict[str, Any]) -> str:
    if not template:
        return ""
    return template.format_map(_SafeFormatDict(context))


def _resource_matches(resource: Dict[str, Any], selector: Any) -> bool:
    if not selector:
        return False
    resource_type = str(resource.get("type") or resource.get("tipo") or "").lower()
    title = str(
        resource.get("title")
        or resource.get("nombre")
        or resource.get("label")
        or ""
    ).lower()
    description = str(resource.get("description") or resource.get("descripcion") or "").lower()
    url = str(resource.get("url") or resource.get("href") or "").lower()

    if isinstance(selector, dict):
        type_filter = selector.get("type")
        if type_filter and resource_type != str(type_filter).lower():
            return False
        title_contains = selector.get("title_contains")
        if title_contains and str(title_contains).lower() not in title:
            return False
        any_contains = selector.get("contains")
        if any_contains:
            needle = str(any_contains).lower()
            if needle not in title and needle not in description and needle not in url:
                return False
        return True

    selector_text = str(selector).strip()
    if not selector_text:
        return False
    selector_lower = selector_text.lower()
    if selector_lower == "all":
        return True
    if selector_lower.startswith("type:"):
        return resource_type == selector_lower.split(":", 1)[1]
    if selector_lower.startswith("title:"):
        needle = selector_lower.split(":", 1)[1]
        return needle in title
    if selector_lower.startswith("url:"):
        needle = selector_lower.split(":", 1)[1]
        return needle in url
    return selector_lower in title or selector_lower in description or selector_lower in url


def _filter_resources(
    resources: Sequence[Dict[str, Any]],
    selectors: Sequence[Any],
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    if not resources or not selectors:
        return []
    selected: List[Dict[str, Any]] = []
    seen_keys: set[Tuple[Optional[str], Optional[str]]] = set()
    for selector in selectors:
        for resource in resources:
            key = (resource.get("url"), resource.get("title") or resource.get("nombre"))
            if key in seen_keys:
                continue
            if _resource_matches(resource, selector):
                selected.append(resource)
                seen_keys.add(key)
                if limit and len(selected) >= limit:
                    return selected
    return selected


def _render_resources(resources: Sequence[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not resources:
        return "", [], []

    lines: List[str] = []
    buttons: List[Dict[str, Any]] = []
    attachments: List[Dict[str, Any]] = []

    for idx, resource in enumerate(resources):
        resource_type = str(resource.get("type") or resource.get("tipo") or "document").lower() or "document"
        icon = _ICON_MAP.get(resource_type, "📎")
        title = str(
            resource.get("cta_text")
            or resource.get("title")
            or resource.get("nombre")
            or resource.get("label")
            or f"Recurso {idx + 1}"
        ).strip()
        description = str(resource.get("description") or resource.get("descripcion") or "").strip()
        url = resource.get("url") or resource.get("href")
        thumbnail = resource.get("thumbnail") or resource.get("image")
        highlight = str(resource.get("highlight") or resource.get("badge") or "").strip()
        price = str(resource.get("price") or resource.get("precio") or "").strip()
        availability = str(resource.get("availability") or resource.get("service_level") or "").strip()

        detail_badges: List[str] = [value for value in (highlight, price) if value]
        header_line = f"{icon} {title}"
        if detail_badges:
            header_line += " · " + " · ".join(detail_badges)
        lines.append(header_line)

        for extra in (description, availability):
            if extra:
                lines.append(f"   {extra}")
        lines.append("")

        if url:
            buttons.append(
                {
                    "id": f"demo_resource_{idx}",
                    "texto": f"{icon} {title}",
                    "type": "url",
                    "url": url,
                    "action": url,
                    "action_id": url,
                }
            )
        attachment: Dict[str, Any] = {
            "type": resource_type,
            "title": title,
        }
        if url:
            attachment["url"] = url
        if thumbnail:
            attachment["thumbnail"] = thumbnail
        attachments.append(attachment)

    return "\n".join(line for line in lines if line), buttons, attachments


def _render_faq_preview(faq_preview: Sequence[Dict[str, Any]], limit: Optional[int]) -> str:
    if not faq_preview:
        return ""
    lines: List[str] = []
    max_items = limit if isinstance(limit, int) and limit > 0 else len(faq_preview)
    for idx, faq in enumerate(faq_preview):
        if idx >= max_items:
            break
        question = str(faq.get("pregunta") or faq.get("question") or "").strip()
        answer = str(faq.get("respuesta") or faq.get("answer") or "").strip()
        if not question:
            continue
        line = f"• ❓ {question}"
        if answer:
            line += f" → {answer}"
        lines.append(line)
    return "\n".join(lines)


def _build_buttons(button_specs: Sequence[Dict[str, Any]], script_key: str) -> List[Dict[str, Any]]:
    buttons: List[Dict[str, Any]] = []
    for spec in button_specs or []:
        if not isinstance(spec, dict):
            continue
        label = spec.get("label") or spec.get("texto")
        if not label:
            continue
        label = str(label)
        url = spec.get("url") or spec.get("href")
        if url:
            buttons.append(
                {
                    "texto": label,
                    "type": "url",
                    "url": url,
                    "id": spec.get("id") or url,
                    "action": url,
                    "action_id": url,
                }
            )
            continue
        target = spec.get("target") or spec.get("id")
        normalized_target = _normalize_key(target)
        if not normalized_target:
            continue
        action_id = f"{_DEMO_ACTION_PREFIX}:{script_key}:{normalized_target}"
        button_payload = {
            "texto": label,
            "action": action_id,
            "action_id": action_id,
            "id": action_id,
        }
        description = spec.get("description") or spec.get("descripcion")
        if description:
            button_payload["descripcion"] = description
        buttons.append(button_payload)
    return buttons


def _compose_response(
    entry: Dict[str, Any],
    script_key: str,
    demo_metadata: Dict[str, Any],
    normalized_text: str,
) -> Dict[str, Any]:
    context = {
        "demo_name": demo_metadata.get("display_name")
        or demo_metadata.get("label")
        or demo_metadata.get("nombre")
        or demo_metadata.get("key")
        or "Chatboc",
        "demo_description": demo_metadata.get("description")
        or demo_metadata.get("descripcion"),
        "demo_key": script_key,
        "user_query": normalized_text,
    }

    message_text = _format_text(entry.get("response", ""), context).strip()

    resource_selectors = entry.get("resource_tags") or entry.get("resources") or []
    max_resources = entry.get("max_resources")
    resources = _filter_resources(demo_metadata.get("resources") or [], resource_selectors, max_resources)
    resource_text, resource_buttons, attachments = _render_resources(resources)
    if resource_text:
        message_text = f"{message_text}\n\n{resource_text}" if message_text else resource_text

    include_faq = entry.get("include_faq_preview")
    if include_faq:
        faq_text = _render_faq_preview(demo_metadata.get("faq_preview") or [], include_faq if isinstance(include_faq, int) else None)
        if faq_text:
            message_text = f"{message_text}\n\n{faq_text}" if message_text else faq_text

    manual_buttons = entry.get("buttons") if isinstance(entry.get("buttons"), list) else []
    option_buttons = _build_buttons(manual_buttons, script_key) + resource_buttons

    message_type = entry.get("message_type")
    if not message_type:
        if len(option_buttons) > 3:
            message_type = "interactive_list"
        elif option_buttons:
            message_type = "interactive_buttons"
        else:
            message_type = "text"

    response: Dict[str, Any] = {
        "message_body": message_text or "Esta demo no tiene contenido para mostrar aún.",
        "options_list": option_buttons,
        "botones": option_buttons,
        "message_type": message_type,
        "fuente": "demo_offline",
        "skip_audio_generation": True,
    }
    if attachments:
        response["adjuntos"] = attachments
    if entry.get("id"):
        response["demo_response_id"] = entry.get("id")
    return response


def maybe_handle_demo_interaction(
    *,
    pregunta: Any,
    tipo_chat: Optional[str],
    demo_metadata: Optional[Dict[str, Any]],
    owner_user: Any = None,
    rubro_obj: Any = None,
    channel: str = "web",
    chat_db_context: Any = None,
    action_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return an offline response for demo sessions, or ``None`` to fallback."""

    if not demo_metadata:
        return None

    key_candidates: List[str] = []
    for raw in (
        demo_metadata.get("key"),
        demo_metadata.get("rubro_clave"),
        getattr(rubro_obj, "clave", None),
        getattr(owner_user, "rubro", None) and getattr(getattr(owner_user, "rubro", None), "clave", None),
        tipo_chat,
    ):
        normalized = _normalize_key(raw)
        if normalized and normalized not in key_candidates:
            key_candidates.append(normalized)

    script: Optional[Dict[str, Any]] = None
    script_key: Optional[str] = None
    for candidate in key_candidates:
        script = _load_script_data(candidate)
        if script:
            script_key = candidate
            break

    if not script or not script_key:
        return None

    user_text: str = ""
    detected_action: Optional[str] = action_id

    if isinstance(pregunta, dict):
        detected_action = detected_action or pregunta.get("action") or pregunta.get("action_id")
        user_text = str(pregunta.get("pregunta") or pregunta.get("text") or "")
    elif isinstance(pregunta, str):
        user_text = pregunta
    else:
        user_text = str(pregunta or "")

    normalized_text = _normalize_text(user_text)
    if not normalized_text and isinstance(pregunta, str):
        normalized_text = _normalize_text(pregunta)

    action_target = _extract_action_target(detected_action, script_key)

    matched_entry: Optional[Dict[str, Any]] = None
    if action_target:
        for entry in _iter_script_entries(script):
            if _entry_matches(entry, normalized_text, action_target):
                matched_entry = entry
                break

    if matched_entry is None:
        for entry in _iter_script_entries(script):
            if _entry_matches(entry, normalized_text, action_target):
                matched_entry = entry
                break

    if matched_entry is None:
        fallback = script.get("fallback") if isinstance(script.get("fallback"), dict) else None
        if not fallback:
            return None
        matched_entry = fallback

    response = _compose_response(matched_entry, script_key, demo_metadata, normalized_text)
    logger.debug(
        "[demo_offline] Resolved demo response for key=%s entry=%s action=%s text='%s'",
        script_key,
        matched_entry.get("id"),
        action_target,
        normalized_text,
    )
    return response
