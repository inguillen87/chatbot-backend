"""Repair only a legacy self-owner reference inside explicit membership.

This resource never accepts credentials, roles, organization IDs or grants.
It uses the existing audit table and serializes changes on the target account.
"""
from hashlib import sha256
import json
import re
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from models import AdminAuditLog, TenantProfile, User
from utils.auth_helpers import auth_session_version, is_clerk_managed_user, is_demo_user_account, is_user_auth_disabled
from utils.roles import canonical_role, is_authorized_superadmin_user, normalize_tenant_type

CONTRACT = 'native_admin.legacy_membership.v1'
LIST_CONTRACT = 'native_admin.legacy_membership_list.v1'
AUDIT_ACTION = 'normalize_native_admin_legacy_reference'


class NativeAdminMembershipError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _authorize(actor):
    if actor is None or is_user_auth_disabled(actor) or not is_authorized_superadmin_user(actor):
        raise NativeAdminMembershipError('superadmin_required', 403)


def _tenant(session, slug, *, lock=False):
    query = session.query(TenantProfile).filter(TenantProfile.slug == slug).populate_existing()
    if lock:
        query = query.with_for_update(of=TenantProfile)
    tenant = query.one_or_none()
    if tenant is None:
        raise NativeAdminMembershipError('tenant_not_found', 404)
    return tenant


def _target(session, tenant, user_id, *, lock=False):
    query = session.query(User).filter(User.id == user_id).populate_existing()
    if lock:
        query = query.with_for_update(of=User)
    target = query.one_or_none()
    # Selection by the organization owner or by email is never a substitute
    # for the actual target's explicit organization membership.
    if target is None or target.tenant_id != tenant.id or target.tenant_slug != tenant.slug:
        raise NativeAdminMembershipError('native_admin_not_found', 404)
    return target


def _uses_native_admin_auth(target):
    metadata = target.accesibilidad if isinstance(target.accesibilidad, dict) else {}
    auth = metadata.get('auth') if isinstance(metadata.get('auth'), dict) else {}
    provider = str(auth.get('provider') or '').strip().lower()
    return not is_clerk_managed_user(target) and provider in {'', 'native', 'local', 'password'}


def _revision(tenant, target, owner, target_ownership, owner_profiles):
    values = [CONTRACT, tenant.id, tenant.slug, tenant.tipo, tenant.is_active,
        tenant.municipio_id, tenant.pyme_id, target.id, target.name, target.email, target.rol, target.tipo_chat,
        target.tenant_id, target.tenant_slug, target.municipio_id, target.pyme_id,
        target.empresa_id, _uses_native_admin_auth(target), is_clerk_managed_user(target), is_user_auth_disabled(target),
        is_demo_user_account(target), owner.id if owner else None,
        owner.tenant_id if owner else None, owner.tenant_slug if owner else None,
        sorted(target_ownership), sorted(owner_profiles)]
    encoded = json.dumps(values, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return sha256(encoded.encode('utf-8')).hexdigest()


def _descriptor(session, tenant, target):
    owner_id = tenant.municipio_id
    owner = session.get(User, owner_id, populate_existing=True) if owner_id else None
    target_ownership = [row[0] for row in session.query(TenantProfile.id).filter(or_(
        TenantProfile.municipio_id == target.id, TenantProfile.pyme_id == target.id)).all()]
    owner_profiles = [row[0] for row in session.query(TenantProfile.id).filter(or_(
        TenantProfile.municipio_id == owner_id, TenantProfile.pyme_id == owner_id)).all()] if owner_id else []
    reason = 'ready'
    if tenant.is_active is not True:
        reason = 'tenant_inactive'
    elif normalize_tenant_type(tenant.tipo) != 'municipio' or tenant.pyme_id is not None:
        reason = 'municipality_reference_required'
    elif canonical_role(target.rol) != 'admin' or normalize_tenant_type(target.tipo_chat) != 'municipio':
        reason = 'native_municipality_admin_required'
    elif not _uses_native_admin_auth(target):
        reason = 'native_admin_required'
    elif is_user_auth_disabled(target) or is_demo_user_account(target):
        reason = 'native_admin_unavailable'
    elif target_ownership:
        reason = 'target_is_tenant_owner'
    elif target.pyme_id is not None or target.empresa_id is not None:
        reason = 'conflicting_legacy_reference'
    elif owner is None or owner.id == target.id or owner_profiles != [tenant.id]:
        reason = 'organization_owner_ambiguous'
    elif (owner.tenant_id not in (None, tenant.id)
            or owner.tenant_slug not in (None, '', tenant.slug)):
        reason = 'organization_owner_conflict'
    elif target.municipio_id == owner_id:
        reason = 'already_consistent'
    elif target.municipio_id != target.id:
        reason = 'legacy_self_reference_required'
    can_apply = reason == 'ready'
    state = 'needs_normalization' if can_apply else ('already_consistent' if reason == 'already_consistent' else 'blocked')
    return {
        'contract_version': CONTRACT,
        'tenant': {'id': tenant.id, 'slug': tenant.slug, 'nombre': tenant.nombre},
        'target': {'id': target.id, 'name': str(target.name or ''), 'email': str(target.email or ''),
            'role': target.rol, 'tipo_chat': normalize_tenant_type(target.tipo_chat),
            'tenant_id': target.tenant_id, 'tenant_slug': target.tenant_slug,
            'auth_provider': ('native' if _uses_native_admin_auth(target)
                else ('clerk' if is_clerk_managed_user(target) else 'unknown'))},
        'state': state, 'can_apply': can_apply, 'reason_code': reason,
        'relation': {'field': 'municipio_id', 'current_owner_reference': target.municipio_id,
            'expected_owner_reference': owner_id, 'is_tenant_owner': bool(target_ownership)},
        'expected_revision': _revision(tenant, target, owner, target_ownership, owner_profiles),
        'permissions': {'can_read': True, 'can_normalize_legacy_reference': can_apply,
            'credentials_change_allowed': False, 'membership_change_allowed': False},
        'credentials_changed': False, 'provider_calls_performed': False,
    }


def read_native_admin_membership(session, *, actor, slug, user_id):
    _authorize(actor)
    tenant = _tenant(session, slug)
    return _descriptor(session, tenant, _target(session, tenant, user_id))


def list_native_admin_memberships(session, *, actor, slug):
    _authorize(actor)
    tenant = _tenant(session, slug)
    users = session.query(User).filter(User.tenant_id == tenant.id,
        User.tenant_slug == tenant.slug).order_by(User.id).all()
    items = []
    for user in users:
        if (canonical_role(user.rol) != 'admin' or not _uses_native_admin_auth(user)
                or normalize_tenant_type(user.tipo_chat) != 'municipio'):
            continue
        item = _descriptor(session, tenant, user)
        if item['relation']['is_tenant_owner']:
            continue
        items.append(item)
    return {'contract_version': LIST_CONTRACT,
        'tenant': {'id': tenant.id, 'slug': tenant.slug, 'nombre': tenant.nombre},
        'items': items, 'permissions': {'can_read': True,
            'credentials_change_allowed': False, 'membership_change_allowed': False},
        'credentials_changed': False, 'provider_calls_performed': False}


def _validate_action(data):
    if not isinstance(data, dict) or set(data) != {'expected_revision', 'request_id'}:
        raise NativeAdminMembershipError('invalid_legacy_membership_action', 400)
    revision = data.get('expected_revision')
    if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{64}', revision):
        raise NativeAdminMembershipError('expected_revision_required', 400)
    request_id = data.get('request_id')
    try:
        parsed = UUID(request_id) if isinstance(request_id, str) else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != request_id:
        raise NativeAdminMembershipError('request_id_required', 400)
    return revision, request_id


def normalize_native_admin_membership(session, *, actor, actor_session_version, slug, user_id, data):
    _authorize(actor)
    expected, request_id = _validate_action(data)
    try:
        tenant = _tenant(session, slug, lock=True)
        # Lock all account rows in a deterministic order after the organization.
        ids = sorted({value for value in (actor.id, user_id, tenant.municipio_id) if value is not None})
        locked = session.query(User).filter(User.id.in_(ids)).order_by(User.id).populate_existing().with_for_update(of=User).all()
        accounts = {user.id: user for user in locked}
        _authorize(accounts.get(actor.id))
        if (isinstance(actor_session_version, bool) or not isinstance(actor_session_version, int)
                or actor_session_version != auth_session_version(accounts[actor.id])):
            raise NativeAdminMembershipError('superadmin_session_revoked', 403)
        target = _target(session, tenant, user_id)
        current = _descriptor(session, tenant, target)
        target_object = f'tenant:{tenant.id}:user:{target.id}'
        prior = session.query(AdminAuditLog).filter(
            AdminAuditLog.action == AUDIT_ACTION,
            AdminAuditLog.target_object == target_object,
            AdminAuditLog.details['request_id'].as_string() == request_id,
        ).one_or_none()
        if prior is not None:
            details = prior.details or {}
            if (prior.admin_user_id != actor.id or details.get('previous_revision') != expected
                    or details.get('revision') != current['expected_revision']
                    or current['state'] != 'already_consistent'):
                raise NativeAdminMembershipError('legacy_membership_revision_conflict', 412)
            result = {**current, 'action_receipt': details['receipt'], 'idempotent_replay': True}
            session.rollback()  # A confirmed receipt read performs no mutation.
            return result
        if current['expected_revision'] != expected:
            raise NativeAdminMembershipError('legacy_membership_revision_conflict', 412)
        if not current['can_apply']:
            raise NativeAdminMembershipError(current['reason_code'], 409)
        old_reference = target.municipio_id
        target.municipio_id = tenant.municipio_id
        session.flush()
        after = _descriptor(session, tenant, target)
        if after['state'] != 'already_consistent':
            raise NativeAdminMembershipError('legacy_membership_unconfirmed', 503)
        receipt = {'request_id': request_id, 'target_user_id': target.id, 'tenant_id': tenant.id,
            'old_reference': old_reference, 'new_reference': target.municipio_id,
            'applied': True, 'revision': after['expected_revision']}
        # Audit failure must abort the same transaction as the account change.
        session.add(AdminAuditLog(admin_user_id=actor.id, action=AUDIT_ACTION,
            target_object=target_object, details={'contract_version': CONTRACT,
                'request_id': request_id, 'previous_revision': expected,
                'revision': after['expected_revision'], 'receipt': receipt,
                'changed_fields': ['municipio_id'], 'credentials_changed': False,
                'role_changed': False, 'organization_changed': False}))
        session.flush()
        result = {**after, 'action_receipt': receipt, 'idempotent_replay': False}
        json.dumps(result, allow_nan=False)
        session.commit()
        return result
    except SQLAlchemyError as error:
        session.rollback()
        raise NativeAdminMembershipError('legacy_membership_unconfirmed', 503) from error
    except BaseException:
        session.rollback()
        raise
