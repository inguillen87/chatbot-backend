import requests

import requests

from services.location_service import geocode_address


def test_geocode_address_success(monkeypatch):
    """Geocoding returns coordinates when Nominatim responds with data."""

    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {
                    "lat": "-34.6",
                    "lon": "-58.4",
                    "display_name": "Av. Siempre Viva 123, Ciudad",
                }
            ]

    def fake_get(url, params=None, headers=None, timeout=5):
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    result = geocode_address("Av Siempre Viva 123")
    assert result["lat"] == -34.6
    assert result["lng"] == -58.4
    assert "maps_search_url" in result


def test_geocode_address_failure(monkeypatch):
    """When Nominatim returns empty data, function yields None."""

    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def fake_get(url, params=None, headers=None, timeout=5):
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    result = geocode_address("some unknown place")
    assert result is None


def test_geocode_address_with_district(monkeypatch):
    """District parameter should be appended to the search query."""

    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    captured_params = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"lat": "-33.0", "lon": "-60.0", "display_name": "Foo"}
            ]

    def fake_get(url, params=None, headers=None, timeout=5):
        nonlocal captured_params
        captured_params = params or {}
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    geocode_address("Av Siempre Viva 123", district="Junin")

    assert captured_params.get("street") == "Av Siempre Viva 123"
    assert captured_params.get("city") == "Junin"
    assert captured_params.get("countrycodes") == "ar"
    assert "q" not in captured_params


def test_geocode_normalizes_corner(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    captured_params = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"lat": "-33.0", "lon": "-60.0", "display_name": "Foo"}
            ]

    def fake_get(url, params=None, headers=None, timeout=5):
        nonlocal captured_params
        captured_params = params or {}
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    geocode_address("don bosco esquina sarmiento", district="Junin")

    assert captured_params.get("q") == "don bosco & sarmiento, Junin, AR"
    assert "city" not in captured_params
    assert "street" not in captured_params


def test_geocode_normalizes_corner_with_e(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    captured_params = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"lat": "-33.0", "lon": "-60.0", "display_name": "Foo"}
            ]

    def fake_get(url, params=None, headers=None, timeout=5):
        nonlocal captured_params
        captured_params = params or {}
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    geocode_address("don bosco e sarmiento", district="Junin")

    assert captured_params.get("q") == "don bosco & sarmiento, Junin, AR"
    assert "street" not in captured_params


def test_geocode_address_uses_google(monkeypatch):
    """If Google API key is present, the Google endpoint is used."""

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "FAKE")

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {
                        "geometry": {"location": {"lat": -33.0, "lng": -60.0}},
                        "formatted_address": "Foo",
                    }
                ]
            }

    def fake_get(url, params=None, timeout=5, headers=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    result = geocode_address("Av Siempre Viva 123")
    assert captured["url"] == "https://maps.googleapis.com/maps/api/geocode/json"
    assert result["lat"] == -33.0 and result["lng"] == -60.0


def test_geocode_address_with_geo_context_google(monkeypatch):
    """Geo context should translate to Google components/bounds."""

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "FAKE")

    geo_ctx = {
        "city": "Junín",
        "state": "Mendoza",
        "country": "AR",
        "region_hint": "ar",
        "bounds": (-68.6, -33.2, -68.3, -32.9),
    }

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {
                        "geometry": {"location": {"lat": -33.0, "lng": -60.0}},
                        "formatted_address": "Foo",
                    }
                ]
            }

    def fake_get(url, params=None, timeout=5, headers=None):
        captured["params"] = params
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    geocode_address("Don Bosco 55", geo_ctx=geo_ctx)

    assert captured["params"]["components"] == (
        "country:AR|administrative_area:Mendoza|locality:Junín"
    )
    assert captured["params"]["region"] == "ar"
    assert captured["params"]["language"] == "es-AR"
    assert (
        captured["params"]["bounds"]
        == "-33.2,-68.6|-32.9,-68.3"
    )


def test_geocode_address_with_geo_context_nominatim(monkeypatch):
    """Geo context biases Nominatim parameters."""

    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    geo_ctx = {
        "city": "Junín",
        "state": "Mendoza",
        "country": "AR",
        "bounds": (-68.6, -33.2, -68.3, -32.9),
    }

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"lat": "-33.0", "lon": "-60.0", "display_name": "Foo"}
            ]

    def fake_get(url, params=None, headers=None, timeout=5):
        captured.update(params)
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    geocode_address("Don Bosco 55", geo_ctx=geo_ctx)

    assert captured["street"] == "Don Bosco 55"
    assert captured["city"] == "Junín"
    assert captured["state"] == "Mendoza"
    assert captured["countrycodes"] == "ar"
    assert captured["viewbox"] == "-68.6,-32.9,-68.3,-33.2"
    assert captured["accept-language"] == "es-AR"



def test_nominatim_sanitizes_city(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    geo_ctx = {"city": "Junin, Mendoza", "state": "Mendoza", "country": "AR"}

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"lat": "-33.0", "lon": "-60.0", "display_name": "Foo"}]

    def fake_get(url, params=None, headers=None, timeout=5):
        nonlocal captured
        captured = params
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    geocode_address("Don Bosco 55", geo_ctx=geo_ctx)

    assert captured["city"] == "Junin"
    assert captured["street"] == "Don Bosco 55"
    assert "q" not in captured

