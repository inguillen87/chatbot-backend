from services.address_resolver import AddressResolver
from unittest.mock import patch
from services.municipio_responder import handle_direccion

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


def test_resolver_handles_city_suffix():
    resolver = AddressResolver(JUNIN_CONFIG)
    with patch(
        "services.address_resolver.requests.get", return_value=_fake_resp(-33.0, -68.5)
    ):
        result = resolver.resolve("Don Bosco 55 Junin")
    assert result["calle"] == "Don Bosco"
    assert result["numero"] == "55"


def test_resolver_includes_maps_search_url():
    resolver = AddressResolver(JUNIN_CONFIG)
    with patch(
        "services.address_resolver.requests.get", return_value=_fake_resp(-33.0, -68.5)
    ):
        result = resolver.resolve("Sarmiento 100 esquina San Martín")
    assert result["maps_search_url"].startswith("https://maps.google.com/?q=")


def test_resolver_ignores_na_input():
    resolver = AddressResolver(JUNIN_CONFIG)
    assert resolver.resolve("N/A") is None


def test_resolver_skips_bounds_when_disabled():
    cfg = dict(JUNIN_CONFIG)
    resolver = AddressResolver(cfg, enforce_bounds=False)
    with patch(
        "services.address_resolver.requests.get", return_value=_fake_resp(-34.5, -69.2)
    ):
        result = resolver.resolve("Fuera 1")
    assert result["validez"] is True


def test_ambiguous_address_candidates():
    class MultiResp:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"lat": "-33.0", "lon": "-68.5", "display_name": "Opcion A"},
                {"lat": "-33.01", "lon": "-68.51", "display_name": "Opcion B"},
            ]

    municipio_cfg = JUNIN_CONFIG
    with patch("services.address_resolver.requests.get", return_value=MultiResp()):
        response = handle_direccion("Sarmiento 100", {}, municipio_cfg)

    assert "options_list" in response
    assert len(response["options_list"]) == 2
    assert "1" in response["options_list"][0]["texto"]
