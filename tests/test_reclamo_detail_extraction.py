import pytest
from services.municipio_responder import extract_reclamo_details_from_text


def test_extract_reclamo_details_includes_contact(monkeypatch):
    def fake_contact(text, fields):
        return {
            "email_cliente": "juan@example.com",
            "dni_cliente": "30111222",
            "nombre_cliente": "Juan Perez",
            "telefono_cliente": "+541112223344",
        }

    called = {"complaint": False}

    def fake_complaint(text, **kwargs):
        called["complaint"] = True
        return {}

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
    assert details["descripcion_sugerida"] == "hay un arbol caido"
    assert details["nombre_sugerido"] == "Juan Perez"
    assert details["telefono_sugerido"] == "+541112223344"
    assert details["email_sugerido"] == "juan@example.com"
    assert details["dni_sugerido"] == "30111222"
    # Heuristics should avoid calling the complaint extractor
    assert not called["complaint"]


def test_llm_called_when_keywords_missing(monkeypatch):
    def fake_complaint(text, **kwargs):
        return {
            "tipo_problema": "luminaria",
            "descripcion_problema": "luz quemada",
            "ubicacion_problema": "Sarmiento 100",
        }

    monkeypatch.setattr(
        "services.municipio_responder.extract_complaint_details_llm",
        fake_complaint,
    )
    monkeypatch.setattr(
        "services.municipio_responder.extract_multiple_contact_details_llm",
        lambda text, fields: {},
    )

    # Phrase without known keywords forces LLM usage
    details = extract_reclamo_details_from_text("problema grave", ["Luminaria"])

    assert details["categoria_sugerida"] == "Luminaria"
    assert details["descripcion_sugerida"] == "luz quemada"
    assert details["direccion_sugerida"] == "Sarmiento 100"


def test_intersection_and_district_parsing():
    details = extract_reclamo_details_from_text(
        "Sarmiento 100 esquina San Martin Junin Mendoza",
        ["Arbolado"],
        default_localidad="Junin",
        default_provincia="Mendoza",
    )
    assert details["direccion_sugerida"] == "Sarmiento 100 esquina San Martin"
    assert details["distrito_sugerido"] == "Junin"
    assert details["distrito_dudoso"] == "Mendoza"


def test_detect_barrio_hint():
    details = extract_reclamo_details_from_text(
        "hay ramas caidas en barrio Jardín del centro",
        ["Arbolado"],
        default_localidad="Junin",
    )
    assert details["barrio_sugerido"] == "Jardín del centro"
