"""Signed Meta greeting -> native worker -> committed, ordered text outbox."""
from copy import deepcopy

import pytest

from models import (
    ChatSessionContext, MunicipioTicket, TicketComentario, WhatsAppOutboundAttempt, db,
)
from services import whatsapp_inbound_worker as worker
from services.constants import CONTEXTO_MUNICIPIO
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.meta_whatsapp_survey import STATE_KEY as SURVEY_NAVIGATION_KEY
from services.whatsapp_survey_conversation import WHATSAPP_SURVEY_FLOW_STATE_KEY
from tests.test_meta_whatsapp_inbound_worker import _post, _stage, _turn
from tests.test_meta_whatsapp_outbox import CONTACT, environment


def _prepare(environment, *, body="hola", followup="missing", surveys=True):
    environment.tenant.nombre = "Junín"
    environment.tenant.configuracion = {
        "nombre": "Junín", "assistant_name": "JUNI", "encuestas_enabled": surveys,
    }
    db.session.commit()
    return _stage(environment, followup=followup, body=body)


def _attempts(turn_id):
    return WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=_turn(turn_id).id).order_by(
        WhatsAppOutboundAttempt.sequence_no).all()


def _bodies(turn_id):
    return [attempt.payload_json["message"]["text"]["body"] for attempt in _attempts(turn_id)]


def _process(tenant_id, turn_id):
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 1, report
    assert _turn(turn_id).status == "completed"
    return report


def test_fresh_hello_has_exact_text_outbox_and_duplicate_does_not_repeat(environment):
    tenant_id, turn_id = _prepare(environment)
    # The canary explicitly selects text; it must not erase global media config
    # or convert an unverified URL into a native MediaRef.
    configured_media = "/static/welcome/juni-saludo-sticker.webp"
    environment.app.config["WELCOME_MEDIA_URL"] = configured_media
    context = db.session.get(ChatSessionContext, _turn(turn_id).chat_session_id)
    before = deepcopy(context.context_data)
    db.session.commit()
    assert WhatsAppOutboundAttempt.query.count() == 0 and environment.calls["post"] == []

    _process(tenant_id, turn_id)
    rows = _attempts(turn_id)
    assert [(row.provider, row.status, row.sequence_no) for row in rows] == [
        ("meta", "pending", 1), ("meta", "pending", 2),
    ]
    assert _bodies(turn_id) == [
        "¡Hola! Soy JUNI, asistente virtual de Junín.",
        "Podés usar estas opciones:\n\n• Escribí Iniciar reclamo.\n• Escribí Encuestas.",
    ]
    assert all(row.payload_json["message"]["to"] == CONTACT for row in rows)
    assert all(row.payload_json["message"]["type"] == "text" for row in rows)
    assert db.session.get(ChatSessionContext, _turn(turn_id).chat_session_id).context_data == before
    assert environment.app.config["WELCOME_MEDIA_URL"] == configured_media
    assert environment.calls["post"] == [] and environment.calls["twilio"] == 0

    duplicate = _post(environment, body="hola")
    assert duplicate == turn_id
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["processed"] == 0
    assert len(_attempts(turn_id)) == 2
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "accepted"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "accepted"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "idle"
    assert [call["json"]["text"]["body"] for call in environment.calls["post"]] == _bodies(turn_id)
    assert environment.calls["twilio"] == 0


@pytest.mark.parametrize("body", ("hola", "buenas tardes", "menú"))
def test_existing_greetings_use_institutional_identity_without_owner_impersonation(environment, body):
    tenant_id, turn_id = _prepare(environment, body=body, surveys=False)
    context = db.session.get(ChatSessionContext, _turn(turn_id).chat_session_id)
    context.context_data = {"profile_name": environment.owner.name}
    db.session.commit()
    _process(tenant_id, turn_id)
    assert _bodies(turn_id) == [
        "¡Hola! Soy JUNI, asistente virtual de Junín.",
        "Podés usar estas opciones:\n\n• Escribí Iniciar reclamo.",
    ]


@pytest.mark.parametrize("active", ("draft", "survey", "survey_navigation", "followup"))
def test_hello_does_not_reset_or_answer_active_state(environment, active):
    tenant_id, turn_id = _prepare(environment, followup="valid" if active == "followup" else "missing")
    context = db.session.get(ChatSessionContext, _turn(turn_id).chat_session_id)
    data = deepcopy(context.context_data)
    municipal = data.setdefault(CONTEXTO_MUNICIPIO, {})
    if active == "draft":
        municipal["reclamo_flow_v2"] = {
            "meta_text_draft_v1": True, "state": "ESPERANDO_DESCRIPCION",
            "datos_reclamo": {"categoria": "Arbolado", "descripcion": "Descripción previa"},
        }
    elif active == "survey":
        municipal[WHATSAPP_SURVEY_FLOW_STATE_KEY] = {
            "step": "question", "question_index": 1, "answers": [{"value": "Respuesta previa"}],
        }
    elif active == "survey_navigation":
        municipal[SURVEY_NAVIGATION_KEY] = {"page": 2, "choices": [{"id": 12, "slug": "synthetic"}]}
    context.context_data = deepcopy(data)
    db.session.commit()
    _process(tenant_id, turn_id)
    assert db.session.get(ChatSessionContext, _turn(turn_id).chat_session_id).context_data == data
    assert len(_attempts(turn_id)) == 2
    assert TicketComentario.query.count() == 0
    assert MunicipioTicket.query.count() == (1 if active == "followup" else 0)
    if active in {"survey", "survey_navigation"}:
        assert "Tu participación sigue en curso" in _bodies(turn_id)[1]
        assert "Iniciar reclamo" not in _bodies(turn_id)[1]
    elif active == "draft":
        assert "Conservo el borrador" in _bodies(turn_id)[1]
        assert "Encuestas" not in _bodies(turn_id)[1]
    assert environment.calls["post"] == []


@pytest.mark.parametrize("body", ("Iniciar reclamo", "Encuestas"))
def test_displayed_text_commands_still_enter_their_native_handlers(environment, body):
    tenant_id, first = _prepare(environment)
    _process(tenant_id, first)
    next_turn = _post(environment, body=body, message_id="wamid.welcome/action+=")
    _process(tenant_id, next_turn)
    output = "\n".join(_bodies(next_turn))
    assert "¡Hola!" not in output
    if body == "Iniciar reclamo":
        context = db.session.get(ChatSessionContext, _turn(next_turn).chat_session_id)
        assert context.context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]["meta_text_draft_v1"] is True
        assert "categoría" in output or "qué está pasando" in output
    else:
        assert "no hay encuestas activas" in output
    assert environment.calls["post"] == []


def test_survey_menu_exit_remains_with_existing_handler(environment):
    tenant_id, turn_id = _prepare(environment, body="menu")
    context = db.session.get(ChatSessionContext, _turn(turn_id).chat_session_id)
    context.context_data = {CONTEXTO_MUNICIPIO: {SURVEY_NAVIGATION_KEY: {"page": 1}}}
    db.session.commit()
    _process(tenant_id, turn_id)
    assert SURVEY_NAVIGATION_KEY not in db.session.get(
        ChatSessionContext, _turn(turn_id).chat_session_id).context_data[CONTEXTO_MUNICIPIO]
    assert "Saliste de la participación" in "\n".join(_bodies(turn_id))


def test_failed_finalization_leaves_no_partial_welcome_or_provider_post(environment, monkeypatch):
    tenant_id, turn_id = _prepare(environment)
    context = db.session.get(ChatSessionContext, _turn(turn_id).chat_session_id)
    before = deepcopy(context.context_data)
    db.session.commit()
    from services import meta_whatsapp_outbox
    original = meta_whatsapp_outbox._stage_on_turn

    def fail_after_staging(*args, **kwargs):
        original(*args, **kwargs)
        assert environment.calls["post"] == []
        raise MetaRuntimeError("synthetic_welcome_commit_unavailable", transient=True)

    monkeypatch.setattr(meta_whatsapp_outbox, "_stage_on_turn", fail_after_staging)
    result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert result["status"] == "retry_wait", result
    assert WhatsAppOutboundAttempt.query.count() == 0
    assert db.session.get(ChatSessionContext, _turn(turn_id).chat_session_id).context_data == before
    assert environment.calls["post"] == [] and environment.calls["twilio"] == 0


@pytest.mark.parametrize("revocation", ("tenant", "connection", "municipal_owner"))
def test_greeting_cannot_bypass_current_sender_or_owner_scope(environment, revocation):
    tenant_id, turn_id = _prepare(environment)
    if revocation == "tenant":
        environment.tenant.is_active = False
    elif revocation == "connection":
        environment.connection.status = "disconnected"
    else:
        environment.owner.tipo_chat = "pyme"
    db.session.commit()
    result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert result["status"] in {"dead", "retry_wait"}, result
    assert _turn(turn_id).status != "completed"
    assert WhatsAppOutboundAttempt.query.count() == 0
    assert environment.calls["post"] == [] and environment.calls["twilio"] == 0
