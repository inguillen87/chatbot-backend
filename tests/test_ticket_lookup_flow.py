import types
import pytest
from unittest.mock import MagicMock
from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO
from models import ChatSessionContext, MunicipioTicket
from app import db

@pytest.fixture
def owner_user(app):
    """Provides a mock owner_user with a rubro object."""
    user = MagicMock()
    user.municipio_id = "1"
    user.rubro.nombre = "municipio"
    return user

def run_turn(message, state=None, numero=None, owner_user=None):
    """Helper function to simulate a turn in the conversation for ticket lookup."""
    session_id = "ticket_session"
    ctx = ChatSessionContext.query.get(session_id)
    if not ctx:
        ctx = ChatSessionContext(chat_session_id=session_id, context_data={})
        db.session.add(ctx)

    muni_context = ctx.context_data.setdefault(CONTEXTO_MUNICIPIO, {})
    if state:
        muni_context["estado_conversacion"] = state
    if numero:
        muni_context["numero_ticket_consulta"] = numero

    db.session.commit()

    resp = responder_municipio(
        pregunta_original=message,
        owner_user=owner_user,
        viewer_user=None,
        rubro_obj=owner_user.rubro,
        chat_db_context=ctx,
        anon_id="anon_test",
        channel="whatsapp",
    )
    db.session.commit()

    ctx_after = ChatSessionContext.query.get(session_id).context_data[CONTEXTO_MUNICIPIO]
    return types.SimpleNamespace(response=resp, ctx=ctx_after)

def test_consulta_flow_direct_number(owner_user, app):
    """
    Tests that providing a ticket number directly triggers the flow to ask for the PIN.
    """
    with app.app_context():
        result = run_turn("397871", owner_user=owner_user)
        assert result.ctx["estado_conversacion"] == "ESPERANDO_NUMERO_TICKET"
        assert result.ctx["numero_ticket_consulta"] == "397871"
        assert "PIN de 6 dígitos" in result.response["message_body"]

def test_ticket_summary_has_basic_links(monkeypatch, owner_user, app):
    """
    Tests that a valid ticket and PIN lookup returns a summary with a "Ver Ticket" link.
    """
    class MockTicket:
        nro_ticket = "M-397871"
        consulta_pin = "734774"
        categoria = "Arbol Caido"
        detalles = "Árbol caído en mi zona"
        pregunta = ""
        estado = "nuevo"
        nombre_vecino = "Test User"

    def fake_query(*args, **kwargs):
        # This will be the query for the ticket
        return MagicMock(first=MagicMock(return_value=MockTicket()))

    with app.app_context():
        monkeypatch.setattr(MunicipioTicket.query, "filter_by", fake_query)
        # The state should be ESPERANDO_NUMERO_TICKET and the user sends the PIN
        result = run_turn("734774", state="ESPERANDO_NUMERO_TICKET", numero="M-397871", owner_user=owner_user)

        body = result.response["message_body"]
        assert "Estado actual: nuevo" in body

        options = result.response.get("options_list", [])
        assert any("Ver Ticket" in opt.get("texto", "") for opt in options)

def test_invalid_ticket_pin(monkeypatch, owner_user, app):
    """
    Tests that an invalid ticket/PIN combination returns an error message.
    """
    def fake_query(*args, **kwargs):
        return MagicMock(first=MagicMock(return_value=None))

    with app.app_context():
        monkeypatch.setattr(MunicipioTicket.query, "filter_by", fake_query)
        # The user sends an invalid PIN
        result = run_turn("111111", state="ESPERANDO_NUMERO_TICKET", numero="111111", owner_user=owner_user)
        assert "No encontramos un ticket" in result.response["message_body"]
        # The state should reset to allow the user to try again or do something else.
        assert result.ctx.get("estado_conversacion") is None
