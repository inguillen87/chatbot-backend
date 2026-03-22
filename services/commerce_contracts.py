from __future__ import annotations

from typing import Any

from services.contact_intake import infer_phone_from_anon_id, normalize_email, normalize_name


def normalize_sales_channel(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return "web"
    if "whatsapp" in value:
        return "whatsapp"
    if "widget" in value:
        return "widget"
    if value in {"manual_admin", "manual", "backoffice"}:
        return "manual_admin"
    if "voice" in value or "telefono" in value or "phone" in value or value == "call_center":
        return "phone"
    if value in {"market", "pwa", "web_widget", "site", "ecommerce"}:
        return "web"
    return value


def build_contact_key(
    *,
    user_id: Any = None,
    email: Any = None,
    phone: Any = None,
    anon_id: Any = None,
    session_id: Any = None,
) -> str | None:
    if user_id:
        return f"user:{user_id}"

    email_normalized = normalize_email(email)
    if email_normalized:
        return f"email:{email_normalized}"

    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) >= 8:
        return f"phone:{digits}"

    if anon_id:
        return f"anon:{str(anon_id).strip()}"

    if session_id:
        return f"session:{str(session_id).strip()}"

    return None


def build_customer_profile(
    *,
    user: Any = None,
    payload: dict[str, Any] | None = None,
    session_id: Any = None,
    channel: Any = None,
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    contact = payload.get("contacto") if isinstance(payload.get("contacto"), dict) else {}

    inferred_phone = infer_phone_from_anon_id(
        payload.get("anon_id")
        or contact.get("anon_id")
        or getattr(user, "anon_id", None)
    )
    raw_phone = (
        contact.get("telefono")
        or contact.get("phone")
        or payload.get("telefono")
        or payload.get("phone")
        or getattr(user, "telefono", None)
        or inferred_phone
    )
    raw_email = contact.get("email") or payload.get("email") or getattr(user, "email", None)
    raw_name = (
        contact.get("nombre")
        or payload.get("nombre")
        or payload.get("name")
        or getattr(user, "name", None)
    )
    anon_id = payload.get("anon_id") or contact.get("anon_id") or getattr(user, "anon_id", None)
    normalized_channel = normalize_sales_channel(channel or payload.get("channel") or payload.get("canal"))
    phone = str(raw_phone).strip() if raw_phone else None
    email = normalize_email(raw_email) or (str(raw_email).strip() if raw_email else None)
    name = normalize_name(raw_name) or (str(raw_name).strip() if raw_name else None)

    return {
        "user_id": getattr(user, "id", None),
        "name": name,
        "email": email,
        "phone": phone,
        "anon_id": anon_id,
        "session_id": str(session_id).strip() if session_id else None,
        "channel": normalized_channel,
        "channel_group": "conversational" if normalized_channel in {"whatsapp", "phone"} else "digital",
        "is_authenticated": bool(getattr(user, "id", None)) and not bool(getattr(user, "anon_id", None)),
        "contact_key": build_contact_key(
            user_id=getattr(user, "id", None),
            email=email,
            phone=phone,
            anon_id=anon_id,
            session_id=session_id,
        ),
    }


def resolve_order_contact_payload(
    *,
    user: Any = None,
    payload: dict[str, Any] | None = None,
    session_id: Any = None,
    channel: Any = None,
) -> dict[str, Any]:
    profile = build_customer_profile(
        user=user,
        payload=payload,
        session_id=session_id,
        channel=channel,
    )
    return {
        "name": profile.get("name"),
        "email": profile.get("email"),
        "phone": profile.get("phone"),
        "anon_id": profile.get("anon_id"),
        "channel": profile.get("channel"),
        "contact_key": profile.get("contact_key"),
        "customer_profile": profile,
    }
