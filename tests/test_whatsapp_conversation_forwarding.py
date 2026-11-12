"""PREP: common/CRM/ticket/finalizer/logic real; HTTP/engine/socket synthetic.

This cohort does not select the closed 72 tests. The old human-not-forwarded
result remains historical; an existing-policy HOLD is its proposed replacement.
Root must install the offline runtime guard before importing this module.
"""
from __future__ import annotations

import inspect
import json
import time
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from models import (
    DomainEffectOutbox, MunicipioTicket, PymeTicket, TicketComentario,
    TicketDomainEffectReceipt, WhatsAppInboundTurn, WhatsAppOutboundAttempt, db,
)
from models_memory import Contact, ContactSnapshot, InteractionEvent
from services import whatsapp_conversation_turn as common
from services import logic
from services.contact_service import resolve_contact
from services.meta_whatsapp_outbox import MetaReplyBundle, MetaReplyContent
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.whatsapp_conversation_contract import ConversationSelection, CollectingConversationSink
from services.whatsapp_conversation_uow import ConversationUnitOfWorkError
from tests.test_whatsapp_conversation_uow import environment, unit_env, _unit, _turn


def _queue(env, monkeypatch):
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "queue")
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", "synthetic-forwarding-" + "x" * 32)
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", str(env.tenant.id))


def _physical(env):
    actual = db.session()
    if not actual.connection().connection.driver_connection.in_transaction:
        actual.execute(text("BEGIN"))
    env.actual_session = actual


def _human(env):
    ticket = PymeTicket(tenant_id=env.tenant.id, pregunta="Synthetic human continuation",
        nro_ticket=880002, anon_id="+" + env.event.contact, estado="en_vivo")
    db.session.add(ticket)
    db.session.flush()
    env.context.context_data = {"human_chat_in_progress": True,
        "ticket_id": ticket.id, "tipo_ticket": "pyme"}
    db.session.commit()  # Fixture before ownership; never a writer commit.
    _physical(env)
    return ticket


def _municipal_followup(env):
    env.tenant.tipo, env.tenant.pyme_id, env.tenant.municipio_id = "municipio", None, env.owner.id
    env.owner.tipo_chat = "municipio"
    ticket = MunicipioTicket(tenant_id=env.tenant.id, municipio_id=env.owner.id,
        pregunta="Synthetic municipal case", asunto="Synthetic followup",
        nro_ticket="880003", consulta_pin="825716", anon_id="+" + env.event.contact,
        estado="nuevo")
    db.session.add(ticket)
    db.session.flush()
    env.context.context_data = {"active_ticket_followup": {
        "ticket_id": ticket.id, "ticket_nro": ticket.nro_ticket,
        "consulta_pin": ticket.consulta_pin, "until": time.time() + 600}}
    db.session.commit()  # Fixture before ownership.
    _physical(env)
    return ticket


def _prepare(turn, sink):
    return common.prepare_conversation_turn(turn, sink,
        on_failure=lambda *_a: (_ for _ in ()).throw(AssertionError("unexpected_legacy_fallback")),
        log=lambda *_a, **_kw: None, safe_reference=lambda *_a: "synthetic-reference")


def _finish(turn, sink, *, engine=None, prepared=None):
    def synthetic_engine(*, unit_of_work=None, conversation_turn_id=None, **kwargs):
        # This engine is the declared synthetic seam, not a ported responder.
        assert unit_of_work is turn.unit_of_work
        if unit_of_work is not None:
            assert conversation_turn_id == turn.durable_claim.turn_id
        return {"message_body": "Synthetic prepared service reply", "message_type": "text",
            "options_list": [], "contexto_actualizado": {"forwarding_marker": True}}
    return common.finish_conversation_turn(turn,
        ConversationSelection(turn.message_body, None, None, None), sink,
        prepared_response=prepared, public_base_url="https://example.invalid",
        engine=engine or synthetic_engine, classify_attachment=lambda *_a: {},
        extract_contact_details=lambda *_a: {},
        resolve_input_contact=lambda phone, name: resolve_contact(phone, name, tenant=turn.tenant_profile),
        receipt_header=lambda: None, normalize_flow_contract=lambda *_a, **_kw: None,
        on_failure=lambda *_a: (_ for _ in ()).throw(AssertionError("unexpected_legacy_fallback")),
        replay_error=RuntimeError, log=lambda *_a, **_kw: None,
        safe_reference=lambda *_a: "synthetic-reference", safe_context_metadata=lambda *_a: {})


@pytest.mark.parametrize("reject_output", (False, True))
def test_human_prepare_uses_real_unit_writer_and_zero_automatic_messages(unit_env, monkeypatch, reject_output):
    env = unit_env
    ticket = _human(env)
    _queue(env, monkeypatch)
    sockets = []
    monkeypatch.setattr(common, "_emit_live_chat_event", lambda payload: sockets.append(payload))
    if reject_output:
        expected = pytest.raises(MetaRuntimeError, match="meta_outbound_media_unsupported")
    else:
        from contextlib import nullcontext
        expected = nullcontext()
    with expected:
        with _unit(env) as unit:
            sink = CollectingConversationSink(supports_media=False)
            prepared = _prepare(_turn(env, unit), sink)
            assert prepared.handled and prepared.legacy_return == ("OK", 200)
            assert sink.snapshot() == () and sockets == []
            comment = TicketComentario.query.one()
            assert comment.pyme_ticket_id == ticket.id and comment.comentario == env.event.text
            receipt = TicketDomainEffectReceipt.query.one()
            assert receipt.idempotency_key == f"whatsapp:{env.tenant.id}:{env.claim.turn_id}:live_chat_comment:pyme:{ticket.id}"
            assert len(receipt.payload_hash) == 64 and DomainEffectOutbox.query.count() == 1
            reply = (MetaReplyBundle((MetaReplyContent("image", "unsupported"),))
                if reject_output else MetaReplyBundle(()))
            result = unit.commit_reply(reply, runtime=env.runtime)
    db.session.expire_all()
    if reject_output:
        assert TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0
        assert sockets == []
    else:
        assert result.committed and result.receipt.outbound_attempt_ids == ()
        assert WhatsAppInboundTurn.query.filter_by(turn_id=env.claim.turn_id).one().status == "completed"
        assert len(sockets) == 1 and sockets[0]["message"]["id"] == TicketComentario.query.one().id
    assert WhatsAppOutboundAttempt.query.count() == 0
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_human_live_socket_failure_is_best_effort_after_commit(unit_env, monkeypatch):
    env = unit_env
    _human(env)
    _queue(env, monkeypatch)
    def socket_failure(_payload):
        raise RuntimeError("synthetic_socket_failure")
    monkeypatch.setattr(common, "_emit_live_chat_event", socket_failure)
    with _unit(env) as unit:
        prepared = _prepare(_turn(env, unit), CollectingConversationSink(supports_media=False))
        assert prepared.handled
        result = unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    assert result.committed and result.after_commit_failures == ("live_chat_message",)
    assert TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == 1
    assert WhatsAppOutboundAttempt.query.count() == 0


def test_human_forwarding_does_not_activate_a_disabled_domain_queue(unit_env, monkeypatch):
    env = unit_env
    _human(env)
    monkeypatch.setitem(env.app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy")
    emitted = Mock()
    monkeypatch.setattr(common, "_emit_live_chat_event", emitted)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_ticket_outbox_required"):
        with _unit(env) as unit:
            _prepare(_turn(env, unit), CollectingConversationSink(supports_media=False))
    emitted.assert_not_called()
    assert TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0
    assert env.app.config["DOMAIN_EFFECT_OUTBOX_MODE"] == "legacy"
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("reject_output", (False, True))
def test_municipal_followup_uses_server_turn_key_without_an_inner_commit(unit_env, monkeypatch, reject_output):
    env = unit_env
    ticket = _municipal_followup(env)
    _queue(env, monkeypatch)
    if reject_output:
        expected = pytest.raises(MetaRuntimeError, match="meta_outbound_media_unsupported")
    else:
        from contextlib import nullcontext
        expected = nullcontext()
    with expected:
        with _unit(env) as unit:
            turn = _turn(env, unit)
            sink = CollectingConversationSink(supports_media=False)
            prepared = _prepare(turn, sink)
            assert prepared.handled and len(sink.snapshot()) == 1
            item = sink.snapshot()[0]
            assert item.kind == "text" and "Sumé tu mensaje" in item.value
            receipt = TicketDomainEffectReceipt.query.one()
            assert receipt.idempotency_key == f"whatsapp:{env.tenant.id}:{env.claim.turn_id}:ticket_followup_comment:{ticket.id}"
            assert TicketComentario.query.one().municipio_ticket_id == ticket.id
            assert DomainEffectOutbox.query.count() == 0  # Both effect flags deliberately false.
            reply = (MetaReplyBundle((MetaReplyContent("image", "unsupported"),)) if reject_output
                else MetaReplyBundle((MetaReplyContent("text", item.value),)))
            result = unit.commit_reply(reply, runtime=env.runtime)
    if reject_output:
        assert TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == 0
        assert WhatsAppOutboundAttempt.query.count() == 0
    else:
        assert result.committed and TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == 1
        assert WhatsAppOutboundAttempt.query.count() == 1
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_finish_prepared_response_forwards_to_both_real_crm_writers(unit_env, monkeypatch):
    env = unit_env
    seen, sockets = [], []
    original_resolve, original_record = common.resolve_or_create_contact, common.record_contact_interaction
    def resolve(*args, **kwargs):
        seen.append(("resolve", kwargs["unit_of_work"], kwargs["conversation_turn_id"]))
        return original_resolve(*args, **kwargs)
    def record(*args, **kwargs):
        seen.append(("record", kwargs["unit_of_work"], kwargs["conversation_turn_id"]))
        return original_record(*args, **kwargs)
    monkeypatch.setattr(common, "resolve_or_create_contact", resolve)
    monkeypatch.setattr(common, "record_contact_interaction", record)
    monkeypatch.setattr(common, "emit_crm_contact_update", lambda *_a: sockets.append(True))
    engine = Mock(side_effect=AssertionError("prepared_response_must_not_invoke_engine"))
    with _unit(env) as unit:
        finished = _finish(_turn(env, unit), CollectingConversationSink(supports_media=False),
            engine=engine, prepared={"message_body": "Synthetic already prepared reply",
                "message_type": "text", "options_list": []})
        assert not finished.legacy_return and seen == [("resolve", unit, env.claim.turn_id), ("record", unit, env.claim.turn_id)]
        assert sockets == [] and InteractionEvent.query.count() == 1
        serialized = json.dumps({"context": env.context.context_data,
            "event": InteractionEvent.query.one().metadata_payload}, allow_nan=False)
        assert "unit_of_work" not in serialized and "conversation_turn_id" not in serialized
        unit.commit_reply(MetaReplyBundle((MetaReplyContent("text", finished.bot_response["message_body"]),)), runtime=env.runtime)
    engine.assert_not_called()
    assert sockets == [True] and Contact.query.count() == ContactSnapshot.query.count() == 1


def test_finish_does_not_bypass_new_crm_ambiguity_guard(unit_env):
    env = unit_env
    db.session.add_all([Contact(id="synthetic-forwarding-a", tenant_id=env.tenant.id,
        phone="+" + env.event.contact, name="Synthetic A"),
        Contact(id="synthetic-forwarding-b", tenant_id=env.tenant.id,
        phone="+" + env.event.contact, name="Synthetic B")])
    db.session.commit()  # Fixture before ownership.
    _physical(env)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_uow_crm_recipient_ambiguous"):
        with _unit(env) as unit:
            _finish(_turn(env, unit), CollectingConversationSink(supports_media=False),
                prepared={"message_body": "Synthetic prepared reply", "message_type": "text", "options_list": []})
    assert Contact.query.count() == 2 and ContactSnapshot.query.count() == InteractionEvent.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0


def _assert_no_engine_paths(monkeypatch):
    blocked = Mock(side_effect=AssertionError("unported_engine_path_called"))
    monkeypatch.setattr(logic, "maybe_handle_catalog_share", blocked)
    monkeypatch.setattr(logic, "maybe_handle_demo_interaction", blocked)
    # Both happen later than catalog; blocking the first proven boundary also
    # catches an entry gate placed too late, before media/LLM/dispatch can run.
    return blocked


def test_common_forwards_private_arguments_to_real_logic_and_holds_before_effects(unit_env, monkeypatch):
    env = unit_env
    blocked = _assert_no_engine_paths(monkeypatch)
    seen = []
    def engine(*, unit_of_work=None, conversation_turn_id=None, **kwargs):
        seen.append((unit_of_work, conversation_turn_id))
        assert "unit_of_work" not in kwargs and "conversation_turn_id" not in kwargs
        return logic.responder_chatboc(unit_of_work=unit_of_work,
            conversation_turn_id=conversation_turn_id, **kwargs)
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_engine_path_not_ported"):
        with _unit(env) as unit:
            _finish(_turn(env, unit), CollectingConversationSink(supports_media=False), engine=engine)
    assert len(seen) == 1 and seen[0][1] == env.claim.turn_id
    blocked.assert_not_called()
    assert Contact.query.count() == InteractionEvent.query.count() == WhatsAppOutboundAttempt.query.count() == 0


@pytest.mark.parametrize("wrong", ("missing-turn", "other-turn", "foreign-tenant", "fake-unit", "turn-without-unit"))
def test_logic_private_gate_denies_before_any_unported_call(unit_env, monkeypatch, wrong):
    env = unit_env
    blocked = _assert_no_engine_paths(monkeypatch)
    signature = inspect.signature(logic.responder_chatboc)
    assert signature.parameters["unit_of_work"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["conversation_turn_id"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(ConversationUnitOfWorkError, match="conversation_"):
        with _unit(env) as unit:
            values = {"unit_of_work": unit, "conversation_turn_id": env.claim.turn_id,
                "tenant_id": env.tenant.id}
            values.update({"missing-turn": {"conversation_turn_id": None},
                "other-turn": {"conversation_turn_id": "synthetic-other-turn"},
                "foreign-tenant": {"tenant_id": env.tenant.id + 1},
                "fake-unit": {"unit_of_work": {"looks_like": "client_json"}},
                "turn-without-unit": {"unit_of_work": None}}[wrong])
            logic.responder_chatboc("Synthetic question", owner_user=env.owner,
                chat_db_context=env.context, tipo_chat="pyme", **values)
    blocked.assert_not_called()
    assert InteractionEvent.query.count() == TicketComentario.query.count() == WhatsAppOutboundAttempt.query.count() == 0


def test_logic_default_legacy_call_keeps_private_arguments_out_of_handler_kwargs(unit_env, monkeypatch):
    env = unit_env
    import services.institutional_assistant as institutional
    import services.pymes as pymes
    monkeypatch.setattr(logic, "maybe_handle_catalog_share", lambda **_kw: None)
    monkeypatch.setattr(institutional, "maybe_handle_institutional_question", lambda *_a, **_kw: None)
    seen = []
    def handler(**kwargs):
        seen.append(kwargs)
        assert "unit_of_work" not in kwargs and "conversation_turn_id" not in kwargs
        return {"message_body": "Synthetic legacy responder", "message_type": "text",
            "options_list": [], "skip_audio_generation": True}
    monkeypatch.setattr(pymes, "responder_pyme", handler)
    result = logic.responder_chatboc("Consulta sintética", owner_user=env.owner,
        chat_db_context=env.context, rubro_nombre_frontend="comercio", tipo_chat="pyme",
        anon_id="+" + env.event.contact, chat_session_uuid=env.claim.chat_session_id,
        channel="whatsapp", tenant_id=env.tenant.id)
    assert result["message_body"] == "Synthetic legacy responder" and len(seen) == 1


def test_common_default_engine_signature_remains_legacy(unit_env, monkeypatch):
    env = unit_env
    seen = []
    def engine(**kwargs):
        seen.append(kwargs)
        assert "unit_of_work" not in kwargs and "conversation_turn_id" not in kwargs
        return {"message_body": "Synthetic legacy engine", "message_type": "text", "options_list": []}
    import services.crm_intelligence as crm
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_a: None)
    finished = _finish(_turn(env), CollectingConversationSink(supports_media=False), engine=engine)
    assert not finished.legacy_return and len(seen) == 1 and InteractionEvent.query.count() == 1
