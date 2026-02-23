import pytest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from app import create_app
from config import TestConfig
from services.municipio_responder import ReclamoFlowHandler, ReclamoState, responder_municipio
from services.constants import CONTEXTO_MUNICIPIO

@pytest.fixture
def handler_waiting_for_address():
    """Provides a ReclamoFlowHandler instance in the ESPERANDO_DIRECCION state."""
    context = {
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "reclamo_flow_v2": {
                    "state": ReclamoState.ESPERANDO_DIRECCION.name,
                    "datos_reclamo": {
                        "categoria": "Arbolado",
                        "descripcion": "rama caída"
                    }
                }
            }
        }
    }
    # The chat_db_context is mocked as it's not essential for this unit test's logic.
    return ReclamoFlowHandler(context, chat_db_context=MagicMock())

def test_location_payload_completes_claim(handler_waiting_for_address):
    """
    Tests that providing a location payload when the bot is waiting for an
    address correctly saves the address and moves to the next step (asking for a photo).
    """
    location_payload = {
        "es_ubicacion": True,
        "ubicacion_usuario": {
            "latitude": "-33.0",
            "longitude": "-68.5",
            "address": "Sarmiento 100, Junin, Mendoza"
        }
    }

    response = handler_waiting_for_address.handle_direccion("Sarmiento 100", location_payload)

    # Check that the address was correctly saved to the flow's context
    saved_address = handler_waiting_for_address.flow_context["datos_reclamo"].get("direccion")
    assert saved_address == "Sarmiento 100, Junin, Mendoza"

    # Check that the state transitioned to asking for a photo
    assert handler_waiting_for_address.flow_context["state"] == ReclamoState.ESPERANDO_FOTO.name

    # Check that the response message is asking for a photo
    assert "agregar una foto" in response.get("message_body", "").lower()

def test_text_address_moves_to_photo_step(handler_waiting_for_address):
    """
    Tests that providing a simple text address also moves the flow to the photo step.
    """
    # Simulate a simple text message with an address
    text_payload = {} # No special payload

    response = handler_waiting_for_address.handle_direccion("San Martin 550", text_payload)

    saved_address = handler_waiting_for_address.flow_context["datos_reclamo"].get("direccion")
    assert saved_address == "San Martin 550"

    assert handler_waiting_for_address.flow_context["state"] == ReclamoState.ESPERANDO_FOTO.name
    assert "agregar una foto" in response.get("message_body", "").lower()


@pytest.fixture
def app_context():
    app = create_app(TestConfig)
    with app.app_context():
        yield app


def test_responder_municipio_location_continues_flow(app_context):
    """Sending a WhatsApp location while waiting for address should keep the flow active."""

    owner_user = MagicMock()
    owner_user.id = 1
    owner_user.municipio_id = "default"

    contexto = {
        CONTEXTO_MUNICIPIO: {
            "estado_conversacion": "EN_FLUJO_RECLAMO",
            "reclamo_flow_v2": {
                "state": ReclamoState.ESPERANDO_DIRECCION.name,
                "datos_reclamo": {
                    "categoria": "Arreglo de calle",
                    "descripcion": "Bache enorme",
                },
            },
        }
    }

    chat_db_context = SimpleNamespace(context_data=contexto, chat_session_id="session-123")

    location_payload = {
        "latitude": "-33.0",
        "longitude": "-68.5",
        "address": "Don Bosco 55, Junín",
    }

    with patch('services.municipio_responder.flag_modified', lambda *args, **kwargs: None):
        response = responder_municipio(
            pregunta_original="",
            owner_user=owner_user,
            rubro_obj=MagicMock(nombre="municipio"),
            chat_db_context=chat_db_context,
            anon_id="anon+123",
            channel="whatsapp",
            location=location_payload,
            es_ubicacion=True,
            ubicacion_usuario=location_payload,
        )

    message = response.get("message_body", "").lower()
    assert "agregar una foto" in message

    flow_context = chat_db_context.context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]
    assert flow_context["state"] == ReclamoState.ESPERANDO_FOTO.name
    assert "don bosco" in flow_context["datos_reclamo"].get("direccion", "").lower()


def test_location_before_name_does_not_stall_flow(app_context):
    """If a location arrives while waiting for name, the flow should continue with proactive options."""

    owner_user = MagicMock()
    owner_user.id = 1
    owner_user.municipio_id = "default"

    contexto = {
        CONTEXTO_MUNICIPIO: {
            "estado_conversacion": "ESPERANDO_NOMBRE_INICIAL",
        }
    }

    chat_db_context = SimpleNamespace(context_data=contexto, chat_session_id="session-456")

    location_payload = {
        "latitude": "-33.0",
        "longitude": "-68.5",
        "address": "Don Bosco 55, Junín",
    }

    with patch('services.municipio_responder.flag_modified', lambda *args, **kwargs: None):
        response = responder_municipio(
            pregunta_original="",
            owner_user=owner_user,
            rubro_obj=MagicMock(nombre="municipio"),
            chat_db_context=chat_db_context,
            anon_id="anon+location",
            channel="web",
            location=location_payload,
            es_ubicacion=True,
            ubicacion_usuario=location_payload,
        )

    message = response.get("message_body", "").lower()
    assert "ubicación" in message and "qué te gustaría hacer" in message
    assert response.get("fuente") == "proactive_location_handler"

    flow_context = chat_db_context.context_data[CONTEXTO_MUNICIPIO]
    assert flow_context["estado_conversacion"] == "ESPERANDO_INTENCION_UBICACION"
