"""Shared SQLAlchemy types and mixins without concrete model imports.

Model modules may import these primitives before the aggregate ``models``
module registers every mapped class. Keep a single definition so existing
imports through ``models`` and direct model imports share the same objects.
"""

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON

from database import db


JSONType = JSONB().with_variant(SQLITE_JSON, "sqlite")


class TimestampMixin:
    """Mixin providing created/updated timestamps compatible with UTC."""

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
