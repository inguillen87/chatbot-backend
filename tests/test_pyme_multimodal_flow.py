import uuid
from unittest.mock import patch

import pytest

from models import CatalogoItem, ChatSessionContext, Rubro, User, db
from flask import current_app
from services.pymes import responder_pyme, _build_pyme_order_success_payload


@pytest.fixture
def pyme_owner(app, client):
    with app.app_context():
        rubro = Rubro(clave="bodega", nombre="Bodega")
        db.session.add(rubro)
        db.session.commit()

        owner = User(
            name="Bodega Cuatro Fincas",
            email="owner@bodega.test",
            rol="admin",
            tipo_chat="pyme",
            rubro=rubro,
            nombre_empresa="Bodega Cuatro Fincas",
        )
        owner.set_password("secret")
        db.session.add(owner)
        db.session.commit()

        catalog_item = CatalogoItem(
            user_id=owner.id,
            nombre="Malbec Reserva 2022",
            descripcion="Vino tinto",
            precio="43200",
            cantidad="Caja x6",
            sku="CF-MALBEC-RSV",
        )
        db.session.add(catalog_item)
        db.session.commit()

        yield owner


def _create_session(owner_id: int) -> ChatSessionContext:
    session = ChatSessionContext(
        chat_session_id=str(uuid.uuid4()),
        user_id=owner_id,
        anon_id="anon-123",
        context_data={},
    )
    db.session.add(session)
    db.session.commit()
    return session


def test_pyme_smoke_text_to_quote_to_order(app, pyme_owner):
    with app.app_context():
        session = _create_session(pyme_owner.id)

        first = responder_pyme(
            "Quiero 2 cajas de Malbec Reserva",
            pyme_owner,
            pyme_owner.rubro,
            chat_db_context=session,
            anon_id="anon-123",
            channel="whatsapp",
        )
        assert "Malbec" in first["message_body"]
        assert first["fuente"] == "pyme_item_agregado"

        quote = responder_pyme(
            "armar presupuesto",
            pyme_owner,
            pyme_owner.rubro,
            chat_db_context=session,
            anon_id="anon-123",
            channel="whatsapp",
        )
        assert "Resumen de tu carrito" in quote["message_body"]
        assert quote["fuente"] == "pyme_presupuesto_resumen"

        confirm = responder_pyme(
            "confirmar pedido",
            pyme_owner,
            pyme_owner.rubro,
            chat_db_context=session,
            anon_id="anon-123",
            channel="whatsapp",
        )
        assert confirm["fuente"] == "pyme_pedido_confirmado"
        assert confirm["data"]["nro_pedido"]
        assert confirm["data"]["monto_total"] > 0
        assert any(
            opt.get("action_id") == "pyme_hacer_pedido" for opt in confirm.get("options_list", [])
        )
        assert confirm.get("delayed_payload")


def test_pyme_image_to_catalog_match(app, pyme_owner):
    with app.app_context():
        session = _create_session(pyme_owner.id)

        fake_result = {"items": [{"sku": "CF-MALBEC-RSV", "cantidad": 1}]}

        with patch("services.pyme_multimodal.analizar_imagen_con_fallback", return_value=fake_result):
            response = responder_pyme(
                {
                    "pregunta": "",
                    "uploaded_file_info": {
                        "url": "https://example.com/malbec.jpg",
                        "mime_type": "image/jpeg",
                    },
                },
                pyme_owner,
                pyme_owner.rubro,
                chat_db_context=session,
                anon_id="anon-456",
                channel="whatsapp",
            )

        assert response["fuente"] == "pyme_imagen_items_agregados"
        assert "Resumen de tu carrito" in response["message_body"]


def test_pyme_pdf_to_catalog_match(app, pyme_owner):
    with app.app_context():
        session = _create_session(pyme_owner.id)

        processing_payload = {
            "success": True,
            "extracted_data": {
                "texto_extraido": "Malbec Reserva 2022 - 3 cajas",
            },
        }

        with patch(
            "services.pyme_multimodal.document_processing_service.process_document_by_id",
            return_value=processing_payload,
        ) as mock_process:
            response = responder_pyme(
                {
                    "pregunta": "",
                    "uploaded_file_info": {
                        "mime_type": "application/pdf",
                        "id": 77,
                        "url": "https://example.com/lista-precios.pdf",
                    },
                },
                pyme_owner,
                pyme_owner.rubro,
                chat_db_context=session,
                anon_id="anon-654",
                channel="whatsapp",
            )

        mock_process.assert_called_once_with(77)
        assert response["fuente"] == "pyme_pdf_items_agregados"
        assert "Resumen de tu carrito" in response["message_body"]


def test_pyme_audio_to_intent_delivery(app, pyme_owner):
    with app.app_context():
        session = _create_session(pyme_owner.id)
        with patch("services.llm_orchestrator.llamar_llm_con_fallback", return_value=({}, None)):
            response = responder_pyme(
                {
                    "pregunta": "Necesito delivery para Godoy Cruz",
                    "uploaded_file_info": {
                        "mime_type": "audio/ogg",
                        "transcribed_text": "Necesito delivery para Godoy Cruz",
                    },
                },
                pyme_owner,
                pyme_owner.rubro,
                chat_db_context=session,
                anon_id="anon-789",
                channel="whatsapp",
            )
        assert response["fuente"] == "pyme_delivery_solicitud_ubicacion"
        assert any(opt["action_id"] == "enviar_ubicacion" for opt in response["options_list"])


def test_pyme_location_to_shipping_estimate(app, pyme_owner):
    with app.app_context():
        session = _create_session(pyme_owner.id)
        response = responder_pyme(
            "Aquí está mi ubicación",
            pyme_owner,
            pyme_owner.rubro,
            chat_db_context=session,
            anon_id="anon-321",
            channel="whatsapp",
            ubicacion_usuario={"lat": -34.60, "lon": -58.38, "address": "CABA"},
        )
        assert response["fuente"] == "pyme_delivery_estimate"
        assert "costo de envío" in response["message_body"].lower()
        assert response["data"]["delivery"]["shipping_total"] > 0


def test_build_pyme_order_success_payload_merges_buttons(app):
    with app.app_context():
        current_app.config["PYME_PEDIDOS_PUBLIC_URL"] = "https://ventas.test/pedidos"
        context = {
            "channel": "whatsapp",
            "rubro_nombre": "Bodega",
            "viewer_user_obj": type("Viewer", (), {"name": "Marcelo"})(),
        }
        handler_response = {
            "success": True,
            "fuente": "pyme_pedido_registrado",
            "message_body": "Resumen de tu carrito",
            "options_list": [
                {"texto": "Ver catálogo", "action_id": "pyme_productos_stock"},
                {"texto": "💬 Ver mi Ticket", "url": "https://ventas.test/pedidos/123"},
            ],
            "data": {
                "nro_pedido": "PED-123456",
                "pedido_id": 77,
                "monto_total": 25000,
                "consulta_pin": "707165",
                "cart_summary": {
                    "items_detalle": [
                        {"nombre_producto": "Malbec Reserva", "cantidad": 2},
                    ],
                    "total_final_con_descuento": 25000,
                },
                "cliente": {"nombre": "Marcelo"},
                "order_summary_text": "Resumen de tu carrito",
            },
        }

        with patch(
            "services.pymes.promo_service.build_ticket_promo_section",
            return_value={
                "message_body": "♻️ Recordá sumarte al Punto Limpio.",
                "button": {"texto": "♻️ Punto Limpio", "url": "https://junin.test/punto"},
            },
        ), patch(
            "services.pymes.get_pyme_menu_payload",
            return_value={
                "message_body": "Menú de la bodega",
                "options_list": [{"texto": "Menú principal", "action_id": "menu_principal"}],
            },
        ):
            payload = _build_pyme_order_success_payload(context, handler_response)

    assert payload["fuente"] == "pyme_pedido_confirmado"
    assert "Pedido" in payload["message_body"]
    assert any(btn.get("action_id") == "pyme_hacer_pedido" for btn in payload["options_list"])
    has_url_button = any(btn.get("type") == "url" for btn in payload["options_list"])
    expected_link = "https://ventas.test/pedidos/PED-123456?pin=707165"
    assert has_url_button or expected_link in payload["message_body"]
    assert payload["delayed_payload"]["message_body"] == "Menú de la bodega"
