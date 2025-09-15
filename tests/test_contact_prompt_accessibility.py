import pytest
from services.municipio_responder import ReclamoFlowHandler, ReclamoState
from services.constants import CONTEXTO_MUNICIPIO

@pytest.fixture
def flow_handler():
    """Provides a ReclamoFlowHandler instance with a basic context."""
    context = {
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "reclamo_flow_v2": {
                    "datos_reclamo": {}
                }
            }
        }
    }
    return ReclamoFlowHandler(context, chat_db_context=None)

def test_prompt_includes_all_fields_when_empty(flow_handler):
    """
    Tests that the prompt asks for all required fields when no contact data is present.
    """
    response = flow_handler.ask_for_contact_details(force_prompt=True)
    prompt = response.get("message_body", "").lower()

    assert "nombre" in prompt
    assert "dni" in prompt
    assert "email" in prompt
    assert "teléfono" in prompt

def test_prompt_omits_known_fields(flow_handler):
    """
    Tests that the prompt message shows known fields and asks for missing ones.
    """
    flow_handler.flow_context['datos_reclamo'].update({
        "nombre": "Juan Perez",
        "email": "juan@test.com"
    })

    response = flow_handler.ask_for_contact_details(force_prompt=True)
    prompt = response.get("message_body", "")
    prompt_lower = prompt.lower()

    assert "nombre completo: juan perez" in prompt_lower
    assert "email: juan@test.com" in prompt_lower

    assert "dni" in prompt_lower
    assert "teléfono" in prompt_lower

def test_prompt_moves_to_confirmation_when_full(flow_handler):
    """
    Tests that if all essential data is present, it moves to the confirmation step.
    """
    flow_handler.flow_context['datos_reclamo'].update({
        "nombre": "Juan Perez",
        "email": "juan@test.com",
        "telefono": "2615551234",
        "dni": "30123456",
        "categoria": "Test Category",
        "descripcion": "Test Description",
        "direccion": "Test Address"
    })

    response = flow_handler.ask_for_contact_details()

    assert "confirmá que los datos de tu reclamo son correctos" in response.get("message_body", "").lower()
    assert response.get("message_type") == "interactive_buttons"
    assert any(opt.get("action_id") == "reclamo_confirmar_si" for opt in response.get("options_list", []))

def test_prompt_can_be_forced_when_full(flow_handler):
    """
    Tests that the prompt can be forced even if all data is present.
    """
    flow_handler.flow_context['datos_reclamo'].update({
        "nombre": "Juan Perez",
        "email": "juan@test.com",
        "telefono": "2615551234",
        "dni": "30123456"
    })

    response = flow_handler.ask_for_contact_details(force_prompt=True)
    prompt_lower = response.get("message_body", "").lower()

    assert "nombre completo: juan perez" in prompt_lower
    assert "dni: 30123456" in prompt_lower
    assert "email: juan@test.com" in prompt_lower
    assert "teléfono: 2615551234" in prompt_lower

    assert "datos que querés corregir" in prompt_lower


def test_default_prompt_when_missing_data(flow_handler):
    """When data is missing and not forced, the handler should prompt for it."""
    response = flow_handler.ask_for_contact_details()
    assert flow_handler.flow_context['state'] == ReclamoState.ESPERANDO_DATOS_CONTACTO.name
    body = response.get("message_body", "").lower()
    assert "dni" in body and "email" in body
