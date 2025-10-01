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
    assert details["categoria"] == "Arbolado"
    assert details["descripcion_sugerida"] == "hay un arbol caido"
    assert details["descripcion"] == "hay un arbol caido"
    assert details["nombre_sugerido"] == "Juan Perez"
    assert details["nombre"] == "Juan Perez"
    assert details["telefono_sugerido"] == "+541112223344"
    assert details["telefono"] == "+541112223344"
    assert details["email_sugerido"] == "juan@example.com"
    assert details["email"] == "juan@example.com"
    assert details["dni_sugerido"] == "30111222"
    assert details["dni"] == "30111222"
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
    assert details["categoria"] == "Luminaria"
    assert details["descripcion_sugerida"] == "luz quemada"
    assert details["descripcion"] == "luz quemada"
    assert details["direccion_sugerida"] == "Sarmiento 100"
    assert details["direccion"] == "Sarmiento 100"


def test_intersection_and_district_parsing():
    details = extract_reclamo_details_from_text(
        "Sarmiento 100 esquina San Martin Junin Mendoza",
        ["Arbolado"],
        default_localidad="Junin",
        default_provincia="Mendoza",
    )
    assert details["direccion_sugerida"] == "Sarmiento 100 esquina San Martin"
    assert details["direccion"] == "Sarmiento 100 esquina San Martin"
    assert details["distrito_sugerido"] == "Junin"
    assert details["distrito"] == "Junin"
    assert details["distrito_dudoso"] == "Mendoza"


def test_intersection_with_city_connector_is_trimmed():
    details = extract_reclamo_details_from_text(
        "Mi dirección es en Don Bosco 55 esquina Sarmiento de Junín",
        ["Luminaria"],
        default_localidad="Junín",
        default_provincia="Buenos Aires",
    )

    assert details["direccion_sugerida"] == "Don Bosco 55 esquina Sarmiento"
    assert details["direccion"] == "Don Bosco 55 esquina Sarmiento"
    assert details.get("distrito_sugerido") == "Junín"


def test_intersection_with_repeated_keyword_keeps_primary_street():
    details = extract_reclamo_details_from_text(
        "Mi dirección es en Don Bosco 55 esquina esquina Sarmiento de Junín",
        ["Luminaria"],
        default_localidad="Junín",
    )

    assert details["direccion_sugerida"] == "Don Bosco 55 esquina Sarmiento"
    assert details["direccion"] == "Don Bosco 55 esquina Sarmiento"


def test_detect_barrio_hint():
    details = extract_reclamo_details_from_text(
        "hay ramas caidas en barrio Jardín del centro",
        ["Arbolado"],
        default_localidad="Junin",
    )
    assert details["barrio_sugerido"] == "Jardín del centro"
    assert details["barrio"] == "Jardín del centro"


def test_free_text_complaint_does_not_guess_contact_or_address(monkeypatch):
    monkeypatch.setattr(
        "services.municipio_responder.extract_multiple_contact_details_llm",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "services.municipio_responder.extract_complaint_details_llm",
        lambda *args, **kwargs: {},
    )

    message = (
        "quiero pedir que corten las ramas de los arboles del barrio jardin en el centro de junin "
        "esta tapando y cruzando la medianera me ensucia toda la pileta"
    )
    details = extract_reclamo_details_from_text(message, ["Arbolado"])

    assert details["categoria_sugerida"] == "Arbolado"
    assert details["descripcion_sugerida"].startswith("corten las ramas de los arboles")
    assert "direccion" not in details
    assert "direccion_sugerida" not in details
    assert "nombre" not in details
    assert "nombre_sugerido" not in details
    assert details.get("barrio_sugerido") == "jardin en el centro de junin"
