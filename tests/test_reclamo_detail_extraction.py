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

    def fake_complaint(text):
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
    def fake_complaint(text):
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
        "Sarmiento 100 esquina San Martin Junin Mendoza", ["Arbolado"]
    )
    assert details["direccion_sugerida"] == "Sarmiento 100 esquina San Martin"
    assert details["distrito_sugerido"] == "Junin Mendoza"


def test_intersection_without_number_parsing():
    details = extract_reclamo_details_from_text(
        "Don Bosco esquina Sarmiento", ["Arbolado"]
    )
    assert details["direccion_sugerida"] == "Don Bosco esquina Sarmiento"


def test_llm_address_fallback(monkeypatch):
    from services.address_normalizer import normalize_and_geocode
    from services.address_resolver import AddressResolver
    import services.llm_utils as llm_utils

    municipio_cfg = {
        "ciudad": "Junin",
        "provincia": "Mendoza",
        "pais": "AR",
        "bounds": [-68.5, -34.7, -68.3, -34.6],
    }

    calls: list[str] = []

    def fake_resolve(self, text):
        calls.append(text)
        if "Don Bosco 55 esquina Sarmiento" in text:
            return {
                "lat": -34.6,
                "lon": -68.5,
                "formatted": "Don Bosco 55 esquina Sarmiento",
                "display_name": "Don Bosco 55 esquina Sarmiento",
            }
        return None

    monkeypatch.setattr(AddressResolver, "resolve", fake_resolve)

    def fake_llm(system_prompt, user_prompt):
        return {
            "calle": "Don Bosco",
            "numero": "55",
            "interseccion": "Sarmiento",
        }

    monkeypatch.setattr(
        "services.address_normalizer.llamar_llm_para_json_estructurado", fake_llm
    )

    result = normalize_and_geocode(
        "don bosco 55 esquina sarmiento , junin mendoza", municipio_cfg
    )

    assert result["formatted"].startswith("Don Bosco 55 esquina Sarmiento")
    assert calls == [
        "don bosco 55 esquina sarmiento , junin mendoza",
        "Don Bosco 55 esquina Sarmiento",
    ]
