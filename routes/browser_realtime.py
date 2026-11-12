"""Authenticated tenant owner trial; public/widget/demo credentials cannot issue voice."""
import re
from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import select
from models import db, TenantProfile, User, AuditEvent
from cutover_writer_fence import cutover_writer_fence_enabled
from utils.auth_helpers import token_requerido, auth_sin_escrituras_implicitas, is_user_auth_disabled, auth_session_version
from utils.roles import is_authorized_superadmin_user
from utils.tenant_admin_access import can_manage_tenant_control_plane
from services.auth_session_lifecycle import request_auth_session_active
from services.institutional_assistant import read_state
from services.institutional_assistant_content import ContentError
from services.browser_realtime import CONTRACT, UI, VoiceError, AcceptedCallError, VoiceLedger, limits, require_trial_current, validate_offer, session_config, provider_request, resolve_provider_key

browser_realtime_bp = Blueprint('browser_realtime', __name__)
PREFIX = '/api/admin/tenants/<slug>/realtime/browser'


def _response(value, status=200):
    response = jsonify(value)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    return response


def _actor():
    claims = getattr(g, 'token_payload', {}) or {}
    actor = getattr(g, 'current_user', None) if getattr(g, 'auth_credential_source', None) in ('token', 'flask_cookie') else None
    if (actor is None or claims.get('session_kind') in ('widget', 'demo')
        or is_user_auth_disabled(actor) or not request_auth_session_active(actor.id)):
        raise VoiceError('browser_voice_authenticated_owner_required', 403)
    return actor


def _tenant(slug):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', slug):
        raise VoiceError('browser_voice_tenant_missing', 404)
    tenant = db.session.execute(select(TenantProfile).where(TenantProfile.slug == slug)).scalar_one_or_none()
    if tenant is None:
        raise VoiceError('browser_voice_tenant_missing', 404)
    return tenant


def _authorize(actor, tenant, *, stopping=False):
    if not can_manage_tenant_control_plane(actor, tenant, require_active=not stopping) or (
            not is_authorized_superadmin_user(actor)
            and actor.id not in (tenant.municipio_id, tenant.pyme_id)):
        raise VoiceError('browser_voice_authenticated_owner_required', 403)


def _fail(error):
    db.session.rollback()
    return _response({'contract_version': CONTRACT, 'reason_code': error.code,
                      'message': UI['pending'] if error.status == 409 else UI['error']}, error.status)


def _record_accepted(ledger, tenant_id, actor_id, identifier, call_id, key):
    try:
        ledger.append(tenant_id, actor_id, identifier, 'accepted', {'call_id': call_id})
    except SQLAlchemyError:
        # The reservation is durable. Never retry creation or expose the SDP.
        # If its receipt cannot be stored, close the known call once; database
        # failure cannot guarantee a durable stop intent for this compensation.
        db.session.rollback()
        try:
            provider_request(key, actor_id, call_id=call_id)
        except VoiceError:
            raise VoiceError('browser_voice_close_pending', 409) from None
        try:
            ledger.append(tenant_id, actor_id, identifier, 'stopped', {'provider_close_accepted': True})
        except SQLAlchemyError:
            db.session.rollback()
        raise VoiceError('browser_voice_ledger_unavailable') from None


@browser_realtime_bp.get(PREFIX + '/capabilities')
@token_requerido
@auth_sin_escrituras_implicitas
def capabilities(current_user, slug):
    try:
        actor, tenant = _actor(), _tenant(slug)
        _authorize(actor, tenant)
        payload = {'contract_version': CONTRACT, 'enabled': False, 'owner_trial': True,
                   'ui': dict(UI), 'tenant': {'id': tenant.id, 'slug': tenant.slug},
                   'generated_video': False, 'telephone_calls': False}
        try:
            payload['limits'] = limits(tenant.configuracion)
            ledger = VoiceLedger(db.session, TenantProfile, AuditEvent)
            payload['admission'] = ledger.admission_snapshot(tenant.id, payload['limits'])
            ledger.require_admission(tenant.id, payload['limits'])
            if not resolve_provider_key(current_app.config):
                raise VoiceError('browser_voice_provider_not_configured')
            state = read_state(tenant, public=True)
            session_config(tenant.configuracion or {}, current_app.config, state)
            payload.update(enabled=True, revision=state['revision'])
        except (VoiceError, ContentError) as error:
            payload['reason_code'] = error.code
            if error.code == 'browser_voice_trial_expired':
                payload['ui']['disabled'] = 'La prueba de voz terminó. Podés seguir usando el chat por texto.'
            elif error.code == 'browser_voice_total_cap':
                payload['ui']['disabled'] = 'Se usaron las sesiones disponibles de esta prueba de voz. El chat por texto sigue disponible.'
            elif error.code == 'browser_voice_previous_session_pending':
                payload['ui']['disabled'] = UI['pending']
            elif error.code == 'browser_voice_hourly_cap':
                payload['ui']['disabled'] = 'Se alcanzó el límite horario de esta prueba de voz. Podés seguir por texto.'
        return _response(payload)
    except VoiceError as error:
        return _fail(error)
    except SQLAlchemyError:
        return _fail(VoiceError('browser_voice_ledger_unavailable'))


@browser_realtime_bp.post(PREFIX + '/sessions')
@token_requerido
@auth_sin_escrituras_implicitas
def start(current_user, slug):
    ledger = VoiceLedger(db.session, TenantProfile, AuditEvent)
    try:
        actor, tenant = _actor(), _tenant(slug)
        actor_id, tenant_id = actor.id, tenant.id
        version = auth_session_version(actor)
        _authorize(actor, tenant)
        if cutover_writer_fence_enabled(current_app.config):
            raise VoiceError('browser_voice_writer_fenced')
        if len(request.get_data(cache=True)) > 55 * 1024:
            raise VoiceError('browser_voice_request_too_large', 413)
        command = request.get_json(silent=True)
        if not isinstance(command, dict) or set(command) != {'sdp', 'revision', 'consent'} or command['consent'] is not True:
            raise VoiceError('browser_voice_explicit_consent_required', 400)
        offer = validate_offer(command['sdp'])
        key = resolve_provider_key(current_app.config)
        if not key:
            raise VoiceError('browser_voice_provider_not_configured')
        tenant = ledger.lock(tenant_id)
        _authorize(actor, tenant)
        quota = limits(tenant.configuracion)
        state = read_state(tenant, public=True)
        if command['revision'] != state['revision']:
            raise VoiceError('browser_voice_revision_conflict', 412)
        config = session_config(tenant.configuracion or {}, current_app.config, state)
        identifier = ledger.reserve(tenant_id, actor_id, state['revision'], quota)
        try:
            # A slow reservation commit must not start a call after expiry.
            require_trial_current(quota, ledger.clock())
        except VoiceError:
            ledger.append(tenant_id, actor_id, identifier, 'failed',
                          {'reason_code': 'browser_voice_trial_expired'})
            raise
        try:
            result = provider_request(key, actor_id, sdp=offer, config=config)
        except AcceptedCallError as error:
            # Location already acknowledged creation, even though its SDP is
            # unusable. Preserve the private ID and durably close exactly once.
            _record_accepted(ledger, tenant_id, actor_id, identifier, error.call_id, key)
            ledger.stop(tenant_id, actor_id, identifier, key, provider=provider_request)
            raise VoiceError(error.code) from None
        except VoiceError:
            # Intent remains durable/uncertain: no second call on timeout or error.
            raise
        _record_accepted(ledger, tenant_id, actor_id, identifier, result['call_id'], key)
        db.session.expire_all()
        fresh_actor = db.session.get(User, actor_id, populate_existing=True)
        fresh_tenant = db.session.get(TenantProfile, tenant_id, populate_existing=True)
        try:
            if (fresh_actor is None or is_user_auth_disabled(fresh_actor)
                    or auth_session_version(fresh_actor) != version
                    or not request_auth_session_active(actor_id)):
                raise VoiceError('browser_voice_session_retired', 403)
            _authorize(fresh_actor, fresh_tenant)
            require_trial_current(quota, ledger.clock())
            fresh_quota = limits(fresh_tenant.configuracion)
            require_trial_current(fresh_quota, ledger.clock())
            if fresh_quota != quota:
                raise VoiceError('browser_voice_state_changed', 412)
            if read_state(fresh_tenant, public=True)['revision'] != state['revision']:
                raise VoiceError('browser_voice_revision_conflict', 412)
            require_trial_current(quota, ledger.clock())
        except (VoiceError, ContentError) as error:
            # Close an acknowledged call when the session/corpus changed in flight.
            ledger.stop(tenant_id, actor_id, identifier, key, provider=provider_request)
            if error.code == 'browser_voice_trial_expired':
                raise error from None
            raise VoiceError('browser_voice_state_changed', 412) from None
        return _response({'contract_version': CONTRACT, 'session_id': identifier,
                          'sdp': result['sdp'], 'revision': state['revision'], 'limits': quota})
    except (VoiceError, ContentError) as error:
        return _fail(error)
    except SQLAlchemyError:
        return _fail(VoiceError('browser_voice_ledger_unavailable'))


@browser_realtime_bp.post(PREFIX + '/sessions/<identifier>/stop')
@token_requerido
@auth_sin_escrituras_implicitas
def stop(current_user, slug, identifier):
    try:
        actor, tenant = _actor(), _tenant(slug)
        _authorize(actor, tenant, stopping=True)
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise VoiceError('browser_voice_session_not_found', 404)
        if cutover_writer_fence_enabled(current_app.config):
            raise VoiceError('browser_voice_writer_fenced')
        key = resolve_provider_key(current_app.config)
        if not key:
            raise VoiceError('browser_voice_provider_not_configured')
        result = VoiceLedger(db.session, TenantProfile, AuditEvent).stop(tenant.id, actor.id, identifier, key)
        return _response({'contract_version': CONTRACT, **result})
    except VoiceError as error:
        return _fail(error)
    except SQLAlchemyError:
        return _fail(VoiceError('browser_voice_ledger_unavailable'))
