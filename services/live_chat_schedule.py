"""Utilities for live chat scheduling and urgency detection."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Iterable, Mapping, Optional, Set, Tuple

from flask import current_app, has_app_context
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"
DEFAULT_START = time(9, 0)
DEFAULT_END = time(13, 0)
DEFAULT_DAYS = {0, 1, 2, 3, 4}  # Monday-Friday

DAY_ALIASES = {
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "mon": 0,
    "monday": 0,
    "lunes": 0,
    "tue": 1,
    "tuesday": 1,
    "martes": 1,
    "wed": 2,
    "wednesday": 2,
    "miercoles": 2,
    "miércoles": 2,
    "thu": 3,
    "thursday": 3,
    "jueves": 3,
    "fri": 4,
    "friday": 4,
    "viernes": 4,
    "sat": 5,
    "saturday": 5,
    "sabado": 5,
    "sábado": 5,
    "sun": 6,
    "sunday": 6,
    "domingo": 6,
}

DAY_NAMES = [
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
]

DEFAULT_URGENT_KEYWORDS = {
    "urgente",
    "urgencia",
    "urgencias",
    "emergencia",
    "emergencias",
    "siniestro",
    "siniestros",
    "accidente",
    "accidentes",
    "choque",
    "choques",
    "incendio",
    "incendios",
    "explosion",
    "explosiones",
    "auxilio",
    "socorro",
    "ambulancia",
    "herido",
    "heridos",
}

DEFAULT_URGENT_PHRASES = {
    "muy urgente",
    "es urgente",
    "es una urgencia",
    "lo antes posible",
    "lo mas rapido posible",
    "lo más rápido posible",
    "fuga de gas",
    "perdida de gas",
    "pérdida de gas",
    "necesito ayuda urgente",
    "ayuda urgente",
}


@dataclass(frozen=True)
class LiveChatSchedule:
    enabled: bool
    days: Set[int]
    start_time: time
    end_time: time
    timezone: ZoneInfo


def _get_config_value(key: str, default):
    if has_app_context():
        return current_app.config.get(key, default)
    return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "si", "sí", "enabled", "on"}:
        return True
    if normalized in {"0", "false", "no", "disabled", "off"}:
        return False
    return default


def _first_config_value(config: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in config and config.get(key) is not None:
            return config.get(key)
    return default


def build_live_chat_transport(
    status: Optional[Mapping[str, Any]] = None,
    *,
    config_override: Optional[Mapping[str, Any]] = None,
) -> dict:
    config = config_override if isinstance(config_override, Mapping) else {}
    enabled_raw = _first_config_value(
        config,
        "socket_enabled",
        "live_chat_socket_enabled",
        default=None,
    )
    if enabled_raw is None:
        enabled_raw = _get_config_value("PUBLIC_SOCKET_IO_ENABLED", None)
    if enabled_raw is None:
        enabled_raw = _get_config_value("LIVE_CHAT_SOCKET_ENABLED", None)

    socket_enabled = _coerce_bool(enabled_raw, default=False)
    socket_url = _first_config_value(config, "socket_url", "socket_io_url", default=None)
    if socket_enabled and not socket_url:
        socket_url = _get_config_value("PUBLIC_SOCKET_IO_URL", None) or _get_config_value("LIVE_CHAT_SOCKET_URL", None) or "/api/socket.io"

    fallback_mode = str(
        _first_config_value(
            config,
            "live_chat_fallback_mode",
            "fallback_mode",
            default="socket_io_enabled" if socket_enabled else "http_chat",
        )
        or ""
    ).strip() or ("socket_io_enabled" if socket_enabled else "http_chat")
    fallback_disabled = fallback_mode.lower() in {"disabled", "none", "polling_disabled"}
    polling_interval_raw = _first_config_value(
        config,
        "live_chat_polling_interval_ms",
        "polling_interval_ms",
        default=_get_config_value("LIVE_CHAT_POLLING_INTERVAL_MS", None),
    )
    try:
        polling_interval_ms = int(polling_interval_raw) if polling_interval_raw is not None else (5000 if socket_enabled else 12000)
    except (TypeError, ValueError):
        polling_interval_ms = 5000 if socket_enabled else 12000
    polling_interval_ms = max(1000, min(polling_interval_ms, 60000))

    status_dict = status if isinstance(status, Mapping) else {}
    return {
        "contract_version": "live_chat.transport.v1",
        "socket_enabled": socket_enabled,
        "socket_url": socket_url if socket_enabled else None,
        "socket_path": "/api/socket.io" if socket_enabled else None,
        "socket_room": status_dict.get("socket_room"),
        "transports": ["websocket", "polling"] if socket_enabled else [],
        "fallback_mode": fallback_mode,
        "http_fallback_enabled": not fallback_disabled,
        "polling_enabled": not fallback_disabled,
        "polling_interval_ms": polling_interval_ms,
    }


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFD", value.lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _parse_day_token(token: str) -> Optional[int]:
    token = token.strip().lower()
    if not token:
        return None
    if token in DAY_ALIASES:
        return DAY_ALIASES[token]
    if token.isdigit():
        idx = int(token)
        if 0 <= idx <= 6:
            return idx
    return None


def _parse_day_range(value: str) -> Set[int]:
    if "-" not in value:
        return set()
    start_token, end_token = value.split("-", 1)
    start_day = _parse_day_token(start_token)
    end_day = _parse_day_token(end_token)
    if start_day is None or end_day is None:
        return set()
    if start_day <= end_day:
        return set(range(start_day, end_day + 1))
    # Wrap around the week (e.g., fri-mon)
    return set(list(range(start_day, 7)) + list(range(0, end_day + 1)))


def _normalize_days(value) -> Set[int]:
    if isinstance(value, set):
        combined: Set[int] = set()
        for item in value:
            combined.update(_normalize_days(item))
        return combined or DEFAULT_DAYS
    if isinstance(value, (list, tuple)):
        combined: Set[int] = set()
        for item in value:
            combined.update(_normalize_days(item))
        return combined or DEFAULT_DAYS
    if isinstance(value, str):
        value = value.strip().lower()
        if not value:
            return DEFAULT_DAYS
        if value in {"todos", "all", "daily", "cada dia", "cada día"}:
            return set(range(7))
        if "-" in value and value not in DAY_ALIASES:
            days = _parse_day_range(value)
            if days:
                return days
        tokens = re.split(r"[\s,;]+", value)
        days = {day for token in tokens if (day := _parse_day_token(token)) is not None}
        return days or DEFAULT_DAYS
    if isinstance(value, int) and 0 <= value <= 6:
        return {value}
    return DEFAULT_DAYS


def _clamp_time(hour: int, minute: int) -> time:
    hour = max(0, min(int(hour), 23))
    minute = max(0, min(int(minute), 59))
    return time(hour, minute)




def _parse_time_value(value, fallback: time) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return fallback
        if ':' in raw:
            try:
                hour_s, minute_s = raw.split(':', 1)
                return _clamp_time(int(hour_s), int(minute_s))
            except Exception:
                return fallback
        if raw.isdigit():
            return _clamp_time(int(raw), 0)
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        try:
            return _clamp_time(int(value[0]), int(value[1]))
        except Exception:
            return fallback
    if isinstance(value, int):
        return _clamp_time(value, 0)
    return fallback


def build_schedule_from_config(config: Optional[dict]) -> LiveChatSchedule:
    config = config if isinstance(config, dict) else {}
    enabled = bool(config.get('enabled', True))

    tz_name = str(config.get('timezone') or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        timezone = ZoneInfo(tz_name)
    except Exception:
        timezone = ZoneInfo(DEFAULT_TIMEZONE)

    days = _normalize_days(config.get('days') if config.get('days') is not None else 'mon-fri')

    start_value = config.get('start_time')
    end_value = config.get('end_time')
    if start_value is None and config.get('start_hour') is not None:
        start_value = (config.get('start_hour'), config.get('start_minute', 0))
    if end_value is None and config.get('end_hour') is not None:
        end_value = (config.get('end_hour'), config.get('end_minute', 0))

    start_time = _parse_time_value(start_value, DEFAULT_START)
    end_time = _parse_time_value(end_value, DEFAULT_END)

    return LiveChatSchedule(
        enabled=enabled,
        days=days,
        start_time=start_time,
        end_time=end_time,
        timezone=timezone,
    )


def _is_live_chat_available_for_schedule(schedule: LiveChatSchedule, now: Optional[datetime] = None) -> bool:
    if not schedule.enabled:
        return False
    if not schedule.days:
        return False

    tz_now = now.astimezone(schedule.timezone) if now else datetime.now(schedule.timezone)
    if tz_now.weekday() not in schedule.days:
        return False

    current_time = tz_now.time()
    start = schedule.start_time
    end = schedule.end_time

    if start <= end:
        return start <= current_time < end
    return current_time >= start or current_time < end

def get_live_chat_schedule() -> LiveChatSchedule:
    enabled = bool(_get_config_value("LIVE_CHAT_SCHEDULE_ENABLED", True))
    tz_name = _get_config_value("LIVE_CHAT_SCHEDULE_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        timezone = ZoneInfo(tz_name)
    except Exception:  # pragma: no cover - fallback path
        logger.warning("Invalid timezone '%s' for live chat schedule. Falling back to default.", tz_name)
        timezone = ZoneInfo(DEFAULT_TIMEZONE)

    days_raw = _get_config_value("LIVE_CHAT_SCHEDULE_DAYS", "mon-fri")
    days = _normalize_days(days_raw)

    start_hour = _get_config_value("LIVE_CHAT_SCHEDULE_START_HOUR", DEFAULT_START.hour)
    start_minute = _get_config_value("LIVE_CHAT_SCHEDULE_START_MINUTE", DEFAULT_START.minute)
    end_hour = _get_config_value("LIVE_CHAT_SCHEDULE_END_HOUR", DEFAULT_END.hour)
    end_minute = _get_config_value("LIVE_CHAT_SCHEDULE_END_MINUTE", DEFAULT_END.minute)

    start_time = _clamp_time(start_hour, start_minute)
    end_time = _clamp_time(end_hour, end_minute)

    return LiveChatSchedule(
        enabled=enabled,
        days=days,
        start_time=start_time,
        end_time=end_time,
        timezone=timezone,
    )


def is_live_chat_available(now: Optional[datetime] = None, schedule: Optional[LiveChatSchedule] = None) -> bool:
    schedule = schedule or get_live_chat_schedule()
    return _is_live_chat_available_for_schedule(schedule, now)


def _describe_day_ranges(days: Iterable[int]) -> str:
    sorted_days = sorted(set(day for day in days if 0 <= day <= 6))
    if not sorted_days:
        return ""

    ranges: list[Tuple[int, int]] = []
    start = prev = sorted_days[0]
    for day in sorted_days[1:]:
        if day == prev + 1:
            prev = day
            continue
        ranges.append((start, prev))
        start = prev = day
    ranges.append((start, prev))

    parts: list[str] = []
    for start_day, end_day in ranges:
        if start_day == end_day:
            parts.append(DAY_NAMES[start_day])
        else:
            parts.append(f"{DAY_NAMES[start_day]} a {DAY_NAMES[end_day]}")

    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" y {parts[-1]}"


def get_schedule_description(schedule: Optional[LiveChatSchedule] = None) -> str:
    schedule = schedule or get_live_chat_schedule()
    if not schedule.days:
        return "sin horario definido"
    days_desc = _describe_day_ranges(schedule.days)
    start_str = schedule.start_time.strftime("%H:%M")
    end_str = schedule.end_time.strftime("%H:%M")
    tz_label = getattr(schedule.timezone, "key", str(schedule.timezone))
    base = f"{days_desc} de {start_str} a {end_str} hs"
    if tz_label and tz_label != DEFAULT_TIMEZONE:
        return f"{base} ({tz_label})"
    return base


def build_live_chat_status(now: Optional[datetime] = None, schedule_override: Optional[dict] = None) -> dict:
    schedule = build_schedule_from_config(schedule_override) if schedule_override is not None else get_live_chat_schedule()
    description = get_schedule_description(schedule)
    days_sorted = sorted(schedule.days)
    days = [DAY_NAMES[day] for day in days_sorted if 0 <= day < len(DAY_NAMES)]
    tz_label = getattr(schedule.timezone, "key", str(schedule.timezone))
    return {
        "enabled": schedule.enabled,
        "available": is_live_chat_available(now, schedule=schedule),
        "description": description,
        "days": days,
        "start_time": schedule.start_time.strftime("%H:%M"),
        "end_time": schedule.end_time.strftime("%H:%M"),
        "timezone": tz_label,
    }


def _build_channel_metadata(status: dict) -> dict:
    live_available = bool(status.get("enabled") and status.get("available"))
    mode = "live" if live_available else "offline"
    schedule_label = status.get("description")
    if live_available:
        primary_label = "Chatear con un agente"
        primary_action = "open_live_chat"
        availability_state = "online"
        badge_label = "En vivo"
        helper_text = "Un agente puede responder ahora por este canal."
    else:
        primary_label = "Dejar mensaje"
        primary_action = "queue_offline_message"
        availability_state = "offline_accepting_messages"
        badge_label = "Fuera de horario"
        helper_text = "El mensaje queda guardado en el ticket para que el equipo lo responda."

    return {
        "mode": mode,
        "availability_state": availability_state,
        "primary_cta": primary_label,
        "primary_action": primary_action,
        "ui": {
            "badge": "online" if live_available else "offline",
            "badge_label": badge_label,
            "primary_cta_label": primary_label,
            "primary_action": primary_action,
            "schedule_label": schedule_label,
            "show_schedule": bool(schedule_label),
            "helper_text": helper_text,
        },
        "cta": {
            "primary": {
                "id": "open_live_chat" if live_available else "leave_offline_message",
                "label": primary_label,
                "action": primary_action,
                "mode": mode,
            },
            "schedule": {
                "id": "view_live_chat_schedule",
                "label": "Ver horario de atencion",
                "schedule_label": schedule_label,
            },
        },
        "offline_message": {
            "enabled": True,
            "action": "queue_offline_message",
            "type": "ticket_bound_message",
            "requires_existing_thread": True,
            "safe_when_outside_hours": True,
        },
    }


def build_tenant_live_chat_status(
    tenant=None,
    *,
    now: Optional[datetime] = None,
    socket_room: Optional[str] = None,
) -> dict:
    tenant_config = getattr(tenant, "configuracion", None) if tenant is not None else None
    schedule_config = (
        tenant_config.get("live_chat_schedule")
        if isinstance(tenant_config, dict)
        and isinstance(tenant_config.get("live_chat_schedule"), dict)
        else None
    )
    status = build_live_chat_status(now=now, schedule_override=schedule_config)
    status["contract_version"] = "live_chat.schedule.v1"
    status["source"] = "tenant_config" if schedule_config else "global_config"
    status.update(_build_channel_metadata(status))
    status["offline_message_enabled"] = True
    status["channel_policy"] = {
        "opens_ticket_room": True,
        "allows_offline_messages": True,
        "frontend_must_keep_user_in_ticket_context": True,
        "offline_action": "queue_offline_message",
    }
    if socket_room:
        status["socket_room"] = socket_room
    if tenant is not None:
        status["tenant_id"] = getattr(tenant, "id", None)
        status["tenant_slug"] = getattr(tenant, "slug", None)
    status["transport"] = build_live_chat_transport(status, config_override=tenant_config)
    return status


def _load_custom_urgency_terms() -> Tuple[Set[str], Set[str]]:
    raw_terms = _get_config_value("LIVE_CHAT_AUTO_URGENCY_KEYWORDS", "")
    if not raw_terms:
        return set(), set()
    keywords: Set[str] = set()
    phrases: Set[str] = set()
    for chunk in re.split(r"[,;]\s*", raw_terms):
        normalized = _normalize_text(chunk)
        if not normalized:
            continue
        if " " in normalized:
            phrases.add(normalized)
        else:
            keywords.add(normalized)
    return keywords, phrases


def detect_urgency_reason(message: Optional[str]) -> Optional[str]:
    normalized = _normalize_text(message or "")
    if not normalized:
        return None

    custom_keywords, custom_phrases = _load_custom_urgency_terms()
    phrases = DEFAULT_URGENT_PHRASES.union(custom_phrases)
    for phrase in phrases:
        if phrase and phrase in normalized:
            return phrase

    keywords = DEFAULT_URGENT_KEYWORDS.union(custom_keywords)
    tokens = normalized.split()
    for token in tokens:
        if token in keywords:
            return token
        if token.startswith("urgent") or token.startswith("emergen"):
            return token
    return None


def should_auto_live_chat(message: Optional[str]) -> bool:
    return detect_urgency_reason(message) is not None
