"""Canonicalize historical survey tenant namespaces.

Revision ID: 20260814_survey_scope_canonical_v1
Revises: 20260802_whatsapp_workflow_v1
Create Date: 2026-08-14

Historical survey rows could be stored under a municipality/business owner
id.  The canonical namespace is now always ``TenantProfile.id``.  This
migration physically rewrites unambiguous legacy surveys only when they have no
participation or evidentiary history, and retains the old integer in
``encuestas_tenant_id`` only as an inbound compatibility alias.

Responses, anchors and immutable governance/materialization/receipt ledgers
are deliberately not rewritten: their fingerprints and hashes include tenant
identity. If one is attached to a legacy namespace, deployment fails closed so
the data can receive a bespoke, audited migration instead of invalidating an
evidentiary record or reopening duplicate participation.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260814_survey_scope_canonical_v1"
down_revision = "20260802_whatsapp_workflow_v1"
branch_labels = None
depends_on = None


_IMMUTABLE_DEPENDENCIES = (
    "survey_governance_release",
    "survey_draft_materialization",
    "survey_response_receipt",
    "survey_response_effect_outbox",
    "survey_eligibility_grant",
    "survey_eligibility_terminal",
)

_SURVEY_SCOPE_LOCK_TABLES = (
    "tenant_profile",
    "enc_encuesta",
    "enc_respuesta",
    "enc_anchor_snapshot",
    *_IMMUTABLE_DEPENDENCIES,
)

# Stable application-level lock reserved for the physical survey namespace
# canonicalization.  It prevents a second deploy/migration process from
# running the preflight and rewrite concurrently.
_POSTGRES_ADVISORY_LOCK_ID = 91_610_393_044_313


def _has_table(connection, table_name: str) -> bool:
    return sa.inspect(connection).has_table(table_name)


def _lock_survey_scope_writers(connection) -> None:
    """Freeze survey identity/history writers for the migration transaction."""

    if connection.dialect.name != "postgresql":
        return

    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _POSTGRES_ADVISORY_LOCK_ID},
    )
    for table_name in _SURVEY_SCOPE_LOCK_TABLES:
        if _has_table(connection, table_name):
            # Names come exclusively from the static allowlist above.
            connection.execute(
                sa.text(f"LOCK TABLE {table_name} IN SHARE ROW EXCLUSIVE MODE")
            )


def _legacy_scope_ids(connection) -> list[int]:
    rows = connection.execute(
        sa.text(
            "SELECT DISTINCT tenant_id FROM enc_encuesta "
            "WHERE tenant_id IS NOT NULL ORDER BY tenant_id"
        )
    ).all()
    return [int(row[0]) for row in rows]


def _tenant_candidates(connection, scope_id: int) -> tuple[set[int], set[int], set[int]]:
    direct_ids = {
        int(row[0])
        for row in connection.execute(
            sa.text("SELECT id FROM tenant_profile WHERE id = :scope_id"),
            {"scope_id": scope_id},
        ).all()
    }
    alias_ids = {
        int(row[0])
        for row in connection.execute(
            sa.text(
                "SELECT id FROM tenant_profile "
                "WHERE encuestas_tenant_id = :scope_id ORDER BY id"
            ),
            {"scope_id": scope_id},
        ).all()
    }
    owner_ids = {
        int(row[0])
        for row in connection.execute(
            sa.text(
                "SELECT id FROM tenant_profile "
                "WHERE municipio_id = :scope_id OR pyme_id = :scope_id ORDER BY id"
            ),
            {"scope_id": scope_id},
        ).all()
    }
    return direct_ids, alias_ids, owner_ids


def _resolve_canonical_tenant_id(connection, scope_id: int) -> int:
    direct_ids, alias_ids, owner_ids = _tenant_candidates(connection, scope_id)

    # A stored enc_encuesta.tenant_id is, by schema contract, a
    # TenantProfile primary key.  Legacy owner ids were only a fallback used by
    # older writers, so an exact primary-key match has precedence over that
    # heuristic.  An explicit compatibility alias is also durable evidence and
    # must agree with the primary key when both are present.
    if direct_ids:
        direct_id = next(iter(direct_ids))
        conflicting_aliases = alias_ids - {direct_id}
        if conflicting_aliases:
            raise RuntimeError(
                "Ambiguous survey tenant namespace has conflicting canonical "
                f"and alias owners: scope={scope_id} direct={sorted(direct_ids)} "
                f"aliases={sorted(alias_ids)} owners={sorted(owner_ids)}"
            )
        return direct_id

    if alias_ids:
        if len(alias_ids) != 1:
            raise RuntimeError(
                "Ambiguous survey tenant namespace has multiple explicit aliases: "
                f"scope={scope_id} aliases={sorted(alias_ids)} "
                f"owners={sorted(owner_ids)}"
            )
        return next(iter(alias_ids))

    if not owner_ids:
        raise RuntimeError(
            f"Orphaned survey tenant namespace {scope_id}: no TenantProfile owner"
        )
    if len(owner_ids) != 1:
        raise RuntimeError(
            "Ambiguous survey tenant namespace has multiple legacy owners: "
            f"scope={scope_id} owners={sorted(owner_ids)}"
        )
    return next(iter(owner_ids))


def _count(connection, sql: str, params: dict[str, int]) -> int:
    return int(connection.execute(sa.text(sql), params).scalar_one() or 0)


def _assert_no_response_or_anchor_history(connection, scope_id: int) -> None:
    params = {"scope_id": scope_id}
    responses = _count(
        connection,
        "SELECT count(*) FROM enc_respuesta AS r "
        "JOIN enc_encuesta AS e ON e.id = r.encuesta_id "
        "WHERE e.tenant_id = :scope_id",
        params,
    )
    if responses:
        raise RuntimeError(
            "Legacy survey namespace has response identity history: "
            f"scope={scope_id} rows={responses}"
        )

    if _has_table(connection, "enc_anchor_snapshot"):
        anchors = _count(
            connection,
            "SELECT count(*) FROM enc_anchor_snapshot AS a "
            "JOIN enc_encuesta AS e ON e.id = a.encuesta_id "
            "WHERE e.tenant_id = :scope_id",
            params,
        )
        if anchors:
            raise RuntimeError(
                "Legacy survey namespace has cryptographic anchor history: "
                f"scope={scope_id} rows={anchors}"
            )


def _assert_no_immutable_dependencies(connection, scope_id: int) -> None:
    params = {"scope_id": scope_id}
    for table_name in _IMMUTABLE_DEPENDENCIES:
        if not _has_table(connection, table_name):
            continue
        dependent_rows = _count(
            connection,
            f"SELECT count(*) FROM {table_name} "
            "WHERE survey_id IN "
            "(SELECT id FROM enc_encuesta WHERE tenant_id = :scope_id)",
            params,
        )
        if dependent_rows:
            raise RuntimeError(
                "Legacy survey namespace has immutable dependencies: "
                f"scope={scope_id} table={table_name} rows={dependent_rows}"
            )


def _reserve_legacy_alias(connection, *, tenant_id: int, scope_id: int) -> None:
    existing = connection.execute(
        sa.text(
            "SELECT encuestas_tenant_id FROM tenant_profile WHERE id = :tenant_id"
        ),
        {"tenant_id": tenant_id},
    ).scalar_one()
    if existing not in (None, scope_id):
        raise RuntimeError(
            "Canonical tenant already has a different survey alias: "
            f"tenant={tenant_id} existing={existing} incoming={scope_id}"
        )
    if existing is None:
        result = connection.execute(
            sa.text(
                "UPDATE tenant_profile SET encuestas_tenant_id = :scope_id "
                "WHERE id = :tenant_id AND encuestas_tenant_id IS NULL"
            ),
            {"scope_id": scope_id, "tenant_id": tenant_id},
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "Survey alias reservation lost its compare-and-set guard: "
                f"tenant={tenant_id} scope={scope_id}"
            )


def _migrate_legacy_scope(connection, *, scope_id: int, tenant_id: int) -> None:
    if scope_id == tenant_id:
        return

    _assert_no_response_or_anchor_history(connection, scope_id)
    _assert_no_immutable_dependencies(connection, scope_id)
    _reserve_legacy_alias(connection, tenant_id=tenant_id, scope_id=scope_id)

    params = {"scope_id": scope_id, "tenant_id": tenant_id}
    survey_count = _count(
        connection,
        "SELECT count(*) FROM enc_encuesta WHERE tenant_id = :scope_id",
        params,
    )

    update_result = connection.execute(
        sa.text(
            "UPDATE enc_encuesta SET tenant_id = :tenant_id "
            "WHERE tenant_id = :scope_id"
        ),
        params,
    )
    if update_result.rowcount != survey_count:
        raise RuntimeError(
            "Survey namespace canonicalization rowcount mismatch: "
            f"legacy={scope_id} canonical={tenant_id} "
            f"updated={update_result.rowcount} expected={survey_count}"
        )

    remaining = _count(
        connection,
        "SELECT count(*) FROM enc_encuesta WHERE tenant_id = :scope_id",
        params,
    )
    moved = _count(
        connection,
        "SELECT count(*) FROM enc_encuesta WHERE tenant_id = :tenant_id",
        params,
    )
    if remaining or moved < survey_count:
        raise RuntimeError(
            "Survey namespace canonicalization postcondition failed: "
            f"legacy={scope_id} canonical={tenant_id} "
            f"remaining={remaining} expected_moved={survey_count} canonical_rows={moved}"
        )


def _canonicalize_survey_tenant_scopes(connection) -> None:
    _lock_survey_scope_writers(connection)
    for scope_id in _legacy_scope_ids(connection):
        tenant_id = _resolve_canonical_tenant_id(connection, scope_id)
        _migrate_legacy_scope(
            connection,
            scope_id=scope_id,
            tenant_id=tenant_id,
        )


def upgrade():
    _canonicalize_survey_tenant_scopes(op.get_bind())


def downgrade():
    # This is an identity repair. Reintroducing owner ids as storage namespaces
    # would recreate the cross-tenant ambiguity this migration removes.
    pass
