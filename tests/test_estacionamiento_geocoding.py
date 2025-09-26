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
