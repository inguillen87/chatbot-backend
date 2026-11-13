"""Credential operations inside the existing, authenticated Production runtime.

No secret export, subprocess, new engine/DSN, provider mutation or token cache.
Each app transaction is independent of the request's authentication session.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time

from sqlalchemy import select, text
from sqlalchemy.orm import Session, lazyload

from models import AuditEvent, AuthProviderSession, AuthSession, ProviderConnection, ProviderSender, User, db
from services import meta_credential_operations as ops
from services.global_writer_authority import evaluate_global_writer_authority, global_writer_authority_lease
from services.meta_whatsapp_runtime import native_graph_request
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY, _keyring
from utils.auth_helpers import auth_session_version, is_user_auth_disabled
from utils.roles import is_authorized_superadmin_user

LOCAL_EXPIRY_SECONDS = 30 * 24 * 3600


@dataclass(frozen=True)
class ActorSession:
    actor_id: int
    lineage_id: str = field(repr=False)
    provider_session_id: str = field(repr=False)
    actor_version: int
    expires_at: int
    audience: str


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _validate_actor(session, actor, *, lock=False, now=None):
    now = int(time.time()) if now is None else now
    # Same lock order as durable session revocation: provider, lineage, actor.
    statements = (
        select(AuthProviderSession).where(AuthProviderSession.provider == "clerk",
            AuthProviderSession.provider_session_id == actor.provider_session_id),
        select(AuthSession).where(AuthSession.id == actor.lineage_id),
        select(User).options(lazyload("*")).where(User.id == actor.actor_id),
    )
    rows = []
    for statement in statements:
        if lock:
            statement = statement.with_for_update(read=True)
        rows.append(session.execute(statement.execution_options(populate_existing=True)).unique().scalar_one_or_none())
    provider, lineage, user = rows
    valid = (provider is not None and provider.revoked_at is None and lineage is not None
        and lineage.actor_id == actor.actor_id and lineage.provider == "clerk"
        and lineage.provider_session_id == actor.provider_session_id and lineage.audience == actor.audience
        and actor.audience in {"tenant_owner", "panel"}
        and lineage.actor_version == actor.actor_version and lineage.revoked_at is None
        and _aware(lineage.expires_at).timestamp() > now and actor.expires_at > now
        and user is not None and not is_user_auth_disabled(user)
        and auth_session_version(user) == actor.actor_version and is_authorized_superadmin_user(user))
    ops._require(valid, "meta_operation_session_revoked")


def release_auth_reads():
    # Authentication is explicitly read-only. Never commit its autobegun TX
    # together with a credential change, nor discard somebody else's writes.
    ops._require(not db.session.new and not db.session.dirty and not db.session.deleted,
                 "meta_operation_authentication_dirty")
    db.session.rollback()
    db.session.remove()


def database_identity(engine, config):
    url = engine.url
    if engine.dialect.name == "sqlite" and config.get("TESTING") is True:
        return hashlib.sha256(b"offline-meta-credential-test-database").hexdigest()
    ops._require(engine.dialect.name == "postgresql" and bool(url.host) and bool(url.database),
                 "meta_operation_postgresql_required")
    ops._require(set(url.query) <= {"sslmode", "channel_binding", "sslrootcert"}
                 and all(isinstance(value, str) for value in url.query.values())
                 and url.query.get("sslmode") in {"require", "verify-ca", "verify-full"}
                 and re.fullmatch(r"[A-Za-z0-9.-]+", url.host)
                 and not any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"))
                 and not (url.port is None and os.environ.get("PGPORT") not in (None, "", "5432")),
                 "meta_operation_database_identity_unverified")
    identity = {"backend": "postgresql", "host": url.host.lower(),
                "port": url.port or 5432, "database": url.database}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def _transaction(engine, *, read_only):
    postgres = engine.dialect.name == "postgresql"
    level = "REPEATABLE READ" if read_only and postgres else "SERIALIZABLE"
    with engine.connect().execution_options(isolation_level=level) as connection:
        with connection.begin() as transaction:
            if postgres:
                if read_only:
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                connection.execute(text("SET LOCAL search_path = public"))
                connection.execute(text("SET LOCAL statement_timeout = '10s'"))
                connection.execute(text("SET LOCAL lock_timeout = '3s'"))
                ops._require(connection.execute(text("SELECT current_database()")).scalar_one() == engine.url.database,
                             "meta_operation_database_identity_mismatch")
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                yield session
            if read_only:
                transaction.rollback()


def run_operation(action, body, actor, config, *, engine=None, graph_get=None, clock=None):
    """Plan or commit one target; return success only after COMMIT completes."""
    engine = db.engine if engine is None else engine
    clock = (lambda: int(time.time())) if clock is None else clock
    graph_get = native_graph_request if graph_get is None else graph_get
    fingerprint = database_identity(engine, config)
    request = ops.OperationRequest(body["target"], actor.actor_id, body["expected_revision"],
        body.get("expires_at"), fingerprint, body["expected_writer_epoch"])
    if action == "apply":
        ops._require(body.get("expected_database_identity_sha256") == fingerprint,
                     "meta_operation_database_identity_mismatch")
        # Apply must retain the exact expiry reviewed in plan, even when the
        # provider has no finite expiry and the local 30-day policy was used.
        ops._require(type(body.get("expires_at")) is int, "meta_operation_reviewed_expiry_required")
    with _transaction(engine, read_only=True) as session:
        _validate_actor(session, actor, now=clock())
        prepared = ops.prepare_operation(session, request, body["access_token"], config,
            graph_get=graph_get, clock=clock, expiry_policy_seconds=LOCAL_EXPIRY_SECONDS)
    if action == "plan":
        return prepared.public_payload()
    # The HTTP writer middleware already owns request_lifetime=True. The
    # default lease call reuses it, preserving FOR SHARE across this COMMIT.
    with global_writer_authority_lease(config) as lease:
        with _transaction(engine, read_only=False) as session:
            _validate_actor(session, actor, lock=True, now=clock())
            result = ops.apply_operation(session, prepared, body["access_token"], config,
                approved_plan_sha256=body["approved_plan_sha256"], writer_lease=lease, clock=clock)
            final = lease.revalidate(config)
            ops._require(final.allowed and final.enabled and final.epoch == request.expected_writer_epoch,
                         "meta_operation_writer_authority_denied")
            ops._require(prepared.observed_at <= clock() < prepared.valid_until,
                         "meta_operation_provider_proof_expired")
            ops._require(actor.expires_at > clock(), "meta_operation_session_revoked")
        result.update(committed=True, writes_performed=True, expiry=prepared.expiry_metadata)
        return result


def status(body, actor, config, *, engine=None, clock=None):
    engine = db.engine if engine is None else engine
    clock = (lambda: int(time.time())) if clock is None else clock
    target = ops.TARGETS[body["target"]]
    fingerprint = database_identity(engine, config)
    authority = evaluate_global_writer_authority(config)
    with _transaction(engine, read_only=True) as session:
        _validate_actor(session, actor, now=clock())
        # Return only metadata. inspect_state below may read encrypted config
        # internally to verify the binding, but never decrypts/returns a token.
        rows = session.execute(select(ProviderConnection.id, ProviderConnection.status,
            ProviderConnection.config[PRIVATE_CONFIG_KEY]["revision"].as_integer().label("revision"),
            ProviderConnection.config[PRIVATE_CONFIG_KEY]["expires_at"].as_integer().label("expires_at"))
            .where(ProviderConnection.tenant_id == target.tenant_id, ProviderConnection.provider == "meta",
                   ProviderConnection.channel == "whatsapp", ProviderConnection.environment == target.environment)
            .limit(3)).mappings().all()
        row = rows[0] if len(rows) == 1 else None
        revision = row["revision"] if row else None
        expiry = row["expires_at"] if row else None
        sender_ids = session.execute(select(ProviderSender.id).where(
            ProviderSender.tenant_id == target.tenant_id, ProviderSender.phone_number_id == target.phone_id,
            ProviderSender.provider_connection_id == row["id"] if row else text("false"))
            .limit(3)).scalars().all()
        event = session.execute(select(AuditEvent.event_type, AuditEvent.created_at,
            AuditEvent.details["plan_sha256"].as_string().label("plan_sha256"),
            AuditEvent.details["revision"].as_integer().label("revision"))
            .where(AuditEvent.tenant_id == target.tenant_id,
                AuditEvent.resource_type == "provider_connection",
                AuditEvent.resource_id == str(row["id"]) if row else text("false"),
                AuditEvent.event_type.in_(["provider_credential.meta_provisioned", "provider_credential.meta_rotated"]))
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1)).mappings().first()
        available = authority.enabled and authority.allowed and type(authority.epoch) is int
        reason = None
        try:
            request = ops.OperationRequest(target.name, actor.actor_id, target.expected_revision,
                clock() + LOCAL_EXPIRY_SECONDS, fingerprint, authority.epoch or 1)
            ops.inspect_state(session, request)
            _keyring(config)
            selected = ops._authority_config(target, config)
            registry = selected.get("META_WHATSAPP_APP_SECRETS")
            secret = registry.get(ops.APP_ID) if isinstance(registry, dict) else None
            ops._require(isinstance(secret, str) and 16 <= len(secret) <= 4096,
                         "meta_operation_app_authority_unconfigured")
        except ops.MetaCredentialOperationError as exc:
            available, reason = False, str(exc)
        except Exception:
            available, reason = False, "meta_operation_credential_keyring_unavailable"
    return {"contract": ops.CONTRACT, "target": target.name,
        "label": "Junín" if target.name == "junin-production" else "Tierra del Fuego (prueba)",
        "tenant_id": target.tenant_id, "tenant_slug": target.slug, "environment": target.environment,
        "provider": "meta", "channel": "whatsapp", "app_id": ops.APP_ID, "waba_id": target.waba_id,
        "phone_number_id": target.phone_id, "phone_number": target.phone,
        "expected_revision": target.expected_revision, "current_revision": revision,
        "next_revision": target.expected_revision + 1, "expected_writer_epoch": authority.epoch,
        "operation_available": bool(available), "reason_code": reason or (None if available else authority.reason_code),
        "database_identity_sha256": fingerprint, "connection_id": row["id"] if row else None,
        "sender_id": sender_ids[0] if len(sender_ids) == 1 else None,
        "connection_status": row["status"] if row else "absent" if not rows else "ambiguous",
        "expires_at": expiry, "expired": bool(type(expiry) is int and expiry <= clock()),
        "last_operation": ({"event_type": event["event_type"], "revision": event["revision"],
                            "plan_sha256": event["plan_sha256"], "created_at": event["created_at"].isoformat()}
                           if event else None), "writes_performed": False}
