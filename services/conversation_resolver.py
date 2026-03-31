from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from models import ChatSessionContext, ChannelSession, Conversation, Message, db


@dataclass
class ResolvedConversation:
    conversation: Conversation
    channel_session: ChannelSession


class ConversationResolver:
    """Resolve/create conversation entities while preserving chat_session_id compatibility."""

    def __init__(self, tenant_id: int):
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.tenant_id = tenant_id

    def resolve_or_create(
        self,
        *,
        chat_session_id: Optional[str],
        channel: str = "web",
        channel_identity: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> ResolvedConversation:
        conversation = None
        channel_session = None

        if chat_session_id:
            chat_ctx = ChatSessionContext.query.filter_by(
                chat_session_id=chat_session_id,
                tenant_id=self.tenant_id,
            ).first()
            if chat_ctx and chat_ctx.conversation_id:
                conversation = db.session.get(Conversation, chat_ctx.conversation_id)
                if chat_ctx.channel_session_id:
                    channel_session = db.session.get(ChannelSession, chat_ctx.channel_session_id)

            if conversation is None:
                conversation = Conversation.query.filter_by(
                    tenant_id=self.tenant_id,
                    legacy_chat_session_id=chat_session_id,
                ).first()

        if conversation is None:
            conversation = Conversation(
                tenant_id=self.tenant_id,
                created_by_user_id=user_id,
                legacy_chat_session_id=chat_session_id,
            )
            db.session.add(conversation)
            db.session.flush()

        if channel_session is None:
            channel_session = ChannelSession.query.filter_by(
                tenant_id=self.tenant_id,
                conversation_id=conversation.id,
                chat_session_id=chat_session_id,
            ).first()

        if channel_session is None:
            channel_session = ChannelSession(
                conversation_id=conversation.id,
                tenant_id=self.tenant_id,
                channel=channel,
                channel_identity=channel_identity,
                chat_session_id=chat_session_id,
            )
            db.session.add(channel_session)
            db.session.flush()

        if chat_session_id:
            chat_ctx = ChatSessionContext.query.filter_by(
                chat_session_id=chat_session_id,
                tenant_id=self.tenant_id,
            ).first()
            if chat_ctx:
                chat_ctx.conversation_id = conversation.id
                chat_ctx.channel_session_id = channel_session.id

        return ResolvedConversation(conversation=conversation, channel_session=channel_session)

    def append_message(
        self,
        *,
        conversation_id: str,
        channel_session_id: Optional[int],
        sender_type: str,
        body: str,
        sender_user_id: Optional[int] = None,
        direction: str = "in",
        metadata: Optional[dict] = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            channel_session_id=channel_session_id,
            tenant_id=self.tenant_id,
            sender_type=sender_type,
            sender_user_id=sender_user_id,
            direction=direction,
            body=body,
            meta_payload=metadata or None,
        )
        db.session.add(message)
        db.session.flush()
        return message
