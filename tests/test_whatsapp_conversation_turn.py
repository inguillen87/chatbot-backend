"""Post-auth conversation stages with real SQLite/context/formatter/CRM.

Actors, engine responses and transport sinks are synthetic. No Meta ingress,
signature, provider availability, delivery or global transaction is certified.
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
import pytest

from models import ChatSessionContext, MunicipioTicket, TenantProfile, TicketComentario, User, db
from models_memory import Contact, InteractionEvent
from services.constants import CONTEXTO_MUNICIPIO
from services.whatsapp_conversation_contract import (
    CollectingConversationSink, ConversationMode, ConversationOutputUnsupported,
    ConversationSelection, ConversationTurn,
)
from services.whatsapp_conversation_turn import prepare_conversation_turn, finish_conversation_turn


CONTACT = "+15550001234"


@pytest.fixture
def conversation():
    app = Flask("conversation-common-synthetic")
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, CUTOVER_WRITER_FENCE_ENABLED=False)
    db.init_app(app)
    with app.app_context():
        assert str(db.engine.url) == "sqlite:///:memory:"
        db.create_all()
        owners, tenants = [], []
        for suffix in ("a", "b"):
            owner = User(name="Synthetic organization " + suffix,
                         email="conversation-" + suffix + "@example.invalid",
                         rol="admin", tipo_chat="municipio", password_hash="not-a-login")
            db.session.add(owner)
            db.session.flush()
            owner.municipio_id = owner.id
            tenant = TenantProfile(slug="conversation-" + suffix, nombre="Synthetic " + suffix,
                                   tipo="municipio", municipio_id=owner.id, is_active=True)
            db.session.add(tenant)
            db.session.flush()
            owner.tenant_id = tenant.id
            owners.append(owner)
            tenants.append(tenant)
        stored = ChatSessionContext(chat_session_id="synthetic-conversation-a",
            user_id=owners[0].id, tenant_id=tenants[0].id, anon_id=CONTACT, context_data={})
        db.session.add(stored)
        db.session.commit()
        turn = ConversationTurn(owner_user=owners[0], tenant_profile=tenants[0], tenant_id=tenants[0].id, end_user=None,
            session_context=stored, chat_session_id=stored.chat_session_id,
            contact_identity=CONTACT, message_body="La rama bloquea la vereda en Calle Prueba 123.",
            action_id=None, source_event_id="synthetic-event-a", durable_claim=None,
            mode=ConversationMode(False, False), original_text="texto original sintético")
        yield SimpleNamespace(app=app, owners=owners, tenants=tenants, stored=stored, turn=turn)
        db.session.rollback()
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _failure(code, exc):
    raise AssertionError(code) from exc


def _prepare(turn, sink):
    return prepare_conversation_turn(turn, sink, on_failure=_failure,
        log=lambda *args: None, safe_reference=lambda value: "synthetic-reference")


def _finish(turn, selection, sink, *, response=None, engine=None, classify=None, extract=None, resolve=None):
    engine = engine or Mock(return_value={"message_body": "Indique el detalle faltante.",
                                         "options_list": [], "message_type": "text"})
    result = finish_conversation_turn(turn, selection, sink,
        prepared_response=response, public_base_url="https://conversation.example.invalid",
        engine=engine, classify_attachment=classify or Mock(return_value={}),
        extract_contact_details=extract or Mock(side_effect=AssertionError("Unexpected LLM extraction")),
        resolve_input_contact=resolve or Mock(return_value=None), receipt_header=lambda: None,
        normalize_flow_contract=lambda payload, **kwargs: None, on_failure=_failure,
        replay_error=RuntimeError, log=lambda *args: None,
        safe_reference=lambda value: "synthetic-reference", safe_context_metadata=lambda value: {})
    assert result.legacy_return is None
    return result, engine


def _ticket(env, index=0, *, state="en_vivo"):
    ticket = MunicipioTicket(pregunta="", asunto="Arbolado", categoria="Arbolado",
        detalles="Detalle sintético", direccion="Calle Prueba 123", estado=state,
        municipio_id=env.owners[index].id, tenant_id=env.tenants[index].id,
        anon_id=CONTACT, nro_ticket="81000" + str(index), consulta_pin="123456")
    db.session.add(ticket)
    db.session.commit()
    return ticket


def test_collecting_sink_keeps_order_and_detaches_mutable_responses():
    sink = CollectingConversationSink(supports_media=False)
    assert sink.snapshot() == ()
    bot = {"message_body": "Segundo", "options_list": [{"texto": "Opción"}]}
    formatted = {"message_body": "Segundo", "contexto_actualizado": {"nested": [1]}}
    sink.emit_text("Primero")
    sink.emit_response(bot, formatted)
    sink.emit_text("Tercero")
    bot["options_list"].clear()
    formatted["contexto_actualizado"]["nested"].append(2)
    emissions = sink.snapshot()
    assert [item.kind for item in emissions] == ["text", "formatted", "text"]
    assert emissions[1].value[0]["options_list"] == [{"texto": "Opción"}]
    assert emissions[1].value[1]["contexto_actualizado"] == {"nested": [1]}


@pytest.mark.parametrize("key", ["image_url", "audio_url", "sticker_url", "document_url", "video_url", "media_url", "_twilio_pre_messages"])
def test_text_sink_rejects_unsupported_media_instead_of_dropping_it(key):
    sink = CollectingConversationSink(supports_media=False)
    with pytest.raises(ConversationOutputUnsupported):
        sink.emit_response({"message_body": "Con medio", key: "synthetic-media"}, {})
    assert sink.snapshot() == ()


@pytest.mark.parametrize("institutional,waiting", [(True, False), (False, True)])
def test_numeric_input_keeps_original_meaning_when_menu_translation_is_inapplicable(conversation, institutional, waiting):
    env = conversation
    context = {"last_options_sent": [{"texto": "Otra acción", "action_id": "unrelated"}]}
    if institutional:
        context["last_options_scope"] = "institutional_knowledge"
    if waiting:
        context[CONTEXTO_MUNICIPIO] = {"esperando_info_llm_reclamo": "direccion",
            "reclamo_flow_v2": {"state": "ESPERANDO_DIRECCION"}}
    env.stored.context_data = context
    db.session.commit()
    turn = replace(env.turn, message_body="123", original_text="123")
    result = _prepare(turn, CollectingConversationSink(supports_media=False))
    assert not result.handled
    assert result.selection.message_body == "123"
    assert result.selection.action_id is None


def test_engine_keeps_text_location_and_current_action_and_persists_tenant_crm(conversation):
    env = conversation
    location = {"latitude": 0, "longitude": -68.31, "address": "Calle Prueba 123"}
    turn = replace(env.turn, location=location, original_profile_name="Synthetic contact",
                   profile_name="Earlier welcome name")
    selection = ConversationSelection(turn.message_body, None, "current-action", "Current", False)
    sink = CollectingConversationSink(supports_media=False)
    _, engine = _finish(turn, selection, sink)
    kwargs = engine.call_args.kwargs
    assert kwargs["pregunta"] == turn.message_body
    assert kwargs["location"] == location and kwargs["location"]["latitude"] == 0
    assert kwargs["action"] == "current-action"
    assert kwargs["profile_name"] == "Synthetic contact"
    assert kwargs["chat_db_context"] is env.stored
    assert kwargs["tenant_id"] == env.tenants[0].id
    db.session.expire_all()
    contact = Contact.query.one()
    interaction = InteractionEvent.query.one()
    assert contact.tenant_id == interaction.tenant_id == env.tenants[0].id
    assert interaction.content == turn.message_body
    assert interaction.metadata_payload["selected_action_id"] == "current-action"
    assert Contact.query.filter_by(tenant_id=env.tenants[1].id).count() == 0
    assert len(sink.snapshot()) == 1


def test_raw_absent_profile_does_not_leak_effective_welcome_name_into_engine(conversation):
    env = conversation
    turn = replace(env.turn, profile_name="Earlier welcome name", original_profile_name=None)
    resolve = Mock(return_value=None)
    _, engine = _finish(turn, ConversationSelection(turn.message_body, None, None, None),
                       CollectingConversationSink(supports_media=False), resolve=resolve)
    resolve.assert_called_once_with(CONTACT, None)
    assert "profile_name" not in engine.call_args.kwargs
    assert Contact.query.one().name != "Earlier welcome name"


def test_admitted_legacy_turn_preserves_resolved_id_without_profile(conversation):
    env = conversation
    turn = replace(env.turn, tenant_profile=None, tenant_id=env.owners[0].id)
    sink = CollectingConversationSink(supports_media=False)
    preparation = _prepare(turn, sink)
    assert not preparation.handled
    _, engine = _finish(turn, preparation.selection, sink)
    assert engine.call_args.kwargs["tenant_profile"] is None
    assert engine.call_args.kwargs["tenant_id"] == env.owners[0].id
    assert Contact.query.count() == 0


def test_prepared_branch_keeps_effective_name_without_calling_engine(conversation):
    env = conversation
    turn = replace(env.turn, profile_name="Prepared branch name", original_profile_name=None)
    engine, resolve = Mock(side_effect=AssertionError("Prepared branch called engine")), Mock()
    _finish(turn, ConversationSelection(turn.message_body, None, "prepared-action", None),
        CollectingConversationSink(supports_media=False), response={"message_body": "Prepared reply", "options_list": []},
        engine=engine, resolve=resolve)
    engine.assert_not_called()
    resolve.assert_not_called()
    assert Contact.query.one().name == "Prepared branch name"


def test_completion_marker_wins_over_stale_formatter_context(conversation):
    env = conversation
    env.stored.context_data = {CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": {"state": "ESPERANDO_CONFIRMACION"}},
        "last_options_sent": [{"texto": "Confirmar", "action_id": "reclamo_confirmar_si"}]}
    db.session.commit()
    completion = {"confirmation_id": "synthetic-completion", "ticket_id": 901,
        "ticket_nro": "M-900001", "consulta_pin": "123456",
        "tracking_url": "https://conversation.example.invalid/tracking/claim/900001"}
    reply = {"message_body": "Recibido", "options_list": [], "_reclamo_completion": completion,
        "contexto_actualizado": deepcopy(env.stored.context_data)}
    _finish(env.turn, ConversationSelection(env.turn.message_body, None, None, None),
        CollectingConversationSink(supports_media=False), response=reply)
    db.session.expire_all()
    current = env.stored.context_data
    assert "reclamo_flow_v2" not in current[CONTEXTO_MUNICIPIO]
    assert "last_options_sent" not in current
    assert current["active_ticket_followup"]["ticket_id"] == 901


@pytest.mark.parametrize("foreign", [False, True])
def test_human_chat_routes_only_to_exact_tenant_ticket_and_may_emit_nothing(conversation, foreign):
    env = conversation
    target = _ticket(env, 1 if foreign else 0)
    env.stored.context_data = {"human_chat_in_progress": True, "ticket_id": target.id,
                               "tipo_ticket": "municipio", "room": "untrusted-room"}
    db.session.commit()
    sink = CollectingConversationSink(supports_media=False)
    result = _prepare(env.turn, sink)
    assert sink.snapshot() == ()
    if foreign:
        assert not result.handled
        assert TicketComentario.query.count() == 0
        db.session.expire_all()
        assert "human_chat_in_progress" not in env.stored.context_data
    else:
        assert result.handled and result.legacy_return == ("OK", 200)
        comment = TicketComentario.query.one()
        assert comment.municipio_ticket_id == target.id
        assert comment.comentario == env.turn.message_body
        assert comment.origen == "whatsapp"


def test_active_claim_suppresses_new_media_classification_hook(conversation):
    env = conversation
    env.stored.context_data = {CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": {"state": "ESPERANDO_DIRECCION"}}}
    db.session.commit()
    turn = replace(env.turn, attachment={"id": 42, "mime_type": "image/jpeg", "url": "https://media.example.invalid/image"})
    sink = CollectingConversationSink(supports_media=False)
    preparation = _prepare(turn, sink)
    classifier = Mock(side_effect=AssertionError("Active claim reclassified media"))
    _, engine = _finish(turn, preparation.selection, sink, classify=classifier)
    classifier.assert_not_called()
    assert engine.call_args.kwargs["skip_media_analysis"] is True
    assert engine.call_args.kwargs["uploaded_file_info"]["id"] == 42
