"""Raw-body Meta authentication and bounded parsing, without effects or routes.

Signature authentication precedes parsing/resolution. The entire batch must bind
to verified senders before any event is returned; callers must durably dedupe and
enqueue before acknowledging. This parser itself never claims delivery or writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import re
from typing import Callable

from services.meta_whatsapp_cloud import (
    MetaContractError, MetaSenderSnapshot, VerifiedMetaBinding, _WAMID,
)
from services.meta_whatsapp_credentials import meta_id
from services.tenant_provider_credentials import ProviderCredentialError

_SIGNATURE = re.compile(r"^sha256=[0-9a-f]{64}$")
_CONTACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS = 100


@dataclass(frozen=True)
class MetaContactIdentity:
    """Signed identity association returned only by a trusted context adapter.

    The phone is an explicit destination, never a derivation from the BSUID.
    The event's verified sender supplies the business/tenant namespace.
    """
    phone: str = field(repr=False)
    user_id: str = field(repr=False)
    parent_user_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class MetaWebhookEvent:
    sender: MetaSenderSnapshot
    kind: str
    event_key: str
    timestamp: int
    message_id: str = field(repr=False)
    contact: str = field(repr=False)
    text: str | None = field(default=None, repr=False)
    status: str | None = None
    error_codes: tuple[int, ...] = ()
    content_type: str = "text"
    selection: str | None = field(default=None, repr=False)
    location: tuple[float, float] | None = field(default=None, repr=False)
    media_id: str | None = field(default=None, repr=False)
    media_mime_type: str | None = field(default=None, repr=False)
    media_sha256: str | None = field(default=None, repr=False)
    contact_identity: MetaContactIdentity | None = field(default=None, repr=False)


def verify_challenge(*, mode: str, token: str, challenge: str, verify_token: str) -> str:
    if (mode != "subscribe" or not isinstance(token, str) or not token
            or not isinstance(verify_token, str) or not verify_token
            or not isinstance(challenge, str) or not 1 <= len(challenge) <= 256
            or not challenge.isascii() or not challenge.isdigit()
            or not hmac.compare_digest(token.encode("utf-8"), verify_token.encode("utf-8"))):
        raise MetaContractError("meta_webhook_challenge_denied")
    return challenge


def verify_signature(*, raw_body: bytes, signature: str, app_secret: str) -> None:
    if (not isinstance(raw_body, bytes) or not 1 <= len(raw_body) <= MAX_BODY_BYTES
            or not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature)
            or not isinstance(app_secret, str) or not 16 <= len(app_secret) <= 4096):
        raise MetaContractError("meta_webhook_signature_invalid")
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise MetaContractError("meta_webhook_signature_invalid")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _list(value, *, allow_empty=False):
    if not isinstance(value, list) or len(value) > MAX_EVENTS or (not value and not allow_empty):
        raise ValueError
    return value


def parse_webhook(*, raw_body: bytes, signature: str, app_secret: str,
                  app_id: str, now: int, binding_resolver: Callable,
                  contact_resolver: Callable | None = None,
                  contact_identity_resolver: Callable | None = None,
                  allow_sandbox_content: bool = False) -> tuple[MetaWebhookEvent, ...]:
    """Resolver is trusted server code keyed by (phone ID, app ID, entry WABA ID).

    It must use resolve_sender with fresh trusted Meta authority, not caller IDs,
    URL tenant, display_phone_number, contact name or a legacy Twilio fallback.
    Unsupported event/message types fail explicitly; no partial successful batch.
    Contact IDs are opaque and never normalized into phones or joined globally.
    A trusted contact adapter is required for newer identity fields; their exact
    current Cloud API schema is not assumed by this isolated foundation.
    A context adapter additionally receives signed contacts and the verified
    sender; it can preserve a typed BSUID association without a global merge.
    """
    verify_signature(raw_body=raw_body, signature=signature, app_secret=app_secret)
    app_id = meta_id(app_id)
    if (type(now) is not int or now < 0 or not callable(binding_resolver)
            or (contact_identity_resolver is not None and
                (not callable(contact_identity_resolver) or contact_resolver is not None))):
        raise MetaContractError("meta_webhook_binding_required")
    try:
        document = json.loads(raw_body.decode("utf-8"), object_pairs_hook=_unique_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(document, dict) or document.get("object") != "whatsapp_business_account":
            raise ValueError
        events, bindings = [], {}
        for entry in _list(document.get("entry")):
            waba_id = meta_id(entry["id"])
            for change in _list(entry.get("changes")):
                value = change["value"]
                if change.get("field") != "messages" or value.get("messaging_product") != "whatsapp":
                    raise ValueError
                phone_id = meta_id(value["metadata"]["phone_number_id"])
                key = (phone_id, app_id, waba_id)
                if key not in bindings:
                    try:
                        bindings[key] = binding_resolver(phone_id, app_id, waba_id)
                    except (MetaContractError, ProviderCredentialError):
                        raise
                    except Exception:
                        raise MetaContractError("meta_webhook_binding_unavailable") from None
                binding = bindings[key]
                if (not isinstance(binding, VerifiedMetaBinding)
                        or binding.sender.phone_number_id != phone_id or binding.sender.app_id != app_id
                        or binding.sender.waba_id != waba_id or binding.authority.sender != binding.sender
                        or binding.authority.credential_revision != binding.credential.revision
                        or binding.authority.webhook_subscribed is not True
                        or not binding.authority.observed_at <= now < binding.authority.valid_until
                        or now >= binding.credential.expires_at):
                    raise MetaContractError("meta_webhook_authority_unavailable")
                # Same phone ID cannot bind differently inside a signed batch.
                if any(b.sender.phone_number_id == phone_id and b.sender != binding.sender
                       for b in bindings.values()):
                    raise MetaContractError("meta_webhook_sender_conflict")
                messages = _list(value.get("messages", []), allow_empty=True)
                statuses = _list(value.get("statuses", []), allow_empty=True)
                if not messages and not statuses:
                    raise ValueError
                for kind, values in (("message", messages), ("status", statuses)):
                    for item in values:
                        message_id, timestamp = item["id"], item["timestamp"]
                        identity = None
                        if contact_identity_resolver is not None:
                            try:
                                contact = contact_identity_resolver(item, kind, value.get("contacts"), binding.sender)
                            except MetaContractError as exc:
                                reason = str(exc)
                                if reason not in {"meta_webhook_contact_schema_invalid", "meta_webhook_contact_conflict",
                                                  "meta_webhook_contact_phone_not_allowed", "meta_webhook_contact_identity_unavailable"}:
                                    reason = "meta_webhook_contact_unavailable"
                                raise MetaContractError(reason) from None
                            except Exception:
                                raise MetaContractError("meta_webhook_contact_unavailable") from None
                            if isinstance(contact, MetaContactIdentity):
                                identity, contact = contact, contact.phone
                                if (not isinstance(identity.user_id, str) or not _CONTACT_ID.fullmatch(identity.user_id)
                                        or (identity.parent_user_id is not None and
                                            (not isinstance(identity.parent_user_id, str) or not _CONTACT_ID.fullmatch(identity.parent_user_id)))):
                                    raise ValueError
                        elif contact_resolver is not None:
                            try:
                                contact = contact_resolver(item, kind)
                            except Exception:
                                raise MetaContractError("meta_webhook_contact_unavailable") from None
                        else:
                            # Do not discard a BSUID in favor of a phone if a new
                            # identity schema is present but not yet verified.
                            if (any(k in item for k in ("from_user_id", "user_id", "recipient_user_id", "parent_user_id",
                                                       "from_parent_user_id", "recipient_parent_user_id"))
                                    or any(isinstance(row, dict) and ("user_id" in row or "parent_user_id" in row)
                                           for row in _list(value.get("contacts", []), allow_empty=True))):
                                raise MetaContractError("meta_webhook_contact_adapter_required")
                            contact = item["from"] if kind == "message" else item["recipient_id"]
                        if (not isinstance(message_id, str) or not _WAMID.fullmatch(message_id)
                                or not isinstance(timestamp, str) or not timestamp.isascii()
                                or not timestamp.isdigit() or len(timestamp) > 12
                                or not isinstance(contact, str) or not _CONTACT_ID.fullmatch(contact)):
                            raise ValueError
                        text, status, codes = None, None, ()
                        content_type, selection, location = "text", None, None
                        media_id, media_mime_type, media_sha256 = None, None, None
                        if kind == "message":
                            content_type = item.get("type")
                            if content_type == "text":
                                text = item["text"]["body"]
                                if not isinstance(text, str) or not text.strip() or len(text) > 4096:
                                    raise ValueError
                            elif allow_sandbox_content is True and content_type == "interactive":
                                interactive = item["interactive"]
                                subtype = interactive["type"]
                                if subtype not in {"button_reply", "list_reply"}:
                                    raise ValueError
                                selection = interactive[subtype]["id"]
                                if not isinstance(selection, str) or not 1 <= len(selection) <= 256:
                                    raise ValueError
                            elif allow_sandbox_content is True and content_type == "location":
                                latitude, longitude = item["location"]["latitude"], item["location"]["longitude"]
                                if (type(latitude) not in (int, float) or type(longitude) not in (int, float)
                                        or not math.isfinite(latitude) or not math.isfinite(longitude)
                                        or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
                                    raise ValueError
                                location = (float(latitude), float(longitude))
                            elif allow_sandbox_content is True and content_type == "audio":
                                audio = item["audio"]
                                media_id = meta_id(audio["id"])
                                media_mime_type = audio.get("mime_type")
                                if (not isinstance(media_mime_type, str) or not 1 <= len(media_mime_type) <= 128
                                        or not re.fullmatch(r"audio/[a-zA-Z0-9.+-]+(?:; *codecs=opus)?", media_mime_type)):
                                    raise ValueError
                                media_sha256 = audio.get("sha256")
                                if media_sha256 is not None and (not isinstance(media_sha256, str)
                                        or not re.fullmatch(r"[A-Za-z0-9+/]{43}=|[0-9a-fA-F]{64}", media_sha256)):
                                    raise ValueError
                            else:
                                raise MetaContractError("meta_webhook_message_type_unsupported")
                        else:
                            status = item["status"]
                            if status not in {"sent", "delivered", "read", "failed"}:
                                raise ValueError
                            errors = _list(item.get("errors", []), allow_empty=True)
                            if any(type(e.get("code")) is not int or not 0 <= e["code"] <= 9999999 for e in errors):
                                raise ValueError
                            codes = tuple(e["code"] for e in errors)
                        material = [binding.sender.tenant_id, binding.sender.connection_id,
                                    binding.sender.sender_id, kind, message_id, status]
                        event_key = "meta:" + hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()
                        events.append(MetaWebhookEvent(binding.sender, kind, event_key,
                                                       int(timestamp), message_id, contact, text, status, codes,
                                                       content_type, selection, location, media_id, media_mime_type, media_sha256, identity))
                        if len(events) > MAX_EVENTS:
                            raise ValueError
        # Delivery timestamps vary across retries; semantic key dedupes within batch.
        unique = {}
        for event in events:
            previous = unique.get(event.event_key)
            if previous is not None and (previous.text, previous.contact, previous.error_codes,
                    previous.content_type, previous.selection, previous.location, previous.media_id,
                    previous.media_mime_type, previous.media_sha256, previous.contact_identity) != (event.text,
                    event.contact, event.error_codes, event.content_type, event.selection, event.location,
                    event.media_id, event.media_mime_type, event.media_sha256, event.contact_identity):
                raise MetaContractError("meta_webhook_duplicate_conflict")
            unique.setdefault(event.event_key, event)
        return tuple(unique.values())
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise MetaContractError("meta_webhook_payload_invalid") from None
