"""PREP: real ORM/writers/staging/UoW/finalizer; synthetic identities and Graph.

Only factory definitions are explicitly imported from the new UoW test module.
No closed Meta/outbox cohort is selected or re-exported by this module.
The application runner must install its offline/environment guard before imports.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    DomainEffectOutbox, PymeTicket, TenantProfile, TicketComentario,
    TicketDomainEffectReceipt, User, WhatsAppInboundTurn, WhatsAppOutboundAttempt, db,
)
from models_memory import Contact, ContactSnapshot, InteractionEvent
from services import crm_intelligence as crm
from services import ticket_service as tickets
from services.meta_whatsapp_outbox import MetaReplyBundle, MetaReplyContent
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.ticket_service import ServicioTickets, build_whatsapp_ticket_effect_key
from services.whatsapp_conversation_uow import ConversationUnitOfWorkError
from tests.test_whatsapp_conversation_uow import environment, unit_env, _unit


def _queue(env, monkeypatch):
    # Synthetic test app only; not activation of any existing tenant/config.
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "queue")
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", "synthetic-domain-intent-" + "x" * 32)
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", str(env.tenant.id))


def _service(monkeypatch):
    service = ServicioTickets()
    service.auto_assign_enabled = False
    service._notificar_ticket_por_email = Mock(side_effect=AssertionError("unexpected_direct_email"))
    monkeypatch.setattr(tickets, "enviar_ticket_a_sigem", Mock(side_effect=AssertionError("unexpected_sigem")))
    return service


def _payload(env, **changes):
    value = {"tenant_id": env.tenant.id, "pyme_id": env.owner.id,
        "asunto": "Synthetic scoped case", "pregunta": "Synthetic service inquiry",
        "categoria": "General", "telefono_cliente": "+" + env.event.contact,
        "email_cliente": "synthetic-requester@example.invalid", "estado": "nuevo"}
    value.update(changes)
    return value


def _effect(env, unit, effect):
    return {"idempotency_key": build_whatsapp_ticket_effect_key(env.tenant.id, env.claim.turn_id, effect),
        "idempotency_tenant_id": env.tenant.id, "unit_of_work": unit,
        "conversation_turn_id": env.claim.turn_id}


def _crm(env, unit, *, emit=False):
    contact = crm.resolve_or_create_contact(env.tenant, phone="+" + env.event.contact,
        name="Synthetic customer", source="whatsapp_inbound", unit_of_work=unit,
        conversation_turn_id=env.claim.turn_id)
    payload = crm.record_contact_interaction(tenant=env.tenant, contact=contact,
        message_body="Synthetic service question", channel="whatsapp", emit=emit,
        metadata={"message_sid": env.claim.provider_message_sid}, unit_of_work=unit,
        conversation_turn_id=env.claim.turn_id)
    return contact, payload


def _empty_counts():
    assert PymeTicket.query.count() == TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == 0
    assert DomainEffectOutbox.query.count() == 0
    assert Contact.query.count() == ContactSnapshot.query.count() == InteractionEvent.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0


def test_case_comment_crm_and_meta_receipt_commit_together_without_direct_delivery(unit_env, monkeypatch):
    env = unit_env
    _queue(env, monkeypatch)
    service = _service(monkeypatch)
    sockets = []
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda tenant, payload: sockets.append((tenant.id, payload["contact_id"])))
    commits = []
    def on_commit(session):
        commits.append("savepoint" if session.in_nested_transaction() else "outer")
    event.listen(env.actual_session, "before_commit", on_commit)
    try:
        with _unit(env) as unit:
            ticket = service.crear_nuevo_ticket("pyme", _payload(env), return_object=True,
                **_effect(env, unit, "create_case"))
            replay = service.crear_nuevo_ticket("pyme", _payload(env), return_object=True,
                **_effect(env, unit, "create_case"))
            assert replay.id == ticket.id
            comment = service.crear_comentario(ticket.id, "pyme", {"comentario": "Synthetic continuation", "es_admin": False},
                **_effect(env, unit, "comment_case"))
            comment_replay = service.crear_comentario(ticket.id, "pyme", {"comentario": "Synthetic continuation", "es_admin": False},
                **_effect(env, unit, "comment_case"))
            assert comment_replay.id == comment.id
            contact, payload = _crm(env, unit, emit=True)
            assert sockets == [] and commits == []
            assert DomainEffectOutbox.query.count() == 4  # two created emails, admin comment email, realtime.
            receipts = TicketDomainEffectReceipt.query.order_by(TicketDomainEffectReceipt.id).all()
            assert len(receipts) == 2 and all(len(row.payload_hash) == 64 for row in receipts)
            assert receipts[0].idempotency_key.endswith(":create_case")
            assert receipts[1].idempotency_key.endswith(":comment_case")
            assert "unit_of_work" not in str(InteractionEvent.query.one().metadata_payload)
            outcome = unit.commit_reply(MetaReplyBundle((MetaReplyContent("text", "Synthetic reply"),)), runtime=env.runtime)
            assert outcome.committed is True
        assert commits == ["savepoint", "outer"]
    finally:
        event.remove(env.actual_session, "before_commit", on_commit)
    assert sockets == [(env.tenant.id, contact.id)]
    assert PymeTicket.query.count() == TicketComentario.query.count() == 1
    assert TicketDomainEffectReceipt.query.count() == 2 and WhatsAppOutboundAttempt.query.count() == 1
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "completed"
    service._notificar_ticket_por_email.assert_not_called()
    tickets.enviar_ticket_a_sigem.assert_not_called()
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_finalizer_rejection_rolls_back_case_receipts_crm_and_domain_effects(unit_env, monkeypatch):
    env = unit_env
    _queue(env, monkeypatch)
    service = _service(monkeypatch)
    emitted = Mock()
    monkeypatch.setattr(crm, "emit_crm_contact_update", emitted)
    with pytest.raises(MetaRuntimeError, match="meta_outbound_media_unsupported"):
        with _unit(env) as unit:
            ticket = service.crear_nuevo_ticket("pyme", _payload(env), return_object=True,
                **_effect(env, unit, "create_case"))
            service.crear_comentario(ticket.id, "pyme", {"comentario": "Synthetic continuation", "es_admin": False},
                **_effect(env, unit, "comment_case"))
            _crm(env, unit, emit=True)
            assert TicketDomainEffectReceipt.query.count() == 2 and DomainEffectOutbox.query.count() == 4
            unit.commit_reply(MetaReplyBundle((MetaReplyContent("image", "unsupported"),)), runtime=env.runtime)
    db.session.expire_all()
    _empty_counts()
    emitted.assert_not_called()
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("wrong", ("missing-turn", "other-turn", "foreign-tenant", "other-key", "missing-key"))
def test_case_writer_refuses_wrong_server_scope_before_any_row(unit_env, monkeypatch, wrong):
    env = unit_env
    _queue(env, monkeypatch)
    service = _service(monkeypatch)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_"):
        with _unit(env) as unit:
            values = _effect(env, unit, "create_case")
            values.update({"missing-turn": {"conversation_turn_id": None},
                "other-turn": {"conversation_turn_id": "different-synthetic-turn"},
                "foreign-tenant": {"idempotency_tenant_id": env.tenant.id + 1},
                "other-key": {"idempotency_key": f"whatsapp:{env.tenant.id}:different-turn:create_case"},
                "missing-key": {"idempotency_key": None}}[wrong])
            service.crear_nuevo_ticket("pyme", _payload(env), **values)
    _empty_counts()


def test_case_does_not_enable_legacy_policy_or_fall_back_to_direct_email(unit_env, monkeypatch):
    env = unit_env
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy")
    service = _service(monkeypatch)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_ticket_outbox_required"):
        with _unit(env) as unit:
            service.crear_nuevo_ticket("pyme", _payload(env), **_effect(env, unit, "create_case"))
    _empty_counts()
    assert env.app.config["DOMAIN_EFFECT_OUTBOX_MODE"] == "legacy"
    service._notificar_ticket_por_email.assert_not_called()


def test_comment_rejects_foreign_ticket_and_keeps_existing_guard(unit_env, monkeypatch):
    env = unit_env
    foreign_owner = User(name="Foreign synthetic owner", email="foreign-uow@example.invalid",
        rol="admin", tipo_chat="pyme", password_hash="synthetic-not-a-login")
    db.session.add(foreign_owner)
    db.session.flush()
    foreign_tenant = TenantProfile(slug="foreign-uow", nombre="Foreign synthetic tenant", tipo="pyme", pyme_id=foreign_owner.id)
    db.session.add(foreign_tenant)
    db.session.flush()
    foreign = PymeTicket(tenant_id=foreign_tenant.id, pregunta="Foreign synthetic case", nro_ticket=900002)
    db.session.add(foreign)
    db.session.commit()  # Fixture before Unit ownership.
    env.actual_session.execute(text("BEGIN"))
    _queue(env, monkeypatch)
    service = _service(monkeypatch)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_ticket_writer_failed"):
        with _unit(env) as unit:
            service.crear_comentario(foreign.id, "pyme", {"comentario": "Must not cross tenant"},
                **_effect(env, unit, "comment_case"))
    assert TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0
    assert PymeTicket.query.count() == 1


def test_caught_payload_conflict_poison_prevents_false_finalization(unit_env, monkeypatch):
    env = unit_env
    _queue(env, monkeypatch)
    service = _service(monkeypatch)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_ticket_writer_failed"):
        with _unit(env) as unit:
            service.crear_nuevo_ticket("pyme", _payload(env), **_effect(env, unit, "create_case"))
            try:
                service.crear_nuevo_ticket("pyme", _payload(env, pregunta="Changed request"),
                    **_effect(env, unit, "create_case"))
            except ConversationUnitOfWorkError:
                pass  # A caught domain error cannot produce an accepted turn.
            unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    _empty_counts()


def test_integrity_error_is_not_rolled_back_or_replayed_by_inner_writer(unit_env, monkeypatch):
    env = unit_env
    _queue(env, monkeypatch)
    service = _service(monkeypatch)
    original_flush = env.actual_session.flush
    attempts = []
    def lose_unique(*args, **kwargs):
        if any(isinstance(row, TicketDomainEffectReceipt) for row in env.actual_session.new):
            attempts.append(True)
            raise IntegrityError("synthetic unique tenant/key", {}, Exception("synthetic collision"))
        return original_flush(*args, **kwargs)
    monkeypatch.setattr(env.actual_session, "flush", lose_unique)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_ticket_integrity_failed"):
        with _unit(env) as unit:
            service.crear_nuevo_ticket("pyme", _payload(env), **_effect(env, unit, "create_case"))
    assert attempts == [True]
    _empty_counts()


@pytest.mark.parametrize("operation", ("municipal-reply", "tenant-reply", "operator-comment"))
def test_unported_operator_delivery_is_explicitly_unavailable_before_sender_resolution(unit_env, monkeypatch, operation):
    env = unit_env
    service = _service(monkeypatch)
    sender_resolution = Mock(side_effect=AssertionError("must_not_resolve_twilio"))
    import services.tenant_twilio_messaging as sender_module
    monkeypatch.setattr(sender_module, "resolve_tenant_twilio_sender_snapshot", sender_resolution)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_ticket_operator_delivery_not_ported"):
        with _unit(env) as unit:
            values = _effect(env, unit, "operator_reply")
            if operation == "municipal-reply":
                service.crear_respuesta_municipio(None, {}, reply_actor=env.owner, **values)
            elif operation == "tenant-reply":
                service.crear_respuesta_tenant(None, {}, reply_actor=env.owner, **values)
            else:
                service.crear_comentario(900001, "pyme", {"comentario": "Synthetic", "es_admin": True}, **values)
    sender_resolution.assert_not_called()
    _empty_counts()


def test_crm_other_turn_cannot_write(unit_env):
    env = unit_env
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_writer_scope_mismatch"):
        with _unit(env) as unit:
            crm.resolve_or_create_contact(env.tenant, phone="+" + env.event.contact,
                unit_of_work=unit, conversation_turn_id="different-synthetic-turn")
    _empty_counts()


def test_crm_contact_scope_is_checked_before_a_pending_cross_tenant_write(unit_env):
    env = unit_env
    contact = Contact(id="synthetic-contact-scope", tenant_id=env.tenant.id, name="Synthetic scoped contact")
    db.session.add(contact)
    db.session.commit()  # Fixture before Unit ownership.
    env.actual_session.execute(text("BEGIN"))
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_crm_contact_scope_mismatch"):
        with _unit(env) as unit:
            contact.tenant_id = env.tenant.id + 1
            crm.record_contact_interaction(tenant=env.tenant, contact=contact,
                message_body="Must not write cross scope", channel="whatsapp",
                unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
    db.session.expire_all()
    assert Contact.query.one().tenant_id == env.tenant.id
    assert InteractionEvent.query.count() == ContactSnapshot.query.count() == 0


def test_crm_detached_tenant_is_not_accepted_as_same_session_writer(unit_env):
    env = unit_env
    with Session(bind=db.engine) as other:
        foreign_session_tenant = other.get(TenantProfile, env.tenant.id)
        with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_crm_tenant_session_mismatch"):
            with _unit(env) as unit:
                crm.resolve_or_create_contact(foreign_session_tenant, phone="+" + env.event.contact,
                    unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
    _empty_counts()


def test_crm_unscoped_legacy_user_does_not_copy_profile_into_tenant(unit_env):
    env = unit_env
    user = User(name="Unscoped synthetic personal user", email="unscoped-uow@example.invalid",
        rol="user", password_hash="synthetic-not-a-login")
    db.session.add(user)
    db.session.commit()  # Fixture before Unit ownership.
    env.actual_session.execute(text("BEGIN"))
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_crm_legacy_user_scope_unproven"):
        with _unit(env) as unit:
            crm.resolve_or_create_contact(env.tenant, phone="+" + env.event.contact, legacy_user=user,
                unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
    _empty_counts()


def test_existing_crm_update_flushes_without_hidden_commit_and_socket_is_best_effort(unit_env, monkeypatch):
    env = unit_env
    contact = Contact(id="synthetic-existing-contact", tenant_id=env.tenant.id,
        name="Existing synthetic customer", phone="+" + env.event.contact)
    db.session.add(contact)
    db.session.commit()  # Fixture before Unit ownership.
    env.actual_session.execute(text("BEGIN"))
    attempted = []
    def failed_socket(*args):
        attempted.append(True)
        raise RuntimeError("synthetic_socket_failure")
    monkeypatch.setattr(crm, "emit_crm_contact_update", failed_socket)
    with _unit(env) as unit:
        found, payload = _crm(env, unit, emit=True)
        assert found.id == contact.id and attempted == []
        outcome = unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    assert attempted == [True] and outcome.after_commit_failures == ("crm_contact_update",)
    assert outcome.committed is True and InteractionEvent.query.count() == 1
    assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "completed"
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []


def test_default_ticket_without_unit_keeps_legacy_commit_and_notifications(unit_env, monkeypatch):
    env = unit_env
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy")
    service = ServicioTickets()
    service.auto_assign_enabled = False
    service._notificar_ticket_por_email = Mock()
    ticket = service.crear_nuevo_ticket("pyme", _payload(env), return_object=True)
    assert ticket is not None
    identity = ticket.id
    db.session.rollback()
    assert db.session.get(PymeTicket, identity) is not None
    service._notificar_ticket_por_email.assert_called_once()
    assert DomainEffectOutbox.query.count() == 0


def test_default_crm_emit_remains_immediate_and_does_not_commit(unit_env, monkeypatch):
    env = unit_env
    emitted = []
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_args: emitted.append(True))
    contact = crm.resolve_or_create_contact(env.tenant, phone="+" + env.event.contact, name="Synthetic legacy CRM")
    crm.record_contact_interaction(tenant=env.tenant, contact=contact,
        message_body="Synthetic", channel="whatsapp", emit=True)
    assert emitted == [True] and InteractionEvent.query.count() == 1
    db.session.rollback()
    assert Contact.query.count() == InteractionEvent.query.count() == 0


# CHILD V2: append-only recipient partition controls. The original 21 cases
# above are byte-identical to V1; their results remain unobserved/PREP.
def _recipient_contacts(env, *, ambiguity=None):
    foreign = Contact(id="a-synthetic-other-recipient", tenant_id=env.tenant.id,
        name="Other synthetic recipient", phone="+5491100000999",
        whatsapp_id="+5491100000999", email="other-recipient@example.invalid",
        external_id="other-synthetic-external", preferences={"marker": "foreign-only"})
    current = Contact(id="b-synthetic-current-recipient", tenant_id=env.tenant.id,
        name="Current synthetic recipient", phone="+" + env.event.contact,
        whatsapp_id="+" + env.event.contact, email="current-recipient@example.invalid",
        preferences={"marker": "current-only"})
    if ambiguity == "same-phone":
        foreign.phone = foreign.whatsapp_id = "+" + env.event.contact
    elif ambiguity == "crossed-channel":
        foreign.whatsapp_id = "+" + env.event.contact
    db.session.add_all([foreign, current])  # Conflicting alias row intentionally first.
    db.session.commit()  # Fixture before ownership.
    env.actual_session.execute(text("BEGIN"))
    return current, foreign


def _contact_rows():
    from copy import deepcopy
    return deepcopy(Contact.query.with_entities(Contact.id, Contact.tenant_id, Contact.name,
        Contact.phone, Contact.whatsapp_id, Contact.email, Contact.external_id,
        Contact.preferences, Contact.tags, Contact.last_interaction_at).order_by(Contact.id).all())


@pytest.mark.parametrize("conflict", ("email", "external", "phone"))
def test_uow_declared_alias_cannot_select_or_merge_another_recipient(unit_env, conflict):
    env = unit_env
    current, foreign = _recipient_contacts(env)
    before = _contact_rows()
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_crm_(alias_recipient_conflict|recipient_mismatch)"):
        with _unit(env) as unit:
            extras = {"email": {"email": foreign.email},
                "external": {"external_id": foreign.external_id},
                "phone": {"whatsapp_id": foreign.phone}}[conflict]
            crm.resolve_or_create_contact(env.tenant, phone=current.phone,
                unit_of_work=unit, conversation_turn_id=env.claim.turn_id, **extras)
    assert _contact_rows() == before
    assert ContactSnapshot.query.count() == InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0


def test_uow_record_cannot_write_another_contact_in_the_same_tenant(unit_env):
    env = unit_env
    current, foreign = _recipient_contacts(env)
    before = _contact_rows()
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_crm_contact_recipient_mismatch"):
        with _unit(env) as unit:
            crm.record_contact_interaction(tenant=env.tenant, contact=foreign,
                message_body="Must not enter another recipient history", channel="whatsapp",
                unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
    assert _contact_rows() == before
    assert ContactSnapshot.query.count() == InteractionEvent.query.count() == 0


@pytest.mark.parametrize("ambiguity", ("same-phone", "crossed-channel"))
@pytest.mark.parametrize("operation", ("resolve", "record"))
def test_uow_duplicate_recipient_is_not_chosen_with_first(unit_env, ambiguity, operation):
    env = unit_env
    current, foreign = _recipient_contacts(env, ambiguity=ambiguity)
    before = _contact_rows()
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_crm_recipient_ambiguous"):
        with _unit(env) as unit:
            if operation == "resolve":
                crm.resolve_or_create_contact(env.tenant, phone=current.phone,
                    unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
            else:
                crm.record_contact_interaction(tenant=env.tenant, contact=current,
                    message_body="Ambiguous synthetic recipient", channel="whatsapp",
                    unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
    assert _contact_rows() == before
    assert ContactSnapshot.query.count() == InteractionEvent.query.count() == 0


@pytest.mark.parametrize("wrong", ("other-phone", "contact-precedence", "duplicate-user"))
def test_uow_legacy_user_must_match_current_phone_and_fresh_resolution(unit_env, wrong):
    env = unit_env
    phone = "+5491100000999" if wrong == "other-phone" else "+" + env.event.contact
    user = User(name="Synthetic personal profile", email="personal-uow@example.invalid",
        telefono=phone, tenant_id=env.tenant.id, tenant_slug=env.tenant.slug,
        rol="user", password_hash="synthetic-not-a-login")
    db.session.add(user)
    if wrong == "duplicate-user":
        db.session.add(User(name="Duplicate synthetic personal profile",
            email="duplicate-personal-uow@example.invalid", telefono=phone,
            tenant_id=env.tenant.id, tenant_slug=env.tenant.slug,
            rol="user", password_hash="synthetic-not-a-login"))
    elif wrong == "contact-precedence":
        db.session.add(Contact(id="synthetic-preferred-contact", tenant_id=env.tenant.id,
            name="Synthetic CRM precedence", phone=phone, email="crm-uow@example.invalid"))
    db.session.commit()  # Fixture before ownership.
    env.actual_session.execute(text("BEGIN"))
    before = _contact_rows()
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_crm_legacy_user_scope_unproven"):
        with _unit(env) as unit:
            crm.resolve_or_create_contact(env.tenant, phone="+" + env.event.contact, legacy_user=user,
                unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
    assert _contact_rows() == before
    assert ContactSnapshot.query.count() == InteractionEvent.query.count() == 0


def test_uow_phone_selects_current_contact_and_keeps_other_history_unchanged(unit_env):
    env = unit_env
    current, foreign = _recipient_contacts(env)
    foreign_before = next(row for row in _contact_rows() if row.id == foreign.id)
    with _unit(env) as unit:
        found = crm.resolve_or_create_contact(env.tenant, phone=current.phone,
            whatsapp_id=current.whatsapp_id, unit_of_work=unit,
            conversation_turn_id=env.claim.turn_id)
        assert found.id == current.id
        payload = crm.record_contact_interaction(tenant=env.tenant, contact=found,
            message_body="Current recipient synthetic history", channel="whatsapp",
            unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
        assert payload["contact_id"] == current.id and payload["email"] == current.email
        unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    assert InteractionEvent.query.one().contact_id == current.id
    assert ContactSnapshot.query.one().contact_id == current.id
    assert next(row for row in _contact_rows() if row.id == foreign.id) == foreign_before
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_uow_unique_current_legacy_user_keeps_its_own_profile(unit_env):
    env = unit_env
    user = User(name="Unique synthetic personal profile", email="unique-personal-uow@example.invalid",
        telefono="+" + env.event.contact, tenant_id=env.tenant.id,
        tenant_slug=env.tenant.slug, rol="user", password_hash="synthetic-not-a-login")
    db.session.add(user)
    db.session.commit()  # Fixture before ownership.
    env.actual_session.execute(text("BEGIN"))
    with _unit(env) as unit:
        found = crm.resolve_or_create_contact(env.tenant, phone=user.telefono,
            legacy_user=user, unit_of_work=unit, conversation_turn_id=env.claim.turn_id)
        assert found.phone == user.telefono and found.email == user.email
        assert found.preferences["legacy_user_id"] == user.id
        unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    assert Contact.query.count() == 1 and InteractionEvent.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == []
