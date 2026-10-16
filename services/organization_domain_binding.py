"""Exact-host binding on existing TenantProfile JSON, without provisioning a provider.

Owner actions stop at pending_platform. Activation is an internal function requiring
a fresh, exact provider/routing attestation; it is deliberately not exposed to HTTP.
Legacy ``dominio`` is a collision signal, never ownership or activation evidence.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import ipaddress
import json
import re
import secrets
import time
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

KEY = 'organization_domain_binding'
CONTRACT = 'organization.domain.v1'
STORE = 'organization.domain_store.v1'
PUBLIC = 'public.tenant_host.v1'
READINESS = 'public.tenant_host_readiness.v1'
CHALLENGE_TTL = 86400
VERIFICATION_TTL = 7 * 86400
RESERVED = {'chatboc.ar', 'www.chatboc.ar', 'api.chatboc.ar', 'admin.chatboc.ar',
    'auth.chatboc.ar', 'app.chatboc.ar', 'panel.chatboc.ar', 'preview.chatboc.ar', 'api-preview.chatboc.ar', 'frontend.chatboc.ar',
    'backend.chatboc.ar', 'login.chatboc.ar', 'static.chatboc.ar', 'assets.chatboc.ar'}
BLOCKED_SUFFIXES = ('.vercel.app', '.onrender.com', '.localhost', '.local', '.internal',
    '.invalid', '.test', '.example', '.arpa', '.localhost.localdomain')
_TIME_FIELDS = ('requested_at', 'challenge_expires_at', 'dns_verified_at',
    'dns_valid_until', 'platform_verified_at', 'platform_valid_until')


class DomainBindingError(ValueError):
    def __init__(self, code, status=400, message='Revisá el dominio y su estado antes de continuar.'):
        self.code, self.status, self.message = code, status, message
        super().__init__(code)


def normalize_host(value):
    """One bare public hostname; no origin, port, IP, wildcard or guessed www alias."""
    if (not isinstance(value, str) or not value or len(value) > 253
            or value != value.strip() or any(unicodedata.category(c)[0] == 'C' for c in value)
            or any(c in value for c in '/\\:@?#*%\u3002\uff0e\uff61')):
        raise DomainBindingError('host_invalid')
    raw = value[:-1] if value.endswith('.') else value
    try:
        host = raw.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise DomainBindingError('host_invalid') from None
    labels = host.split('.')
    if (len(host) > 253 or len(labels) < 2 or not re.fullmatch(r'[a-z][a-z0-9-]{1,62}', labels[-1])
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
        raise DomainBindingError('host_invalid')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise DomainBindingError('host_invalid')
    if host in RESERVED or any(host.endswith(suffix) or host == suffix[1:] for suffix in BLOCKED_SUFFIXES):
        raise DomainBindingError('host_invalid')
    return host


def _host_for_tenant(value, tenant):
    host = normalize_host(value)
    if host.endswith('.chatboc.ar') and host != f'{tenant.slug}.chatboc.ar':
        raise DomainBindingError('host_reserved', 409, 'Ese subdominio está reservado para otra organización.')
    return host


def _now(value=None):
    value = int(time.time()) if value is None else value
    if type(value) is not int or value < 1:
        raise DomainBindingError('domain_clock_invalid', 503)
    return value


def empty_record(tenant):
    return {'contract_version': STORE, 'tenant_id': tenant.id, 'tenant_slug': tenant.slug,
        'version': 0, 'host': None, 'status': 'unconfigured', 'challenge': None,
        **{key: None for key in _TIME_FIELDS}}


def read_record(tenant):
    config = getattr(tenant, 'configuracion', None)
    if config is not None and not isinstance(config, dict):
        raise DomainBindingError('domain_storage_invalid', 409)
    record = (config or {}).get(KEY)
    if record is None:
        return empty_record(tenant)
    empty = empty_record(tenant)
    invalid = (not isinstance(record, dict) or set(record) != set(empty)
        or record['contract_version'] != STORE or record['tenant_id'] != tenant.id
        or type(record['tenant_id']) is not int or record['tenant_slug'] != tenant.slug
        or type(record['version']) is not int or not 1 <= record['version'] < 2147483647
        or not isinstance(record['status'], str)
        or record['status'] not in {'pending_dns', 'pending_platform', 'active', 'revoked'})
    if invalid:
        raise DomainBindingError('domain_storage_invalid', 409)
    try:
        if record['host'] != _host_for_tenant(record['host'], tenant):
            raise DomainBindingError('domain_storage_invalid', 409)
    except DomainBindingError:
        raise DomainBindingError('domain_storage_invalid', 409) from None
    for key in _TIME_FIELDS:
        if record[key] is not None and (type(record[key]) is not int or record[key] < 1):
            raise DomainBindingError('domain_storage_invalid', 409)
    if record['status'] == 'revoked':
        if record['challenge'] is not None or any(record[k] is not None for k in _TIME_FIELDS):
            raise DomainBindingError('domain_storage_invalid', 409)
    else:
        if (not isinstance(record['challenge'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['challenge'])
                or record['requested_at'] is None
                or record['challenge_expires_at'] != record['requested_at'] + CHALLENGE_TTL):
            raise DomainBindingError('domain_storage_invalid', 409)
        if record['status'] == 'pending_dns':
            if any(record[k] is not None for k in _TIME_FIELDS[2:]):
                raise DomainBindingError('domain_storage_invalid', 409)
        elif (record['dns_verified_at'] is None or record['dns_valid_until'] is None
                or not record['requested_at'] <= record['dns_verified_at'] <= record['challenge_expires_at']
                or not record['dns_verified_at'] < record['dns_valid_until'] <= record['dns_verified_at'] + VERIFICATION_TTL):
            raise DomainBindingError('domain_storage_invalid', 409)
        if record['status'] == 'pending_platform' and any(record[k] is not None for k in _TIME_FIELDS[4:]):
            raise DomainBindingError('domain_storage_invalid', 409)
        if record['status'] == 'active' and (record['platform_verified_at'] is None
                or record['platform_valid_until'] is None
                or not record['dns_verified_at'] <= record['platform_verified_at'] < record['dns_valid_until']
                or not record['platform_verified_at'] < record['platform_valid_until'] <= record['platform_verified_at'] + VERIFICATION_TTL):
            raise DomainBindingError('domain_storage_invalid', 409)
    return deepcopy(record)


def revision(tenant, record):
    return sha256(json.dumps([CONTRACT, tenant.id, tenant.slug, record], sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _active(record, now):
    return (record['status'] == 'active' and record['platform_verified_at'] <= now
        < min(record['dns_valid_until'], record['platform_valid_until']))


def build_domain_binding(tenant, *, can_edit=False, entitled=False, writes_blocked=False, now=None):
    now = _now(now)
    record = read_record(tenant)
    reason = ('maintenance' if writes_blocked else 'tenant_admin_required' if not can_edit
        else 'pro_plan_required' if not entitled else 'ready')
    active = _active(record, now) and entitled is True and tenant.is_active is True
    status = 'verification_expired' if record['status'] == 'active' and not active else record['status']
    dns = None
    if record['status'] in {'pending_dns', 'pending_platform'}:
        dns = {'type': 'TXT', 'name': '_chatboc-verify.' + record['host'],
            'value': 'chatboc-domain-verification=' + record['challenge'],
            'expires_at': record['challenge_expires_at']}
    return {'contract_version': CONTRACT, 'tenant': {'id': tenant.id, 'slug': tenant.slug},
        'revision': revision(tenant, record), 'version': record['version'],
        'host': record['host'], 'status': status, 'active': active,
        'can_edit': reason == 'ready', 'can_revoke': can_edit is True and not writes_blocked,
        'reason_code': reason, 'dns_proof': dns,
        'valid_until': min(record['dns_valid_until'], record['platform_valid_until']) if active else None,
        'save_endpoint': f'/api/admin/tenants/{tenant.slug}/domain',
        'provider_changes_performed': False,
        'scope_note': 'La solicitud prepara el dominio. Se activa después de verificar DNS, HTTPS y el acceso de la aplicación.'}


def read_dns_txt(host):
    """Read-only DNS with a fixed public TXT name, bounded time and record size."""
    import dns.resolver
    try:
        answer = dns.resolver.resolve('_chatboc-verify.' + normalize_host(host), 'TXT',
            lifetime=3, search=False, raise_on_no_answer=True)
        if len(answer) > 16:
            raise DomainBindingError('domain_dns_unavailable', 503)
        values = []
        for row in answer:
            raw = b''.join(row.strings)
            if len(raw) > 512:
                raise DomainBindingError('domain_dns_unavailable', 503)
            values.append(raw.decode('ascii'))
        return values
    except DomainBindingError:
        raise
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except Exception:
        raise DomainBindingError('domain_dns_unavailable', 503, 'No pudimos consultar el DNS. La solicitud sigue pendiente.') from None


def _collisions(session, tenant_model, tenant, host):
    # Existing JSON has no unique index. Domain writers take a table-level lock;
    # the public resolver independently rejects collisions, including legacy edits.
    for other in session.query(tenant_model).populate_existing().all():
        if other.id == tenant.id:
            continue
        try:
            legacy = normalize_host(other.dominio) if other.dominio else None
        except DomainBindingError:
            legacy = None
        # Legacy routing aliases www/apex. Reserve both here as a collision
        # boundary only, never as a way to choose the public tenant.
        legacy_aliases = {host, host[4:] if host.startswith('www.') else 'www.' + host}
        if legacy in legacy_aliases:
            raise DomainBindingError('host_in_use', 409, 'Ese dominio ya está asociado a otra organización.')
        record = read_record(other)
        if record['host'] == host and record['status'] != 'revoked':
            raise DomainBindingError('host_in_use', 409, 'Ese dominio ya tiene una solicitud en otra organización.')


def _locked_scope(session, tenant_model, user_model, tenant_id, actor_id, authorize):
    if session.get_bind().dialect.name == 'postgresql':
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text('LOCK TABLE tenant_profile IN SHARE ROW EXCLUSIVE MODE'))
    tenant = session.query(tenant_model).filter_by(id=tenant_id).populate_existing().with_for_update().one_or_none()
    actor = session.query(user_model).filter_by(id=actor_id).populate_existing().with_for_update().one_or_none()
    if tenant is None or actor is None or tenant.is_active is not True or authorize(actor, tenant) is not True:
        raise DomainBindingError('domain_forbidden', 403, 'Necesitás permiso de administración en esta organización.')
    return tenant, actor


def _persist(session, tenant, actor, audit_model, record, operation, entitlement, now):
    old = read_record(tenant)
    record['version'] = old['version'] + 1
    tenant.configuracion = {**(tenant.configuracion or {}), KEY: record}
    session.add(audit_model(tenant_id=tenant.id, actor_user_id=actor.id,
        event_type='organization.domain.' + operation, resource_type='tenant_profile',
        resource_id=str(tenant.id), details={'contract_version': CONTRACT,
            'host': record['host'], 'previous_version': old['version'], 'version': record['version'],
            'provider_changes_performed': False}))
    session.flush()
    result = {'contract_version': 'organization.domain_save.v1', 'saved': True,
        'domain': build_domain_binding(tenant, can_edit=True, entitled=entitlement(tenant), now=now),
        'provider_changes_performed': False}
    json.dumps(result, allow_nan=False)
    session.commit()
    return result


def save_domain_binding(session, tenant_model, user_model, audit_model, *, tenant_id,
        actor_id, data, authorize, entitlement, now=None, txt_reader=None):
    now = _now(now)
    try:
        if (not isinstance(data, dict) or not isinstance(data.get('operation'), str)
                or data.get('operation') not in {'request', 'verify_dns', 'revoke'}
                or set(data) != ({'expected_revision', 'operation', 'host'} if data['operation'] == 'request'
                    else {'expected_revision', 'operation'})):
            raise DomainBindingError('domain_request_invalid')
        if not isinstance(data['expected_revision'], str) or not re.fullmatch(r'[0-9a-f]{64}', data['expected_revision']):
            raise DomainBindingError('domain_revision_required', 428, 'Consultá el estado actual antes de guardar.')
        tenant, actor = _locked_scope(session, tenant_model, user_model, tenant_id, actor_id, authorize)
        if data['operation'] != 'revoke' and entitlement(tenant) is not True:
            raise DomainBindingError('domain_pro_required', 403, 'El dominio propio requiere un plan Pro o Full registrado en el servidor.')
        record = read_record(tenant)
        if revision(tenant, record) != data['expected_revision']:
            raise DomainBindingError('domain_revision_conflict', 412, 'La solicitud cambió. Actualizá el estado antes de continuar.')
        kind = data['operation']
        if kind == 'request':
            if record['status'] == 'active':
                raise DomainBindingError('domain_revoke_required', 409, 'Revocá el dominio anterior antes de reemplazarlo.')
            host = _host_for_tenant(data['host'], tenant)
            _collisions(session, tenant_model, tenant, host)
            record = {**empty_record(tenant), 'host': host, 'status': 'pending_dns',
                'challenge': secrets.token_hex(32), 'requested_at': now, 'challenge_expires_at': now + CHALLENGE_TTL}
        elif kind == 'verify_dns':
            if record['status'] != 'pending_dns' or not record['requested_at'] <= now < record['challenge_expires_at']:
                raise DomainBindingError('domain_dns_request_expired', 409, 'Solicitá una verificación nueva del dominio.')
            _collisions(session, tenant_model, tenant, record['host'])
            values = (txt_reader or read_dns_txt)(record['host'])
            if not isinstance(values, (list, tuple)) or len(values) > 16 or any(not isinstance(v, str) or not v.isascii() or len(v) > 512 for v in values):
                raise DomainBindingError('domain_dns_unavailable', 503)
            expected = 'chatboc-domain-verification=' + record['challenge']
            if not any(secrets.compare_digest(expected, value) for value in values):
                raise DomainBindingError('domain_dns_not_verified', 409, 'El TXT todavía no coincide. La solicitud sigue pendiente.')
            record.update(status='pending_platform', dns_verified_at=now, dns_valid_until=now + VERIFICATION_TTL)
        else:
            if record['host'] is None:
                raise DomainBindingError('domain_not_configured', 409)
            record = {**empty_record(tenant), 'host': record['host'], 'status': 'revoked'}
        return _persist(session, tenant, actor, audit_model, record, kind, entitlement, now)
    except SQLAlchemyError:
        session.rollback()
        raise DomainBindingError('domain_save_unconfirmed', 503, 'No pudimos confirmar el cambio. Consultá su estado antes de reintentar.') from None
    except BaseException:
        session.rollback()
        raise


@dataclass(frozen=True)
class PlatformObservation:
    """Trusted provisioner readback, never fields accepted from an owner HTTP request."""
    host: str
    frontend_project_id: str
    observed_at: int
    valid_until: int
    ownership_verified: bool
    https_ready: bool
    app_routes_ready: bool


def activate_verified_binding(session, tenant_model, user_model, audit_model, *, tenant_id,
        actor_id, expected_revision, authorize, entitlement, observation, expected_project_id, now=None):
    now = _now(now)
    try:
        tenant, actor = _locked_scope(session, tenant_model, user_model, tenant_id, actor_id, authorize)
        record = read_record(tenant)
        if entitlement(tenant) is not True:
            raise DomainBindingError('domain_pro_required', 403)
        if revision(tenant, record) != expected_revision:
            raise DomainBindingError('domain_revision_conflict', 412)
        if record['status'] != 'pending_platform' or not record['dns_verified_at'] <= now < record['dns_valid_until']:
            raise DomainBindingError('domain_platform_not_ready', 409)
        if (type(observation) is not PlatformObservation or observation.host != record['host']
                or not isinstance(expected_project_id, str) or not expected_project_id.startswith('prj_')
                or observation.frontend_project_id != expected_project_id
                or type(observation.observed_at) is not int or not 0 <= now - observation.observed_at <= 60
                or type(observation.valid_until) is not int or not now < observation.valid_until <= now + VERIFICATION_TTL
                or observation.ownership_verified is not True or observation.https_ready is not True
                or observation.app_routes_ready is not True):
            raise DomainBindingError('domain_platform_not_verified', 409)
        _collisions(session, tenant_model, tenant, record['host'])
        record.update(status='active', platform_verified_at=now, platform_valid_until=observation.valid_until)
        return _persist(session, tenant, actor, audit_model, record, 'activate_verified', entitlement, now)
    except SQLAlchemyError:
        session.rollback()
        raise DomainBindingError('domain_save_unconfirmed', 503) from None
    except BaseException:
        session.rollback()
        raise


def _public_logo(value):
    from services.organization_profile_settings import validate_public_url, ProfileSettingsError
    if not isinstance(value, str) or len(value) > 512 or any(unicodedata.category(c)[0] == 'C' for c in value):
        return None
    if value.startswith('/') and not value.startswith('//') and '\\' not in value:
        return value
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
            return None
        validate_public_url(value)
        return value
    except (ValueError, ProfileSettingsError):
        return None


def resolve_active_host(session, tenant_model, host, *, entitlement, now=None):
    now = _now(now)
    host = normalize_host(host)
    matches = []
    tenants = session.query(tenant_model).populate_existing().all()
    for tenant in tenants:
        try:
            record = read_record(tenant)
        except DomainBindingError:
            # Malformed registry cannot prove uniqueness, so do not choose a tenant.
            return None
        if record['host'] == host and record['status'] != 'revoked':
            matches.append((tenant, record))
    if len(matches) != 1:
        return None
    tenant, record = matches[0]
    if tenant.is_active is not True or entitlement(tenant) is not True or not _active(record, now):
        return None
    for other in tenants:
        if other.id == tenant.id:
            continue
        try:
            legacy_aliases = {host, host[4:] if host.startswith('www.') else 'www.' + host}
            if other.dominio and normalize_host(other.dominio) in legacy_aliases:
                return None
        except DomainBindingError:
            pass
    from services.organization_branding import build_workspace_appearance
    from services.plan_access import tenant_allows_workspace_branding
    appearance = build_workspace_appearance(tenant, entitled=tenant_allows_workspace_branding(tenant))
    palette = appearance['appearance'] if appearance else None
    theme = getattr(tenant, 'theme_json', None) or getattr(tenant, 'tema', None) or {}
    def color(name, default):
        value = theme.get(name) if isinstance(theme, dict) else None
        return value.upper() if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) else default
    brand = {'primary_color': color('primary_color', '#2563EB'), 'accent_color': color('accent_color', '#0F766E')}
    if palette and palette['active']:
        brand = {key + '_color': palette[key]['background'] for key in ('primary', 'accent')}
    return {'contract_version': PUBLIC, 'host': host, 'origin': 'https://' + host,
        'tenant': {'id': tenant.id, 'slug': tenant.slug, 'nombre': tenant.nombre, 'tipo': tenant.tipo,
            'logo_url': _public_logo(tenant.logo_url)}, 'brand': brand,
        'paths': {'home': '/', 'login': '/login', 'workspace': '/perfil'},
        'binding': {'status': 'active', 'verified': True,
            'valid_until': min(record['dns_valid_until'], record['platform_valid_until'])}}


def build_host_readiness(session, tenant_model, *, host, tenant_id, tenant_slug,
        expected_revision, nonce, backend_source, entitlement, now=None, txt_reader=None):
    """One pending-host infrastructure challenge, never public UX or authentication.

    A fresh nonce is selected by the operator for each observation. The exact
    owner-approved binding revision is required before any DNS lookup. The
    endpoint only exposes the caller's matching identity and deployment revision;
    it never issues a session, publishes a tenant, or activates a binding.
    """
    now = _now(now)
    host = normalize_host(host)
    if (type(tenant_id) is not int or tenant_id < 1
            or not isinstance(tenant_slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', tenant_slug)
            or not isinstance(expected_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_revision)
            or not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{64}', nonce)):
        raise DomainBindingError('host_readiness_invalid')
    tenant = session.query(tenant_model).filter_by(id=tenant_id, slug=tenant_slug).populate_existing().one_or_none()
    if tenant is None or tenant.is_active is not True or entitlement(tenant) is not True:
        return None
    record = read_record(tenant)
    if (record['host'] != host or record['status'] != 'pending_platform'
            or revision(tenant, record) != expected_revision
            or not record['dns_verified_at'] <= now < record['dns_valid_until']):
        return None
    try:
        _collisions(session, tenant_model, tenant, host)
    except DomainBindingError:
        return None
    if not isinstance(backend_source, str) or not re.fullmatch(r'[0-9a-f]{40}', backend_source):
        raise DomainBindingError('host_readiness_runtime_unavailable', 503)
    values = (txt_reader or read_dns_txt)(host)
    if (not isinstance(values, (list, tuple)) or len(values) > 16
            or any(not isinstance(value, str) or not value.isascii() or len(value) > 512 for value in values)):
        raise DomainBindingError('domain_dns_unavailable', 503)
    proof = 'chatboc-domain-verification=' + record['challenge']
    if not any(secrets.compare_digest(proof, value) for value in values):
        return None
    return {'contract_version': READINESS, 'host': host, 'nonce': nonce,
        'tenant': {'id': tenant.id, 'slug': tenant.slug}, 'revision': expected_revision,
        'backend_source': backend_source, 'observed_at': now,
        'scope': 'pending_domain_infrastructure_only', 'active': False,
        'auth_e2e_verified': False, 'private_assets_included': False}
