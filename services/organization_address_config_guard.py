"""Keep address authority out of stale, general TenantProfile JSON updates.

In PostgreSQL the protected fragments are read from the CURRENT row inside the
UPDATE expression, not from an earlier Python merge. Only one narrowly scoped
service flush may replace one named section. No extra table lock or provider I/O
is added to the general settings writers. Direct external SQL is outside this
ORM boundary and must not be used as an address activation channel.
"""
from contextlib import contextmanager
from copy import deepcopy

from sqlalchemy import case, event, func, inspect, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, object_session
from sqlalchemy.sql.dml import Insert, Update
from sqlalchemy.sql.elements import BindParameter, Null

SECTIONS = ('organization_domain_binding', 'organization_address_bindings_v2')
ABSENT = object()
_PERMITS = '_organization_address_one_flush_sections'
_GUARDED_EXPRESSION = 'organization_address_current_row_preserved'


class AddressConfigGuardError(ValueError):
    pass


@contextmanager
def address_section_write(session, tenant, section, value=ABSENT):
    """Authorize exactly this section/value for one flush, always clear on exit."""
    owner = object_session(tenant)
    if (owner is None or owner.info is not session.info or section not in SECTIONS
            or (value is not ABSENT and not isinstance(value, dict))):
        raise AddressConfigGuardError('address_private_write_scope_invalid')
    pending = owner.info.setdefault(_PERMITS, {})
    if id(tenant) in pending:
        raise AddressConfigGuardError('address_private_write_scope_invalid')
    marker = (tenant, section, deepcopy(value) if value is not ABSENT else ABSENT)
    pending[id(tenant)] = marker
    try:
        yield
    finally:
        remaining = owner.info.get(_PERMITS, {})
        if remaining.get(id(tenant)) is marker:
            remaining.pop(id(tenant), None)
        if not remaining:
            owner.info.pop(_PERMITS, None)


def _consume(tenant, candidate):
    session = object_session(tenant)
    pending = session.info.get(_PERMITS, {}) if session is not None else {}
    marker = pending.pop(id(tenant), None)
    if marker is None:
        return None
    subject, section, expected = marker
    actual = candidate.get(section, ABSENT) if isinstance(candidate, dict) else ABSENT
    if subject is not tenant or (actual is not ABSENT if expected is ABSENT else actual != expected):
        raise AddressConfigGuardError('address_private_write_value_changed')
    return section


def _clear(session, *_args):
    session.info.pop(_PERMITS, None)


def install_address_config_guard(tenant_model):
    if getattr(tenant_model, '_address_config_guard_installed', False):
        return
    tenant_model._address_config_guard_installed = True
    table = tenant_model.__table__
    column = table.c.configuracion

    def targets_model(target):
        # ORM aliases/clones keep the same underlying Table. Do not attach a
        # model's policy to unrelated engines/metadata merely sharing its name.
        pending, seen = [target], set()
        while pending:
            candidate = pending.pop()
            if candidate is table:
                return True
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            pending.extend((getattr(candidate, 'original', None), getattr(candidate, 'element', None)))
            deannotate = getattr(candidate, '_deannotate', None)
            if deannotate is not None:
                pending.append(deannotate())
        return False

    def protect_update(_mapper, connection, tenant):
        if not inspect(tenant).attrs.configuracion.history.has_changes():
            return
        candidate = tenant.configuracion
        if candidate is not None and not isinstance(candidate, dict):
            raise AddressConfigGuardError('address_configuration_object_required')
        permitted = _consume(tenant, candidate)
        preserve = tuple(section for section in SECTIONS if section != permitted)
        cleaned = {key: deepcopy(value) for key, value in (candidate or {}).items()
            if key not in preserve}
        if connection.dialect.name == 'postgresql':
            expression = literal(cleaned, type_=JSONB)
            present = []
            for section in preserve:
                exists = (func.jsonb_typeof(column) == 'object') & column.has_key(section)
                present.append(exists)
                fragment = case((exists, func.jsonb_build_object(literal(section), column.op('->')(section))),
                    else_=literal({}, type_=JSONB))
                expression = expression.op('||')(fragment)
            # Preserve the legacy null representation when no protected data
            # needs to be retained. Normal ORM state is expired/refetched after
            # this SQL expression, so subsequent descriptors use stored data.
            if candidate is None:
                expression = case((or_(*present), expression), else_=literal(None, type_=JSONB))
            tenant.configuracion = expression._annotate({_GUARDED_EXPRESSION: True})
        else:
            # The test-only SQLite engine has a single writer. Production is
            # PostgreSQL; its atomic expression above is the concurrency gate.
            current = connection.execute(select(column).where(table.c.id == tenant.id)).scalar_one()
            for section in preserve:
                if isinstance(current, dict) and section in current:
                    cleaned[section] = deepcopy(current[section])
            value = cleaned if cleaned or candidate is not None else None
            tenant.configuracion = literal(value, type_=column.type)._annotate({_GUARDED_EXPRESSION: True})

    def protect_insert(_mapper, _connection, tenant):
        candidate = tenant.configuracion
        if candidate is None:
            return
        if not isinstance(candidate, dict):
            raise AddressConfigGuardError('address_configuration_object_required')
        permitted = _consume(tenant, candidate)
        tenant.configuracion = {key: deepcopy(value) for key, value in candidate.items()
            if key not in SECTIONS or key == permitted}

    def reject_bulk(execute_state):
        if not execute_state.is_update:
            return
        statement = execute_state.statement
        target = getattr(statement, 'table', None)
        if not targets_model(target):
            return
        names = {getattr(key, 'key', key) for key in (getattr(statement, '_values', None) or {})}
        parameters = execute_state.parameters
        for row in parameters if isinstance(parameters, list) else [parameters]:
            if isinstance(row, dict):
                names.update(row)
        if 'configuracion' in names:
            raise AddressConfigGuardError('address_bulk_configuration_write_forbidden')

    def reject_unguarded_update(_connection, statement, multiparams, params, _execution_options):
        # Legacy bulk_update_mappings/bulk_save_objects do not dispatch mapper
        # events or do_orm_execute. Reject that bypass at the compiled UPDATE
        # boundary as well; ordinary updates carry the expression above.
        if not isinstance(statement, (Insert, Update)):
            return
        target = statement.table
        if not targets_model(target):
            return
        values = {getattr(key, 'key', key): value for key, value
            in (getattr(statement, '_values', None) or {}).items()}
        values.update({getattr(key, 'key', key): value for key, value
            in (getattr(statement, '_ordered_values', None) or ())})
        if isinstance(statement, Insert):
            conflict = getattr(statement, '_post_values_clause', None)
            updates = {getattr(key, 'key', key) for key, _value
                in (getattr(conflict, 'update_values_to_set', None) or ())}
            if 'configuracion' in updates:
                raise AddressConfigGuardError('address_bulk_configuration_write_forbidden')
            selected = {getattr(key, 'key', key) for key in (getattr(statement, '_select_names', None) or ())}
            if 'configuracion' in selected:
                raise AddressConfigGuardError('address_bulk_configuration_write_forbidden')
            rows = [*(multiparams or ()), params]
            candidates = [values.get('configuracion')]
            for batch in (getattr(statement, '_multi_values', None) or ()):
                for row in batch:
                    if not isinstance(row, dict):
                        raise AddressConfigGuardError('address_bulk_configuration_write_forbidden')
                    normalized = {getattr(key, 'key', key): value for key, value in row.items()}
                    candidates.append(normalized.get('configuracion'))
            candidates.extend(row.get('configuracion') for row in rows
                if isinstance(row, dict))
            for value in list(candidates):
                if isinstance(value, BindParameter):
                    if value.callable is not None:
                        raise AddressConfigGuardError('address_bulk_configuration_write_forbidden')
                    candidates.extend(row[value.key] for row in rows
                        if isinstance(row, dict) and value.key in row)
            for value in candidates:
                value = getattr(value, 'value', value)
                if value is not None and not isinstance(value, (dict, Null)):
                    raise AddressConfigGuardError('address_bulk_configuration_write_forbidden')
                if isinstance(value, dict) and any(key in value for key in SECTIONS):
                    raise AddressConfigGuardError('address_bulk_configuration_write_forbidden')
            return
        changed = 'configuracion' in values or any(isinstance(row, dict) and 'configuracion' in row
            for row in [*(multiparams or ()), params])
        if changed and not getattr(values.get('configuracion'), '_annotations', {}).get(_GUARDED_EXPRESSION):
            raise AddressConfigGuardError('address_bulk_configuration_write_forbidden')

    def expire_expression_results(session, _flush_context):
        # A bare bound literal (SQLite) is not automatically expired by the ORM
        # in the same way as PostgreSQL's binary JSONB expression. Never let an
        # expression or a pre-merge Python snapshot escape as the saved state.
        for tenant in list(session.identity_map.values()):
            if isinstance(tenant, tenant_model):
                value = inspect(tenant).dict.get('configuracion')
                if getattr(value, '_annotations', {}).get(_GUARDED_EXPRESSION):
                    session.expire(tenant, ['configuracion'])

    event.listen(tenant_model, 'before_update', protect_update, propagate=True)
    event.listen(tenant_model, 'before_insert', protect_insert, propagate=True)
    event.listen(Session, 'do_orm_execute', reject_bulk)
    event.listen(Engine, 'before_execute', reject_unguarded_update)
    event.listen(Session, 'after_flush_postexec', _clear)
    event.listen(Session, 'after_flush_postexec', expire_expression_results)
    event.listen(Session, 'after_rollback', _clear)
    event.listen(Session, 'after_commit', _clear)
