"""Three address levels on existing private tenant JSON, without provider writes.

Legacy custom-domain records stay in place. All claims use the v1 table lock;
external DNS/provider work happens before that short transaction's lock.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re
import secrets
import unicodedata
from urllib.parse import quote

from flask import current_app, has_app_context
from sqlalchemy.exc import SQLAlchemyError

from services import organization_domain_binding as legacy
from services.organization_address_vercel import (
    AddressObservation, AddressPlan, plan_digest, validate_address_host,
)
from services.organization_domain_vercel import ProvisioningError, canonical, FRONTEND_PROJECT
from services.plan_access import (
    FULL_INTEGRATION_PLANS, normalize_plan, tenant_allows_custom_domains,
    tenant_is_demo_context, tenant_allows_new_custom_domains,
)

KEY = 'organization_address_bindings_v2'
CONTRACT = 'organization.addresses.v2'
STORE = 'organization.address_store.v2'
READINESS = 'public.tenant_address_readiness.v2'
KINDS = frozenset({'platform', 'custom'})
STATUSES = frozenset({'pending_dns', 'pending_platform', 'active', 'revoked'})
TIMES = ('requested_at', 'request_expires_at', 'challenge_expires_at',
    'dns_verified_at', 'dns_valid_until', 'platform_verified_at', 'platform_valid_until')
REQUEST_TTL = legacy.CHALLENGE_TTL
DomainBindingError = legacy.DomainBindingError

_OPERATOR_UI = {
    'contract_version': 'organization.address_operator_ui.v1',
    'labels': {
        'title': 'Publicación de la dirección',
        'description': 'Revisá las referencias de la publicación actual antes de confirmar la vinculación.',
        'kind': 'Tipo de dirección',
        'platform': 'Subdominio de la plataforma',
        'custom': 'Dominio propio',
        'advanced_title': 'Referencias de la publicación',
        'advanced_description': 'Usá las referencias ya revisadas de la publicación actual en Producción. El servidor vuelve a comprobarlas antes de activar la vinculación.',
        'deployment': 'Identificador de la publicación',
        'source': 'Revisión del frontend',
        'routes': 'Huella de las rutas publicadas',
        'reviewed_version': 'Versión revisada',
        'refresh': 'Actualizar estado',
        'plan': 'Preparar revisión',
        'review_title': 'Revisar la vinculación',
        'tenant': 'Organización',
        'host': 'Dirección',
        'revision': 'Revisión de la dirección',
        'digest': 'Huella de la revisión',
        'observed': 'Observada el',
        'expires': 'Válida hasta',
        'backend_source': 'Revisión del backend',
        'confirm_title': 'Confirmar la activación',
        'confirm_description': 'Confirmá la activación de esta vinculación existente. El servidor vuelve a validar la revisión y la evidencia de publicación; esta acción no cambia DNS ni configura al proveedor.',
        'confirm': 'Activar vinculación',
        'cancel': 'Cancelar',
        'loading': 'Actualizando estado',
        'planning': 'Preparando revisión',
        'activating': 'Confirmando activación',
        'active': 'Vinculación activa',
        'plan_ready': 'Revisión preparada',
    },
    'messages': {
        'not_ready': 'La solicitud necesita completar sus verificaciones antes de preparar la publicación.',
        'descriptor_error': 'No se pudo consultar el estado de la dirección. Actualizá el estado antes de continuar.',
        'plan_error': 'No se pudo preparar la revisión. Conservá las referencias y actualizá el estado antes de continuar.',
        'activation_error': 'El servidor no confirmó la activación. Actualizá el estado y revisá el resultado antes de continuar.',
        'uncertain': 'No se pudo confirmar el resultado de la activación. Actualizá el estado antes de volver a intentarlo.',
        'expired': 'La revisión venció. Actualizá el estado y prepará una revisión nueva antes de confirmar.',
        'input_invalid': 'Revisá los identificadores y huellas de la publicación antes de preparar la revisión.',
        'descriptor_changed': 'El estado de la dirección cambió. Actualizalo y revisá nuevamente la vinculación antes de confirmar.',
        'platform_admin_required': 'La publicación requiere autorización vigente de administración de la plataforma.',
        'operator_runtime_unavailable': 'La verificación de publicación no está disponible. Conservá la solicitud y consultá el estado antes de continuar.',
        'maintenance': 'La activación está temporalmente en modo consulta. Conservá las referencias y revisá el estado más adelante.',
        'not_pending': 'Esta dirección no está pendiente de publicación. Actualizá su estado antes de continuar.',
        'success': 'El servidor confirmó la vinculación activa.',
    },
}


def _require(value, code, status=400):
    if not value:
        raise DomainBindingError(code, status)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'address_alias_configuration_invalid', 503)
        result[key] = value
    return result


def alias_map(config=None):
    config = current_app.config if config is None and has_app_context() else (config or {})
    raw = config.get('ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON') or '{}'
    try:
        _require(isinstance(raw, str) and len(raw) <= 32768,
            'address_alias_configuration_invalid', 503)
        result = json.loads(raw, object_pairs_hook=_unique)
        _require(isinstance(result, dict) and len(result) <= 128,
            'address_alias_configuration_invalid', 503)
        for host, scope in result.items():
            _require(isinstance(host, str) and host.endswith('.vercel.app')
                and validate_address_host(host, 'platform', (host,)) == host
                and isinstance(scope, dict) and set(scope) == {'tenant_id', 'tenant_slug'}
                and type(scope['tenant_id']) is int and scope['tenant_id'] > 0
                and isinstance(scope['tenant_slug'], str)
                and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', scope['tenant_slug']),
                'address_alias_configuration_invalid', 503)
        for scope in result.values():
            _require(sum(other == scope for other in result.values()) <= 32,
                'address_alias_configuration_invalid', 503)
        return result
    except (ValueError, TypeError, ProvisioningError):
        raise DomainBindingError('address_alias_configuration_invalid', 503) from None


def aliases_for(tenant, config=None):
    return tuple(sorted(host for host, scope in alias_map(config).items()
        if scope == {'tenant_id': tenant.id, 'tenant_slug': tenant.slug}))


def normalize_request_host(value, kind, tenant, config=None):
    _require(isinstance(kind, str) and kind in KINDS and isinstance(value, str) and value == value.strip()
        and value and len(value) <= 253 and not any(unicodedata.category(char)[0] == 'C' for char in value)
        and not any(char in value for char in '\u3002\uff0e\uff61'), 'address_host_invalid')
    try:
        host = value.rstrip('.').encode('idna').decode('ascii').lower()
        _require(not value.endswith('..'), 'address_host_invalid')
        return validate_address_host(host, kind, aliases_for(tenant, config) if kind == 'platform' else ())
    except (UnicodeError, ProvisioningError):
        raise DomainBindingError('address_host_invalid') from None


def empty_store(tenant):
    return {'contract_version': STORE, 'tenant_id': tenant.id, 'tenant_slug': tenant.slug,
        'version': 0, 'platform': None, 'custom': None}


def _empty_slot(host=None, status='revoked'):
    return {'host': host, 'status': status, 'challenge': None, **{key: None for key in TIMES}}


def read_store(tenant):
    config = getattr(tenant, 'configuracion', None)
    _require(config is None or isinstance(config, dict), 'address_storage_invalid', 409)
    value = (config or {}).get(KEY)
    if value is None:
        return empty_store(tenant)
    expected = empty_store(tenant)
    _require(isinstance(value, dict) and set(value) == set(expected)
        and value['contract_version'] == STORE and type(value['tenant_id']) is int
        and value['tenant_id'] == tenant.id and value['tenant_slug'] == tenant.slug
        and type(value['version']) is int and 1 <= value['version'] < 2147483647,
        'address_storage_invalid', 409)
    for kind in KINDS:
        row = value[kind]
        if row is None:
            continue
        _require(isinstance(row, dict) and set(row) == set(_empty_slot())
            and isinstance(row['status'], str) and row['status'] in STATUSES and isinstance(row['host'], str),
            'address_storage_invalid', 409)
        try:
            # A policy removal suspends a stored alias; it must not corrupt the
            # whole registry. Current admission is rechecked on resolve/activate.
            validate_address_host(row['host'], kind,
                (row['host'],) if kind == 'platform' and row['host'].endswith('.vercel.app') else ())
        except ProvisioningError:
            raise DomainBindingError('address_storage_invalid', 409) from None
        _require(all(row[key] is None or (type(row[key]) is int and row[key] > 0)
            for key in TIMES), 'address_storage_invalid', 409)
        if row['status'] == 'revoked':
            _require(row['challenge'] is None and all(row[key] is None for key in TIMES),
                'address_storage_invalid', 409)
            continue
        _require(row['requested_at'] is not None
            and row['request_expires_at'] == row['requested_at'] + REQUEST_TTL,
            'address_storage_invalid', 409)
        if kind == 'platform':
            _require(row['status'] != 'pending_dns' and row['challenge'] is None
                and all(row[key] is None for key in ('challenge_expires_at', 'dns_verified_at', 'dns_valid_until')),
                'address_storage_invalid', 409)
        else:
            _require(isinstance(row['challenge'], str) and re.fullmatch(r'[a-f0-9]{64}', row['challenge'])
                and row['challenge_expires_at'] == row['request_expires_at'], 'address_storage_invalid', 409)
            if row['status'] == 'pending_dns':
                _require(row['dns_verified_at'] is None and row['dns_valid_until'] is None,
                    'address_storage_invalid', 409)
            else:
                _require(row['dns_verified_at'] is not None and row['dns_valid_until'] is not None
                    and row['requested_at'] <= row['dns_verified_at'] < row['challenge_expires_at']
                    and row['dns_verified_at'] < row['dns_valid_until'] <= row['dns_verified_at'] + legacy.VERIFICATION_TTL,
                    'address_storage_invalid', 409)
        if row['status'] == 'active':
            deadline = row['request_expires_at'] if kind == 'platform' else row['dns_valid_until']
            _require(row['platform_verified_at'] is not None and row['platform_valid_until'] is not None
                and row['requested_at'] <= row['platform_verified_at'] < deadline
                and (kind == 'platform' or row['dns_verified_at'] <= row['platform_verified_at'])
                and row['platform_verified_at'] < row['platform_valid_until'] <= row['platform_verified_at'] + legacy.VERIFICATION_TTL,
                'address_storage_invalid', 409)
        else:
            _require(row['platform_verified_at'] is None and row['platform_valid_until'] is None,
                'address_storage_invalid', 409)
    return deepcopy(value)


def slots(tenant):
    value, old = read_store(tenant), legacy.read_record(tenant)
    old_claim = old['host'] is not None and old['status'] != 'revoked'
    new_claim = value['custom'] is not None and value['custom']['status'] != 'revoked'
    _require(not (old_claim and new_claim), 'address_storage_conflict', 409)
    custom = (old, 'legacy_v1') if old_claim else ((value['custom'], 'v2')
        if value['custom'] is not None else ((old, 'legacy_v1') if old['host'] else (None, None)))
    return {'platform': (value['platform'], 'v2' if value['platform'] else None), 'custom': custom}


def revision(tenant):
    return sha256(canonical([CONTRACT, tenant.id, tenant.slug, read_store(tenant), legacy.read_record(tenant)])).hexdigest()


def eligible(tenant, kind, source='v2'):
    if source == 'legacy_v1':
        return tenant_allows_custom_domains(tenant)
    if kind == 'custom':
        return tenant_allows_new_custom_domains(tenant)
    return bool(tenant.is_active is True and not tenant_is_demo_context(tenant)
        and normalize_plan(tenant.plan) in ({'pro', *FULL_INTEGRATION_PLANS}
            if kind == 'platform' else FULL_INTEGRATION_PLANS))


def active(tenant, kind, row, source, now, config=None):
    if not row or row['status'] != 'active' or not eligible(tenant, kind, source):
        return False
    if source == 'legacy_v1':
        return legacy._active(row, now)
    if kind == 'platform':
        try:
            normalize_request_host(row['host'], kind, tenant, config)
        except DomainBindingError:
            return False
        return row['platform_verified_at'] <= now < row['platform_valid_until']
    return row['platform_verified_at'] <= now < min(row['dns_valid_until'], row['platform_valid_until'])


def claim_rows(tenant):
    """All occupied claims, including expired/suspended ones, without policy reads."""
    old = legacy.read_record(tenant)
    rows = [('legacy_v1', old)] if old['host'] and old['status'] != 'revoked' else []
    value = read_store(tenant)
    rows.extend((kind, value[kind]) for kind in KINDS if value[kind] and value[kind]['status'] != 'revoked')
    return rows


def assert_available(session, tenant_model, tenant, kind, host):
    for other in session.query(tenant_model).populate_existing().all():
        if other.id != tenant.id and other.dominio:
            try:
                old_host = legacy.normalize_host(other.dominio)
            except DomainBindingError:
                old_host = None
            _require(old_host not in {host, host[4:] if host.startswith('www.') else 'www.' + host},
                'address_host_in_use', 409)
        for other_kind, row in claim_rows(other):
            if other.id == tenant.id and other_kind == kind:
                continue
            _require(row['host'] != host, 'address_host_in_use', 409)


def legacy_custom_occupied(tenant):
    row = read_store(tenant)['custom']
    return bool(row and row['status'] != 'revoked')


def pending_deadline(kind, row, source):
    return (row['dns_valid_until'] if kind == 'custom' else row['request_expires_at'])


def activation_capabilities(*, is_operator=False, runtime_available=False, writes_blocked=False):
    reason = ('maintenance' if writes_blocked else 'operator_runtime_unavailable' if not runtime_available
        else 'platform_admin_required' if not is_operator else 'ready')
    return {'mode': 'platform_operator', 'available': reason == 'ready',
        'can_plan': reason == 'ready', 'can_activate': reason == 'ready', 'reason_code': reason,
        'provider_changes_supported': False}


def build_addresses(tenant, *, can_manage=False, writes_blocked=False,
        is_operator=False, runtime_available=False, config=None, now=None):
    now = legacy._now(now)
    current = slots(tenant)
    result = {'contract_version': CONTRACT, 'tenant': {'id': tenant.id, 'slug': tenant.slug},
        'revision': revision(tenant), 'version': read_store(tenant)['version'] + legacy.read_record(tenant)['version'],
        'base': {'url': 'https://chatboc.ar/t/' + quote(tenant.slug, safe=''), 'available': tenant.is_active is True}}
    capabilities = {'can_manage': can_manage is True, 'writes_blocked': writes_blocked is True}
    for kind in ('platform', 'custom'):
        row, source = current[kind]
        entitled = eligible(tenant, kind, source or 'v2')
        is_active = active(tenant, kind, row, source, now, config)
        status = row['status'] if row else 'unconfigured'
        policy_admitted = True
        if row and kind == 'platform':
            try:
                normalize_request_host(row['host'], kind, tenant, config)
            except DomainBindingError:
                policy_admitted = False
        if row and status != 'revoked' and (not entitled or not policy_admitted):
            status = 'suspended'
        elif row and status == 'active' and not is_active:
            status = 'verification_expired'
        elif row and status in {'pending_dns', 'pending_platform'}:
            deadline = row['challenge_expires_at'] if status == 'pending_dns' else pending_deadline(kind, row, source)
            if deadline is None or deadline <= now:
                status = 'verification_expired'
        proof = None
        if row and kind == 'custom' and row['status'] in {'pending_dns', 'pending_platform'}:
            proof = {'type': 'TXT', 'name': '_chatboc-verify.' + row['host'],
                'value': 'chatboc-domain-verification=' + row['challenge'],
                'expires_at': row['challenge_expires_at']}
        valid_until = None
        if is_active:
            valid_until = row['platform_valid_until'] if kind == 'platform' else min(row['dns_valid_until'], row['platform_valid_until'])
        result[kind] = {'kind': kind, 'source': source, 'host': row['host'] if row else None,
            'status': status, 'active': bool(is_active), 'valid_until': valid_until, 'dns_proof': proof,
            'allowed_aliases': list(aliases_for(tenant, config)) if kind == 'platform' else [],
            'scope_note': ('Subdominio de la plataforma. No requiere un TXT del cliente; la publicación y HTTPS deben verificarse antes de activarlo.'
                if kind == 'platform' else
                'Dominio heredado del cliente. Conserva compatibilidad con planes Pro y Full; una solicitud nueva requiere Full. Requiere prueba TXT y verificación de publicación y HTTPS.'
                if source == 'legacy_v1' and row['status'] != 'revoked' else
                'Dominio del cliente. Requiere un plan Full, prueba TXT y verificación de publicación y HTTPS.')}
        admission = eligible(tenant, kind) if status in {'unconfigured', 'revoked'} else entitled
        plan_reason = ('pro_plan_required' if kind == 'platform'
            or (source == 'legacy_v1' and status not in {'unconfigured', 'revoked'}) else 'full_plan_required')
        reason = ('maintenance' if writes_blocked else 'tenant_admin_required' if not can_manage
            else plan_reason if not admission else 'platform_alias_unavailable' if not policy_admitted
            else 'address_revoke_required' if status == 'verification_expired' else 'ready')
        permitted = reason == 'ready'
        capabilities[kind] = {'eligible': bool(admission), 'can_request': permitted and status in {'unconfigured', 'revoked'},
            'can_verify_dns': permitted and status == 'pending_dns' and kind == 'custom',
            'can_revoke': bool(can_manage and not writes_blocked and row and row['status'] != 'revoked'),
            'reason_code': reason}
    capabilities['activation'] = activation_capabilities(is_operator=is_operator,
        runtime_available=runtime_available, writes_blocked=writes_blocked)
    result.update(capabilities=capabilities,
        canonical_url=('https://' + result['custom']['host'] + '/' if result['custom']['active']
            else 'https://' + result['platform']['host'] + '/' if result['platform']['active'] else result['base']['url']),
        save_endpoint=f'/api/admin/tenants/{tenant.slug}/addresses',
        activation_plan_endpoint=f'/api/admin/tenants/{tenant.slug}/addresses/activation-plan',
        activation_endpoint=f'/api/admin/tenants/{tenant.slug}/addresses/activate',
        provider_changes_performed=False, auth_e2e_verified=False,
        operator_ui=deepcopy(_OPERATOR_UI))
    return result


def _persist(session, tenant, actor, audit_model, row, *, kind, source, operation,
        config, now, commit=True, evidence=None, final_validate=None):
    from services.organization_address_config_guard import address_section_write
    before = revision(tenant)
    if source == 'legacy_v1':
        row['version'] = legacy.read_record(tenant)['version'] + 1
        tenant.configuracion = {**(tenant.configuracion or {}), legacy.KEY: row}
    else:
        value = read_store(tenant)
        value[kind] = row
        value['version'] += 1
        tenant.configuracion = {**(tenant.configuracion or {}), KEY: value}
    session.add(audit_model(tenant_id=tenant.id, actor_user_id=actor.id,
        event_type='organization.address.' + operation, resource_type='tenant_profile',
        resource_id=str(tenant.id), details={'contract_version': CONTRACT, 'kind': kind,
            'source': source, 'host': row['host'], 'previous_revision': before,
            'revision': revision(tenant), 'provider_changes_performed': False,
            **({'provider_evidence': evidence} if evidence is not None else {})}))
    section = legacy.KEY if source == 'legacy_v1' else KEY
    with address_section_write(session, tenant, section, tenant.configuracion[section]):
        session.flush()
    result = {'contract_version': 'organization.addresses_save.v2', 'saved': True,
        'addresses': build_addresses(tenant, can_manage=True, config=config, now=now),
        'provider_changes_performed': False}
    if final_validate is not None:
        final_validate()
    if commit:
        session.commit()
    return result


def _dns_snapshot(session, tenant_model, user_model, tenant_id, actor_id, data, authorize, now):
    from utils.auth_helpers import is_user_auth_disabled
    tenant = session.get(tenant_model, tenant_id, populate_existing=True)
    actor = session.get(user_model, actor_id, populate_existing=True)
    _require(tenant is not None and tenant.is_active is True and actor is not None
        and not is_user_auth_disabled(actor) and authorize(actor, tenant) is True,
        'address_forbidden', 403)
    _require(revision(tenant) == data['expected_revision'], 'address_revision_conflict', 412)
    row, source = slots(tenant)['custom']
    _require(row is not None and row['status'] == 'pending_dns' and eligible(tenant, 'custom', source)
        and row['requested_at'] <= now < row['challenge_expires_at'], 'address_dns_request_expired', 409)
    return deepcopy(row), source


def save_addresses(session, tenant_model, user_model, audit_model, *, tenant_id,
        actor_id, data, authorize, config=None, now=None, txt_reader=None):
    fixed_now, now = now, legacy._now(now)
    try:
        _require(isinstance(data, dict) and isinstance(data.get('kind'), str) and data.get('kind') in KINDS
            and isinstance(data.get('operation'), str) and data.get('operation') in {'request', 'verify_dns', 'revoke'}
            and set(data) == ({'kind', 'operation', 'expected_revision', 'host'} if data['operation'] == 'request'
                else {'kind', 'operation', 'expected_revision'}), 'address_request_invalid')
        _require(isinstance(data['expected_revision'], str) and re.fullmatch(r'[a-f0-9]{64}', data['expected_revision']),
            'address_revision_required', 428)
        kind, operation = data['kind'], data['operation']
        dns = None
        if operation == 'verify_dns':
            _require(kind == 'custom', 'address_dns_not_applicable', 409)
            snapshot, _ = _dns_snapshot(session, tenant_model, user_model, tenant_id, actor_id, data, authorize, now)
            values = (txt_reader or legacy.read_dns_txt)(snapshot['host'])
            legacy.validate_dns_values(values)
            _require(any(secrets.compare_digest('chatboc-domain-verification=' + snapshot['challenge'], value)
                for value in values), 'address_dns_not_verified', 409)
            dns = (snapshot['host'], legacy._now(fixed_now))
        # No network call occurs after acquiring the shared v1/v2 writer lock.
        tenant, actor = legacy._locked_scope(session, tenant_model, user_model, tenant_id, actor_id, authorize)
        now = legacy._now(fixed_now)
        _require(revision(tenant) == data['expected_revision'], 'address_revision_conflict', 412)
        row, source = slots(tenant)[kind]
        if operation != 'revoke':
            _require(eligible(tenant, kind, source or 'v2'),
                'address_pro_required' if kind == 'platform' else 'address_full_required', 403)
        if operation == 'request':
            _require(row is None or row['status'] == 'revoked', 'address_revoke_required', 409)
            # Revoked legacy state does not grandfather a new Pro custom domain.
            _require(eligible(tenant, kind), 'address_full_required' if kind == 'custom' else 'address_pro_required', 403)
            host = normalize_request_host(data['host'], kind, tenant, config)
            assert_available(session, tenant_model, tenant, kind, host)
            row = {**_empty_slot(host, 'pending_platform' if kind == 'platform' else 'pending_dns'),
                'requested_at': now, 'request_expires_at': now + REQUEST_TTL}
            if kind == 'custom':
                row.update(challenge=secrets.token_hex(32), challenge_expires_at=now + REQUEST_TTL)
            source = 'v2'
        elif operation == 'verify_dns':
            _require(row is not None and row['status'] == 'pending_dns' and dns is not None
                and row['host'] == dns[0] and row['requested_at'] <= dns[1] <= now < row['challenge_expires_at']
                and now - dns[1] <= 60, 'address_dns_request_expired', 409)
            assert_available(session, tenant_model, tenant, 'legacy_v1' if source == 'legacy_v1' else kind, row['host'])
            row.update(status='pending_platform', dns_verified_at=dns[1], dns_valid_until=dns[1] + legacy.VERIFICATION_TTL)
        else:
            _require(row is not None and row['status'] != 'revoked', 'address_not_configured', 409)
            row = ({**legacy.empty_record(tenant), 'host': row['host'], 'status': 'revoked'}
                if source == 'legacy_v1' else _empty_slot(row['host']))
        return _persist(session, tenant, actor, audit_model, row, kind=kind, source=source,
            operation=operation, config=config, now=now)
    except SQLAlchemyError:
        session.rollback()
        raise DomainBindingError('address_save_unconfirmed', 503) from None
    except BaseException:
        session.rollback()
        raise


def prepare_plan(tenant, *, kind, expected_revision, frontend_deployment,
        frontend_source, routes_sha256, backend_source, config=None, now=None):
    now = legacy._now(now)
    _require(isinstance(kind, str) and kind in KINDS, 'address_request_invalid')
    _require(revision(tenant) == expected_revision, 'address_revision_conflict', 412)
    row, source = slots(tenant)[kind]
    _require(row is not None and row['status'] == 'pending_platform'
        and eligible(tenant, kind, source), 'address_platform_not_ready', 409)
    normalize_request_host(row['host'], kind, tenant, config)
    valid_until = pending_deadline(kind, row, source)
    _require(type(valid_until) is int and row['requested_at'] <= now < valid_until
        and (kind == 'platform' or row['dns_verified_at'] <= now), 'address_verification_expired', 409)
    plan = AddressPlan(row['host'], tenant.id, tenant.slug, kind, expected_revision,
        valid_until, frontend_deployment, frontend_source, backend_source, routes_sha256,
        aliases_for(tenant, config) if kind == 'platform' else ())
    plan.validate(now)
    return plan


def activate_address(session, tenant_model, user_model, audit_model, *, actor_id,
        plan, observation, approved_plan_sha256, authorize, config=None, now=None, commit=True, clock=None):
    read_clock = lambda: legacy._now(now if now is not None else clock() if clock is not None else None)
    try:
        _require(type(plan) is AddressPlan and type(observation) is AddressObservation,
            'address_platform_not_verified', 409)
        tenant, actor = legacy._locked_scope(session, tenant_model, user_model, plan.tenant_id, actor_id, authorize)
        current_time = read_clock()
        current = prepare_plan(tenant, kind=plan.kind, expected_revision=plan.revision,
            frontend_deployment=plan.frontend_deployment, frontend_source=plan.frontend_source,
            routes_sha256=plan.routes_sha256, backend_source=plan.backend_source, config=config, now=current_time)
        digest = plan_digest(current)
        _require(digest == approved_plan_sha256 == plan_digest(plan) == observation.plan_sha256
            and observation.host == plan.host and observation.kind == plan.kind
            and observation.project_id == FRONTEND_PROJECT and type(observation.observed_at) is int
            and 0 <= current_time - observation.observed_at <= 60 and type(observation.valid_until) is int
            and current_time < observation.valid_until <= min(plan.valid_until, current_time + legacy.VERIFICATION_TTL),
            'address_platform_not_verified', 409)
        row, source = slots(tenant)[plan.kind]
        assert_available(session, tenant_model, tenant, 'legacy_v1' if source == 'legacy_v1' else plan.kind, plan.host)
        row.update(status='active', platform_verified_at=observation.observed_at,
            platform_valid_until=observation.valid_until)
        def validate_time():
            at_commit = read_clock()
            _require(0 <= at_commit - observation.observed_at <= 60
                and at_commit < min(plan.valid_until, observation.valid_until), 'address_platform_not_verified', 409)
        return _persist(session, tenant, actor, audit_model, row, kind=plan.kind, source=source,
            operation='activate', config=config, now=current_time, commit=commit, final_validate=validate_time,
            evidence={'plan_sha256': digest, 'project_id': plan.project_id,
                'frontend_deployment': plan.frontend_deployment, 'frontend_source': plan.frontend_source,
                'backend_source': plan.backend_source, 'routes_sha256': plan.routes_sha256,
                'observed_at': observation.observed_at, 'valid_until': observation.valid_until})
    except SQLAlchemyError:
        session.rollback()
        raise DomainBindingError('address_save_unconfirmed', 503) from None
    except BaseException:
        session.rollback()
        raise


def resolve_host(session, tenant_model, host, *, legacy_entitlement, config=None, now=None):
    now = legacy._now(now)
    if isinstance(host, str) and host.lower().rstrip('.').endswith('.vercel.app'):
        _require(host == host.strip() and not host.endswith('..'), 'host_invalid')
        host = host.lower().removesuffix('.')
        try:
            validate_address_host(host, 'platform', (host,))
        except ProvisioningError:
            raise DomainBindingError('host_invalid') from None
    else:
        host = legacy.normalize_host(host)
    tenants = session.query(tenant_model).populate_existing().all()
    matches = []
    try:
        for tenant in tenants:
            slots(tenant)  # Reject contradictory/malformed registry records.
            for kind, row in claim_rows(tenant):
                if row['host'] == host:
                    matches.append((tenant, kind, row))
    except DomainBindingError:
        return None
    if len(matches) != 1:
        return None
    tenant, kind, row = matches[0]
    source = 'legacy_v1' if kind == 'legacy_v1' else 'v2'
    kind = 'custom' if kind == 'legacy_v1' else kind
    if source == 'legacy_v1':
        if tenant.is_active is not True or legacy_entitlement(tenant) is not True or not legacy._active(row, now):
            return None
    elif not active(tenant, kind, row, source, now, config):
        return None
    try:
        assert_available(session, tenant_model, tenant, 'legacy_v1' if source == 'legacy_v1' else kind, host)
    except DomainBindingError:
        return None
    valid_until = row['platform_valid_until'] if kind == 'platform' else min(row['dns_valid_until'], row['platform_valid_until'])
    result = legacy.public_payload(tenant, host, valid_until)
    if source == 'v2':
        other, other_source = slots(tenant)['custom']
        redirect = kind == 'platform' and active(tenant, 'custom', other, other_source, now, config)
        if redirect:
            try:
                assert_available(session, tenant_model, tenant, 'legacy_v1' if other_source == 'legacy_v1' else 'custom', other['host'])
            except DomainBindingError:
                redirect = False
        result['canonical_origin'] = 'https://' + other['host'] if redirect else result['origin']
        result['canonical_redirect_required'] = bool(redirect)
    return result


def build_readiness(session, tenant_model, *, host, tenant_id, tenant_slug, kind,
        expected_revision, nonce, backend_source, config=None, now=None, txt_reader=None):
    now = legacy._now(now)
    _require(isinstance(kind, str) and kind in KINDS and type(tenant_id) is int and tenant_id > 0
        and isinstance(tenant_slug, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', tenant_slug)
        and isinstance(expected_revision, str) and re.fullmatch(r'[a-f0-9]{64}', expected_revision)
        and isinstance(nonce, str) and re.fullmatch(r'[a-f0-9]{64}', nonce), 'address_readiness_invalid')
    tenant = session.query(tenant_model).filter_by(id=tenant_id, slug=tenant_slug).populate_existing().one_or_none()
    if tenant is None or not eligible(tenant, kind, slots(tenant)[kind][1] or 'v2'):
        return None
    row, source = slots(tenant)[kind]
    if row is None or row['status'] != 'pending_platform' or row['host'] != host or revision(tenant) != expected_revision:
        return None
    normalize_request_host(host, kind, tenant, config)
    if (not row['requested_at'] <= now < pending_deadline(kind, row, source)
            or (kind == 'custom' and row['dns_verified_at'] > now)):
        return None
    try:
        assert_available(session, tenant_model, tenant, 'legacy_v1' if source == 'legacy_v1' else kind, host)
    except DomainBindingError:
        return None
    _require(isinstance(backend_source, str) and re.fullmatch(r'[a-f0-9]{40}', backend_source),
        'address_readiness_runtime_unavailable', 503)
    if kind == 'custom':
        values = (txt_reader or legacy.read_dns_txt)(host)
        legacy.validate_dns_values(values)
        if not any(secrets.compare_digest('chatboc-domain-verification=' + row['challenge'], value) for value in values):
            return None
    return {'contract_version': READINESS, 'host': host, 'tenant': {'id': tenant_id, 'slug': tenant_slug},
        'kind': kind, 'revision': expected_revision, 'nonce': nonce, 'backend_source': backend_source,
        'observed_at': now, 'scope': 'pending_address_infrastructure_only', 'active': False,
        'auth_e2e_verified': False, 'private_assets_included': False}
