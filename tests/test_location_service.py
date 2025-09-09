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

