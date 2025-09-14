import pytest
from unittest.mock import patch, MagicMock
from services.municipio_responder import ReclamoFlowHandler, ReclamoState
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
