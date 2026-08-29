from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, Mapping, Sequence


HISTORY_PAGINATION_CONTRACT_VERSION = "conversation.history.cursor.v1"
DEFAULT_HISTORY_PAGE_LIMIT = 50
MAX_HISTORY_PAGE_LIMIT = 100


class HistoryPaginationError(ValueError):
    """Raised when a history cursor or page limit is not valid."""


def parse_history_page_params(args: Mapping[str, Any]) -> tuple[int, str | None]:
    raw_limit = args.get("limit")
    if raw_limit in (None, ""):
        limit = DEFAULT_HISTORY_PAGE_LIMIT
    else:
        try:
            limit = int(str(raw_limit))
        except (TypeError, ValueError) as exc:
            raise HistoryPaginationError("limit debe ser un entero") from exc
    if limit < 1 or limit > MAX_HISTORY_PAGE_LIMIT:
        raise HistoryPaginationError(
            f"limit debe estar entre 1 y {MAX_HISTORY_PAGE_LIMIT}"
        )

    raw_cursor = args.get("cursor")
    cursor = str(raw_cursor).strip() if raw_cursor not in (None, "") else None
    if cursor and len(cursor) > 1024:
        raise HistoryPaginationError("cursor invalido")
    if cursor:
        _decode_cursor(cursor)
    return limit, cursor


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _stable_item_id(item: Mapping[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    explicit = (
        item.get("id")
        or item.get("comment_id")
        or payload.get("id")
        or payload.get("comment_id")
        or payload.get("comentario_id")
    )
    if explicit not in (None, ""):
        return str(explicit)
    digest = hashlib.sha256(_stable_json(item).encode("utf-8")).hexdigest()[:24]
    return f"history:{digest}"


def _stable_item_timestamp(item: Mapping[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    return str(
        item.get("timestamp")
        or item.get("fecha")
        or item.get("created_at")
        or payload.get("timestamp")
        or payload.get("fecha")
        or payload.get("created_at")
        or ""
    )


def _item_key(item: Mapping[str, Any]) -> tuple[str, str]:
    return _stable_item_timestamp(item), _stable_item_id(item)


def _encode_cursor(key: tuple[str, str]) -> str:
    raw = _stable_json({"v": 1, "ts": key[0], "id": key[1]}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(f"{cursor}{padding}").decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise HistoryPaginationError("cursor invalido") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise HistoryPaginationError("cursor invalido")
    timestamp = payload.get("ts")
    item_id = payload.get("id")
    if not isinstance(timestamp, str) or not isinstance(item_id, str) or not item_id:
        raise HistoryPaginationError("cursor invalido")
    return timestamp, item_id


def paginate_history_items(
    items: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the newest eligible page in chronological order.

    The cursor points at the oldest item already delivered. New messages can be
    appended without moving that boundary, so subsequent requests remain
    stable while Socket.IO or polling updates the newest page.
    """

    cursor_key = _decode_cursor(cursor) if cursor else None
    normalized_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_item in items or []:
        if not isinstance(raw_item, Mapping):
            continue
        normalized = dict(raw_item)
        normalized.setdefault("id", _stable_item_id(normalized))
        normalized_by_key.setdefault(_item_key(normalized), normalized)

    ordered = [normalized_by_key[key] for key in sorted(normalized_by_key)]
    eligible = [item for item in ordered if cursor_key is None or _item_key(item) < cursor_key]
    page = eligible[-limit:]
    has_more = len(eligible) > len(page)
    next_cursor = _encode_cursor(_item_key(page[0])) if has_more and page else None
    pagination = {
        "contract_version": HISTORY_PAGINATION_CONTRACT_VERSION,
        "direction": "older",
        "order": "chronological_asc",
        "limit": limit,
        "returned_count": len(page),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }
    return page, pagination


def page_payloads(
    page: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project a unified page back into the legacy timeline/message fields."""

    timeline: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for item in page:
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
        normalized = dict(payload)
        source = str(item.get("source") or "")
        stream_type = str(item.get("stream_type") or normalized.get("tipo") or "")
        message_like = bool(
            stream_type == "message"
            or normalized.get("tipo") in {"comentario", "archivo"}
            or normalized.get("attachmentInfo")
            or normalized.get("attachments")
        )
        if message_like:
            messages.append(normalized)
        if source != "chat_history":
            timeline.append(normalized)
    return timeline, messages
