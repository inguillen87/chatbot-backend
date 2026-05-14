import services.municipio_responder as municipio_responder
from services.municipio_responder import CONTEXTO_MUNICIPIO, ReclamoFlowHandler, ReclamoState


def test_dni_response_does_not_overwrite_claim_address_or_name():
    context = {
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "reclamo_flow_v2": {
                    "state": ReclamoState.ESPERANDO_DATOS_CONTACTO.name,
                    "datos_reclamo": {
                        "categoria": "Luminaria",
                        "direccion": "Av San Martin 123",
                        "descripcion": "luminaria apagada",
                        "descripcion_resumida": "Luminaria apagada",
                        "nombre": "QA Texto",
                        "email": "qa.texto@example.com",
                        "telefono": "+5492615550000",
                        "foto_url": "https://example.com/foto.png",
                    },
                }
            }
        }
    }

    handler = ReclamoFlowHandler(context, None)
    response = handler.handle_datos_contacto("Mi DNI es 30111222.")
    datos = context["chat_db_context_data"][CONTEXTO_MUNICIPIO]["reclamo_flow_v2"]["datos_reclamo"]

    assert datos["direccion"] == "Av San Martin 123"
    assert datos["nombre"] == "QA Texto"
    assert datos["dni"] == "30111222"
    assert "Av San Martin 123" in response["message_body"]


def test_reclamo_text_extracts_clean_address_and_description(monkeypatch):
    monkeypatch.setattr(municipio_responder, "extract_multiple_contact_details_llm", lambda *args, **kwargs: {})
    monkeypatch.setattr(municipio_responder, "extract_complaint_details_llm", lambda *args, **kwargs: {})

    details = municipio_responder.extract_reclamo_details_from_text(
        "Hola, soy QA Texto, email qa.texto@example.com, telefono +549261551234. "
        "Quiero registrar un reclamo por luminaria apagada en Av San Martin 123, "
        "distrito Centro, Junin.",
        [{"texto": "Luminaria"}, {"texto": "Semaforo"}],
    )

    assert details["direccion"] == "Av San Martin 123"
    assert details["descripcion"] == "luminaria apagada"
    assert details["distrito"] == "Centro"
