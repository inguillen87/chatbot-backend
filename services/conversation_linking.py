from __future__ import annotations

import secrets
import string
from datetime import timedelta, timezone
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

from models import (
    ChannelSession,
    ChatSessionContext,
    Conversation,
    ConversationLinkRequest,
    db,
)
from utils.time_utils import get_local_now


def _normalize_phone(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit() or ch == "+")


def _generate_otp(size: int = 6) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(size))


def _to_utc_naive(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class ConversationLinkingService:
    def __init__(self, tenant_id: int):
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.tenant_id = tenant_id

    def create_whatsapp_link_request(
        self,
        *,
        conversation_id: Optional[str],
        chat_session_id: Optional[str],
        whatsapp_number: str,
        requested_by_user_id: Optional[int],
        ttl_minutes: int = 10,
    ) -> tuple[ConversationLinkRequest, str]:
        target_identity = _normalize_phone(whatsapp_number)
        if not target_identity:
            raise ValueError("whatsapp_number is required")
        self._enforce_rate_limit(target_identity=target_identity)

        conversation = None
        source_channel_session_id = None
        if conversation_id:
            conversation = Conversation.query.filter_by(id=conversation_id, tenant_id=self.tenant_id).first()
        elif chat_session_id:
            ctx = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id, tenant_id=self.tenant_id).first()
            if ctx and ctx.conversation_id:
                conversation = Conversation.query.filter_by(id=ctx.conversation_id, tenant_id=self.tenant_id).first()
                source_channel_session_id = ctx.channel_session_id

        if conversation is None:
            raise ValueError("conversation not found")

        otp = _generate_otp()
        token = secrets.token_urlsafe(24)
        link_request = ConversationLinkRequest(
            tenant_id=self.tenant_id,
            conversation_id=conversation.id,
            source_channel_session_id=source_channel_session_id,
            target_channel="whatsapp",
            target_identity=target_identity,
            otp_code=generate_password_hash(otp),
            deep_link_token=token,
            status="pending",
            expires_at=get_local_now() + timedelta(minutes=ttl_minutes),
            created_by_user_id=requested_by_user_id,
        )
        db.session.add(link_request)
        db.session.flush()
        return link_request, otp

    def _enforce_rate_limit(self, *, target_identity: str) -> None:
        # Basic anti-abuse guard for repeated link requests to the same destination.
        since = get_local_now() - timedelta(minutes=10)
        recent = (
            ConversationLinkRequest.query.filter_by(
                tenant_id=self.tenant_id,
                target_channel="whatsapp",
                target_identity=target_identity,
            )
            .filter(ConversationLinkRequest.created_at >= since)
            .count()
        )
        if recent >= 3:
            raise PermissionError("too many link requests for this number, try again later")

    def confirm_whatsapp_link(
        self,
        *,
        deep_link_token: Optional[str],
        otp_code: Optional[str],
        whatsapp_chat_session_id: Optional[str],
    ) -> tuple[ConversationLinkRequest, ChannelSession, bool]:
        if not deep_link_token:
            raise ValueError("deep_link_token is required")

        link_request = ConversationLinkRequest.query.filter_by(
            deep_link_token=deep_link_token,
            tenant_id=self.tenant_id,
        ).first()
        if not link_request:
            raise LookupError("link request not found")

        now = get_local_now()
        if link_request.status == "confirmed":
            linked_session = self._resolve_or_create_whatsapp_session(
                conversation_id=link_request.conversation_id,
                target_identity=link_request.target_identity,
                whatsapp_chat_session_id=whatsapp_chat_session_id,
            )
            return link_request, linked_session, True

        expires_at = _to_utc_naive(link_request.expires_at)
        now_naive = _to_utc_naive(now)
        if expires_at and now_naive and now_naive > expires_at:
            link_request.status = "expired"
            db.session.flush()
            raise TimeoutError("link request expired")

        submitted_otp = str(otp_code or "").strip()
        if submitted_otp:
            hashed_or_plain = str(link_request.otp_code or "")
            is_valid = (
                check_password_hash(hashed_or_plain, submitted_otp)
                if hashed_or_plain.startswith("scrypt:") or hashed_or_plain.startswith("pbkdf2:")
                else submitted_otp == hashed_or_plain
            )
            if not is_valid:
                raise PermissionError("invalid otp")

        linked_session = self._resolve_or_create_whatsapp_session(
            conversation_id=link_request.conversation_id,
            target_identity=link_request.target_identity,
            whatsapp_chat_session_id=whatsapp_chat_session_id,
        )

        link_request.status = "confirmed"
        link_request.confirmed_at = now
        db.session.flush()
        return link_request, linked_session, False

    def _resolve_or_create_whatsapp_session(
        self,
        *,
        conversation_id: str,
        target_identity: str,
        whatsapp_chat_session_id: Optional[str],
    ) -> ChannelSession:
        session = ChannelSession.query.filter_by(
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            channel="whatsapp",
            channel_identity=target_identity,
        ).first()
        if session is None:
            session = ChannelSession(
                tenant_id=self.tenant_id,
                conversation_id=conversation_id,
                channel="whatsapp",
                channel_identity=target_identity,
                chat_session_id=whatsapp_chat_session_id,
                status="active",
            )
            db.session.add(session)
            db.session.flush()

        if whatsapp_chat_session_id:
            ctx = ChatSessionContext.query.filter_by(
                tenant_id=self.tenant_id,
                chat_session_id=whatsapp_chat_session_id,
            ).first()
            if ctx:
                ctx.conversation_id = conversation_id
                ctx.channel_session_id = session.id

        return session
