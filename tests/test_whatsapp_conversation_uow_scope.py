"""New V2 server-partition controls; original 22 V1 cases remain byteexact.

Real common/ORM/CRM/finalizer. Engine and provider identities/HTTP are synthetic.
No callbacks, ingress or provider settings are installed by these proposals.
"""
from dataclasses import replace
import uuid

import pytest
from sqlalchemy import event, text

from models import User, TenantProfile, WhatsAppInboundTurn, WhatsAppOutboundAttempt, ChannelSessionIdentityBinding, db
from models_memory import Contact, ContactSnapshot, InteractionEvent
from services import whatsapp_conversation_turn as common
from services.channel_session_identity import derive_channel_session_identity_hmac, resolve_channel_session_identity_secret
from services.whatsapp_conversation_contract import CollectingConversationSink
from services.whatsapp_conversation_uow import ConversationUnitOfWorkError
from tests.test_whatsapp_conversation_uow import environment, unit_env, _unit, _turn, _finish, _reply


@pytest.fixture
def scope_env(unit_env):
    env = unit_env
    owner = User(name="Synthetic other owner", email="other-owner-uow@example.invalid",
                 rol="admin", tipo_chat="pyme", password_hash="synthetic-not-a-login")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="synthetic-other-uow", nombre="Synthetic other organization",
                           tipo="pyme", pyme_id=owner.id, is_active=True)
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
    def person(label, target, target_owner, phone):
        user = User(name="Synthetic person " + label, email="person-" + label + "-uow@example.invalid",
            telefono=phone, rol="usuario", tipo_chat="pyme", tenant_id=target.id,
            tenant_slug=target.slug, empresa_id=target_owner.id, pyme_id=target_owner.id,
            password_hash="synthetic-not-a-login")
        db.session.add(user)
        return user
    canonical = "+" + env.event.contact
    env.other_owner, env.other_tenant = owner, tenant
    env.own_person = person("own", env.tenant, env.owner, canonical)
    env.foreign_person = person("foreign", tenant, owner, canonical)
    env.other_phone_person = person("other-phone", env.tenant, env.owner, "+5491100000299")
    db.session.commit()  # Synthetic preconditions before Unit ownership.
    _physical_begin(env)
    return env


def _physical_begin(env):
    driver = env.actual_session.connection().connection.driver_connection
    if not driver.in_transaction:
        env.actual_session.execute(text("BEGIN"))
    assert driver.in_transaction


def _snapshot(env):
    with db.session.no_autoflush:
        context = db.session.execute(text(
            "SELECT user_id, tenant_id, anon_id, context_data FROM chat_session_context WHERE chat_session_id=:s"),
            {"s": env.claim.chat_session_id}).one()
    return (tuple(context), Contact.query.count(), ContactSnapshot.query.count(),
            InteractionEvent.query.count(), WhatsAppOutboundAttempt.query.count())


@pytest.mark.parametrize("case", (
    "foreign-owner", "foreign-end-user", "wrong-phone-end-user", "different-contact",
    "context-other-owner", "context-other-phone", "recipient-hmac-disagreement",
    "dirty-context-owner", "dirty-profile-owner",
))
def test_incoherent_server_partition_fails_before_engine_crm_or_dml(scope_env, monkeypatch, case):
    env = scope_env
    if case == "context-other-owner":
        env.context.user_id = env.other_owner.id
    elif case == "context-other-phone":
        env.context.anon_id = "+5491100000299"
    elif case == "recipient-hmac-disagreement":
        other = derive_channel_session_identity_hmac(
            secret=resolve_channel_session_identity_secret(env.app.config), tenant_id=env.tenant.id,
            provider="meta", identity_version=env.claim.session_identity_version,
            provider_identity="+5491100000299")
        binding = db.session.get(ChannelSessionIdentityBinding, env.claim.session_identity_binding_id)
        binding.identity_hmac = other
        row = WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one()
        row.session_identity_hmac = other
        env.claim = replace(env.claim, session_identity_hmac=other)
    if case in {"context-other-owner", "context-other-phone", "recipient-hmac-disagreement"}:
        db.session.commit()  # Persist the deliberately inconsistent fixture, not a runtime repair.
        _physical_begin(env)
    before = _snapshot(env)
    calls = {"engine": 0, "crm": 0}
    actual_crm = common.resolve_or_create_contact
    def observe_crm(*a, **k):
        calls["crm"] += 1
        return actual_crm(*a, **k)
    monkeypatch.setattr(common, "resolve_or_create_contact", observe_crm)
    def engine(**kwargs):
        calls["engine"] += 1
        return {"message_body": "This must not run", "message_type": "text", "options_list": []}
    connection = env.actual_session.connection()
    dml = []
    def observe_sql(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            dml.append(statement.split(None, 1)[0].upper())
    event.listen(connection, "before_cursor_execute", observe_sql)
    try:
        with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_"):
            with _unit(env) as unit:
                turn = _turn(env, unit)
                if case == "foreign-owner":
                    turn = replace(turn, owner_user=env.other_owner)
                elif case == "foreign-end-user":
                    turn = replace(turn, end_user=env.foreign_person)
                elif case == "wrong-phone-end-user":
                    turn = replace(turn, end_user=env.other_phone_person)
                elif case == "different-contact":
                    turn = replace(turn, contact_identity="+5491100000299")
                elif case == "dirty-context-owner":
                    env.context.user_id = env.other_owner.id
                elif case == "dirty-profile-owner":
                    env.tenant.pyme_id = env.other_owner.id
                _finish(turn, CollectingConversationSink(supports_media=False), engine)
    finally:
        event.remove(connection, "before_cursor_execute", observe_sql)
    assert calls == {"engine": 0, "crm": 0} and dml == []
    assert _snapshot(env) == before
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_unambiguous_own_user_passes_but_canonical_contact_requires_explicit_none(scope_env):
    env = scope_env
    with _unit(env) as unit:
        turn = replace(_turn(env, unit), end_user=env.own_person)
        unit.require_turn(turn)
        assert unit.contact_identity == "+" + env.event.contact
        with pytest.raises(AttributeError):
            unit.contact_identity = "+5491100000299"
        unit.commit_reply(_reply(_finish(turn, CollectingConversationSink(supports_media=False))), runtime=env.runtime)
    assert Contact.query.count() == InteractionEvent.query.count() == 1


def test_preferred_contact_does_not_authorize_legacy_user_pii(scope_env, monkeypatch):
    env = scope_env
    contact = Contact(id=str(uuid.uuid4()), tenant_id=env.tenant.id,
        phone="+" + env.event.contact, whatsapp_id="+" + env.event.contact,
        name="Synthetic preferred contact", email="preferred-uow@example.invalid")
    db.session.add(contact)
    db.session.commit()
    _physical_begin(env)
    calls = []
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_end_user_scope_mismatch"):
        with _unit(env) as unit:
            _finish(replace(_turn(env, unit), end_user=env.own_person),
                CollectingConversationSink(supports_media=False), lambda **k: calls.append(k))
    assert calls == [] and InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    _physical_begin(env)
    with _unit(env) as unit:
        turn = replace(_turn(env, unit), end_user=None)
        unit.commit_reply(_reply(_finish(turn, CollectingConversationSink(supports_media=False))), runtime=env.runtime)
    assert Contact.query.count() == InteractionEvent.query.count() == 1


def test_engine_identity_reparenting_is_rechecked_before_actual_crm(scope_env, monkeypatch):
    env = scope_env
    calls = {"engine": 0, "crm": 0}
    actual = common.resolve_or_create_contact
    def observe(*a, **k):
        calls["crm"] += 1
        return actual(*a, **k)
    monkeypatch.setattr(common, "resolve_or_create_contact", observe)
    def engine(**kwargs):
        calls["engine"] += 1
        env.own_person.name = "Synthetic unapproved mutation"
        return {"message_body": "Synthetic response", "message_type": "text", "options_list": []}
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_end_user_scope_mismatch"):
        with _unit(env) as unit:
            _finish(replace(_turn(env, unit), end_user=env.own_person),
                CollectingConversationSink(supports_media=False), engine)
    assert calls == {"engine": 1, "crm": 0}
    assert InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    assert env.own_person.name == "Synthetic person own"


def test_persisted_binding_revocation_is_not_hidden_by_loaded_orm_instance(scope_env):
    env = scope_env
    binding = db.session.get(ChannelSessionIdentityBinding, env.claim.session_identity_binding_id)
    calls = []
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_binding_partition_mismatch"):
        with _unit(env) as unit:
            # Synthetic database-side change after entering the Unit, with ORM
            # synchronization explicitly off. This is not a concurrency/PG claim.
            env.actual_session.execute(text(
                "UPDATE channel_session_identity_binding SET status='quarantined' WHERE id=:id"),
                {"id": binding.id})
            assert binding.status == "active"
            _finish(_turn(env, unit), CollectingConversationSink(supports_media=False),
                lambda **k: calls.append(k))
    assert calls == [] and InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    assert db.session.get(ChannelSessionIdentityBinding, env.claim.session_identity_binding_id).status == "active"


def test_real_crm_wrapper_can_log_socket_failure_without_propagating_it(scope_env, monkeypatch, caplog):
    env = scope_env
    import socket_service
    calls = []
    def socket_failure(*_args):
        calls.append(True)
        raise RuntimeError("synthetic_socket_transport_failure")
    monkeypatch.setattr(socket_service, "emit_crm_contact_update", socket_failure)
    with _unit(env) as unit:
        finished = _finish(_turn(env, unit), CollectingConversationSink(supports_media=False))
        assert calls == []
        result = unit.commit_reply(_reply(finished), runtime=env.runtime)
    assert calls == [True] and result.committed is True
    # The real wrapper catches/logs the socket error. This tuple describes only
    # exceptions propagated by callbacks, never a delivery/detection guarantee.
    assert result.after_commit_failures == ()
    assert "realtime emit skipped" in caplog.text
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "completed"
    assert InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 1
    assert env.context.anon_id is None  # No raw phone restored into canonical context.
    assert env.calls["post"] == [] and env.calls["twilio"] == 0
