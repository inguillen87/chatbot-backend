"""repair three evidence-backed legacy municipal ticket tenant scopes

Revision ID: 20260825_legacy_municipio_ticket_scope_repair_v1
Revises: 20260825_demo_survey_participation_v1
Create Date: 2026-08-25 18:00:00.000000

This is deliberately a bounded data repair, not a heuristic backfill.  The
three ticket IDs below were reconciled against the private Render media
inventory.  Their database attachments independently converge on the same
active municipal tenant through each municipal actor's unique institutional
owner.  An optional explicit actor tenant may corroborate that scope but may
never contradict it.

Anonymous, orphaned, ambiguous, or merely actor-associated tickets are not
assigned here.  Application reads quarantine any remaining municipal ticket
whose explicit tenant is not structurally municipal.
"""

from __future__ import annotations

from collections import Counter

from alembic import op
import sqlalchemy as sa


revision = "20260825_legacy_municipio_ticket_scope_repair_v1"
down_revision = "20260825_demo_survey_participation_v1"
branch_labels = None
depends_on = None


_EXPECTED_ATTACHMENT_COUNTS = {
    322: 15,
    344: 1,
    347: 1,
}
_SOURCE_TENANT_SLUG = "almacen"
_SOURCE_TENANT_TYPE = "pyme"
_TARGET_TENANT_SLUG = "junin"
_TARGET_TENANT_TYPE = "municipio"


class _RepairPreconditionError(RuntimeError):
    pass


def _fail(reason_code: str) -> None:
    raise _RepairPreconditionError(
        f"legacy_municipio_ticket_tenant_repair:{reason_code}"
    )


def _normalized(value: object) -> str:
    return str(value or "").strip().lower()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _load_required_tables(bind) -> dict[str, sa.Table]:
    required_columns = {
        "tenant_profile": {
            "id",
            "slug",
            "tipo",
            "municipio_id",
            "pyme_id",
            "is_active",
        },
        "user": {"id", "tenant_id", "tipo_chat", "empresa_id"},
        "municipio_ticket": {"id", "user_id", "municipio_id", "tenant_id"},
        "archivo_adjunto": {"id", "user_id", "municipio_ticket_id"},
    }
    inspector = sa.inspect(bind)
    available_tables = set(inspector.get_table_names())
    if not set(required_columns).issubset(available_tables):
        _fail("required_schema_missing")

    metadata = sa.MetaData()
    tables: dict[str, sa.Table] = {}
    for name, expected in required_columns.items():
        actual = {column["name"] for column in inspector.get_columns(name)}
        if not expected.issubset(actual):
            _fail("required_schema_missing")
        tables[name] = sa.Table(name, metadata, autoload_with=bind)
    return tables


def _lock_repair_scope(bind) -> None:
    if bind.dialect.name != "postgresql":
        return
    # The evidence check and update must observe one immutable snapshot.  A
    # short lock timeout fails the deployment closed instead of waiting while
    # an application writer changes an attachment, actor, tenant, or ticket.
    bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    bind.execute(
        sa.text(
            'LOCK TABLE tenant_profile, "user", municipio_ticket, '
            "archivo_adjunto IN SHARE MODE"
        )
    )


def _one_tenant_by_slug(bind, tenant_table: sa.Table, slug: str):
    rows = (
        bind.execute(
            sa.select(tenant_table).where(tenant_table.c.slug == slug)
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        _fail("tenant_anchor_changed")
    return rows[0]


def _validate_tenant_anchors(source, target) -> tuple[int, int, int]:
    source_id = _positive_int(source["id"])
    source_owner_id = _positive_int(source["pyme_id"])
    target_id = _positive_int(target["id"])
    target_owner_id = _positive_int(target["municipio_id"])
    if (
        source_id is None
        or source_owner_id is None
        or _normalized(source["slug"]) != _SOURCE_TENANT_SLUG
        or _normalized(source["tipo"]) != _SOURCE_TENANT_TYPE
        or source["municipio_id"] is not None
    ):
        _fail("source_tenant_contract_changed")
    if (
        target_id is None
        or target_owner_id is None
        or _normalized(target["slug"]) != _TARGET_TENANT_SLUG
        or _normalized(target["tipo"]) != _TARGET_TENANT_TYPE
        or target["pyme_id"] is not None
        or target["is_active"] not in (True, 1)
    ):
        _fail("target_tenant_contract_changed")
    if source_id == target_id:
        _fail("tenant_anchors_collide")
    return source_id, source_owner_id, target_id


def _validate_unique_actor_owner(
    bind,
    tenant_table: sa.Table,
    *,
    owner_id: int,
    target_id: int,
) -> None:
    candidate_ids = tuple(
        int(value)
        for value in bind.execute(
            sa.select(tenant_table.c.id)
            .where(
                sa.or_(
                    tenant_table.c.municipio_id == owner_id,
                    tenant_table.c.pyme_id == owner_id,
                )
            )
            .order_by(tenant_table.c.id.asc())
        ).scalars()
    )
    if candidate_ids != (target_id,):
        _fail("attachment_actor_owner_not_unique")


def upgrade() -> None:
    bind = op.get_bind()
    tables = _load_required_tables(bind)
    _lock_repair_scope(bind)

    tenants = tables["tenant_profile"]
    users = tables["user"]
    tickets = tables["municipio_ticket"]
    attachments = tables["archivo_adjunto"]
    expected_ticket_ids = tuple(sorted(_EXPECTED_ATTACHMENT_COUNTS))

    ticket_rows = (
        bind.execute(
            sa.select(tickets)
            .where(tickets.c.id.in_(expected_ticket_ids))
            .order_by(tickets.c.id.asc())
        )
        .mappings()
        .all()
    )
    # A fresh database has none of the historical source rows and needs no
    # repair.  A partial set is evidence drift and must never be guessed.
    if not ticket_rows:
        return
    if tuple(int(row["id"]) for row in ticket_rows) != expected_ticket_ids:
        _fail("expected_ticket_set_changed")

    source = _one_tenant_by_slug(bind, tenants, _SOURCE_TENANT_SLUG)
    target = _one_tenant_by_slug(bind, tenants, _TARGET_TENANT_SLUG)
    source_id, source_owner_id, target_id = _validate_tenant_anchors(source, target)
    target_owner_id = int(target["municipio_id"])

    states: list[str] = []
    for row in ticket_rows:
        if _positive_int(row["municipio_id"]) != source_owner_id:
            _fail("legacy_owner_anchor_changed")
        current_tenant_id = _positive_int(row["tenant_id"])
        if current_tenant_id == source_id:
            states.append("source")
        elif current_tenant_id == target_id:
            states.append("target")
        else:
            _fail("ticket_scope_state_changed")

    attachment_rows = (
        bind.execute(
            sa.select(attachments)
            .where(attachments.c.municipio_ticket_id.in_(expected_ticket_ids))
            .order_by(
                attachments.c.municipio_ticket_id.asc(),
                attachments.c.id.asc(),
            )
        )
        .mappings()
        .all()
    )
    observed_counts = Counter(
        int(row["municipio_ticket_id"]) for row in attachment_rows
    )
    if dict(observed_counts) != _EXPECTED_ATTACHMENT_COUNTS:
        _fail("attachment_evidence_count_changed")

    actor_ids = {_positive_int(row["user_id"]) for row in attachment_rows}
    if None in actor_ids:
        _fail("attachment_actor_missing")
    normalized_actor_ids = tuple(sorted(int(value) for value in actor_ids))
    actor_rows = {
        int(row["id"]): row
        for row in bind.execute(
            sa.select(users).where(users.c.id.in_(normalized_actor_ids))
        )
        .mappings()
        .all()
    }
    if set(actor_rows) != set(normalized_actor_ids):
        _fail("attachment_actor_orphaned")

    validated_owner_ids: set[int] = set()
    for actor in actor_rows.values():
        actor_owner_id = _positive_int(actor["empresa_id"])
        explicit_actor_tenant_id = actor["tenant_id"]
        if (
            _normalized(actor["tipo_chat"]) != _TARGET_TENANT_TYPE
            or actor_owner_id != target_owner_id
            or (
                explicit_actor_tenant_id is not None
                and _positive_int(explicit_actor_tenant_id) != target_id
            )
        ):
            _fail("attachment_actor_scope_conflict")
        if actor_owner_id not in validated_owner_ids:
            _validate_unique_actor_owner(
                bind,
                tenants,
                owner_id=actor_owner_id,
                target_id=target_id,
            )
            validated_owner_ids.add(actor_owner_id)

    if states == ["target"] * len(expected_ticket_ids):
        return
    if states != ["source"] * len(expected_ticket_ids):
        _fail("partial_repair_state_detected")

    result = bind.execute(
        tickets.update()
        .where(
            sa.and_(
                tickets.c.id.in_(expected_ticket_ids),
                tickets.c.tenant_id == source_id,
                tickets.c.municipio_id == source_owner_id,
            )
        )
        .values(tenant_id=target_id)
    )
    if result.rowcount != len(expected_ticket_ids):
        _fail("concurrent_ticket_change_detected")


def downgrade() -> None:
    # The old tenant values are proven cross-domain assignments.  Reinstating
    # them would recreate unauthorized PYME visibility, so this repair is an
    # intentionally irreversible data correction.
    pass
