from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import jwt
from flask import current_app


LIVE_CHAT_TOKEN_AUDIENCE = "chatboc-ticket-room"
LIVE_CHAT_TOKEN_ISSUER = "chatboc-live-chat"
LIVE_CHAT_TOKEN_SCOPE = "ticket_room:read"
SUPPORTED_TICKET_TYPES = {"municipio", "pyme"}


class LiveChatAccessError(ValueError):
    pass


def build_ticket_room(ticket_type: str, ticket_id: int | str) -> str:
    normalized_type = str(ticket_type or "").strip().lower()
    if normalized_type not in SUPPORTED_TICKET_TYPES:
        raise LiveChatAccessError("unsupported_ticket_type")
    try:
        normalized_id = int(ticket_id)
    except (TypeError, ValueError) as exc:
        raise LiveChatAccessError("invalid_ticket_id") from exc
    if normalized_id <= 0:
        raise LiveChatAccessError("invalid_ticket_id")
    return f"ticket_{normalized_type}_{normalized_id}"


def _secret_key() -> str:
    secret = str(current_app.config.get("SECRET_KEY") or "").strip()
    if not secret:
        raise LiveChatAccessError("secret_key_not_configured")
    return secret


def issue_ticket_room_token(
    ticket_type: str,
    ticket_id: int | str,
    *,
    ttl_seconds: int | None = None,
) -> str:
    room = build_ticket_room(ticket_type, ticket_id)
    normalized_type = str(ticket_type).strip().lower()
    normalized_id = int(ticket_id)
    configured_ttl = ttl_seconds or current_app.config.get("LIVE_CHAT_ROOM_TOKEN_TTL_SECONDS", 14400)
    try:
        ttl = max(300, min(int(configured_ttl), 86400))
    except (TypeError, ValueError):
        ttl = 14400
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": LIVE_CHAT_TOKEN_ISSUER,
            "aud": LIVE_CHAT_TOKEN_AUDIENCE,
            "scope": LIVE_CHAT_TOKEN_SCOPE,
            "ticket_type": normalized_type,
            "ticket_id": normalized_id,
            "room": room,
            "iat": now,
            "exp": now + timedelta(seconds=ttl),
        },
        _secret_key(),
        algorithm="HS256",
    )
    return token.decode("utf-8") if isinstance(token, bytes) else str(token)


def verify_ticket_room_token(token: str, *, expected_room: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        raise LiveChatAccessError("missing_access_token")
    try:
        payload = jwt.decode(
            token.strip(),
            _secret_key(),
            algorithms=["HS256"],
            audience=LIVE_CHAT_TOKEN_AUDIENCE,
            issuer=LIVE_CHAT_TOKEN_ISSUER,
            options={
                "require": [
                    "aud",
                    "exp",
                    "iat",
                    "iss",
                    "room",
                    "scope",
                    "ticket_id",
                    "ticket_type",
                ]
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise LiveChatAccessError("expired_access_token") from exc
    except jwt.InvalidTokenError as exc:
        raise LiveChatAccessError("invalid_access_token") from exc

    ticket_type = str(payload.get("ticket_type") or "").strip().lower()
    ticket_id = payload.get("ticket_id")
    room = build_ticket_room(ticket_type, ticket_id)
    if payload.get("scope") != LIVE_CHAT_TOKEN_SCOPE:
        raise LiveChatAccessError("invalid_access_scope")
    if room != str(payload.get("room") or "") or room != str(expected_room or ""):
        raise LiveChatAccessError("ticket_room_mismatch")
    return {**payload, "ticket_type": ticket_type, "ticket_id": int(ticket_id), "room": room}


def attach_ticket_room_access(
    status: Mapping[str, Any] | None,
    *,
    ticket_type: str,
    ticket_id: int | str,
) -> dict[str, Any]:
    result = deepcopy(dict(status or {}))
    room = build_ticket_room(ticket_type, ticket_id)
    access_token = issue_ticket_room_token(ticket_type, ticket_id)
    transport = deepcopy(result.get("transport")) if isinstance(result.get("transport"), Mapping) else {}
    transport.update(
        {
            "socket_room": room,
            "access_token": access_token,
            "access_mode": "signed_ticket_room",
        }
    )
    result.update(
        {
            "socket_room": room,
            "access_token": access_token,
            "access_mode": "signed_ticket_room",
            "transport": transport,
        }
    )
    return result
