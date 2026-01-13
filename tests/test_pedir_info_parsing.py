import pytest

from services.municipio_responder import (
    _normalize_pedir_info_fields,
    _normalize_pedir_info_value,
    _prefill_contacto_from_context,
)


def test_normalize_pedir_info_fields_splits_commas_and_conjunctions():
    pedir_info = "ubicacion, distrito, nombre, dni, email y telefono"
    assert _normalize_pedir_info_fields(pedir_info) == [
        "ubicacion",
        "distrito",
        "nombre",
        "dni",
        "email",
        "telefono",
    ]
    assert _normalize_pedir_info_value(pedir_info) == "ubicacion"


def test_prefill_contacto_from_context_removes_known_fields():
    contexto = {
        "contacto_usuario": {
            "nombre": "Ana Perez",
            "email": "ana@example.com",
            "direccion": "Calle 123",
        }
    }
    datos_parciales = {}
    remaining = _prefill_contacto_from_context(
        contexto,
        datos_parciales,
        ["nombre", "email", "ubicacion", "telefono"],
    )

    assert datos_parciales["nombre"] == "Ana Perez"
    assert datos_parciales["email"] == "ana@example.com"
    assert datos_parciales["ubicacion"] == "Calle 123"
    assert remaining == ["telefono"]
