from __future__ import annotations

from datetime import timedelta
from typing import Any

from models import TicketComentario, TicketRealtimeState, db
from utils.time_utils import get_local_now, datetime_to_iso_utc


PRESENCE_ACTIVE_WINDOW_MINUTES = 5
PRESENCE_IDLE_WINDOW_MINUTES = 15


def build_viewer_key(*, user_id: Any = None, anon_id: Any = None, pin: Any = None) -> str | None:
    if user_id:
        return f"user:{user_id}"
    if anon_id:
        return f"anon:{anon_id}"
    if pin:
        return f"pin:{pin}"
    return None


def upsert_ticket_presence(*, ticket_type: str, ticket_id: int, viewer_key: str, viewer_user_id: int | None = None, viewer_anon_id: str | None = None, viewer_role: str | None = None, active_session_id: str | None = None, presence_status: str = "active") -> TicketRealtimeState:
    state = TicketRealtimeState.query.filter_by(
        ticket_type=ticket_type,
        ticket_id=ticket_id,
        viewer_key=viewer_key,
    ).first()
    if not state:
        state = TicketRealtimeState(
            ticket_type=ticket_type,
            ticket_id=ticket_id,
            viewer_key=viewer_key,
        )
        db.session.add(state)

    state.viewer_user_id = viewer_user_id
    state.viewer_anon_id = viewer_anon_id
    state.viewer_role = viewer_role
    state.active_session_id = active_session_id
    state.presence_status = presence_status or "active"
    state.last_presence_at = get_local_now()
    return state


def mark_ticket_read(*, ticket_type: str, ticket_id: int, viewer_key: str, last_read_comment_id: int | None, viewer_user_id: int | None = None, viewer_anon_id: str | None = None, viewer_role: str | None = None, active_session_id: str | None = None) -> TicketRealtimeState:
    state = upsert_ticket_presence(
        ticket_type=ticket_type,
        ticket_id=ticket_id,
        viewer_key=viewer_key,
        viewer_user_id=viewer_user_id,
        viewer_anon_id=viewer_anon_id,
        viewer_role=viewer_role,
        active_session_id=active_session_id,
        presence_status="active",
    )
    state.last_read_comment_id = last_read_comment_id
    state.last_read_at = get_local_now()
    return state


def _normalize_presence_dt(value, now):
    if value and getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=now.tzinfo)
    return value


def _derive_effective_presence_status(*, stored_status: str | None, last_presence_at, now) -> str:
    if not last_presence_at:
        return "inactive"

    last_presence_at = _normalize_presence_dt(last_presence_at, now)
    inactive_cutoff = now - timedelta(minutes=PRESENCE_IDLE_WINDOW_MINUTES)
    active_cutoff = now - timedelta(minutes=PRESENCE_ACTIVE_WINDOW_MINUTES)

    if stored_status == "inactive" or last_presence_at < inactive_cutoff:
        return "inactive"
    if stored_status == "idle" or last_presence_at < active_cutoff:
        return "idle"
    return "active"


def build_ticket_realtime_summary(*, ticket_type: str, ticket_id: int) -> dict[str, Any]:
    now = get_local_now()
    active_cutoff = now - timedelta(minutes=PRESENCE_ACTIVE_WINDOW_MINUTES)
    rows = TicketRealtimeState.query.filter_by(ticket_type=ticket_type, ticket_id=ticket_id).all()
    latest_comment_id = (
        db.session.query(db.func.max(TicketComentario.id))
        .filter(
            TicketComentario.municipio_ticket_id == ticket_id if ticket_type == "municipio" else TicketComentario.pyme_ticket_id == ticket_id
        )
        .scalar()
        or 0
    )

    active_viewers = []
    idle_viewers = []
    read_states = []
    for row in rows:
        last_presence_at = _normalize_presence_dt(row.last_presence_at, now)
        effective_presence_status = _derive_effective_presence_status(
            stored_status=row.presence_status,
            last_presence_at=last_presence_at,
            now=now,
        )
        is_active = bool(effective_presence_status == "active" and last_presence_at and last_presence_at >= active_cutoff)
        row_dict = row.to_dict()
        row_dict["is_active"] = is_active
        row_dict["effective_presence_status"] = effective_presence_status
        last_read_comment_id = row.last_read_comment_id or 0
        row_dict["latest_comment_id"] = latest_comment_id
        row_dict["unread_count"] = max(int(latest_comment_id) - int(last_read_comment_id), 0)
        row_dict["has_unread"] = row_dict["unread_count"] > 0
        read_states.append(row_dict)
        if is_active:
            active_viewers.append(row_dict)
        elif effective_presence_status == "idle":
            idle_viewers.append(row_dict)

    unread_viewers = [item for item in read_states if item.get("has_unread")]

    return {
        "presence": {
            "active_count": len(active_viewers),
            "active_viewers": active_viewers,
            "idle_count": len(idle_viewers),
            "idle_viewers": idle_viewers,
            "active_window_minutes": PRESENCE_ACTIVE_WINDOW_MINUTES,
            "idle_window_minutes": PRESENCE_IDLE_WINDOW_MINUTES,
        },
        "read_state": {
            "latest_comment_id": latest_comment_id,
            "viewers": read_states,
            "unread_viewers": unread_viewers,
            "unread_viewer_count": len(unread_viewers),
            "latest_read_at": max(
                (item["last_read_at"] for item in read_states if item.get("last_read_at")),
                default=None,
            ),
        },
        "meta": {
            "generated_at": datetime_to_iso_utc(now),
        },
    }


def build_ticket_collaboration_state(*, ticket_type: str, ticket_id: int) -> dict[str, Any]:
    summary = build_ticket_realtime_summary(ticket_type=ticket_type, ticket_id=ticket_id)
    return {
        "active_viewers_count": summary["presence"]["active_count"],
        "idle_viewers_count": summary["presence"]["idle_count"],
        "unread_viewer_count": summary["read_state"]["unread_viewer_count"],
        "latest_comment_id": summary["read_state"]["latest_comment_id"],
        "latest_read_at": summary["read_state"]["latest_read_at"],
        "active_window_minutes": summary["presence"]["active_window_minutes"],
        "idle_window_minutes": summary["presence"]["idle_window_minutes"],
    }
