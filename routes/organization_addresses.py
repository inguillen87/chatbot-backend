"""Private owner requests and real platform-admin verified activation."""
import json
import os
import re

from flask import Blueprint, current_app, g, jsonify, request
from limits import parse
from limits.storage import RedisClusterStorage, RedisSentinelStorage, RedisStorage

from cutover_writer_fence import cutover_read_only_view, cutover_writer_fence_enabled
from extensions import limiter
from models import AuditEvent, TenantProfile, User, db
from routes.meta_credential_admin import _authenticate, RequestDenied
from services import organization_addresses as service
from services import organization_address_runtime as runtime
from utils.auth_helpers import auth_sin_escrituras_implicitas, token_requerido
from utils.meta_credential_http import PANEL_ORIGINS
from utils.organization_address_http import is_address_operator_request
from utils.roles import is_authorized_superadmin_user
from utils.tenant_admin_access import can_manage_tenant_control_plane

organization_addresses_bp = Blueprint('organization_addresses', __name__)
MAX_BODY = 4096


def _response(payload, status=200):
    result = jsonify(payload)
    result.status_code = status
    result.headers['Cache-Control'] = 'private, no-store'
    result.headers['Pragma'] = 'no-cache'
    result.headers['Vary'] = 'Cookie, Authorization, Origin'
    result.headers['X-Content-Type-Options'] = 'nosniff'
    return result


def _error(code, status, *, uncertain=False):
    return _response({'contract_version': 'organization.addresses_error.v2', 'reason_code': code,
        'retryable': False, **({'requires_status_check': True, 'database_outcome': 'unverified'}
            if uncertain else {'writes_performed': False})}, status)


def _origins():
    allowed = set(PANEL_ORIGINS)
    if not is_address_operator_request():
        allowed.update(value.strip() for value in os.getenv('CORS_ALLOWED_ORIGINS', '').split(',')
            if value.strip() and value.strip() != '*')
        allowed.add(request.host_url.rstrip('/'))
    return allowed


@organization_addresses_bp.before_request
def boundary():
    if request.query_string:
        return _error('address_request_invalid', 400)
    origin = request.headers.get('Origin')
    if origin is not None and origin not in _origins():
        return _error('address_origin_forbidden', 403)
    if request.method == 'OPTIONS':
        method = 'POST' if is_address_operator_request() else 'PUT'
        if origin not in _origins() or request.headers.get('Access-Control-Request-Method') not in {'GET', method}:
            return _error('address_origin_forbidden', 403)
        result = _response({}, 204)
        result.headers['Access-Control-Allow-Origin'] = origin
        result.headers['Access-Control-Allow-Credentials'] = 'true'
        result.headers['Access-Control-Allow-Methods'] = 'GET, ' + method
        result.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return result
    if request.method in {'POST', 'PUT'}:
        if (request.headers.get('Authorization') is None
                and request.cookies.get(current_app.config.get('AUTH_TOKEN_COOKIE_NAME', 'auth_token'))
                and origin not in _origins()):
            return _error('address_origin_forbidden', 403)
        if request.mimetype != 'application/json' or request.headers.get('Content-Encoding'):
            return _error('address_json_required', 415)
        if request.content_length is None or not 0 < request.content_length <= MAX_BODY:
            return _error('address_body_limit', 413)


def _body():
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value
    try:
        raw = request.get_data(cache=False)
        if len(raw) > MAX_BODY:
            raise ValueError
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise service.DomainBindingError('address_request_invalid') from None


def _rate_limit(actor_id):
    try:
        strategy = limiter.limiter
        if (current_app.config.get('TESTING') is not True and not isinstance(strategy.storage,
                (RedisStorage, RedisClusterStorage, RedisSentinelStorage))):
            raise service.DomainBindingError('address_rate_storage_unavailable', 503)
        for limit in ('12 per minute', '60 per hour'):
            if not strategy.hit(parse(limit), 'organization-addresses-v2', str(actor_id)):
                raise service.DomainBindingError('address_rate_limited', 429)
    except service.DomainBindingError:
        raise
    except Exception:
        raise service.DomainBindingError('address_rate_storage_unavailable', 503) from None


def _is_operator(user):
    claims = getattr(g, 'token_payload', {})
    return bool(is_authorized_superadmin_user(user) and isinstance(claims, dict)
        and claims.get('auth_provider') == 'clerk' and claims.get('session_kind') == 'clerk'
        and claims.get('auth_audience') in {'tenant_owner', 'panel'}
        and claims.get('auth_intent', 'tenant_owner') == 'tenant_owner' and not claims.get('impersonated_by'))


@organization_addresses_bp.route('/api/admin/tenants/<slug>/addresses', methods=['GET', 'PUT'])
@token_requerido
@auth_sin_escrituras_implicitas
def owner(current_user, slug):
    tenant = TenantProfile.query.filter_by(slug=slug).first()
    if tenant is None:
        return _error('address_not_found', 404)
    if not can_manage_tenant_control_plane(current_user, tenant):
        return _error('address_forbidden', 403)
    blocked = cutover_writer_fence_enabled(current_app.config)
    try:
        if request.method == 'GET':
            return _response(service.build_addresses(tenant, can_manage=True, writes_blocked=blocked,
                is_operator=_is_operator(current_user),
                runtime_available=runtime.runtime_available(current_app.config), config=current_app.config))
        if blocked:
            return _error('address_maintenance', 503)
        activation = service.activation_capabilities(is_operator=_is_operator(current_user),
            runtime_available=runtime.runtime_available(current_app.config))
        _rate_limit(current_user.id)
        result = service.save_addresses(db.session, TenantProfile, User, AuditEvent,
            tenant_id=tenant.id, actor_id=current_user.id, data=_body(),
            authorize=can_manage_tenant_control_plane, config=current_app.config)
        result['addresses']['capabilities']['activation'] = activation
        return _response(result)
    except service.DomainBindingError as exc:
        return _error(exc.code, exc.status, uncertain=exc.code == 'address_save_unconfirmed')
    except Exception:
        db.session.rollback()
        return _error('address_save_unconfirmed' if request.method == 'PUT' else 'address_runtime_unavailable',
            503, uncertain=request.method == 'PUT')


def _operator(action, slug):
    try:
        # This is the existing real Clerk JWT+durable-lineage platform-admin
        # authenticator; the Meta feature flag is not an alternate authority.
        actor = _authenticate()
        _rate_limit(actor.actor_id)
        result = runtime.run_operation(action, slug, _body(), actor, current_app.config)
        return _response(result)
    except RequestDenied as exc:
        suffix = exc.code.removeprefix('meta_operation_')
        return _error('address_' + suffix if re.fullmatch(r'[a-z_]{1,80}', suffix)
            else 'address_authentication_required', exc.status)
    except service.DomainBindingError as exc:
        return _error(exc.code, exc.status, uncertain=exc.code == 'address_save_unconfirmed')
    except Exception:
        return _error('address_runtime_unavailable', 503, uncertain=action == 'activate')
    finally:
        request.__dict__.pop('_cached_data', None)
        request.__dict__.pop('_cached_json', None)


@organization_addresses_bp.post('/api/admin/tenants/<slug>/addresses/activation-plan')
@cutover_read_only_view
def activation_plan(slug):
    return _operator('plan', slug)


@organization_addresses_bp.post('/api/admin/tenants/<slug>/addresses/activate')
def activate(slug):
    return _operator('activate', slug)
