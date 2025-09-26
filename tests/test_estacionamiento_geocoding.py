import math

from services.estacionamiento_service import CAMARAS, consultar_ocupacion
from services.estacionamiento_utils import _dist_m, aproximar_coordenadas_por_texto


def test_heuristica_identifica_junin():
    resultado = aproximar_coordenadas_por_texto("don bosco 55 junin", CAMARAS)
    assert resultado is not None
    cam = resultado["camera"]
    assert cam["id"] == "junin-centro-01"
    distancia = _dist_m(resultado["lat"], resultado["lon"], cam["lat"], cam["lon"])
    assert distancia < cam["cobertura_m"]
    assert resultado["confidence"] >= 0.4


def test_consulta_textual_usa_heuristica():
    respuesta = consultar_ocupacion("Av. San Martín y Sarmiento, Mendoza")
    assert "Junín" not in respuesta.get("texto", "")
    assert "San Martín" in respuesta.get("texto", "")
    assert respuesta.get("geocode_source") == "camaras_heuristica"
    assert math.isfinite(respuesta.get("distance_m", 0))


def test_consultar_ocupacion_rechaza_comandos():
    respuesta = consultar_ocupacion("buscar_estacionamiento")
    assert respuesta.get("geocode_source") == "invalid_query"
    assert "ubicación" in respuesta.get("texto", "").lower()


def test_consultar_ocupacion_con_latitude_longitude():
    ubicacion = {
        "latitude": -33.0127,
        "longitude": -68.4979,
        "accuracy": 35,
        "address": "San Martín y Don Bosco, Junín",
    }
    respuesta = consultar_ocupacion(ubicacion)
    assert respuesta.get("geocode_source") == "provided"
    assert respuesta.get("camera")
    assert respuesta.get("distance_m", 9999) < 900
    confianza = respuesta.get("geocode_confidence")
    assert confianza is None or 0.45 <= confianza <= 0.96
