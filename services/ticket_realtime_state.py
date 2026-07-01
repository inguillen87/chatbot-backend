from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from collections import defaultdict

from models import TicketComentario, TicketRealtimeState, db
from utils.time_utils import get_local_now, datetime_to_iso_utc


logger = logging.getLogger(__name__)

PRESENCE_ACTIVE_WINDOW_MINUTES = 5
PRESENCE_IDLE_WINDOW_MINUTES = 15
PRESENCE_STALE_RETENTION_HOURS = 24
PRESENCE_PRUNE_INTERVAL_SECONDS = 60
_last_prune_at = None


def build_viewer_key(*, user_id: Any = None, anon_id: Any = None, pin: Any = None) -> str | None:
    if user_id:
        return f"user:{user_id}"
    if anon_id:
        return f"anon:{anon_id}"
    if pin:
        return f"pin:{pin}"
    return None


def upsert_ticket_presence(*, ticket_type: str, ticket_id: int, viewer_key: str, viewer_user_id: int | None = None, viewer_anon_id: str | None = None, viewer_role: str | None = None, active_session_id: str | None = None, presence_status: str = "active") -> TicketRealtimeState:
    global _last_prune_at
    now = get_local_now()
    should_prune = (
        _last_prune_at is None
        or (now - _last_prune_at).total_seconds() >= PRESENCE_PRUNE_INTERVAL_SECONDS
    )
    if should_prune:
        prune_stale_ticket_realtime_states(now=now)
        _last_prune_at = now
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


def prune_stale_ticket_realtime_states(*, now=None, retention_hours: int = PRESENCE_STALE_RETENTION_HOURS) -> int:
    now = now or get_local_now()
    cutoff = now - timedelta(hours=max(int(retention_hours or PRESENCE_STALE_RETENTION_HOURS), 1))
    deleted = (
        TicketRealtimeState.query
        .filter(
            TicketRealtimeState.last_presence_at.isnot(None),
            TicketRealtimeState.last_presence_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


def _viewer_identity_key(row: TicketRealtimeState) -> tuple[str, str | int]:
    if row.viewer_user_id:
        return ("user", row.viewer_user_id)
    if row.viewer_anon_id:
        return ("anon", row.viewer_anon_id)
    return ("viewer_key", row.viewer_key)


def _dedupe_rows(rows: list[TicketRealtimeState]) -> list[TicketRealtimeState]:
    selected: dict[tuple[str, str | int], TicketRealtimeState] = {}
    for row in rows:
        identity = _viewer_identity_key(row)
        current = selected.get(identity)
        if current is None:
            selected[identity] = row
            continue
        current_dt = _normalize_presence_dt(current.last_presence_at or current.updated_at, get_local_now())
        row_dt = _normalize_presence_dt(row.last_presence_at or row.updated_at, get_local_now())
        if (row_dt or get_local_now()) >= (current_dt or get_local_now()):
            selected[identity] = row
    return list(selected.values())


def _empty_ticket_realtime_summary(*, ticket_type: str, ticket_id: int, reason_code: str) -> dict[str, Any]:
    return {
        "presence": {
            "active_count": 0,
            "active_viewers": [],
            "idle_count": 0,
            "idle_viewers": [],
            "active_window_minutes": PRESENCE_ACTIVE_WINDOW_MINUTES,
            "idle_window_minutes": PRESENCE_IDLE_WINDOW_MINUTES,
        },
        "read_state": {
            "latest_comment_id": 0,
            "viewers": [],
            "unread_viewers": [],
            "unread_viewer_count": 0,
            "latest_read_at": None,
        },
        "meta": {
            "generated_at": datetime_to_iso_utc(get_local_now()),
            "viewer_rows_considered": 0,
            "stale_retention_hours": PRESENCE_STALE_RETENTION_HOURS,
            "degraded": True,
            "retryable": True,
            "reason_code": reason_code,
            "ticket_type": ticket_type,
            "ticket_id": ticket_id,
        },
    }


def _build_ticket_realtime_summary_unchecked(*, ticket_type: str, ticket_id: int) -> dict[str, Any]:
    now = get_local_now()
    active_cutoff = now - timedelta(minutes=PRESENCE_ACTIVE_WINDOW_MINUTES)
    prune_stale_ticket_realtime_states(now=now)
    rows = TicketRealtimeState.query.filter_by(ticket_type=ticket_type, ticket_id=ticket_id).all()
    rows = _dedupe_rows(rows)
    comment_filter = (
        TicketComentario.municipio_ticket_id == ticket_id
        if ticket_type == "municipio"
        else TicketComentario.pyme_ticket_id == ticket_id
    )
    latest_comment_id = (
        db.session.query(db.func.max(TicketComentario.id))
        .filter(comment_filter)
        .scalar()
        or 0
    )
    total_comments = int(
        db.session.query(db.func.count(TicketComentario.id))
        .filter(comment_filter)
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
        if latest_comment_id and last_read_comment_id:
            unread_count = (
                db.session.query(db.func.count(TicketComentario.id))
                .filter(comment_filter, TicketComentario.id > int(last_read_comment_id))
                .scalar()
                or 0
            )
        elif latest_comment_id:
            unread_count = total_comments
        else:
            unread_count = 0
        row_dict["unread_count"] = int(unread_count)
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
            "viewer_rows_considered": len(rows),
            "stale_retention_hours": PRESENCE_STALE_RETENTION_HOURS,
        },
    }


def build_ticket_realtime_summary(*, ticket_type: str, ticket_id: int) -> dict[str, Any]:
    try:
        return _build_ticket_realtime_summary_unchecked(ticket_type=ticket_type, ticket_id=ticket_id)
    except Exception as exc:
        logger.warning(
            "Ticket realtime summary degraded for %s ticket %s: %s",
            ticket_type,
            ticket_id,
            exc,
            exc_info=True,
        )
        return _empty_ticket_realtime_summary(
            ticket_type=ticket_type,
            ticket_id=ticket_id,
            reason_code="ticket_realtime_summary_unavailable",
        )


def build_ticket_collaboration_states(
    *,
    ticket_type: str,
    ticket_ids: list[int],
    latest_comment_ids: dict[int, int] | None = None,
    comment_counts: dict[int, int] | None = None,
) -> dict[int, dict[str, Any]]:
    """Return lightweight collaboration state for many tickets in one DB pass."""

    unique_ids = sorted({int(ticket_id) for ticket_id in ticket_ids if ticket_id is not None})
    if not unique_ids:
        return {}

    now = get_local_now()
    active_cutoff = now - timedelta(minutes=PRESENCE_ACTIVE_WINDOW_MINUTES)
    prune_stale_ticket_realtime_states(now=now)

    latest_comment_ids = dict(latest_comment_ids or {})
    comment_counts = dict(comment_counts or {})

    missing_stats_ids = [
        ticket_id
        for ticket_id in unique_ids
        if ticket_id not in latest_comment_ids or ticket_id not in comment_counts
    ]
    if missing_stats_ids:
        comment_column = (
            TicketComentario.municipio_ticket_id
            if ticket_type == "municipio"
            else TicketComentario.pyme_ticket_id
        )
        rows = (
            db.session.query(
                comment_column.label("ticket_id"),
                db.func.max(TicketComentario.id).label("latest_comment_id"),
                db.func.count(TicketComentario.id).label("comment_count"),
            )
            .filter(comment_column.in_(missing_stats_ids))
            .group_by(comment_column)
            .all()
        )
        for row in rows:
            latest_comment_ids[int(row.ticket_id)] = int(row.latest_comment_id or 0)
            comment_counts[int(row.ticket_id)] = int(row.comment_count or 0)

    realtime_rows = (
        TicketRealtimeState.query
        .filter(
            TicketRealtimeState.ticket_type == ticket_type,
            TicketRealtimeState.ticket_id.in_(unique_ids),
        )
        .all()
    )

    rows_by_ticket: dict[int, list[TicketRealtimeState]] = defaultdict(list)
    for row in realtime_rows:
        rows_by_ticket[int(row.ticket_id)].append(row)

    states: dict[int, dict[str, Any]] = {}
    for ticket_id in unique_ids:
        latest_comment_id = int(latest_comment_ids.get(ticket_id) or 0)
        total_comments = int(comment_counts.get(ticket_id) or 0)
        active_count = 0
        idle_count = 0
        unread_count = 0
        latest_read_at = None

        for row in _dedupe_rows(rows_by_ticket.get(ticket_id, [])):
            last_presence_at = _normalize_presence_dt(row.last_presence_at, now)
            effective_presence_status = _derive_effective_presence_status(
                stored_status=row.presence_status,
                last_presence_at=last_presence_at,
                now=now,
            )
            if (
                effective_presence_status == "active"
                and last_presence_at
                and last_presence_at >= active_cutoff
            ):
                active_count += 1
            elif effective_presence_status == "idle":
                idle_count += 1

            last_read_comment_id = int(row.last_read_comment_id or 0)
            if latest_comment_id and (not last_read_comment_id or latest_comment_id > last_read_comment_id):
                unread_count += 1

            row_last_read_at = row.last_read_at
            if row_last_read_at and (latest_read_at is None or row_last_read_at > latest_read_at):
                latest_read_at = row_last_read_at

        status = "healthy"
        if unread_count > 0:
            status = "attention_needed"
        if unread_count > 0 and active_count > 0:
            status = "actively_managed"

        states[ticket_id] = {
            "active_viewers_count": active_count,
            "idle_viewers_count": idle_count,
            "unread_viewer_count": unread_count,
            "latest_comment_id": latest_comment_id,
            "latest_read_at": datetime_to_iso_utc(latest_read_at),
            "active_window_minutes": PRESENCE_ACTIVE_WINDOW_MINUTES,
            "idle_window_minutes": PRESENCE_IDLE_WINDOW_MINUTES,
            "operational_status": status,
            "collaboration_hint": f"{active_count} activos / {idle_count} idle / {unread_count} unread",
            "comment_count": total_comments,
        }

    return states


def build_ticket_collaboration_state(*, ticket_type: str, ticket_id: int) -> dict[str, Any]:
    summary = build_ticket_realtime_summary(ticket_type=ticket_type, ticket_id=ticket_id)
    unread_count = summary["read_state"]["unread_viewer_count"]
    active_count = summary["presence"]["active_count"]
    status = "healthy"
    if unread_count > 0:
        status = "attention_needed"
    if unread_count > 0 and active_count > 0:
        status = "actively_managed"
    return {
        "active_viewers_count": summary["presence"]["active_count"],
        "idle_viewers_count": summary["presence"]["idle_count"],
        "unread_viewer_count": summary["read_state"]["unread_viewer_count"],
        "latest_comment_id": summary["read_state"]["latest_comment_id"],
        "latest_read_at": summary["read_state"]["latest_read_at"],
        "active_window_minutes": summary["presence"]["active_window_minutes"],
        "idle_window_minutes": summary["presence"]["idle_window_minutes"],
        "operational_status": status,
        "collaboration_hint": f"{active_count} activos · {summary['presence']['idle_count']} idle · {unread_count} unread",
    }
