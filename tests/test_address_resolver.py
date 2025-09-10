from services.address_resolver import AddressResolver
from unittest.mock import patch


def _fake_resp(lat, lon, display="Stub"):
    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return [{"lat": str(lat), "lon": str(lon), "display_name": display}]
    return FakeResponse()


def test_intersection_sarmiento_san_martin():
    resolver = AddressResolver()
    with patch('services.address_resolver.requests.get', return_value=_fake_resp(-33.0, -68.5)):
        result = resolver.resolve("Sarmiento 100 esquina San Martín")
    assert result["precision"] == "intersection"
    assert result["entre_calles"] == ["Sarmiento", "San Martín"]
    assert result["localidad"] == "Junín"
    assert result["validez"] is True
    assert "Sarmiento" in result["formatted"] and "San Martín" in result["formatted"]
