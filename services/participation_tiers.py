"""Participation levels: the more a person takes part, the higher their level.

Computed from the points *earned* (surveys, votes, claims, reports), never from
the balance, so redeeming a benefit does not lower anyone's level. Same idea as
tiered civic-rewards programmes: a visible next step encourages taking part.
"""
from __future__ import annotations

from typing import Any

TIERS: tuple[tuple[int, str], ...] = (
    (0, "Vecino"),
    (500, "Vecino activo"),
    (2000, "Referente del barrio"),
    (5000, "Embajador"),
)


def tier_for(earned: int) -> dict[str, Any]:
    earned = max(0, int(earned or 0))
    index = max(i for i, (floor, _) in enumerate(TIERS) if earned >= floor)
    floor, name = TIERS[index]
    following = TIERS[index + 1] if index + 1 < len(TIERS) else None
    progress = 100
    if following is not None:
        progress = int(round(100 * (earned - floor) / (following[0] - floor)))
    return {
        "level": name,
        "level_index": index,
        "levels": [label for _, label in TIERS],
        "earned_points": earned,
        "next_level": following[1] if following else None,
        "points_to_next_level": (following[0] - earned) if following else 0,
        "level_progress": max(0, min(100, progress)),
    }


def earned_points(user_id: int, tenant_id: int | None) -> int:
    from sqlalchemy import func

    from models import PointsTransaction, db

    query = db.session.query(func.coalesce(func.sum(PointsTransaction.delta), 0)).filter(
        PointsTransaction.user_id == int(user_id), PointsTransaction.delta > 0)
    if tenant_id is not None:
        query = query.filter(PointsTransaction.tenant_id == int(tenant_id))
    return int(query.scalar() or 0)
