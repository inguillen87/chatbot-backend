from services.address_resolver import AddressResolver
from unittest.mock import patch

def _fake_resp(lat, lon, display="Stub"):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"lat": str(lat), "lon": str(lon), "display_name": display}]

    return FakeResponse()


JUNIN_CONFIG = {
    "ciudad": "Junín",
    "provincia": "Mendoza",
    "pais": "AR",
    "bounds": (-68.6, -33.1, -68.4, -32.9),
    "conflicting_jurisdicciones": ["san martin"],
}


def test_intersection_sarmiento_san_martin():
    resolver = AddressResolver(JUNIN_CONFIG)
    with patch(
        "services.address_resolver.requests.get", return_value=_fake_resp(-33.0, -68.5)
    ):
        result = resolver.resolve("Sarmiento 100 esquina San Martín")
    assert result["precision"] == "intersection"
    assert result["entre_calles"] == ["Sarmiento", "San Martin"]
    assert result["localidad"] == "Junín"
    assert result["validez"] is True
    assert "Sarmiento" in result["formatted"] and "San Martin" in result["formatted"]


def test_intersection_with_e_connector():
    resolver = AddressResolver(JUNIN_CONFIG)
    with patch(
        "services.address_resolver.requests.get", return_value=_fake_resp(-33.0, -68.5)
    ):
        result = resolver.resolve("Don Bosco e Sarmiento")
    assert result["precision"] == "intersection"
    assert result["entre_calles"] == ["Don Bosco", "Sarmiento"]


def test_dynamic_municipio_config():
    config = {
        "ciudad": "Ciudad X",
        "provincia": "Provincia Y",
        "pais": "AR",
        "bounds": (-1.0, -1.0, 1.0, 1.0),
    }
    resolver = AddressResolver(config)
    with patch(
        "services.address_resolver.requests.get", return_value=_fake_resp(0.0, 0.0)
    ):
        result = resolver.resolve("Falsa 123")
    assert result["localidad"] == "Ciudad X"
    assert result["provincia"] == "Provincia Y"


def test_numeric_street_name():
    resolver = AddressResolver(JUNIN_CONFIG)
    with patch(
        "services.address_resolver.requests.get", return_value=_fake_resp(-33.0, -68.5)
    ):
        result = resolver.resolve("9 de julio 120")
    assert result["calle"] == "9 De Julio"
    assert result["numero"] == "120"


def test_resolver_includes_maps_search_url():
    resolver = AddressResolver(JUNIN_CONFIG)
    with patch(
        "services.address_resolver.requests.get", return_value=_fake_resp(-33.0, -68.5)
    ):
        result = resolver.resolve("Sarmiento 100 esquina San Martín")
    assert result["maps_search_url"].startswith("https://www.google.com/maps/search/")
