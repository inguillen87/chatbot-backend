import pytest
from services.municipio_responder import extract_reclamo_details_from_text


def test_extract_reclamo_details_includes_contact(monkeypatch):
    def fake_complaint(text):
        return {
            "tipo_problema": "arbol caido",
            "descripcion_problema": "arbol caido en calle",
            "ubicacion_problema": "Sarmiento 100",
            "distrito_problema": "Centro",
            "nombre_usuario": "Juan Perez",
            "telefono_usuario": "+541112223344",
        }

    def fake_contact(text, fields):
        return {
            "email_cliente": "juan@example.com",
            "dni_cliente": "30111222",
        }

    monkeypatch.setattr(
        "services.municipio_responder.extract_complaint_details_llm",
        fake_complaint,
    )
    monkeypatch.setattr(
        "services.municipio_responder.extract_multiple_contact_details_llm",
        fake_contact,
    )

    details = extract_reclamo_details_from_text("hay un arbol caido", ["Arbolado"])

    assert details["categoria_sugerida"] == "Arbolado"
    assert details["descripcion_sugerida"] == "arbol caido en calle"
    assert details["direccion_sugerida"] == "Sarmiento 100"
    assert details["distrito_sugerido"] == "Centro"
    assert details["nombre_sugerido"] == "Juan Perez"
    assert details["telefono_sugerido"] == "+541112223344"
    assert details["email_sugerido"] == "juan@example.com"
    assert details["dni_sugerido"] == "30111222"
