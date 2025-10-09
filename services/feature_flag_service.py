"""Helpers for tenant-specific feature toggles."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from extensions import db
from models import FeatureToggle

_TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled"}


def _coerce_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def get_feature_toggle(owner_id: Optional[int], key: str) -> Optional[bool]:
    """Return the boolean value for a feature toggle or ``None`` if unset."""

    if owner_id is None:
        return None

    stmt = (
        select(FeatureToggle.value)
        .where(FeatureToggle.owner_id == owner_id)
        .where(FeatureToggle.key == key)
        .limit(1)
    )
    result = db.session.execute(stmt).scalar_one_or_none()
    return _coerce_bool(result)


def encuestas_habilitadas(owner_id: Optional[int]) -> bool:
    """Return True when the 'encuestas' feature flag is explicitly enabled."""

    toggle = get_feature_toggle(owner_id, "encuestas")
    return bool(toggle) if toggle is not None else False
