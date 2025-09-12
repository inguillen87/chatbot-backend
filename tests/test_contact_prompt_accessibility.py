from unittest.mock import MagicMock

from services.municipio_responder import (
    pedir_datos_contacto_compacto,
    ReclamoFlowHandler,
    CONTEXTO_MUNICIPIO,
)


def test_contact_prompt_is_accessible():
    msg = pedir_datos_contacto_compacto()
    body = msg["message_body"]
    assert "*Nombre y apellido*" in body
    assert "*DNI*" in body
    assert "*Teléfono*" in body
    assert "*Email*" in body
    # Ensure bullet formatting and guidance text
    assert body.count("•") >= 4
    assert "Podés mandarlos en una sola línea" in body


def test_contact_prompt_only_missing_fields():
    msg = pedir_datos_contacto_compacto(["nombre", "telefono"])
    body = msg["message_body"]
    assert "*Nombre y apellido*" in body
    assert "*Teléfono*" in body
    assert "*DNI*" not in body
    assert "*Email*" not in body
    assert body.count("•") == 2


def _build_handler_with_contact():
    flow_context = {
        "datos_reclamo": {
            "categoria": "Bache",
            "direccion": "Calle 123",
            "descripcion": "pozo",
            "nombre": "Juan",
            "dni": "123",
            "telefono": "+5400000000",
            "email": "juan@example.com",
        }
    }
    context = {"chat_db_context_data": {CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": flow_context}}}
    return ReclamoFlowHandler(context, MagicMock())


def test_contact_summary_includes_repeat_and_help():
    handler = _build_handler_with_contact()
    msg = handler.ask_for_contact_details()
    options = [opt["texto"] for opt in msg.get("options_list", [])]
    assert "4. Repetir" in options
    assert "5. Ayuda" in options
