"""Provider-bound WhatsApp inbound content classification.

This module only classifies the signed Twilio form.  It never fetches media,
parses contact cards, calls an AI provider, or infers business intent.  The
webhook applies the result only after resolving the authoritative tenant and
validating the Twilio signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


WHATSAPP_INBOUND_CONTENT_CONTRACT_VERSION = "whatsapp.inbound_content.v1"

_OUTBOUND_STATUS_VALUES = frozenset(
    {
        "accepted",
        "canceled",
        "delivered",
        "failed",
        "partially_delivered",
        "queued",
        "read",
        "scheduled",
        "sending",
        "sent",
        "undelivered",
    }
)
_CALL_EVENT_KEYS = frozenset(
    {
        "CallSid",
        "CallStatus",
        "CallbackSource",
        "DialCallSid",
        "DialCallStatus",
        "ParentCallSid",
        "SequenceNumber",
    }
)
_VCARD_MIME_TYPES = frozenset(
    {
        "application/vcard",
        "text/directory",
        "text/vcard",
        "text/x-vcard",
    }
)
_AUDIO_APPLICATION_MIME_TYPES = frozenset({"application/ogg"})


@dataclass(frozen=True)
class WhatsAppInboundContent:
    """Bounded classification result; no raw body, URL, or contact data."""

    kind: str
    durable_message_kind: str
    is_message: bool
    is_event_only: bool
    has_media: bool = False
    media_mime_type: str | None = None
    reason: str | None = None
    contract_version: str = WHATSAPP_INBOUND_CONTENT_CONTRACT_VERSION


def canonical_media_mime_type(value: Any) -> str:
    """Normalize MIME case and strip parameters such as Ogg codecs."""

    return str(value or "").split(";", 1)[0].strip().lower()


def is_audio_media_type(value: Any) -> bool:
    mime_type = canonical_media_mime_type(value)
    return mime_type.startswith("audio/") or mime_type in _AUDIO_APPLICATION_MIME_TYPES


def _non_empty(payload: Mapping[str, Any], key: str) -> bool:
    return bool(str(payload.get(key) or "").strip())


def _positive_media_count(payload: Mapping[str, Any]) -> bool:
    try:
        return int(str(payload.get("NumMedia") or "0").strip()) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _first_media(payload: Mapping[str, Any]) -> tuple[bool, str]:
    has_media = _positive_media_count(payload)
    mime_type = ""
    for index in range(10):
        media_url = _non_empty(payload, f"MediaUrl{index}")
        candidate_mime = canonical_media_mime_type(payload.get(f"MediaContentType{index}"))
        if media_url or candidate_mime:
            has_media = True
            if not mime_type and candidate_mime:
                mime_type = candidate_mime
    return has_media, mime_type


def _is_emoji_only(value: Any) -> bool:
    text = str(value or "")
    if not text.strip():
        return False
    has_emoji = False
    has_keycap = "\u20e3" in text
    for character in text:
        codepoint = ord(character)
        if character.isspace() or codepoint in {0x200D, 0x20E3, 0xFE0E, 0xFE0F}:
            continue
        if has_keycap and character in "#*0123456789":
            continue
        if (
            0x1F000 <= codepoint <= 0x1FAFF
            or 0x2600 <= codepoint <= 0x27BF
            or 0x2300 <= codepoint <= 0x23FF
            or 0x1F1E6 <= codepoint <= 0x1F1FF
        ):
            has_emoji = True
            continue
        return False
    return has_emoji


def _media_kind(mime_type: str) -> tuple[str, str]:
    if mime_type == "image/webp":
        # Twilio documents image/webp as the WhatsApp sticker MIME type.
        return "sticker", "image"
    if mime_type in _VCARD_MIME_TYPES:
        return "contact", "contact"
    if is_audio_media_type(mime_type):
        return "audio", "audio"
    if mime_type.startswith("image/"):
        return "image", "image"
    if mime_type.startswith("video/"):
        return "video", "video"
    if mime_type:
        return "document", "document"
    return "unsupported_media", "unknown"


def classify_twilio_whatsapp_payload(payload: Mapping[str, Any]) -> WhatsAppInboundContent:
    """Classify a Twilio form without interpreting its language or contents."""

    if not isinstance(payload, Mapping):
        return WhatsAppInboundContent(
            kind="control_event",
            durable_message_kind="unknown",
            is_message=False,
            is_event_only=True,
            reason="payload_not_mapping",
        )

    has_media, media_mime_type = _first_media(payload)
    has_location = _non_empty(payload, "Latitude") and _non_empty(payload, "Longitude")
    has_interactive = any(
        _non_empty(payload, key)
        for key in ("ButtonPayload", "ListId", "InteractiveData", "FlowData")
    ) or isinstance(payload.get("safe_flow_submission"), Mapping)
    body = str(payload.get("Body") or "").strip()
    has_message_content = bool(body or has_media or has_location or has_interactive)

    status = str(payload.get("MessageStatus") or payload.get("SmsStatus") or "").strip().lower()
    if not has_message_content and status in _OUTBOUND_STATUS_VALUES:
        return WhatsAppInboundContent(
            kind="status_event",
            durable_message_kind="unknown",
            is_message=False,
            is_event_only=True,
            reason="delivery_status_callback",
        )

    if not has_message_content and any(_non_empty(payload, key) for key in _CALL_EVENT_KEYS):
        return WhatsAppInboundContent(
            kind="call_event",
            durable_message_kind="unknown",
            is_message=False,
            is_event_only=True,
            reason="call_control_callback",
        )

    if not has_message_content and not (
        _non_empty(payload, "MessageSid") or _non_empty(payload, "SmsMessageSid")
    ):
        return WhatsAppInboundContent(
            kind="control_event",
            durable_message_kind="unknown",
            is_message=False,
            is_event_only=True,
            reason="no_message_identity_or_content",
        )

    if isinstance(payload.get("safe_flow_submission"), Mapping) or _non_empty(payload, "FlowData"):
        return WhatsAppInboundContent("flow", "flow", True, False)
    if has_location:
        return WhatsAppInboundContent("location", "location", True, False)
    if has_interactive:
        return WhatsAppInboundContent("interactive", "interactive", True, False)
    if has_media:
        kind, durable_kind = _media_kind(media_mime_type)
        return WhatsAppInboundContent(
            kind=kind,
            durable_message_kind=durable_kind,
            is_message=True,
            is_event_only=False,
            has_media=True,
            media_mime_type=media_mime_type or None,
            reason="missing_media_content_type" if not media_mime_type else None,
        )
    if _is_emoji_only(body) and _non_empty(payload, "OriginalRepliedMessageSid"):
        return WhatsAppInboundContent("reaction", "text", True, False)
    if _is_emoji_only(body):
        return WhatsAppInboundContent("emoji", "text", True, False)
    if body:
        return WhatsAppInboundContent("text", "text", True, False)
    return WhatsAppInboundContent(
        kind="unsupported",
        durable_message_kind="unknown",
        is_message=True,
        is_event_only=False,
        reason="empty_provider_message",
    )


def honest_unprocessable_reply(kind: str, *, reason: str | None = None) -> str:
    """Return an actionable reply that never claims unavailable understanding."""

    normalized = str(kind or "unsupported").strip().lower()
    if normalized == "audio":
        return (
            "Recibí tu audio, pero no pude transcribirlo con suficiente confianza. "
            "No voy a adivinar lo que dice. Reenviá la nota de voz o escribí el detalle; "
            "también podés pedir hablar con una persona."
        )
    if normalized == "video":
        return (
            "Recibí tu video, pero este canal todavía no puede interpretar su contenido "
            "automáticamente. Escribí qué muestra y qué necesitás, o pedí hablar con una persona."
        )
    if normalized == "sticker":
        return (
            "Recibí tu sticker. Para poder ayudarte, escribime qué necesitás o enviá una foto, "
            "un audio, un archivo o tu ubicación."
        )
    if normalized == "contact":
        return (
            "Recibí la tarjeta de contacto. Por seguridad no voy a usarla para cambiar tus datos "
            "ni crear una gestión sin tu confirmación. Decime qué querés hacer con ese contacto."
        )
    if reason == "media_too_large":
        return (
            "Recibí el archivo, pero supera el límite seguro de procesamiento. "
            "Reenviá una versión más liviana o escribí una descripción; también podés pedir "
            "hablar con una persona."
        )
    if normalized in {"image", "document", "unsupported_media"}:
        label = "imagen" if normalized == "image" else "archivo"
        return (
            f"Recibí el {label}, pero no pude procesarlo de forma confiable. "
            "Reenviálo o escribí una descripción; no se creó ninguna gestión con contenido supuesto."
        )
    return (
        "Recibí un tipo de mensaje que este canal no puede interpretar de forma confiable. "
        "Reenviálo como texto, audio, foto, PDF o ubicación, o pedí hablar con una persona."
    )


__all__ = [
    "WHATSAPP_INBOUND_CONTENT_CONTRACT_VERSION",
    "WhatsAppInboundContent",
    "canonical_media_mime_type",
    "classify_twilio_whatsapp_payload",
    "honest_unprocessable_reply",
    "is_audio_media_type",
]
