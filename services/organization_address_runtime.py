"""Clerk platform-admin execution of an exact, observed address activation.

Uses the established durable session checks and runtime DB, never a caller DSN,
token, observation, or alternative authentication credential. Provider reads
complete before the short writer transaction/namespace lock begins.
"""
from dataclasses import asdict
from contextlib import contextmanager
import os
import re
import time

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import text
from sqlalchemy.orm import Session

from cutover_writer_fence import cutover_writer_fence_enabled
from models import AuditEvent, TenantProfile, User, db
from services import meta_credential_admin_runtime as admin_session
from services import organization_addresses as addresses
from services.organization_address_vercel import AddressVercelAdapter, plan_digest
from services.organization_domain_vercel import VercelTransport, ProvisioningError
from services.global_writer_authority import global_writer_authority_lease
from utils.roles import is_authorized_superadmin_user

TEAM = 'team_BV1xuY6BnEzGanfok8GAyjZv'


def runtime_available(config):
    token = config.get('ORGANIZATION_ADDRESS_VERCEL_TOKEN')
    return bool(config.get('ORGANIZATION_ADDRESS_ACTIVATION_ENABLED') is True
        and isinstance(token, str) and 16 <= len(token) <= 4096
        and token == token.strip() and token.isascii() and not any(char.isspace() for char in token)
        and config.get('ORGANIZATION_ADDRESS_VERCEL_TEAM_ID') == TEAM
        and isinstance(config.get('BACKEND_VERSION'), str)
        and re.fullmatch(r'[a-f0-9]{40}', config['BACKEND_VERSION'])
        and (config.get('TESTING') is True or
            (os.environ.get('VERCEL') == '1' and os.environ.get('VERCEL_ENV') == 'production')))


def _actor(session, actor, *, lock=False, now):
    try:
        admin_session._validate_actor(session, actor, lock=lock, now=now)
    except Exception:
        raise addresses.DomainBindingError('address_session_revoked', 401) from None


def _body(action, body):
    keys = {'kind', 'expected_revision', 'frontend_deployment', 'frontend_source', 'routes_sha256'}
    if action == 'activate':
        keys.add('approved_plan_sha256')
    addresses._require(action in {'plan', 'activate'} and isinstance(body, dict) and set(body) == keys
        and isinstance(body['kind'], str) and body['kind'] in addresses.KINDS
        and all(isinstance(body[key], str) and re.fullmatch(r'[a-f0-9]{64}', body[key])
            for key in {'expected_revision', 'routes_sha256'} | ({'approved_plan_sha256'} if action == 'activate' else set()))
        and isinstance(body['frontend_deployment'], str)
        and re.fullmatch(r'dpl_[A-Za-z0-9]{8,128}', body['frontend_deployment'])
        and isinstance(body['frontend_source'], str) and re.fullmatch(r'[a-f0-9]{40}', body['frontend_source']),
        'address_request_invalid')


def _prepare(session, slug, body, config, now):
    tenant = session.query(TenantProfile).filter_by(slug=slug).populate_existing().one_or_none()
    addresses._require(tenant is not None, 'address_not_found', 404)
    plan = addresses.prepare_plan(tenant, kind=body['kind'], expected_revision=body['expected_revision'],
        frontend_deployment=body['frontend_deployment'], frontend_source=body['frontend_source'],
        routes_sha256=body['routes_sha256'], backend_source=config['BACKEND_VERSION'], config=config, now=now)
    row, source = addresses.slots(tenant)[plan.kind]
    addresses.assert_available(session, TenantProfile, tenant,
        'legacy_v1' if source == 'legacy_v1' else plan.kind, row['host'])
    return plan


@contextmanager
def _transaction(engine, *, read_only):
    postgres = engine.dialect.name == 'postgresql'
    # The namespace lock serializes claims. READ COMMITTED is deliberate: a
    # snapshot taken before waiting for that lock must not hide a winning claim.
    level = ('REPEATABLE READ' if read_only else 'READ COMMITTED') if postgres else 'SERIALIZABLE'
    with engine.connect().execution_options(isolation_level=level) as connection:
        with connection.begin() as transaction:
            if postgres:
                if read_only:
                    connection.execute(text('SET TRANSACTION READ ONLY'))
                connection.execute(text('SET LOCAL search_path = public'))
                connection.execute(text("SET LOCAL statement_timeout = '10s'"))
                connection.execute(text("SET LOCAL lock_timeout = '3s'"))
                addresses._require(connection.execute(text('SELECT current_database()')).scalar_one()
                    == engine.url.database, 'address_database_identity_mismatch', 503)
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                yield session
            if read_only:
                transaction.rollback()


def run_operation(action, slug, body, actor, config, *, engine=None, adapter=None, clock=None):
    """Plan is read-only. Activation reports saved only after the outer COMMIT."""
    _body(action, body)
    addresses._require(runtime_available(config), 'address_operator_runtime_unavailable', 503)
    addresses._require(not cutover_writer_fence_enabled(config), 'address_maintenance', 503)
    engine = db.engine if engine is None else engine
    clock = (lambda: int(time.time())) if clock is None else clock
    try:
        # Validate the configured app engine without creating/exporting a DSN.
        admin_session.database_identity(engine, config)
        with _transaction(engine, read_only=True) as session:
            _actor(session, actor, now=clock())
            plan = _prepare(session, slug, body, config, clock())
        if action == 'activate':
            addresses._require(body['approved_plan_sha256'] == plan_digest(plan), 'address_plan_conflict', 412)
        # No DB transaction or namespace lock spans these provider/TLS reads.
        observer = adapter if adapter is not None else AddressVercelAdapter(VercelTransport(
            config['ORGANIZATION_ADDRESS_VERCEL_TOKEN'], team_id=TEAM), clock=clock)
        observation = observer.observe(plan)
        if action == 'plan':
            # Recheck authorization and CAS after network latency too.
            with _transaction(engine, read_only=True) as session:
                _actor(session, actor, now=clock())
                fresh = _prepare(session, slug, body, config, clock())
                addresses._require(plan_digest(fresh) == plan_digest(plan), 'address_plan_conflict', 412)
            return {'contract_version': 'organization.address_activation_plan.v2',
                'plan': asdict(plan), 'plan_sha256': plan_digest(plan),
                'observed_at': observation.observed_at, 'valid_until': observation.valid_until,
                'writes_performed': False, 'provider_changes_performed': False, 'auth_e2e_verified': False}
        with global_writer_authority_lease(config) as lease:
            addresses._require(lease.revalidate(config).allowed, 'address_writer_authority_denied', 503)
            with _transaction(engine, read_only=False) as session:
                # Preserve v1/v2 lock order: namespace, durable provider/lineage,
                # then target + actor. There is no external I/O in this block.
                addresses.legacy.lock_address_writers(session)
                _actor(session, actor, lock=True, now=clock())
                result = addresses.activate_address(session, TenantProfile, User, AuditEvent,
                    actor_id=actor.actor_id, plan=plan, observation=observation,
                    approved_plan_sha256=body['approved_plan_sha256'],
                    authorize=lambda user, _tenant: is_authorized_superadmin_user(user),
                    config=config, clock=clock, commit=False)
                addresses._require(lease.revalidate(config).allowed
                    and not cutover_writer_fence_enabled(config), 'address_writer_authority_denied', 503)
                addresses._require(actor.expires_at > clock()
                    and observation.observed_at <= clock() < observation.valid_until
                    and clock() - observation.observed_at <= 60, 'address_platform_not_verified', 409)
            result.update(committed=True, writes_performed=True, approved_plan_sha256=plan_digest(plan))
            result['addresses']['capabilities']['activation'] = addresses.activation_capabilities(
                is_operator=True, runtime_available=True)
            return result
    except addresses.DomainBindingError:
        raise
    except ProvisioningError:
        raise addresses.DomainBindingError('address_provider_verification_failed', 409) from None
    except SQLAlchemyError:
        raise addresses.DomainBindingError('address_save_unconfirmed' if action == 'activate'
            else 'address_runtime_unavailable', 503) from None
    except Exception:
        raise addresses.DomainBindingError('address_save_unconfirmed' if action == 'activate'
            else 'address_runtime_unavailable', 503) from None
