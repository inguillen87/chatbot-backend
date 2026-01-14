from services.municipio_responder import (
    _extract_expected_fields_from_text,
    _normalize_pedir_info_fields,
    _prefill_contacto_from_context,
)


def test_normalize_pedir_info_fields_splits_commas():
    fields = _normalize_pedir_info_fields("ubicacion, distrito, nombre, dni, email y telefono")
    assert fields == ["ubicacion", "distrito", "nombre", "dni", "email", "telefono"]


def test_prefill_contacto_from_context_removes_known_fields():
    contexto = {"contacto_usuario": {"nombre": "Ana", "email": "ana@example.com"}}
    datos_parciales = {}
    remaining = _prefill_contacto_from_context(contexto, datos_parciales, ["nombre", "email", "telefono"])

    assert datos_parciales == {"nombre": "Ana", "email": "ana@example.com"}
    assert remaining == ["telefono"]


def test_prefill_contacto_from_context_handles_sugerencia_contacto_bundle():
    contexto = {
        "contacto_usuario": {
            "nombre": "Ana",
            "dni": "12345678",
            "email": "ana@example.com",
            "direccion": "Calle 1",
            "telefono": "2615550000",
        }
    }
    datos_parciales = {}
    remaining = _prefill_contacto_from_context(contexto, datos_parciales, ["datos_contacto_sugerencia"])

    assert remaining == []
    assert datos_parciales["direccion"] == "Calle 1"


def test_extract_expected_fields_from_text_finds_contact_and_location():
    raw_text = "Don Bosco 55 esquina Sarmiento, Junín. mail test@ex.com 2615551234"
    expected = ["ubicacion", "distrito", "email", "telefono"]
    context = {}

    extracted = _extract_expected_fields_from_text(raw_text, expected, context, {})

    assert extracted["ubicacion"].startswith("Don Bosco 55")
    assert extracted["distrito"] == "Junín"
    assert extracted["email"] == "test@ex.com"
    assert extracted["telefono"].endswith("2615551234")
