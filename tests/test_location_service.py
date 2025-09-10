import requests

from services.location_service import geocode_address


def test_geocode_address_success(monkeypatch):
    """Geocoding returns coordinates when Nominatim responds with data."""

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

    assert captured_params.get("q") == "Av Siempre Viva 123, Junin"

