import types
import pytest
from unittest.mock import MagicMock
from sqlalchemy.orm.attributes import flag_modified
from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO
from models import ChatSessionContext, MunicipioTicket
from app import db

@pytest.fixture
def owner_user(client):
    """Provides a mock owner_user with a rubro object."""
    user = MagicMock()
    user.id = 1
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

    context_data = dict(ctx.context_data or {})
    muni_context = dict(context_data.get(CONTEXTO_MUNICIPIO) or {})
    if state:
        muni_context["estado_conversacion"] = state
    else:
        muni_context.pop("estado_conversacion", None)
    if numero:
        muni_context["numero_ticket_consulta"] = numero
    else:
        muni_context.pop("numero_ticket_consulta", None)

    context_data[CONTEXTO_MUNICIPIO] = muni_context
    ctx.context_data = context_data
    flag_modified(ctx, "context_data")

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

def test_ticket_summary_has_basic_links(owner_user, app):
    """
    Tests that a valid ticket and PIN lookup returns a summary with a "Ver Ticket" link.
    """
    with app.app_context():
        db.session.add(
            MunicipioTicket(
                nro_ticket="397871",
                consulta_pin="734774",
                categoria="Arbol Caido",
                detalles="Arbol caido en mi zona",
                pregunta="",
                estado="nuevo",
                nombre_vecino="Test User",
                municipio_id=1,
            )
        )
        db.session.commit()
        # The state should be ESPERANDO_NUMERO_TICKET and the user sends the PIN
        result = run_turn("734774", state="ESPERANDO_NUMERO_TICKET", numero="397871", owner_user=owner_user)

        body = result.response["message_body"]
        assert "Estado actual:" in body
        assert "nuevo" in body

        options = result.response.get("options_list", [])
        assert any("Ticket" in opt.get("texto", "") for opt in options)

def test_invalid_ticket_pin(owner_user, app):
    """
    Tests that an invalid ticket/PIN combination returns an error message.
    """
    with app.app_context():
        # The user sends an invalid PIN
        result = run_turn("111111", state="ESPERANDO_NUMERO_TICKET", numero="111111", owner_user=owner_user)
        assert "No encontramos un ticket" in result.response["message_body"]
        # The state should reset to allow the user to try again or do something else.
        assert result.ctx.get("estado_conversacion") is None
