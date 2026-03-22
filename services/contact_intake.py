from __future__ import annotations

import re
from typing import Any

PLACEHOLDER_EMAIL_SUFFIXES = (
    "@whatsapp.chatboc.com",
    "@anon.chatboc.com",
)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_placeholder_email(value: Any) -> bool:
    email = (_clean_text(value) or "").lower()
    return any(email.endswith(suffix) for suffix in PLACEHOLDER_EMAIL_SUFFIXES)


def normalize_email(value: Any) -> str | None:
    email = (_clean_text(value) or "").lower()
    if not email or is_placeholder_email(email):
        return None
    return email


def normalize_name(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    if text.lower() in {"vecino/a", "anonimo", "anónimo", "cliente", "usuario"}:
        return None
    return text


def infer_phone_from_anon_id(anon_id: Any) -> str | None:
    raw = _clean_text(anon_id)
    if not raw:
        return None

    explicit_match = re.search(r"\+\d{8,}", raw)
    if explicit_match:
        return explicit_match.group(0)

    normalized = raw.lower()
    cleaned = "".join(ch for ch in raw if ch.isdigit())
    if len(cleaned) < 8 or len(cleaned) > 15:
        return None

    if normalized.startswith(("whatsapp", "wa:", "tel:", "phone:")):
        return f"+{cleaned}"

    if re.fullmatch(r"\+?[\d\s().-]{8,}", raw):
        return f"+{cleaned}"

    return None


def resolve_contact_snapshot(*, datos: dict | None = None, profile_name: Any = None, telefono_contexto: Any = None, email_contexto: Any = None, anon_id: Any = None) -> dict[str, str | None]:
    datos = datos if isinstance(datos, dict) else {}
    nombre = (
        normalize_name(datos.get("nombre_usuario_detectado"))
        or normalize_name(datos.get("nombre"))
        or normalize_name(profile_name)
    )
    telefono = (
        _clean_text(datos.get("telefono_detectado"))
        or _clean_text(datos.get("telefono"))
        or _clean_text(telefono_contexto)
        or infer_phone_from_anon_id(anon_id)
    )
    email = (
        normalize_email(datos.get("email_detectado"))
        or normalize_email(datos.get("email"))
        or normalize_email(email_contexto)
    )
    return {
        "nombre": nombre,
        "telefono": telefono,
        "email": email,
    }


def missing_contact_fields(snapshot: dict[str, Any], *, required: tuple[str, ...] = ("nombre", "telefono", "email")) -> list[str]:
    missing: list[str] = []
    for field in required:
        if not _clean_text(snapshot.get(field)):
            missing.append(field)
    return missing
