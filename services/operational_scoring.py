from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


DEFAULT_TICKET_PRIORITY_CONFIG: dict[str, Any] = {
    "sla_breached_weight": 50,
    "unread_weight": 15,
    "active_viewer_weight": 10,
    "idle_viewer_weight": 5,
    "stage_weights": {
        "nuevo": 10,
        "contactado": 10,
        "en_proceso": 5,
    },
}


def _normalize_ticket_priority_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = deepcopy(DEFAULT_TICKET_PRIORITY_CONFIG)
    incoming = config or {}

    for key in ("sla_breached_weight", "unread_weight", "active_viewer_weight", "idle_viewer_weight"):
        if incoming.get(key) is None:
            continue
        try:
            normalized[key] = int(incoming.get(key))
        except (TypeError, ValueError):
            continue

    stage_weights = incoming.get("stage_weights")
    if isinstance(stage_weights, dict):
        for raw_stage, raw_weight in stage_weights.items():
            try:
                normalized["stage_weights"][str(raw_stage).strip().lower()] = int(raw_weight)
            except (TypeError, ValueError):
                continue

    return normalized


def build_ticket_priority_score(
    *,
    sla_breached_flag: bool,
    collaboration_state: dict[str, Any] | None,
    stage: str | None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = _normalize_ticket_priority_config(config)
    collaboration_state = collaboration_state or {}
    stage_key = str(stage or "nuevo").strip().lower()

    unread_viewers = int(collaboration_state.get("unread_viewer_count", 0) or 0)
    active_viewers = int(collaboration_state.get("active_viewers_count", 0) or 0)
    idle_viewers = int(collaboration_state.get("idle_viewers_count", 0) or 0)

    breakdown = {
        "sla_breached": cfg["sla_breached_weight"] if sla_breached_flag else 0,
        "unread_viewers": unread_viewers * cfg["unread_weight"],
        "active_viewers": active_viewers * cfg["active_viewer_weight"],
        "idle_viewers": idle_viewers * cfg["idle_viewer_weight"],
        "stage_bonus": int(cfg["stage_weights"].get(stage_key, 0) or 0),
    }
    score = int(sum(breakdown.values()))

    reasons = [
        {
            "kind": key,
            "score": value,
        }
        for key, value in breakdown.items()
        if value > 0
    ]

    return {
        "score": score,
        "breakdown": breakdown,
        "reasons": reasons,
        "config": cfg,
    }


def build_lead_portfolio_score(
    *,
    latest_message: str,
    has_email: bool,
    has_phone: bool,
    open_tickets: int,
    last_seen: datetime | None,
) -> dict[str, Any]:
    text = str(latest_message or "").strip().lower()
    breakdown = {
        "open_ticket_pressure": min(max(int(open_tickets or 0), 0) * 7, 28),
        "has_email": 12 if has_email else 0,
        "has_phone": 18 if has_phone else 0,
        "urgency_keywords": 20 if any(word in text for word in ("urgente", "ya", "ahora", "problema", "caido", "caída", "error")) else 0,
        "buying_intent": 12 if any(word in text for word in ("precio", "presupuesto", "propuesta", "contratar", "plan full", "demo")) else 0,
        "recency": 0,
    }
    if last_seen:
        now = datetime.now(timezone.utc)
        last_dt = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
        age_hours = max(0, (now - last_dt).total_seconds() / 3600)
        if age_hours <= 0.5:
            breakdown["recency"] = 12
        elif age_hours <= 2:
            breakdown["recency"] = 8
        elif age_hours <= 24:
            breakdown["recency"] = 4

    score = int(min(sum(breakdown.values()), 100))
    reasons = [{"kind": key, "score": value} for key, value in breakdown.items() if value > 0]
    return {
        "score": score,
        "breakdown": breakdown,
        "reasons": reasons,
    }
