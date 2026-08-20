import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

from app import db
from models import TenantProfile, User
from services.municipio_responder import responder_municipio


def test_reclamo_handler_categoria_buttons(client):
    with patch("services.municipio_responder.find_global_menu_action", return_value=None):
        with patch("services.municipio_responder.llamar_llm_con_fallback"):
            owner_user = MagicMock()
            owner_user.id = 1
            chat_db_context = MagicMock()
            chat_db_context.context_data = {}

            response = responder_municipio(
                pregunta_original="quiero hacer un reclamo",
                owner_user=owner_user,
                rubro_obj=None,
                viewer_user=None,
                chat_db_context=chat_db_context,
                anon_id="test_anon_id",
                channel="whatsapp",
            )

    assert response is not None
    from services.response_formatter import build_interactive_response

    formatted_response = build_interactive_response(
        options=response.get("options_list", []),
        body_text=response.get("message_body"),
        channel="whatsapp",
        message_type="text",
        original_bot_response=response,
    )
    body = formatted_response["text"]["body"]
    assert "Eleg" in body
    assert "Luminaria" in body
    assert "Arreglo de calle" in body


def test_reclamo_handler_share_location_button(client):
    with patch("services.municipio_responder.find_global_menu_action", return_value=None):
        with patch(
            "services.municipio_responder.llamar_gemini",
            return_value=(
                {
                    "message_body": "Reclamo por *Luminaria*. ¿Cuál es la dirección exacta?",
                    "accion_backend": "crear_reclamo",
                    "datos_estructura": {
                        "target": "municipio",
                        "categoria": "Luminaria",
                        "descripcion": "Poste de luz roto",
                    },
                    "pedir_info": "ubicacion",
                    "botones": [],
                },
                {},
            ),
        ):
            owner_user = User(
                email="whatsapp-location-owner@example.com",
                name="WhatsApp location owner",
                rol="admin",
                tipo_chat="municipio",
            )
            owner_user.set_password("whatsapp-location-test-only")
            db.session.add(owner_user)
            db.session.flush()
            tenant = TenantProfile(
                slug="whatsapp-location-owner",
                nombre="WhatsApp location owner",
                tipo="municipio",
                municipio_id=owner_user.id,
            )
            db.session.add(tenant)
            db.session.flush()
            owner_user.tenant_id = tenant.id
            owner_user.tenant_slug = tenant.slug
            db.session.commit()
            chat_db_context = MagicMock()
            chat_db_context.context_data = {}

            response = responder_municipio(
                pregunta_original="Poste de luz roto",
                owner_user=owner_user,
                rubro_obj=None,
                viewer_user=None,
                chat_db_context=chat_db_context,
                anon_id="test_anon_id",
                channel="whatsapp",
            )

    assert response is not None
    from services.response_formatter import build_interactive_response

    formatted_response = build_interactive_response(
        options=response.get("options_list", []),
        body_text=response.get("message_body"),
        channel="whatsapp",
        message_type="text",
        original_bot_response=response,
    )
    body = formatted_response["text"]["body"]
    assert "Reclamo por *Luminaria*" in body
    assert "direcci" in body


def test_ticket_status_handler_ticket_number_shortcut(client):
    with patch("services.municipio_responder.find_global_menu_action", return_value=None):
        with patch("services.municipio_responder.llamar_llm_con_fallback"):
            owner_user = MagicMock()
            owner_user.id = 1
            chat_db_context = MagicMock()
            chat_db_context.context_data = {}

            response = responder_municipio(
                pregunta_original="quiero saber el estado de mi ticket 12345",
                owner_user=owner_user,
                rubro_obj=None,
                viewer_user=None,
                chat_db_context=chat_db_context,
                anon_id="test_anon_id",
                channel="whatsapp",
            )

    assert response is not None
    assert "PIN" in response["message_body"]
    assert chat_db_context.context_data["contexto_municipio_v2"]["numero_ticket_consulta"] == "12345"


if __name__ == "__main__":
    unittest.main()
