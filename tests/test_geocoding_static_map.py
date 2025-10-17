from unittest.mock import patch

import pytest

from services.herramientas_municipio import validar_y_formatear_direccion


@pytest.fixture
def fake_resolver():
    with patch("services.herramientas_municipio.AddressResolver") as mock_cls:
        instance = mock_cls.return_value
        instance._within_bounds.return_value = True
        yield instance


def _mock_resolution(lat=-33.0, lon=-68.5):
    return {
        "formatted": "Av. Siempreviva 742, Junín, Mendoza, AR",
        "lat": lat,
        "lon": lon,
        "calle": "Av. Siempreviva",
        "numero": "742",
        "entre_calles": [],
        "barrio": None,
        "localidad": "Junín",
        "provincia": "Mendoza",
        "pais": "AR",
        "precision": "point",
        "validez": True,
    }


def test_validar_y_formatear_direccion_usa_static_map_de_google(fake_resolver, monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "TEST_KEY")
    fake_resolver.resolve.return_value = _mock_resolution()

    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")

    assert resultado["static_map_url"] == (
        "https://maps.googleapis.com/maps/api/staticmap?center="
        "-33,-68.5&zoom=18&size=800x500&markers=color:red|-33,-68.5&key=TEST_KEY"
    )
    assert resultado["maps_link"] == "https://www.google.com/maps?q=-33.0,-68.5"


def test_validar_y_formatear_direccion_usa_fallback_openstreetmap(fake_resolver, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("STATIC_MAP_FALLBACK_TEMPLATE", raising=False)
    fake_resolver.resolve.return_value = _mock_resolution()

    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")

    assert resultado["static_map_url"].startswith(
        "https://staticmap.openstreetmap.de/staticmap.php?"
    )
    assert "center=-33,-68.5" in resultado["static_map_url"]


def test_validar_y_formatear_direccion_respetar_plantilla_personalizada(fake_resolver, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.setenv(
        "STATIC_MAP_FALLBACK_TEMPLATE",
        "https://cdn.example/maps/{lat}/{lon}/preview.png",
    )
    fake_resolver.resolve.return_value = _mock_resolution()

    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")

    assert resultado["static_map_url"] == "https://cdn.example/maps/-33/-68.5/preview.png"
