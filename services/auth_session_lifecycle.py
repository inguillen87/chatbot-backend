"""Durable actor/session retirement; proofs grant retirement and nothing else."""
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
from uuid import uuid4

import jwt
from flask import current_app, g, has_request_context, request, session
from sqlalchemy import select

from models import db, AuthSession, AuthProviderSession, AuthSessionAudit, AuthSessionRetirement, User

RETIRE_PATHS = frozenset({'/api/v2/auth/logout', '/api/v2/auth/sessions/retire'})
CONTRACT = 'chatboc.session_retirement.v1'
RECEIPT_CONTRACT = 'chatboc.session_retirement_receipt.v1'


class SessionLifecycleError(ValueError):
    def __init__(self, code, status=401):
        self.code, self.status = code, status
        super().__init__(code)


def is_retirement_request(req=None):
    req = req or request
    return req.path.rstrip('/') in RETIRE_PATHS


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _sid_hash(sid):
    return hashlib.sha256(str(sid).encode()).hexdigest() if sid else None


def _provider_row(sid, *, lock=False):
    if not isinstance(sid, str) or not 1 <= len(sid) <= 255:
        raise SessionLifecycleError('auth_provider_session_invalid')
    if lock:
        dialect = db.session.get_bind().dialect.name
        if dialect == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise SessionLifecycleError('auth_session_storage_unavailable', 503)
        db.session.execute(insert(AuthProviderSession).values(provider='clerk', provider_session_id=sid,
            revision=1, remote_status='pending').on_conflict_do_nothing())
    query = select(AuthProviderSession).where(AuthProviderSession.provider == 'clerk',
        AuthProviderSession.provider_session_id == sid).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    return db.session.execute(query).scalar_one_or_none()


def provider_session_revoked(sid):
    row = _provider_row(sid)
    return row is not None and row.revoked_at is not None


def revoke_provider_session(sid, *, reason, commit=False, confirmed=False):
    row = _provider_row(sid, lock=True)
    if row.revoked_at is None:
        row.revoked_at, row.reason = _now(), str(reason)[:120]
        row.revision += 1
    if confirmed:
        row.remote_status = 'confirmed'
    # Lock order is provider, then lineage everywhere, including exchange.
    lineages = db.session.execute(select(AuthSession).where(AuthSession.provider == 'clerk',
        AuthSession.provider_session_id == sid).with_for_update()).scalars().all()
    for lineage in lineages:
        if lineage.revoked_at is None:
            lineage.revoked_at = _now()
            lineage.revision += 1
    db.session.flush()
    if commit:
        db.session.commit()
    return True


def rotate_cookie_session():
    """A new SID even for an initially empty Flask-Session dictionary."""
    if not has_request_context():
        return None
    old_sid = getattr(session, 'sid', None)
    session.clear()
    if old_sid:
        delete = getattr(current_app.session_interface, '_delete_session', None)
        store_id = getattr(current_app.session_interface, '_get_store_id', None)
        if callable(delete) and callable(store_id):
            delete(store_id(old_sid))
    if not hasattr(session, 'sid'):
        raise SessionLifecycleError('auth_session_backend_unsupported', 503)
    session.sid = secrets.token_urlsafe(32)
    session.modified = True
    return session.sid


def issue_token(payload, *, bind_cookie=False, commit=True):
    """Issue a new verified panel lineage; public/demo credentials stay separate."""
    claims = dict(payload)
    if claims.get('session_kind') in {'widget', 'demo'} or claims.get('demo_mode'):
        return jwt.encode(claims, current_app.config['SECRET_KEY'], algorithm='HS256')
    actor_id = claims.get('user_id')
    actor = db.session.get(User, actor_id)
    if actor is None:
        raise SessionLifecycleError('auth_actor_not_available')
    from utils.auth_helpers import auth_session_version, is_user_auth_disabled
    if is_user_auth_disabled(actor):
        raise SessionLifecycleError('auth_actor_not_available')
    provider = 'clerk' if claims.get('auth_provider') == 'clerk' else 'native'
    sid = claims.get('clerk_sid') or claims.get('sid') if provider == 'clerk' else None
    if provider == 'clerk':
        provider_row = _provider_row(sid, lock=True)
        if provider_row.revoked_at is not None:
            raise SessionLifecycleError('auth_session_revoked')
    expires = claims.get('exp')
    if isinstance(expires, (int, float)):
        expires = datetime.fromtimestamp(expires, timezone.utc)
    if not isinstance(expires, datetime) or _aware(expires) <= _now():
        raise SessionLifecycleError('auth_session_expiration_invalid')
    audience = str(claims.get('audience') or 'panel')
    cookie_sid = rotate_cookie_session() if bind_cookie else None
    lineage = AuthSession(id=uuid4().hex, actor_id=actor.id, actor_version=auth_session_version(actor), provider=provider,
        audience=audience, provider_session_id=sid, flask_sid_hash=_sid_hash(cookie_sid),
        retirement_nonce=secrets.token_hex(32), created_at=_now(), expires_at=_aware(expires), revision=1)
    db.session.add(lineage)
    db.session.flush()
    claims.update(asid=lineage.id, jti=secrets.token_urlsafe(24),
                  auth_audience=audience, auth_provider=provider, iat=_now())
    token = jwt.encode(claims, current_app.config['SECRET_KEY'], algorithm='HS256')
    if bind_cookie and has_request_context():
        session['_auth_lineage_id'] = lineage.id
    if commit:
        db.session.commit()
    if has_request_context():
        g.issued_auth_lineage_id = lineage.id
    return token


def lineage_for_claims(claims, *, lock=False):
    lineage_id = claims.get('asid')
    if not isinstance(lineage_id, str) or not re.fullmatch(r'[a-f0-9]{32}', lineage_id):
        raise SessionLifecycleError('auth_session_reauthentication_required')
    query = select(AuthSession).where(AuthSession.id == lineage_id).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    row = db.session.execute(query).scalar_one_or_none()
    if (row is None or row.actor_id != claims.get('user_id') or row.provider != claims.get('auth_provider')
        or row.audience != claims.get('auth_audience') or not isinstance(claims.get('jti'), str)
        or row.revoked_at is not None or _aware(row.expires_at) <= _now()):
        raise SessionLifecycleError('auth_session_revoked')
    from utils.auth_helpers import auth_session_version, is_user_auth_disabled
    actor = db.session.get(User, row.actor_id, populate_existing=True)
    if actor is None or is_user_auth_disabled(actor) or auth_session_version(actor) != row.actor_version:
        raise SessionLifecycleError('auth_session_revoked')
    if row.provider == 'clerk':
        sid = claims.get('clerk_sid') or claims.get('sid')
        if sid != row.provider_session_id or provider_session_revoked(sid):
            raise SessionLifecycleError('auth_session_revoked')
    return row


def cookie_lineage_for_user(user_id):
    lineage_id = session.get('_auth_lineage_id')
    row = db.session.get(AuthSession, lineage_id, populate_existing=True) if lineage_id else None
    if (row is None or row.actor_id != int(user_id) or row.provider != 'native'
        or not row.flask_sid_hash or row.flask_sid_hash != _sid_hash(getattr(session, 'sid', None))
        or row.revoked_at is not None or _aware(row.expires_at) <= _now()):
        return None
    from utils.auth_helpers import auth_session_version, is_user_auth_disabled
    actor = db.session.get(User, row.actor_id, populate_existing=True)
    if actor is None or is_user_auth_disabled(actor) or auth_session_version(actor) != row.actor_version:
        return None
    return row


def _proof(row):
    material = f'{CONTRACT}:{row.id}:{row.actor_id}:{row.provider}:{row.retirement_nonce}'
    secret = str(current_app.config['SECRET_KEY']).encode()
    return row.id + '.' + hmac.new(secret, material.encode(), hashlib.sha256).hexdigest()


def retirement_descriptor(row):
    return {'contract_version': CONTRACT, 'actor_id': str(row.actor_id), 'provider': row.provider,
            'lineage_id': row.id, 'clerk_session_id': row.provider_session_id,
            'expires_at': _aware(row.expires_at).isoformat(), 'proof': _proof(row)}


def descriptor_for_token(token):
    claims = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
    return retirement_descriptor(lineage_for_claims(claims))


def descriptor_for_request():
    actor = getattr(g, 'current_user', None) or getattr(g, 'viewer', None)
    if getattr(g, 'auth_credential_source', None) == 'flask_cookie':
        row = cookie_lineage_for_user(actor.id if actor is not None else session.get('_user_id', 0))
        return retirement_descriptor(row) if row else None
    claims = getattr(g, 'token_payload', None)
    if getattr(g, 'auth_credential_source', None) == 'token' and not (isinstance(claims, dict) and claims.get('asid')):
        return None
    if (isinstance(claims, dict) and claims.get('asid')
        and (actor is None or claims.get('user_id') == actor.id)):
        return retirement_descriptor(lineage_for_claims(claims))
    token = getattr(g, 'auth_token', None)
    if token and not session.get('_user_id'):
        return descriptor_for_token(token)
    row = cookie_lineage_for_user(session.get('_user_id', 0))
    return retirement_descriptor(row) if row else None


def request_auth_session_active(actor_id):
    """Recheck the exact request credential after an external I/O boundary."""
    try:
        if getattr(g, 'auth_credential_source', None) == 'flask_cookie':
            return cookie_lineage_for_user(actor_id) is not None
        claims = getattr(g, 'token_payload', None)
        if (isinstance(claims, dict) and claims.get('asid')
            and claims.get('user_id') == actor_id):
            lineage_for_claims(claims)
            return True
        if getattr(g, 'auth_credential_source', None) == 'token':
            return False
        row = cookie_lineage_for_user(actor_id)
        return row is not None
    except SessionLifecycleError:
        return False


def attach_retirement_descriptor(response):
    try:
        return _attach_retirement_descriptor(response)
    except SessionLifecycleError as error:
        # Retirement may commit after route authorization but before delivery.
        # Discard the entire successful payload and every stale issuer cookie.
        rejected = current_app.json.response({'contract_version':'shared.error.v1', 'reason_code':error.code})
        rejected.status_code = error.status
        rejected.headers['Cache-Control'] = 'no-store'
        return rejected


def _attach_retirement_descriptor(response):
    """Verified login/exchange/refresh and /me share the retirement contract."""
    if is_retirement_request() or not response.is_json or response.status_code >= 400:
        return response
    if (request.blueprint not in {'auth', 'auth_api', 'v2_auth', 'legacy_auth', 'webauthn'}
        and request.path.rstrip('/') not in {'/api/me', '/api/login', '/login'}
        and not getattr(g, 'issued_auth_lineage_id', None)):
        return response
    value = response.get_json(silent=True)
    if not isinstance(value, dict):
        return response
    token = value.get('token')
    descriptor = None
    if request.path.rstrip('/').endswith('/me'):
        descriptor = descriptor_for_request()
    elif (isinstance(token, str) and token.count('.') == 2
          and (getattr(g, 'issued_auth_lineage_id', None) or value.get('session_retirement'))):
        claims = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        if claims.get('session_kind') not in {'widget', 'demo'} and not claims.get('demo_mode'):
            descriptor = retirement_descriptor(lineage_for_claims(claims))
    if descriptor is not None:
        value['session_retirement'] = descriptor
        claims = getattr(g, 'token_payload', None)
        if (getattr(g, 'auth_credential_source', None) == 'token' and isinstance(claims, dict)
            and claims.get('asid') == descriptor['lineage_id']
            and str(claims.get('user_id')) == descriptor['actor_id']
            and type(claims.get('impersonated_by')) is int and claims['impersonated_by'] > 0):
            value['session_context'] = {'kind': 'impersonation',
                                       'initiated_by_actor_id': str(claims['impersonated_by'])}
        response.set_data(current_app.json.dumps(value))
        response.headers['Cache-Control'] = 'no-store'
    return response


def refresh_native_token(token, *, expires_at):
    from utils.auth_helpers import user_from_token
    actor = user_from_token(token)
    if actor is None:
        raise SessionLifecycleError('auth_session_revoked')
    claims = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
    if claims.get('auth_provider') != 'native':
        raise SessionLifecycleError('provider_resync_required')
    row = lineage_for_claims(claims, lock=True)
    # Retain family and its fixed expiration. Children cannot survive retirement.
    claims.update(jti=secrets.token_urlsafe(24), iat=_now(), exp=min(_aware(expires_at), _aware(row.expires_at)))
    renewed = jwt.encode(claims, current_app.config['SECRET_KEY'], algorithm='HS256')
    descriptor = retirement_descriptor(row)
    db.session.commit()
    return renewed, descriptor


def retire_session(proof, request_id):
    if (not isinstance(proof, str) or not re.fullmatch(r'[a-f0-9]{32}\.[a-f0-9]{64}', proof)
        or not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', request_id)):
        raise SessionLifecycleError('session_retirement_command_invalid', 400)
    row = db.session.get(AuthSession, proof.split('.')[0], populate_existing=True)
    if row is None or not hmac.compare_digest(proof, _proof(row)) or _aware(row.expires_at) <= _now():
        raise SessionLifecycleError('session_retirement_proof_invalid')
    # Provider lock precedes the family lock, including retirement and exchange.
    # Capture the transition only after locking, never from a stale snapshot.
    if row.provider == 'clerk':
        _provider_row(row.provider_session_id, lock=True)
    row = db.session.execute(select(AuthSession).where(AuthSession.id == row.id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one()
    previously_revoked = row.revoked_at is not None
    if row.provider == 'clerk':
        revoke_provider_session(row.provider_session_id, reason='actor_retirement')
    existing = db.session.get(AuthSessionRetirement, request_id, populate_existing=True)
    if existing is not None:
        if existing.lineage_id != row.id:
            raise SessionLifecycleError('session_retirement_request_conflict', 409)
        db.session.commit()
        return {**existing.receipt, 'status': 'already_retired'}
    already = previously_revoked
    if row.revoked_at is None:
        row.revoked_at = _now()
        row.revision += 1
    receipt = {'contract_version': RECEIPT_CONTRACT, 'status': 'already_retired' if already else 'retired',
               'lineage_id': row.id, 'local_revoked': True,
               'provider_revocation': {'status': 'pending' if row.provider == 'clerk' else 'not_applicable'}}
    db.session.add(AuthSessionAudit(id=uuid4().hex, lineage_id=row.id, actor_id=row.actor_id,
        event_type='retirement_replay' if already else 'retired', request_id=request_id, created_at=_now()))
    db.session.add(AuthSessionRetirement(request_id=request_id, lineage_id=row.id, actor_id=row.actor_id,
        receipt=receipt, created_at=_now()))
    try:
        db.session.commit()
    except Exception as error:
        from sqlalchemy.exc import IntegrityError
        if not isinstance(error, IntegrityError):
            raise
        db.session.rollback()
        collision = db.session.get(AuthSessionRetirement, request_id, populate_existing=True)
        if collision is None:
            raise
        if collision.lineage_id != row.id:
            raise SessionLifecycleError('session_retirement_request_conflict', 409) from error
        return {**collision.receipt, 'status': 'already_retired'}
    return receipt


def complete_provider_retirement(lineage_id, request_id):
    """One bounded exact-SID call after local retirement commits.

    An uncertain network result remains pending. A durable attempting marker
    prevents another worker or replay from blindly repeating the provider call.
    """
    row = db.session.get(AuthSession, lineage_id, populate_existing=True)
    if row is None or row.revoked_at is None or row.provider != 'clerk':
        return 'not_applicable'
    provider = _provider_row(row.provider_session_id, lock=True)
    if provider.remote_status != 'pending':
        status = provider.remote_status
        db.session.commit()
        return status if status in {'confirmed', 'failed'} else 'pending'
    provider.remote_status = 'attempting'
    sid = row.provider_session_id
    db.session.commit()
    from services.clerk_auth_service import revoke_exact_clerk_session
    status = revoke_exact_clerk_session(sid)
    provider = _provider_row(sid, lock=True)
    # A terminal webhook arriving during I/O is authoritative confirmation.
    if provider.remote_status != 'confirmed':
        provider.remote_status = status if status in {'confirmed', 'failed'} else 'uncertain'
    final_status = 'pending' if provider.remote_status == 'uncertain' else provider.remote_status
    receipt = db.session.get(AuthSessionRetirement, request_id, populate_existing=True)
    if receipt is not None and receipt.lineage_id == lineage_id:
        receipt.receipt = {**receipt.receipt, 'provider_revocation': {'status': final_status}}
    db.session.add(AuthSessionAudit(id=uuid4().hex, lineage_id=lineage_id, actor_id=row.actor_id,
        event_type='provider_' + final_status, request_id=request_id, created_at=_now()))
    db.session.commit()
    return final_status
