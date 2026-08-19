"""Provider-aware policy for human voice transfers.

The LLM may request a handoff, but only Python is allowed to authorize the
transport and destination.  Both the HTTP TwiML route and OpenAI Realtime tool
executor call this module so a provider restriction cannot be bypassed by
choosing a different entry point.
"""

from __future__ import annotations

import re
from typing import Any


_SUPPORTED_PSTN_TRANSFER_PROVIDERS = {"twilio"}
_PHONE_FORMAT_RE = re.compile(r"^\+?[0-9().\-\s]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class VoiceTransferPolicyError(ValueError):
    """Safe policy rejection with a non-sensitive machine code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def normalize_voice_e164(value: Any) -> str | None:
    """Normalize common phone formatting while rejecting non-phone content."""

    raw = str(value or "").strip()
    if raw.lower().startswith("whatsapp:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw or not _PHONE_FORMAT_RE.fullmatch(raw):
        return None
    digits = re.sub(r"\D", "", raw)
    candidate = f"+{digits}"
    return candidate if _E164_RE.fullmatch(candidate) else None


def is_whatsapp_calling_endpoint(value: Any) -> bool:
    """Return whether the provider endpoint identifies a WhatsApp call leg."""

    return str(value or "").strip().lower().startswith("whatsapp:")


def validate_pstn_voice_transfer(
    *,
    provider: str,
    from_endpoint: Any,
    to_endpoint: Any,
    requested_target: Any,
    configured_target: Any,
) -> str:
    """Authorize one provider PSTN handoff and return its normalized target.

    WhatsApp Business Calling legs cannot be bridged to PSTN by Twilio/Meta.
    Unknown transfer providers fail closed until they have an explicit
    transport contract.
    """

    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in _SUPPORTED_PSTN_TRANSFER_PROVIDERS:
        raise VoiceTransferPolicyError("transfer_provider_not_supported")

    requested = normalize_voice_e164(requested_target)
    allowed = normalize_voice_e164(configured_target)
    if not requested or not allowed or requested != allowed:
        raise VoiceTransferPolicyError("transfer_target_not_authorized")

    if is_whatsapp_calling_endpoint(from_endpoint) or is_whatsapp_calling_endpoint(
        to_endpoint
    ):
        raise VoiceTransferPolicyError("whatsapp_pstn_bridge_forbidden")

    return requested
