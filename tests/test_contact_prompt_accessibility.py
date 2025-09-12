from services.municipio_responder import pedir_datos_contacto_compacto


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
