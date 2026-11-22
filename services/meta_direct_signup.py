"""Organization-bound, one-use Embedded Signup. Remote calls are never retried by the transport."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import lazyload

from cutover_writer_fence import cutover_writer_fence_enabled
from models import AuditEvent, MetaDirectSignupSession as Attempt, ProviderConnection as Connection
from models import ProviderSender as Sender, TenantProfile, WhatsappNumero
from services import meta_whatsapp_credentials as vault
from services.global_writer_authority import global_writer_authority_lease
from services.meta_credential_operations import TARGETS
from services.meta_direct_signup_graph import DirectSignupGraph
from services.meta_direct_signup_labels import PANEL_LABELS
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.provider_connection_cutover_contract import MANAGED_CONNECTION_MARKER
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY, ProviderCredentialError, _keyring
from services.twilio_tech_provider import STATE_KEY
from utils.meta_credential_http import private_renewal_logs


FLAG = "META_WHATSAPP_DIRECT_EMBEDDED_SIGNUP_ENABLED"
MARKER = "meta_direct_embedded_signup"
SESSION_SECONDS = 900
CONTRACT = "chatboc.meta_direct_embedded_signup.v1"
_STATE = re.compile(r"[A-Za-z0-9_-]{43}")
_SESSION = re.compile(r"[A-Za-z0-9_.:-]{1,160}")


class SignupError(Exception):
    def __init__(self, reason, status):
        super().__init__(reason)
        self.reason, self.status = reason, status


def failure(reason, status):
    return {"contract_version": CONTRACT, "ok": False, "reason_code": reason}, status


def _result(row, now=None):
    if row.status == "exchanging" and now is not None and now >= row.created_at_epoch + SESSION_SECONDS + 300:
        return failure("meta_signup_new_authorization_required", 409)
    http = {"pending": 202, "connected": 200, "rejected": 422, "exchanging": 202}[row.status]
    if row.status == "rejected" and row.reason_code in {
        "meta_signup_asset_in_use", "meta_signup_asset_reserved", "meta_signup_superseded",
    }:
        http = 409
    return {"contract_version": CONTRACT, "ok": row.status == "connected",
            "status": row.status, "reason_code": row.reason_code}, http


def _now():
    return int(time.time())


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _key(config):
    key = config.get("SECRET_KEY")
    if isinstance(key, str):
        key = key.encode()
    if not isinstance(key, bytes) or len(key) < 32:
        raise SignupError("meta_signup_config_unavailable", 503)
    return key


def _code_hash(config, code):
    return hmac.digest(_key(config), b"meta.embedded-signup.code.v1\0" + code.encode("ascii"), "sha256").hex()


def _writer(config, lease):
    decision = lease.revalidate(config)
    if cutover_writer_fence_enabled(config) or not decision.allowed or (
        not decision.enabled and config.get("TESTING") is not True
    ):
        raise SignupError("meta_signup_writer_unavailable", 503)


def _commit(session, config, lease):
    _writer(config, lease)
    session.commit()


def reserved_tenant(org):
    protected = TARGETS["junin-production"]
    return org.id == protected.tenant_id or org.slug.casefold() == protected.slug


def panel_capability(org, config):
    if config.get(FLAG) is not True or not org.is_active or reserved_tenant(org):
        return None
    app_id = DirectSignupGraph.app_id
    configuration_id = config.get("TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID")
    if (config.get("TWILIO_META_APP_ID") != app_id or not isinstance(configuration_id, str)
            or not re.fullmatch(r"[1-9][0-9]{0,39}", configuration_id)):
        configuration_id = None
    return {"enabled": True, "app_id": app_id, "configuration_id": configuration_id, "labels": PANEL_LABELS}


def _guard_org(session, tenant_id, authorize):
    org = session.scalars(select(TenantProfile).options(lazyload("*")).filter_by(id=tenant_id).with_for_update(of=TenantProfile)
                          .execution_options(populate_existing=True)).unique().one_or_none()
    if org is None or org.is_active is not True or not authorize(org):
        raise SignupError("meta_signup_organization_denied", 403)
    if reserved_tenant(org):
        raise SignupError("meta_signup_asset_reserved", 409)
    return org


def _run(session, config, run):
    if config.get(FLAG) is not True:
        return failure("meta_signup_unavailable", 404)
    with private_renewal_logs():
        try:
            dialect = session.get_bind().dialect.name
            if dialect != "postgresql" and not (dialect == "sqlite" and config.get("TESTING") is True):
                raise SignupError("meta_signup_storage_unavailable", 503)
            with global_writer_authority_lease(config) as lease:
                _writer(config, lease)
                return run(lease)
        except SignupError as exc:
            return failure(exc.reason, exc.status)
        except IntegrityError:
            return failure("meta_signup_conflict", 409)
        except (SQLAlchemyError, ProviderCredentialError, MetaRuntimeError):
            return failure("meta_signup_unavailable", 503)
        finally:
            # Each successful stage commits explicitly. A failed stage releases
            # its locks and cannot be flushed later by the Flask request.
            session.rollback()


def start_signup(session, config, tenant_id, actor_id, *, authorize, clock=_now):
    def issue(lease):
        _guard_org(session, tenant_id, authorize)
        _key(config)
        _keyring(config)
        state = secrets.token_urlsafe(32)
        now = clock()
        session.add(Attempt(state_hash=_digest(state), tenant_id=tenant_id, actor_user_id=actor_id,
                            created_at_epoch=now, status="issued"))
        _commit(session, config, lease)
        return {"contract_version": CONTRACT, "ok": True, "signup_state": state,
                "expires_in": SESSION_SECONDS}, 201
    return _run(session, config, issue)


def _locked_attempt(session, state_hash, tenant_id, actor_id):
    row = session.scalars(select(Attempt).filter_by(state_hash=state_hash, tenant_id=tenant_id,
                          actor_user_id=actor_id).with_for_update()
                          .execution_options(populate_existing=True)).one_or_none()
    if row is None:
        raise SignupError("meta_signup_session_invalid", 403)
    return row


def _protect_assets(session, org, waba, phone_id, phone=None, *, own_connection=None, own_sender=None):
    if any(t.waba_id == waba or t.phone_id == phone_id or (phone and t.phone == phone)
           for t in TARGETS.values()):
        raise SignupError("meta_signup_asset_reserved", 409)
    # No existing ownership is replaced, including manual or legacy records.
    account_matches = (Connection.external_account_id == waba) | (Connection.external_business_id == waba)
    occupied = select(Connection.id).where(Connection.channel == "whatsapp", or_(
        account_matches & ((Connection.tenant_id != org.id) | (Connection.provider == "meta")),
        (Connection.tenant_id == org.id) & (Connection.provider == "meta")
        & (Connection.environment == "production")))
    if own_connection is not None:
        occupied = occupied.where(Connection.id != own_connection)
    match = [Sender.waba_id == waba, Sender.phone_number_id == phone_id]
    legacy = TenantProfile.configuracion[STATE_KEY]
    foreign = select(TenantProfile.id).where(TenantProfile.id != org.id, or_(
        legacy["waba_id"].as_string() == waba, legacy["phone_number_id"].as_string() == phone_id))
    if session.scalar(foreign.limit(1)) is not None:
        raise SignupError("meta_signup_asset_in_use", 409)
    if phone:
        variants = {phone, phone.lstrip("+"), "whatsapp:" + phone}
        match.extend([Sender.phone_number.in_(variants), Sender.sender_id.in_(variants)])
        owner_ids = {i for i in (org.municipio_id, org.pyme_id) if i}
        foreign_numbers = select(WhatsappNumero.id).where(WhatsappNumero.numero_whatsapp.in_(variants),
                                                           WhatsappNumero.user_id.not_in(owner_ids))
        foreign_org = select(TenantProfile.id).where(TenantProfile.id != org.id,
                                                     TenantProfile.whatsapp_sender_id.in_(variants))
        if session.scalar(foreign_numbers.limit(1)) is not None or session.scalar(foreign_org.limit(1)) is not None:
            raise SignupError("meta_signup_asset_in_use", 409)
    senders = select(Sender.id).where(Sender.channel == "whatsapp", or_(*match))
    if own_sender is not None:
        senders = senders.where(Sender.id != own_sender)
    if session.scalar(occupied.limit(1)) is not None or session.scalar(senders.limit(1)) is not None:
        raise SignupError("meta_signup_asset_in_use", 409)


def _revision(row):
    # Sessions from the first version of the pending schema could only seal v1.
    return 1 if row.credential_revision is None else row.credential_revision


@dataclass
class _ExpiredBinding:
    owner: Attempt
    connection: Connection
    sender: Sender

    def snapshot(self):
        # Capture immutable values before releasing the transaction for OAuth.
        return (self.owner.state_hash, self.connection.id, self.sender.id, self.sender.phone_number,
                dict(self.connection.config[PRIVATE_CONFIG_KEY]))


def _eligible_binding(session, org, app_id, waba, phone_id, now, config, *, phone=None):
    connection = session.scalars(select(Connection).filter_by(tenant_id=org.id, provider="meta",
        channel="whatsapp", environment="production").execution_options(populate_existing=True)).one_or_none()
    if connection is None:
        _protect_assets(session, org, waba, phone_id, phone)
        return None

    conflict = SignupError("meta_signup_asset_in_use", 409)
    cfg = connection.config if isinstance(connection.config, dict) else {}
    envelope = cfg.get(PRIVATE_CONFIG_KEY)
    managed = cfg.get(MANAGED_CONNECTION_MARKER)
    if (connection.status != "pending" or connection.credentials_ref != vault.VAULT_REF
            or connection.external_app_id != app_id or connection.external_account_id != waba
            or cfg.get(MARKER) is not True or (isinstance(managed, dict) and managed.get("enabled") is True)
            or not isinstance(envelope, dict) or envelope.get("contract") != vault.CONTRACT
            or type(envelope.get("expires_at")) is not int or not 0 < envelope["expires_at"] <= now
            or type(envelope.get("revision")) is not int or envelope["revision"] < 1):
        raise conflict

    senders = session.scalars(select(Sender).filter_by(provider_connection_id=connection.id)
                              .limit(2).execution_options(populate_existing=True)).all()
    if len(senders) != 1:
        raise conflict
    sender = senders[0]
    if (sender.tenant_id != org.id or sender.status != "pending" or sender.channel != "whatsapp"
            or sender.waba_id != waba or sender.phone_number_id != phone_id
            or not sender.phone_number or (phone is not None and phone != sender.phone_number)):
        raise conflict
    owners = session.scalars(select(Attempt).filter_by(connection_id=connection.id, status="pending")
                             .limit(2).execution_options(populate_existing=True)).all()
    if len(owners) != 1:
        raise conflict
    owner = owners[0]
    if (not owner.code_hash or not owner.provider_session_hash
            or (owner.tenant_id, owner.waba_id, owner.phone_number_id, owner.sender_id, _revision(owner))
            != (org.id, waba, phone_id, sender.id, envelope["revision"])):
        raise conflict
    _protect_assets(session, org, waba, phone_id, sender.phone_number,
                    own_connection=connection.id, own_sender=sender.id)
    try:
        vault.authenticate_expired_envelope(connection=connection, tenant_id=org.id, now=now, app_config=config)
    except ProviderCredentialError:
        raise conflict from None
    return _ExpiredBinding(owner, connection, sender)


def _audit(session, row, name):
    session.add(AuditEvent(tenant_id=row.tenant_id, actor_user_id=row.actor_user_id,
                           event_type=name, resource_type="provider_connection",
                           resource_id=str(row.connection_id),
                           details={"sender_id": row.sender_id, "revision": _revision(row), "status": row.status}))


def _lock_bindings(session):
    if session.get_bind().dialect.name == "postgresql":
        # Same short table lock as the T-15 writer, covering older writers
        # without a global WABA/phone unique index. No network I/O under it.
        session.execute(text("LOCK TABLE public.tenant_profile, public.provider_connection, "
                             "public.provider_sender, public.whatsapp_numero IN SHARE ROW EXCLUSIVE MODE"))


def _bind(session, row, org, graph, proof, config, now, expected):
    _lock_bindings(session)
    previous = _eligible_binding(session, org, graph.app_id, row.waba_id, row.phone_number_id, now, config,
                                 phone=proof.phone)
    if (previous.snapshot() if previous else None) != expected:
        raise SignupError("meta_signup_asset_in_use", 409)
    if previous:
        connection, sender = previous.connection, previous.sender
        revision = _revision(previous.owner) + 1
        previous.owner.status, previous.owner.reason_code = "rejected", "meta_signup_superseded"
    else:
        revision = 1
        connection = Connection(tenant_id=org.id, provider="meta", channel="whatsapp", environment="production",
                                status="pending", external_app_id=graph.app_id, external_account_id=row.waba_id,
                                credentials_ref=vault.VAULT_REF, config={MARKER: True})
        session.add(connection)
        session.flush()
        sender = Sender(tenant_id=org.id, channel="whatsapp", provider_connection_id=connection.id,
                        phone_number=proof.phone, phone_number_id=row.phone_number_id,
                        waba_id=row.waba_id, status="pending")
        session.add(sender)
        session.flush()
    envelope = vault.seal_token(connection=connection, tenant_id=org.id,
                                access_token=proof.credential.access_token, revision=revision,
                                expires_at=proof.credential.expires_at, now=now, app_config=config)
    connection.config = {**connection.config, PRIVATE_CONFIG_KEY: envelope}
    row.credential_revision = revision
    row.connection_id, row.sender_id, row.status = connection.id, sender.id, "pending"
    row.reason_code = "meta_subscription_pending" if proof.registered else "meta_phone_registration_pending"
    _audit(session, row, "provider_credential.meta_signup_reauthorized" if previous else "provider_credential.meta_signup_sealed")


def _subscription(session, config, lease, row, graph, proof, clock, authorize):
    # Called with the attempt lock. Only the dedicated pending pair can be resumed.
    connection = session.scalars(select(Connection).filter_by(id=row.connection_id, tenant_id=row.tenant_id,
        provider="meta", channel="whatsapp", environment="production", status="pending",
        credentials_ref=vault.VAULT_REF, external_app_id=graph.app_id, external_account_id=row.waba_id)
        .with_for_update().execution_options(populate_existing=True)).one_or_none()
    sender = session.scalars(select(Sender).filter_by(id=row.sender_id, tenant_id=row.tenant_id,
        provider_connection_id=row.connection_id, channel="whatsapp", status="pending",
        waba_id=row.waba_id, phone_number_id=row.phone_number_id).with_for_update()
        .execution_options(populate_existing=True)).one_or_none()
    if not connection or not sender or (connection.config or {}).get(MARKER) is not True:
        raise SignupError("meta_signup_binding_changed", 409)
    envelope = (connection.config or {}).get(PRIVATE_CONFIG_KEY)
    if isinstance(envelope, dict) and type(envelope.get("expires_at")) is int and envelope["expires_at"] <= clock():
        return failure("meta_signup_credential_reauthorization_required", 409)
    credential = vault.open_token(connection=connection, tenant_id=row.tenant_id, now=clock(), app_config=config)
    if credential.revision != _revision(row):
        raise SignupError("meta_signup_binding_changed", 409)
    org = _guard_org(session, row.tenant_id, authorize)
    def check_ownership():
        _protect_assets(session, org, row.waba_id, row.phone_number_id, sender.phone_number,
                        own_connection=connection.id, own_sender=sender.id)
    check_ownership()
    try:
        if proof is None:
            proof = graph.verify_credential(row.waba_id, row.phone_number_id, credential)
        if proof.phone != sender.phone_number:
            raise SignupError("meta_signup_binding_changed", 409)
        if proof.registered:
            _guard_org(session, row.tenant_id, authorize)
            _writer(config, lease)
            graph.subscribe_and_verify(row.waba_id, proof.credential)
            _lock_bindings(session)
            _guard_org(session, row.tenant_id, authorize)
            check_ownership()
            if clock() >= min(proof.credential.expires_at, credential.expires_at):
                raise SignupError("meta_signup_credential_expired", 409)
            row.status, row.reason_code = "connected", None
            connection.status = sender.status = "connected"
            _audit(session, row, "whatsapp.meta_signup_connected")
        else:
            row.reason_code = "meta_phone_registration_pending"
    except MetaRuntimeError:
        row.reason_code = "meta_subscription_unverified"
    answer = _result(row, clock())
    _commit(session, config, lease)
    return answer


def finish_signup(session, config, tenant_id, actor_id, *, state, sdk_session, waba, phone_id, code,
                  authorize, graph_factory=None, clock=_now):
    graph_factory = graph_factory or DirectSignupGraph
    def finish(lease):
        if not isinstance(state, str) or not _STATE.fullmatch(state):
            raise SignupError("meta_signup_session_invalid", 403)
        try:
            vault.meta_id(waba)
            vault.meta_id(phone_id)
        except ProviderCredentialError:
            raise SignupError("meta_signup_assets_invalid", 422) from None
        if not isinstance(code, str) or not code.isascii() or not code.isprintable() or not 1 <= len(code) <= 4096:
            raise SignupError("meta_signup_code_invalid", 422)
        if sdk_session is not None and (not isinstance(sdk_session, str) or not _SESSION.fullmatch(sdk_session)):
            raise SignupError("meta_signup_session_invalid", 422)
        state_hash, sdk_hash, code_hash = _digest(state), _digest(sdk_session or state), _code_hash(config, code)
        org = _guard_org(session, tenant_id, authorize)
        row = _locked_attempt(session, state_hash, tenant_id, actor_id)
        if row.status != "issued":
            if (row.waba_id, row.phone_number_id, row.provider_session_hash, row.code_hash) != (
                    waba, phone_id, sdk_hash, code_hash):
                raise SignupError("meta_signup_session_conflict", 409)
            if row.status != "pending":
                return _result(row, clock())
            graph = graph_factory(config, clock=clock)
            return _subscription(session, config, lease, row, graph, None, clock, authorize)
        if not row.created_at_epoch <= clock() < row.created_at_epoch + SESSION_SECONDS:
            raise SignupError("meta_signup_session_expired", 410)
        _keyring(config)
        graph = graph_factory(config, clock=clock)
        previous = _eligible_binding(session, org, graph.app_id, waba, phone_id, clock(), config)
        expected = previous.snapshot() if previous else None
        # Commit the one-shot claim before OAuth: after transport/process loss a
        # second worker cannot safely tell whether Meta consumed the code.
        claimed = session.execute(update(Attempt).where(Attempt.state_hash == state_hash,
            Attempt.status == "issued").values(status="exchanging", code_hash=code_hash,
            provider_session_hash=sdk_hash, waba_id=waba, phone_number_id=phone_id,
            reason_code="meta_signup_exchange_unconfirmed"))
        if claimed.rowcount != 1:
            raise SignupError("meta_signup_session_conflict", 409)
        _commit(session, config, lease)
        try:
            proof = graph.exchange_and_verify(code, waba, phone_id)
        except MetaRuntimeError as exc:
            row = _locked_attempt(session, state_hash, tenant_id, actor_id)
            row.status = "rejected"
            row.reason_code = "meta_signup_exchange_unconfirmed" if exc.transient else "meta_authority_denied"
            answer = _result(row)
            _commit(session, config, lease)
            return answer
        org = _guard_org(session, tenant_id, authorize)
        row = _locked_attempt(session, state_hash, tenant_id, actor_id)
        if row.status != "exchanging":
            raise SignupError("meta_signup_session_conflict", 409)
        try:
            _bind(session, row, org, graph, proof, config, clock(), expected)
        except SignupError as exc:
            if exc.status != 409:
                raise
            # Ownership was established by a concurrent or legacy writer
            # between preflight and the final check. Never subscribe this app.
            row.status, row.reason_code = "rejected", exc.reason
            answer = _result(row)
            _commit(session, config, lease)
            return answer
        _commit(session, config, lease)
        _guard_org(session, tenant_id, authorize)
        row = _locked_attempt(session, state_hash, tenant_id, actor_id)
        if row.status != "pending":
            return _result(row)
        return _subscription(session, config, lease, row, graph, proof, clock, authorize)
    return _run(session, config, finish)


def resume_signup(session, config, tenant_id, actor_id, *, state, authorize, clock=_now, graph_factory=None):
    """Recover from a page reload or lost acknowledgement using only the server state."""
    def resume(lease):
        if not isinstance(state, str) or not _STATE.fullmatch(state):
            raise SignupError("meta_signup_session_invalid", 403)
        _guard_org(session, tenant_id, authorize)
        row = _locked_attempt(session, _digest(state), tenant_id, actor_id)
        if row.status == "issued":
            raise SignupError("meta_signup_completion_required", 409)
        if row.status != "pending":
            return _result(row, clock())
        graph = (graph_factory or DirectSignupGraph)(config, clock=clock)
        return _subscription(session, config, lease, row, graph, None, clock, authorize)
    return _run(session, config, resume)
