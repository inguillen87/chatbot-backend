import types
import pytest
from services.municipio_responder import (
    extract_ticket_number,
    extract_pin,
    is_greeting,
    responder_municipio,
    CONTEXTO_MUNICIPIO,
)
from models import ChatSessionContext
from app import db


def run_turn(message, state=None, numero=None, owner_user=None):
    existing = ChatSessionContext.query.get("ticket_session")
    if existing:
        db.session.delete(existing)
        db.session.commit()
    ctx = ChatSessionContext(chat_session_id="ticket_session")
    ctx.context_data = {}
    muni = ctx.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
    muni["estado_conversacion"] = state or "ESPERANDO_SELECCION_MENU_PRINCIPAL"
    if numero:
        muni["numero_ticket_consulta"] = numero
    db.session.add(ctx)
    db.session.commit()
    resp = responder_municipio(
        pregunta_original=message,
        owner_user=owner_user,
        viewer_user=None,
        rubro_obj=owner_user.rubro,
        chat_db_context=ctx,
        anon_id="anon",
        channel="whatsapp",
    )
    db.session.commit()
    ctx_after = ChatSessionContext.query.get("ticket_session").context_data[CONTEXTO_MUNICIPIO]
    return types.SimpleNamespace(response=resp, ctx=ctx_after)


def test_parse_ticket_variants():
    assert extract_ticket_number("397871") == "397871"
    assert extract_ticket_number("M-397871") == "397871"
    assert extract_ticket_number("m 397871") == "397871"
    assert extract_ticket_number("ticket 397871") == "397871"


def test_parse_pin_variants():
    assert extract_pin("734774") == "734774"
    assert extract_pin("PIN: 734774") == "734774"


def test_no_greeting_on_numbers():
    assert not is_greeting("734774", "ESPERANDO_PIN_TICKET")


def test_consulta_flow_direct_number(owner_user):
    result = run_turn("397871", owner_user=owner_user)
    assert result.ctx["estado_conversacion"] == "ESPERANDO_PIN_TICKET"
    assert result.ctx["numero_ticket_consulta"] == "397871"
    assert "PIN de 6 dígitos" in result.response["message_body"]


def test_ticket_summary_has_links(monkeypatch, owner_user):
    class Ticket:
        numero = "397871"
        categoria = "Arbol Caido"
        descripcion = "Árbol caído en mi zona"
        estado_actual = "nuevo"

    def fake_get(numero, pin):
        return Ticket()

    monkeypatch.setattr("services.municipio_responder.api_ticket_get", fake_get)
    result = run_turn("734774", state="ESPERANDO_PIN_TICKET", numero="397871", owner_user=owner_user)
    body = result.response["message_body"]
    assert "Junín Punto Limpio" in body
    assert "Ver mi Ticket" in body
