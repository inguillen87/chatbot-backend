import requests

from services.location_service import (
    geocode_address,
    _normalize_corner,
    _extract_from_maps_url,
)

extract_location = geocode_address


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
    assert "q" not in captured


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


def test_normalize_corner_removes_number():
    assert _normalize_corner("Don Bosco 55 esquina Sarmiento") == "Don Bosco & Sarmiento"


def test_extract_from_maps_url(monkeypatch):
    class FakeResp:
        url = "https://www.google.com/maps/@-33.1,-68.5,17z"

    calls = {"head": 0}

    def fake_head(url, allow_redirects=True, timeout=5):
        calls["head"] += 1
        return FakeResp()

    def fake_get(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("GET should not be used for short-link resolution")

    monkeypatch.setattr(requests, "head", fake_head)
    monkeypatch.setattr(requests, "get", fake_get)
    from services import location_service

    location_service._RESOLVED_URL_CACHE.clear()
    data = _extract_from_maps_url("https://maps.app.goo.gl/abc")
    assert data["lat"] == -33.1 and data["lng"] == -68.5
    assert calls["head"] == 1


def test_extract_from_maps_url_cache(monkeypatch):
    class FakeResp:
        url = "https://www.google.com/maps/@-33.2,-68.6,17z"

    calls = {"head": 0}

    def fake_head(url, allow_redirects=True, timeout=5):
        calls["head"] += 1
        return FakeResp()

    def fake_get(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("GET should not be used for short-link resolution")

    monkeypatch.setattr(requests, "head", fake_head)
    monkeypatch.setattr(requests, "get", fake_get)
    from services import location_service

    location_service._RESOLVED_URL_CACHE.clear()
    url = "https://maps.app.goo.gl/xyz"
    _extract_from_maps_url(url)
    _extract_from_maps_url(url)
    assert calls["head"] == 1


def test_extract_location_shortlink_at_coords(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    class HeadResp:
        url = "https://www.google.com/maps/@-33.1,-68.5,17z"

    calls = {"head": 0}

    def fake_head(url, allow_redirects=True, timeout=5):
        calls["head"] += 1
        return HeadResp()

    def fake_get(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("GET should not be used when resolving short link")

    monkeypatch.setattr(requests, "head", fake_head)
    monkeypatch.setattr(requests, "get", fake_get)
    from services import location_service

    location_service._RESOLVED_URL_CACHE.clear()
    result = extract_location("https://maps.app.goo.gl/atxyz")
    assert result["lat"] == -33.1 and result["lng"] == -68.5
    assert (
        result["maps_search_url"]
        == "https://www.google.com/maps/search/?api=1&query=-33.1,-68.5"
    )
    assert calls["head"] == 1


def test_extract_location_shortlink_q_coords(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    class HeadResp:
        url = "https://www.google.com/maps?q=-33.2,-68.6"

    calls = {"head": 0}

    def fake_head(url, allow_redirects=True, timeout=5):
        calls["head"] += 1
        return HeadResp()

    def fake_get(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("GET should not be used when resolving short link")

    monkeypatch.setattr(requests, "head", fake_head)
    monkeypatch.setattr(requests, "get", fake_get)
    from services import location_service

    location_service._RESOLVED_URL_CACHE.clear()
    result = extract_location("https://maps.app.goo.gl/qxyz")
    assert result["lat"] == -33.2 and result["lng"] == -68.6
    assert (
        result["maps_search_url"]
        == "https://www.google.com/maps/search/?api=1&query=-33.2,-68.6"
    )
    assert calls["head"] == 1

