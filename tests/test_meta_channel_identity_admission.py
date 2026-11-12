"""Proposed admission-identity tests; no closed suite imported or collected.

ORM/constraints/identity helpers are real. All tenants, phones and inbox rows are
synthetic. The inbox fixture below is deliberately not a validated Meta ingress.
"""
from datetime import datetime, timedelta, timezone
import json
import secrets
from types import SimpleNamespace
import uuid

from flask import Flask
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from models import User, TenantProfile, ChatSessionContext, ChannelSessionIdentityBinding, WhatsAppInboundTurn, db
from services.channel_session_identity import (
    ChannelSessionIdentityError, ChannelSessionIdentityConflict,
    derive_channel_session_identity_hmac, stage_meta_channel_session_identity,
)

PHONE = "+5491100000203"
NOW = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def admission_env():
    app = Flask("synthetic-meta-identity-admission")
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False, CUTOVER_WRITER_FENCE_ENABLED=False,
        CHANNEL_SESSION_IDENTITY_MODE="enforce",
        CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1=secrets.token_hex(32))
    db.init_app(app)
    with app.app_context():
        assert str(db.engine.url) == "sqlite:///:memory:"
        db.create_all()
        identities = []
        for label in ("a", "b"):
            owner = User(name="Synthetic owner " + label, email="admission-" + label + "@example.invalid",
                tipo_chat="pyme", rol="admin", password_hash="synthetic-not-a-login")
            db.session.add(owner)
            db.session.flush()
            tenant = TenantProfile(slug="synthetic-admission-" + label, nombre="Synthetic " + label,
                tipo="pyme", pyme_id=owner.id, is_active=True)
            db.session.add(tenant)
            db.session.flush()
            identities.append((owner.id, tenant.id))
        db.session.commit()
        env = SimpleNamespace(app=app, owner=identities[0][0], tenant=identities[0][1],
            foreign_owner=identities[1][0], foreign_tenant=identities[1][1], session=db.session())
        _begin(env)
        try:
            yield env
        finally:
            db.session.rollback()
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def _begin(env):
    actual = env.session.connection().connection.driver_connection
    if not actual.in_transaction:
        env.session.execute(text("BEGIN"))  # Fixture-only physical BEGIN, no engine policy changes.
    env.transaction = env.session.get_transaction()
    assert actual.in_transaction and not env.session.in_nested_transaction()


def _stage(env, **kwargs):
    args = dict(config=env.app.config, tenant_id=env.tenant, owner_user_id=env.owner,
                provider_identity=PHONE, now=NOW)
    args.update(kwargs)
    return stage_meta_channel_session_identity(env.session, env.transaction, **args)


def _inbox_fixture(env, resolution):
    # Actual pending-inbox model/constraints, constructed synthetically after
    # identity. This does not stand in for raw-HMAC/provider-scope validation.
    row = WhatsAppInboundTurn(turn_id=str(uuid.uuid4()), tenant_id=resolution.tenant_id,
        provider="meta", provider_message_sid="wamid.synthetic/" + uuid.uuid4().hex + "+=",
        stream_key="synthetic-admission-stream", message_kind="text", payload_digest="a" * 64,
        payload_json={"synthetic": True}, status="received", available_at=NOW, received_at=NOW,
        chat_session_id=resolution.chat_session_id, session_identity_binding_id=resolution.binding_id,
        session_identity_version=resolution.identity_version, session_identity_hmac=resolution.identity_hmac)
    env.session.add(row)
    env.session.flush()
    return row


@pytest.mark.parametrize("owner_action", ("rollback", "commit"))
def test_identity_and_pending_inbox_share_caller_transaction(admission_env, owner_action):
    env = admission_env
    commits = []
    def observe_commit(session):
        commits.append(session.in_nested_transaction())
    event.listen(env.session, "before_commit", observe_commit)
    try:
        staged = _stage(env)
        assert staged.committed is False and staged.resolution.outcome == "created"
        assert env.session.get_transaction() is env.transaction and not env.session.in_nested_transaction()
        context = env.session.get(ChatSessionContext, staged.resolution.chat_session_id)
        assert context.anon_id is None and context.user_id == env.owner
        assert PHONE not in json.dumps(context.context_data)
        expected = derive_channel_session_identity_hmac(
            secret=env.app.config["CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1"], tenant_id=env.tenant,
            provider="meta", identity_version="v1", provider_identity=PHONE)
        assert staged.resolution.identity_hmac == expected
        _inbox_fixture(env, staged.resolution)
        assert commits == []
        getattr(env.session, owner_action)()
    finally:
        event.remove(env.session, "before_commit", observe_commit)
    expected_count = 1 if owner_action == "commit" else 0
    assert ChannelSessionIdentityBinding.query.count() == ChatSessionContext.query.count() == expected_count
    assert WhatsAppInboundTurn.query.count() == expected_count
    assert commits == ([False] if owner_action == "commit" else [])


def test_later_event_failure_leaves_whole_batch_rollback_to_owner(admission_env):
    env = admission_env
    first = _stage(env)
    _inbox_fixture(env, first.resolution)
    with pytest.raises(ChannelSessionIdentityConflict, match="session_identity_admission_owner_scope_mismatch"):
        _stage(env, owner_user_id=env.foreign_owner, provider_identity="+5491100000204")
    assert ChannelSessionIdentityBinding.query.count() == WhatsAppInboundTurn.query.count() == 1
    env.session.rollback()
    assert ChannelSessionIdentityBinding.query.count() == ChatSessionContext.query.count() == WhatsAppInboundTurn.query.count() == 0


def test_duplicate_current_identity_reuses_same_binding_without_commit(admission_env):
    env = admission_env
    first, second = _stage(env), _stage(env)
    assert first.resolution.binding_id == second.resolution.binding_id
    assert second.committed is False and second.resolution.outcome == "existing"
    assert ChannelSessionIdentityBinding.query.one().generation == 1
    assert ChannelSessionIdentityBinding.query.count() == ChatSessionContext.query.count() == 1


def test_same_phone_is_partitioned_by_tenant_and_version(admission_env):
    env = admission_env
    first = _stage(env).resolution
    foreign = _stage(env, tenant_id=env.foreign_tenant, owner_user_id=env.foreign_owner).resolution
    version = _stage(env, identity_version="v2").resolution
    assert len({first.binding_id, foreign.binding_id, version.binding_id}) == 3
    assert len({first.identity_hmac, foreign.identity_hmac, version.identity_hmac}) == 3
    assert len({first.chat_session_id, foreign.chat_session_id, version.chat_session_id}) == 3
    assert all(env.session.get(ChatSessionContext, row.chat_session_id).anon_id is None
               for row in (first, foreign, version))


@pytest.mark.parametrize("case", ("unique", "ambiguous", "foreign-explicit"))
def test_adoption_and_isolation_preserve_legacy_partition(admission_env, case):
    env = admission_env
    rows = []
    for _ in range(2 if case == "ambiguous" else 1):
        row = ChatSessionContext(chat_session_id=str(uuid.uuid4()), anon_id=PHONE,
            user_id=env.foreign_owner if case == "foreign-explicit" else env.owner,
            tenant_id=env.foreign_tenant if case == "foreign-explicit" else None,
            context_data={"synthetic_history": True})
        env.session.add(row)
        rows.append(row)
    env.session.commit()
    ids = [row.chat_session_id for row in rows]
    _begin(env)
    staged = _stage(env, explicit_legacy_chat_session_id=ids[0] if case == "foreign-explicit" else None)
    if case == "unique":
        assert staged.resolution.outcome == "adopted" and staged.resolution.chat_session_id == ids[0]
        assert staged.resolution.continuity_preserved is True
    else:
        assert staged.resolution.outcome == "isolated" and staged.resolution.chat_session_id not in ids
        assert staged.resolution.continuity_preserved is False
        assert all(env.session.get(ChatSessionContext, sid).context_data == {"synthetic_history": True} for sid in ids)
    env.session.rollback()
    assert ChannelSessionIdentityBinding.query.count() == 0
    assert ChatSessionContext.query.count() == len(ids)
    if case == "unique":
        assert env.session.get(ChatSessionContext, ids[0]).tenant_id is None


def test_binding_repair_is_admission_only_and_does_not_rewrite_existing_inbox_lease(admission_env):
    env = admission_env
    first = _stage(env).resolution
    inbox = _inbox_fixture(env, first)
    inbox.status, inbox.lease_token, inbox.leased_until = "processing", "synthetic-lease", NOW + timedelta(seconds=60)
    inbox.processing_started_at = NOW
    inbox_id, old_context_id = inbox.turn_id, first.chat_session_id
    env.session.commit()
    binding = env.session.get(ChannelSessionIdentityBinding, first.binding_id)
    binding.status = "quarantined"
    env.session.commit()
    _begin(env)
    staged = _stage(env)
    assert staged.resolution.outcome == "isolated" and staged.resolution.chat_session_id != old_context_id
    assert binding.generation == 2 and binding.previous_session_digest
    old = WhatsAppInboundTurn.query.filter_by(turn_id=inbox_id).one()
    assert (old.chat_session_id, old.status, old.lease_token) == (old_context_id, "processing", "synthetic-lease")
    assert old.leased_until.replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=60)
    env.session.rollback()
    old_binding = env.session.get(ChannelSessionIdentityBinding, first.binding_id)
    assert (old_binding.status, old_binding.generation, old_binding.chat_session_id) == ("quarantined", 1, old_context_id)


@pytest.mark.parametrize("case", ("owner", "tenant-inactive", "version", "phone", "legacy-mode", "naive-clock"))
def test_invalid_authoritative_inputs_fail_without_creating_identity(admission_env, case):
    env = admission_env
    kwargs = {}
    if case == "owner":
        kwargs["owner_user_id"] = env.foreign_owner
    elif case == "tenant-inactive":
        env.session.get(TenantProfile, env.tenant).is_active = False
        env.session.commit()
        _begin(env)
    elif case == "version":
        kwargs["identity_version"] = "bad version"
    elif case == "phone":
        kwargs["provider_identity"] = "opaque-contact"
    elif case == "legacy-mode":
        env.app.config["CHANNEL_SESSION_IDENTITY_MODE"] = "legacy"
    else:
        kwargs["now"] = NOW.replace(tzinfo=None)
    with pytest.raises(ChannelSessionIdentityError):
        _stage(env, **kwargs)
    assert ChannelSessionIdentityBinding.query.count() == ChatSessionContext.query.count() == 0


@pytest.mark.parametrize("case", ("pending", "nested", "other-session"))
def test_caller_session_transaction_and_explicit_flush_precondition(admission_env, case):
    env = admission_env
    if case == "pending":
        env.session.get(TenantProfile, env.tenant).nombre = "Synthetic caller WIP"
        with pytest.raises(ChannelSessionIdentityError, match="session_identity_admission_pending_work"):
            _stage(env)
        with env.session.no_autoflush:
            assert env.session.execute(text("SELECT nombre FROM tenant_profile WHERE id=:id"), {"id": env.tenant}).scalar_one() != "Synthetic caller WIP"
        env.session.rollback()
    elif case == "nested":
        with env.session.begin_nested():
            with pytest.raises(ChannelSessionIdentityError, match="session_identity_admission_transaction_required"):
                _stage(env)
    else:
        with Session(bind=db.engine) as foreign:
            with pytest.raises(ChannelSessionIdentityError, match="session_identity_admission_session_mismatch"):
                stage_meta_channel_session_identity(foreign, env.transaction, config=env.app.config,
                    tenant_id=env.tenant, owner_user_id=env.owner, provider_identity=PHONE)
    assert ChannelSessionIdentityBinding.query.count() == ChatSessionContext.query.count() == 0


def test_unique_constraint_failure_requires_owner_rollback_without_local_retry(admission_env):
    env = admission_env
    injections = []
    def competing_insert(session, flush_context, instances):
        if injections:
            return
        candidate = next((x for x in session.new if isinstance(x, ChannelSessionIdentityBinding)), None)
        if candidate is not None:
            injections.append(True)
            # Two inserts in the same SQLite flush, not PostgreSQL concurrency.
            fields = ("tenant_id", "channel", "provider", "identity_version", "identity_hmac",
                "chat_session_id", "status", "continuity_status", "generation", "contract_version",
                "last_verified_at", "created_at", "updated_at")
            session.add(ChannelSessionIdentityBinding(**{key: getattr(candidate, key) for key in fields}))
    event.listen(env.session, "before_flush", competing_insert)
    try:
        with pytest.raises(ChannelSessionIdentityConflict, match="session_identity_admission_constraint_conflict"):
            _stage(env)
    finally:
        event.remove(env.session, "before_flush", competing_insert)
    assert injections == [True] and env.session.in_transaction()
    assert env.session.get_transaction() is env.transaction and not env.transaction.is_active
    env.session.rollback()  # The batch owner must abandon the failed transaction.
    assert ChannelSessionIdentityBinding.query.count() == ChatSessionContext.query.count() == 0
