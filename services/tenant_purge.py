"""Scoped, previewable hard purge of exactly one tenant.

Across the schema ``municipio_id`` and ``pyme_id`` hold the *owner user* id,
never the tenant id.  A purge therefore scopes rows by ``tenant_id`` and by the
owners that belong to this tenant alone, and it refuses anything that would
reach another organisation.  The caller owns the transaction: rolling it back
turns the same statements into an exact preview.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from sqlalchemy import Integer, String, false, func, or_, select

CONTRACT_VERSION = "chatboc.tenant_purge.v1"
MAX_CASCADE_DEPTH = 10
# A reference through one of these names means "this row belongs to it".
_OWNERSHIP_COLUMNS = frozenset({"tenant_id", "user_id", "municipio_id", "pyme_id"})
_OWNER_SCOPE_COLUMNS = ("municipio_id", "pyme_id")


class TenantPurgeError(RuntimeError):
    """A fixed, non-sensitive reason the purge refuses to continue."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _references(metadata) -> dict[str, list[tuple[Any, Any, Any]]]:
    references: dict[str, list[tuple[Any, Any, Any]]] = {}
    for table in metadata.tables.values():
        for foreign_key in table.foreign_keys:
            references.setdefault(foreign_key.column.table.name, []).append(
                (table, foreign_key.parent, foreign_key.column)
            )
    return references


def _matches(column, ids: Iterable[int], *labels: str):
    """Compare by the column's own type; a text column also knows the slug."""

    values = [int(value) for value in ids]
    if isinstance(column.type, Integer):
        return column.in_(values) if values else false()
    if isinstance(column.type, String):
        texts = [str(value) for value in values] + [label for label in labels if label]
        return column.in_(texts) if texts else false()
    # Any other type cannot hold one of these ids.
    return false()


class _Purge:
    def __init__(self, session, metadata, tenant_table, user_table):
        self.session = session
        self.tenant_table = tenant_table
        self.user_table = user_table
        self.references = _references(metadata)
        self.deleted: Counter[str] = Counter()
        self.detached: Counter[str] = Counter()
        # Fixed identifier of the table in hand, for a failure report.
        self.current_table: str | None = None
        # Rows of other tenants may never disappear as a side effect.
        self.guard_tenant_rows = False

    def _count(self, table, condition) -> int:
        return int(
            self.session.execute(
                select(func.count()).select_from(table).where(condition)
            ).scalar()
            or 0
        )

    def _detach(self, table, column, condition) -> None:
        result = self.session.execute(
            table.update().where(condition).values({column.name: None})
        )
        if result.rowcount:
            self.detached[f"{table.name}.{column.name}"] += result.rowcount

    def delete_where(self, table, condition, stack: tuple[str, ...] = ()) -> None:
        if len(stack) >= MAX_CASCADE_DEPTH:
            raise TenantPurgeError("tenant_purge_cascade_too_deep")
        self.current_table = table.name
        if not self._count(table, condition):
            # Nothing here, so nothing can reference it: skip the whole subtree.
            return
        for child, child_column, parent_column in self.references.get(table.name, ()):
            matched = child_column.in_(select(parent_column).where(condition))
            if child is table or child.name in stack:
                # Self reference or cycle: detach, never recurse.
                if child_column.nullable:
                    self._detach(child, child_column, matched)
                continue
            if child is self.user_table:
                # Accounts are only ever removed explicitly, by id.
                if not child_column.nullable:
                    if self._count(child, matched):
                        raise TenantPurgeError("tenant_purge_user_reference_blocked")
                    continue
                self._detach(child, child_column, matched)
                continue
            if child is self.tenant_table and self.guard_tenant_rows:
                if self._count(child, matched):
                    raise TenantPurgeError("tenant_purge_would_reach_other_tenant")
                continue
            owned = (
                child_column.name in _OWNERSHIP_COLUMNS
                or not child_column.nullable
                or child_column.primary_key
            )
            if owned:
                self.delete_where(child, matched, stack + (table.name,))
            else:
                self._detach(child, child_column, matched)
        self.current_table = table.name
        result = self.session.execute(table.delete().where(condition))
        if result.rowcount:
            self.deleted[table.name] += result.rowcount


def _ids(values: Iterable[Any]) -> list[int]:
    return sorted({int(value) for value in values if value is not None})


def purge_tenant(
    session,
    metadata,
    tenant_table,
    user_table,
    tenant,
    *,
    purge_users: bool,
    protected_user_ids: Iterable[int] = (),
    purge_state: "_Purge | None" = None,
) -> dict[str, Any]:
    """Delete one tenant inside the caller's transaction and report what went."""

    tenant_id = int(tenant.id)
    slug = str(tenant.slug)
    purge = purge_state if purge_state is not None else _Purge(
        session, metadata, tenant_table, user_table
    )

    owner_ids = _ids((tenant.municipio_id, tenant.pyme_id))
    owners_elsewhere = set()
    for row in session.execute(
        select(tenant_table.c.municipio_id, tenant_table.c.pyme_id).where(
            tenant_table.c.id != tenant_id
        )
    ):
        owners_elsewhere.update(value for value in row if value is not None)
    exclusive_owner_ids = [value for value in owner_ids if value not in owners_elsewhere]
    shared_owner_ids = [value for value in owner_ids if value in owners_elsewhere]

    users = user_table
    membership = [users.c.tenant_id == tenant_id, users.c.tenant_slug == slug]
    if exclusive_owner_ids:
        membership.append(users.c.id.in_(exclusive_owner_ids))
        for name in ("pyme_id", "municipio_id", "empresa_id"):
            if name in users.c:
                membership.append(users.c[name].in_(exclusive_owner_ids))
    protected = set(_ids(protected_user_ids)) | owners_elsewhere
    user_ids: list[int] = []
    kept_user_ids: list[int] = []
    for user_id, user_tenant_id in session.execute(
        select(users.c.id, users.c.tenant_id).where(or_(*membership))
    ):
        belongs_elsewhere = user_tenant_id is not None and int(user_tenant_id) != tenant_id
        if user_id in protected or belongs_elsewhere:
            kept_user_ids.append(int(user_id))
        else:
            user_ids.append(int(user_id))
    user_ids.sort()
    kept_user_ids.sort()

    data_tables = [
        table
        for table in reversed(metadata.sorted_tables)
        if table is not tenant_table and table is not user_table
    ]
    for table in data_tables:
        if "tenant_id" in table.c:
            purge.delete_where(table, _matches(table.c.tenant_id, [tenant_id], slug))
    if exclusive_owner_ids:
        for table in data_tables:
            for name in _OWNER_SCOPE_COLUMNS:
                if name in table.c:
                    purge.delete_where(table, _matches(table.c[name], exclusive_owner_ids))

    if purge_users and user_ids:
        for table in data_tables:
            if "user_id" in table.c:
                purge.delete_where(table, _matches(table.c.user_id, user_ids))

    # The tenant row goes before its owner account: it references that account.
    session.execute(
        users.update()
        .where(or_(users.c.tenant_id == tenant_id, users.c.tenant_slug == slug))
        .values(tenant_id=None, tenant_slug=None)
    )
    purge.delete_where(tenant_table, tenant_table.c.id == tenant_id)

    if purge_users and user_ids:
        purge.guard_tenant_rows = True
        purge.delete_where(users, users.c.id.in_(user_ids))

    return {
        "contract_version": CONTRACT_VERSION,
        "tenant_id": tenant_id,
        "slug": slug,
        "owner_ids": exclusive_owner_ids,
        "shared_owner_ids_kept": shared_owner_ids,
        "purge_users": bool(purge_users),
        "user_ids": user_ids if purge_users else [],
        "user_ids_kept": kept_user_ids if purge_users else sorted(user_ids + kept_user_ids),
        "deleted": dict(sorted(purge.deleted.items())),
        "detached": dict(sorted(purge.detached.items())),
    }


def new_purge_state(session, metadata, tenant_table, user_table) -> _Purge:
    """State the caller may keep to name the table in hand after a failure."""

    return _Purge(session, metadata, tenant_table, user_table)


def tenant_account_filters(user_model, tenant) -> list[Any]:
    """Filters for the accounts of one tenant; owner ids are user ids."""

    owner_ids = _ids((tenant.municipio_id, tenant.pyme_id))
    filters = [user_model.tenant_id == tenant.id, user_model.tenant_slug == tenant.slug]
    if owner_ids:
        filters.extend(
            [
                user_model.id.in_(owner_ids),
                user_model.empresa_id.in_(owner_ids),
                user_model.pyme_id.in_(owner_ids),
                user_model.municipio_id.in_(owner_ids),
            ]
        )
    return filters
