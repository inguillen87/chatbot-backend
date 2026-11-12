"""Internal, bounded Meta provisioning/renewal; no public upload or message send.

The operator authenticates the process and supplies a token in memory. Planning
is read-only. Applying requires the same approved plan, a fresh provider proof,
the current global writer lease, and one transaction owned by the caller.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import hmac
import json
import re
import time
from typing import Any, Callable, Mapping

from sqlalchemy import select, text, update
from sqlalchemy.orm import lazyload

from cutover_writer_fence import cutover_writer_fence_enabled
from models import AuditEvent, ProviderConnection, ProviderSender, TenantProfile, User
from services import meta_whatsapp_credentials as vault
from services.meta_whatsapp_cloud import MetaSenderSnapshot
from services.meta_whatsapp_runtime import (
    APP_SECRETS_KEY, GRAPH_VERSION_KEY, MetaOutboundRuntime, MetaRuntimeError,
    _object_response, native_graph_request,
)
from services.provider_connection_cutover_contract import MANAGED_CONNECTION_MARKER
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY, _keyring
from utils.roles import is_authorized_superadmin_user

CONTRACT = "chatboc.meta_credential_operation.v1"
APP_ID = "1719510329487224"
_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{16,4096}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class MetaCredentialOperationError(RuntimeError):
    """Only application-owned fixed codes cross the CLI boundary."""


def _require(condition, code):
    if not condition:
        raise MetaCredentialOperationError(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode()


@dataclass(frozen=True)
class Target:
    name: str
    tenant_id: int
    slug: str
    environment: str
    waba_id: str
    phone_id: str
    phone: str
    connection_id: int | None
    sender_id: int | None
    expected_revision: int
    callback: str


TARGETS = {
    "junin-production": Target("junin-production", 22, "junin", "production",
        "10068207913300410", "660753250460901", "+17432643718", None, None, 0,
        "https://api.chatboc.ar/webhook/meta/whatsapp/" + APP_ID),
    "tdf-sandbox": Target("tdf-sandbox", 46, "tierra-del-fuego", "sandbox",
        "2192778428137676", "1137550632773388", "+15556565679", 3, 1, 6,
        "https://api.chatboc.ar/webhook/meta/tdf-sandbox"),
}


@dataclass(frozen=True)
class OperationRequest:
    target: str
    actor_user_id: int
    expected_revision: int
    expires_at: int | None
    database_identity_sha256: str
    expected_writer_epoch: int

    def validate(self, now):
        _require(self.target in TARGETS, "meta_operation_target_invalid")
        _require(type(self.actor_user_id) is int and self.actor_user_id > 0,
                 "meta_operation_actor_invalid")
        _require(type(self.expected_revision) is int
                 and self.expected_revision == TARGETS[self.target].expected_revision,
                 "meta_operation_expected_revision_invalid")
        _require(type(self.expires_at) is int and self.expires_at > now,
                 "meta_operation_expiry_invalid")
        _require(isinstance(self.database_identity_sha256, str)
                 and bool(_DIGEST.fullmatch(self.database_identity_sha256)),
                 "meta_operation_database_identity_invalid")
        _require(type(self.expected_writer_epoch) is int and self.expected_writer_epoch > 0,
                 "meta_operation_writer_epoch_invalid")


@dataclass
class InspectedState:
    tenant: Any = field(repr=False)
    connection: Any = field(repr=False)
    sender: Any = field(repr=False)
    fingerprint: str


@dataclass(frozen=True)
class PreparedOperation:
    request: OperationRequest
    plan: dict
    plan_sha256: str
    valid_until: int
    observed_at: int
    expiry_metadata: dict | None = None

    def public_payload(self):
        return {"contract": CONTRACT, "dry_run": True, "plan": self.plan,
                "plan_sha256": self.plan_sha256, "provider_checked_at": self.observed_at,
                "writes_performed": False, "messages_sent": False,
                "provider_mutations": False, **({"expiry": self.expiry_metadata} if self.expiry_metadata else {})}


def _columns(row):
    if row is None:
        return None
    # Used only inside a digest. Never return configuration/envelope contents.
    return {column.name: getattr(row, column.name).isoformat()
            if hasattr(getattr(row, column.name), "isoformat") else getattr(row, column.name)
            for column in row.__table__.columns}


def inspect_state(session, request: OperationRequest, *, lock=False):
    target = TARGETS[request.target]
    stmt = select(TenantProfile).options(lazyload("*")).where(TenantProfile.id == target.tenant_id)
    if lock:
        stmt = stmt.with_for_update()
    tenant = session.execute(stmt.execution_options(populate_existing=True, autoflush=False)).scalar_one_or_none()
    _require(tenant is not None and tenant.is_active is True and tenant.slug == target.slug
             and tenant.municipio_id is not None, "meta_operation_tenant_mismatch")
    actor = session.execute(select(User).options(lazyload("*")).where(User.id == request.actor_user_id)
        .execution_options(populate_existing=True, autoflush=False)).unique().scalar_one_or_none()
    _require(actor is not None and is_authorized_superadmin_user(actor),
             "meta_operation_platform_admin_required")
    metadata = getattr(actor, "accesibilidad", None)
    _require(metadata is None or isinstance(metadata, dict), "meta_operation_actor_disabled")
    auth = (metadata or {}).get("auth", {})
    clerk = auth.get("clerk", {}) if isinstance(auth, dict) else None
    _require(isinstance(auth, dict) and isinstance(clerk, dict)
             and not auth.get("disabled") and not clerk.get("disabled"), "meta_operation_actor_disabled")
    connections = session.execute(select(ProviderConnection).where(
        ProviderConnection.tenant_id == target.tenant_id,
        ProviderConnection.provider == "meta", ProviderConnection.channel == "whatsapp",
        ProviderConnection.environment == target.environment)
        .execution_options(populate_existing=True, autoflush=False)).scalars().all()
    senders = session.execute(select(ProviderSender).where(
        ProviderSender.channel == "whatsapp",
        (ProviderSender.phone_number_id == target.phone_id)
        | (ProviderSender.phone_number == target.phone)
        | (ProviderSender.sender_id == "whatsapp:" + target.phone))
        .execution_options(populate_existing=True, autoflush=False)).scalars().all()
    connection = sender = None
    if target.connection_id is None:
        _require(not connections and not senders, "meta_operation_new_binding_not_empty")
    else:
        _require(len(connections) == 1 and len(senders) == 1,
                 "meta_operation_binding_not_unique")
        connection, sender = connections[0], senders[0]
        _require(connection.id == target.connection_id and sender.id == target.sender_id
                 and sender.tenant_id == target.tenant_id
                 and sender.provider_connection_id == connection.id
                 and sender.phone_number_id == target.phone_id and sender.waba_id == target.waba_id
                 and connection.external_app_id == APP_ID
                 and connection.external_account_id == target.waba_id
                 and connection.credentials_ref == vault.VAULT_REF
                 and connection.status == "connected" and sender.status == "connected",
                 "meta_operation_binding_mismatch")
        owned_senders = session.execute(select(ProviderSender.id).where(
            ProviderSender.provider_connection_id == connection.id)).scalars().all()
        _require(owned_senders == [sender.id], "meta_operation_connection_has_other_senders")
        cfg = connection.config
        _require(isinstance(cfg, dict), "meta_operation_envelope_invalid")
        marker = cfg.get(MANAGED_CONNECTION_MARKER)
        _require(not isinstance(marker, dict) or marker.get("enabled") is not True,
                 "meta_operation_managed_connection_blocked")
        envelope = cfg.get(PRIVATE_CONFIG_KEY)
        _require(isinstance(envelope, dict) and envelope.get("contract") == vault.CONTRACT
                 and type(envelope.get("revision")) is int
                 and envelope["revision"] == request.expected_revision,
                 "meta_operation_revision_conflict")
        # Expired tokens are deliberately not opened during renewal.
    fingerprint = hashlib.sha256(_canonical({
        "tenant": {"id": tenant.id, "slug": tenant.slug, "active": tenant.is_active,
                   "owner": tenant.municipio_id},
        "connection": _columns(connection), "sender": _columns(sender),
    })).hexdigest()
    return InspectedState(tenant, connection, sender, fingerprint)


def _authority_config(target, config):
    selected = dict(config)
    if target.name == "tdf-sandbox":
        _require(config.get("META_TDF_SANDBOX_APP_ID") == APP_ID,
                 "meta_operation_app_secret_scope_invalid")
        secret = config.get("META_TDF_SANDBOX_APP_SECRET")
        selected[APP_SECRETS_KEY] = {APP_ID: secret}
        selected[GRAPH_VERSION_KEY] = config.get("META_TDF_SANDBOX_GRAPH_VERSION", "v25.0")
    return selected


def verify_new_credential(target, request, token, config, *, graph_get, clock, clamp_expiry=False):
    """Use existing authority validation, permitting only three fixed GET paths."""
    config = _authority_config(target, config)
    version = config.get(GRAPH_VERSION_KEY, "v25.0")
    _require(isinstance(version, str) and bool(re.fullmatch(r"v[1-9][0-9]{0,2}\.0", version)),
             "meta_operation_graph_version_invalid")
    base = "https://graph.facebook.com/" + version + "/"
    allowed = {base + "debug_token", base + target.waba_id + "/phone_numbers",
               base + target.waba_id + "/subscribed_apps"}
    checked = {"phone": False, "expiries": []}

    def get_only(method, url, **kwargs):
        _require(method == "GET" and url in allowed, "meta_operation_provider_get_only")
        if url.endswith("/phone_numbers"):
            kwargs["params"] = {"fields": "id,display_phone_number", "limit": "100"}
        response = graph_get(method, url, **kwargs)
        payload = _object_response(response)
        if url.endswith("/debug_token"):
            data = payload.get("data")
            _require(isinstance(data, dict), "meta_operation_provider_proof_invalid")
            for key in ("expires_at", "data_access_expires_at"):
                value = data.get(key)
                if type(value) is int and value > 0:
                    checked["expiries"].append(value)
        elif url.endswith("/phone_numbers"):
            rows = payload.get("data")
            _require(isinstance(rows, list), "meta_operation_provider_phone_invalid")
            matches = [row for row in rows if isinstance(row, dict) and row.get("id") == target.phone_id]
            _require(len(matches) == 1, "meta_operation_provider_phone_invalid")
            display = matches[0].get("display_phone_number")
            _require(isinstance(display, str) and bool(re.fullmatch(r"[+()0-9 -]{1,64}", display))
                     and "+" + re.sub(r"[^0-9]", "", display) == target.phone,
                     "meta_operation_provider_phone_mismatch")
            checked["phone"] = True
        return response

    runtime = MetaOutboundRuntime(config, get=get_only, clock=clock)
    sender = MetaSenderSnapshot(target.tenant_id, target.connection_id or 0,
        target.sender_id or 0, APP_ID, target.waba_id, target.phone_id, target.environment)
    credential = vault.MetaCredential(request.expected_revision + 1, request.expires_at, token)
    proof = runtime.authority(sender, credential)
    _require(checked["phone"] and (clamp_expiry or all(request.expires_at <= expiry for expiry in checked["expiries"])),
             "meta_operation_local_expiry_exceeds_provider")
    provider_expiry = min(checked["expiries"]) if checked["expiries"] else None
    expiry = min(request.expires_at, provider_expiry) if provider_expiry and clamp_expiry else request.expires_at
    return proof, version, expiry, provider_expiry


def _plan(request, state, version):
    target = TARGETS[request.target]
    return {"target": target.name, "tenant_id": target.tenant_id, "tenant_slug": target.slug,
            "provider": "meta", "channel": "whatsapp", "environment": target.environment,
            "app_id": APP_ID, "waba_id": target.waba_id, "phone_number_id": target.phone_id,
            "phone_number": target.phone, "connection_id": target.connection_id,
            "sender_id": target.sender_id, "action": "create" if target.connection_id is None else "rotate",
            "expected_revision": request.expected_revision, "next_revision": request.expected_revision + 1,
            "expires_at": request.expires_at, "actor_user_id": request.actor_user_id,
            "database_identity_sha256": request.database_identity_sha256,
            "expected_writer_epoch": request.expected_writer_epoch, "graph_version": version,
            "state_sha256": state.fingerprint,
            "new_sender_fields": {"phone_number": target.phone, "phone_number_id": target.phone_id,
                "waba_id": target.waba_id, "status": "connected", "display_name": "Junín · Meta WhatsApp",
                "webhook_url": target.callback, "status_callback_url": target.callback}
                if target.connection_id is None else None,
            "changes": ["provider_connection:create", "provider_sender:create", "audit_event:create"]
                if target.connection_id is None else ["provider_connection:credential_envelope", "audit_event:create"],
            "messages_sent": False, "provider_mutations": False,
            "environment_changes": False, "historical_twilio_changes": False}


def _plan_digest(plan, token):
    # Binds approval to this high-entropy credential without exposing its hash,
    # plaintext, encrypted envelope or any encryption key in the public plan.
    return hashlib.sha256(b"chatboc.meta-credential-plan.v1\0" + _canonical(plan)
                          + b"\0" + token.encode("ascii")).hexdigest()


def prepare_operation(session, request, token, config, *, graph_get=native_graph_request,
                      clock=lambda: int(time.time()), expiry_policy_seconds=None):
    now = clock()
    if expiry_policy_seconds is not None:
        _require(expiry_policy_seconds == 30 * 24 * 3600, "meta_operation_expiry_policy_invalid")
        if request.expires_at is None:
            request = replace(request, expires_at=now + expiry_policy_seconds)
        _require(type(request.expires_at) is int and request.expires_at <= now + expiry_policy_seconds,
                 "meta_operation_expiry_exceeds_local_policy")
    request.validate(now)
    _require(isinstance(token, str) and bool(_TOKEN.fullmatch(token)), "meta_operation_token_invalid")
    _keyring(config)  # Readiness only; no token from the old envelope is opened.
    with session.no_autoflush:
        state = inspect_state(session, request)
        proof, version, expiry, provider_expiry = verify_new_credential(TARGETS[request.target], request, token, config,
            graph_get=graph_get, clock=clock, clamp_expiry=expiry_policy_seconds is not None)
    request = replace(request, expires_at=expiry)
    plan = _plan(request, state, version)
    metadata = None if expiry_policy_seconds is None else {
        "expires_at": expiry, "provider_expires_at": provider_expiry,
        "source": "provider" if provider_expiry == expiry else "local_policy",
        "local_maximum_seconds": expiry_policy_seconds,
    }
    return PreparedOperation(request, plan, _plan_digest(plan, token), proof.valid_until, proof.observed_at, metadata)


def apply_operation(session, prepared, token, config, *, approved_plan_sha256, writer_lease,
                    clock=lambda: int(time.time())):
    """Stage one atomic mutation; caller must revalidate lease and commit/rollback."""
    request, target = prepared.request, TARGETS[prepared.request.target]
    request.validate(clock())
    _require(not cutover_writer_fence_enabled(config), "meta_operation_writer_fenced")
    decision = writer_lease.revalidate(config)
    _require(decision.allowed and decision.enabled and decision.epoch == request.expected_writer_epoch,
             "meta_operation_writer_authority_denied")
    _require(isinstance(approved_plan_sha256, str) and bool(_DIGEST.fullmatch(approved_plan_sha256))
             and hmac.compare_digest(approved_plan_sha256, prepared.plan_sha256)
             and hmac.compare_digest(prepared.plan_sha256, _plan_digest(prepared.plan, token)),
             "meta_operation_plan_mismatch")
    _require(prepared.observed_at <= clock() < prepared.valid_until,
             "meta_operation_provider_proof_expired")
    dialect = session.get_bind().dialect.name
    _require(dialect == "postgresql" or (dialect == "sqlite" and config.get("TESTING") is True),
             "meta_operation_postgresql_required")
    if dialect == "postgresql":
        # No global unique phone-ID constraint exists. A brief writer table lock
        # protects the final global uniqueness check, including non-CLI writers.
        # Provider GETs happen before this lock and before acquiring writer lease.
        session.execute(text("LOCK TABLE public.provider_connection, public.provider_sender IN SHARE ROW EXCLUSIVE MODE"))
    with session.no_autoflush:
        state = inspect_state(session, request, lock=True)
    _require(state.fingerprint == prepared.plan["state_sha256"], "meta_operation_state_changed")
    connection, sender = state.connection, state.sender
    if connection is None:
        connection = ProviderConnection(tenant_id=target.tenant_id, provider="meta", channel="whatsapp",
            environment="production", status="connected", display_name="Junín · Meta WhatsApp",
            external_app_id=APP_ID, external_account_id=target.waba_id,
            credentials_ref=vault.VAULT_REF, config={})
        session.add(connection)
        session.flush()
        sender = ProviderSender(tenant_id=target.tenant_id, provider_connection_id=connection.id,
            channel="whatsapp", sender_type="whatsapp_business", phone_number=target.phone,
            phone_number_id=target.phone_id, waba_id=target.waba_id, status="connected",
            display_name="Junín · Meta WhatsApp", webhook_url=target.callback,
            status_callback_url=target.callback)
        session.add(sender)
        session.flush()
    prior = deepcopy(connection.config)
    envelope = vault.seal_token(connection=connection, tenant_id=target.tenant_id, access_token=token,
        revision=request.expected_revision + 1, expires_at=request.expires_at,
        now=clock(), app_config=config)
    changed = session.execute(update(ProviderConnection).where(
        ProviderConnection.id == connection.id, ProviderConnection.tenant_id == target.tenant_id,
        ProviderConnection.provider == "meta", ProviderConnection.channel == "whatsapp",
        ProviderConnection.environment == target.environment,
        ProviderConnection.external_app_id == APP_ID,
        ProviderConnection.external_account_id == target.waba_id,
        ProviderConnection.credentials_ref == vault.VAULT_REF,
        ProviderConnection.config == prior)
        .values(config={**prior, PRIVATE_CONFIG_KEY: envelope})
        .returning(ProviderConnection.id).execution_options(synchronize_session=False)).scalar_one_or_none()
    _require(changed is not None, "meta_operation_revision_conflict")
    session.add(AuditEvent(tenant_id=target.tenant_id, actor_user_id=request.actor_user_id,
        event_type="provider_credential.meta_provisioned" if target.connection_id is None else "provider_credential.meta_rotated",
        resource_type="provider_connection", resource_id=str(connection.id),
        details={"contract": CONTRACT, "provider": "meta", "environment": target.environment,
                 "sender_id": sender.id, "previous_revision": request.expected_revision,
                 "revision": request.expected_revision + 1, "expires_at": request.expires_at,
                 "plan_sha256": prepared.plan_sha256, "provider_observed_at": prepared.observed_at}))
    session.flush()
    session.refresh(connection)
    installed = vault.open_token(connection=connection, tenant_id=target.tenant_id,
                                 now=clock(), app_config=config)
    _require(hmac.compare_digest(installed.access_token, token), "meta_operation_roundtrip_failed")
    _require(prepared.observed_at <= clock() < prepared.valid_until,
             "meta_operation_provider_proof_expired")
    return {"contract": CONTRACT, "dry_run": False, "target": target.name,
            "tenant_id": target.tenant_id, "connection_id": connection.id, "sender_id": sender.id,
            "revision": installed.revision, "expires_at": installed.expires_at,
            "plan_sha256": prepared.plan_sha256, "messages_sent": False,
            "provider_mutations": False, "environment_changes": False}
