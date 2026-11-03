"""Server-only post-auth contracts; callers retain transport and authority."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class ConversationOutputUnsupported(RuntimeError):
    pass


@dataclass(frozen=True)
class ConversationMode:
    # These values come from the authenticated route's existing validators.
    # A label, user text or request JSON cannot select a mode.
    flow_submission_present: bool
    force_chatboc_demo_hub: bool


@dataclass(frozen=True)
class ConversationTurn:
    owner_user: Any
    tenant_profile: Any
    # Preserve the authenticated caller's resolved legacy/profile identifier.
    # This value does not itself confer tenant or provider authority.
    tenant_id: int | None
    end_user: Any
    session_context: Any
    chat_session_id: str
    contact_identity: str
    message_body: str
    action_id: str | None
    source_event_id: str | None
    durable_claim: Any
    mode: ConversationMode
    original_text: str | None = None
    location: Mapping[str, Any] | None = None
    attachment: Mapping[str, Any] | None = None
    # Raw provider ProfileName remains distinct from the effective local name.
    original_profile_name: str | None = None
    profile_name: str | None = None
    inbound_content_metadata: Mapping[str, Any] | None = None
    interpreted_media: Mapping[str, Any] | None = None
    skip_media_analysis: bool = False
    validated_flow_submission: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ConversationSelection:
    message_body: str
    option: Mapping[str, Any] | None
    action_id: str | None
    option_label: str | None
    skip_media_analysis: bool = False


@dataclass(frozen=True)
class ConversationPreparation:
    selection: ConversationSelection | None
    legacy_return: tuple[str, int] | None = None

    @property
    def handled(self):
        return self.legacy_return is not None


@dataclass(frozen=True)
class ConversationFinish:
    bot_response: Mapping[str, Any] | None
    formatted_response: Mapping[str, Any] | None
    legacy_return: tuple[str, int] | None = None
    response_text_before_format: str | None = None


class ConversationSink(Protocol):
    """0..N ordered emissions, not a Twilio Client or a durable-state authority.

    The legacy adapter may call its existing transport helper. A durable Meta
    sink only collects validated content; the caller atomically finalizes the
    same leased turn through its provider-specific outbox finalizer. Empty
    output is valid after a handled human-chat turn. Unsupported media must
    raise before outbox materialization, never disappear from the sequence.
    """
    enabled: bool

    def emit_text(self, body: str) -> None: ...

    def emit_response(self, bot_response: Mapping[str, Any],
                      formatted_response: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class ConversationEmission:
    kind: str
    value: Any


class CollectingConversationSink:
    """In-memory boundary only; these objects must not be sent/persisted raw.

    A provider adapter must allowlist public payload fields and validate the
    entire ordered tuple before staging its own typed durable messages.
    No object here confers tenant, actor, delivery or media authority.
    """
    enabled = True

    def __init__(self, *, supports_media: bool):
        self.supports_media = supports_media
        self._items = []

    def emit_text(self, body):
        if not isinstance(body, str) or not body:
            raise ConversationOutputUnsupported("conversation_text_invalid")
        self._items.append(ConversationEmission("text", body))

    def emit_response(self, bot_response, formatted_response):
        from copy import deepcopy
        media_keys = ("image_url", "audio_url", "sticker_url", "document_url", "video_url", "media_url")
        if not self.supports_media and (any(bot_response.get(key) for key in media_keys)
                or any(formatted_response.get(key) for key in media_keys)
                or bot_response.get("_twilio_pre_messages")):
            raise ConversationOutputUnsupported("conversation_media_unsupported")
        self._items.append(ConversationEmission("formatted",
            (deepcopy(dict(bot_response)), deepcopy(dict(formatted_response)))))

    def snapshot(self):
        return tuple(self._items)
