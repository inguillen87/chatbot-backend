import pytest
from unittest.mock import patch
from services.herramientas_municipio import validar_y_formatear_direccion, consultar_noticias_municipio
from models import MunicipioTicket, db
from sqlalchemy import text
from app import create_app
from config import TestConfig
from datetime import datetime

@pytest.fixture
def app_context():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        db.session.execute(text("PRAGMA foreign_keys=OFF"))
        yield
        db.session.remove()
        db.drop_all()

@patch('services.herramientas_municipio.AddressResolver.resolve')
def test_validar_y_formatear_direccion_exitosa(mock_resolve):
    mock_resolve.return_value = {
        "formatted": "Av. Siempreviva 742, Junín, Mendoza, AR",
        "lat": -33.0,
        "lon": -68.5,
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
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")
    assert resultado == {
        "formatted_address": "Av. Siempreviva 742, Junín, Mendoza, AR",
        "lat": -33.0,
        "lng": -68.5,
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

@patch('services.herramientas_municipio.AddressResolver.resolve', return_value=None)
def test_validar_y_formatear_direccion_invalida(mock_resolve):
    resultado = validar_y_formatear_direccion("una dirección inválida")
    assert resultado is None

def test_consultar_noticias_municipio_exitosa(app_context):
    """
    Tests the successful retrieval of news and events from the database.
    """
    # Arrange
    mock_item_1 = MunicipioTicket(asunto="Noticia 1", detalles="Resumen 1", categoria="Noticia", fecha=datetime.now(), municipio_id=1, nro_ticket="N1")
    mock_item_2 = MunicipioTicket(asunto="Evento 2", detalles="Resumen 2", categoria="Evento", fecha=datetime.now(), municipio_id=1, nro_ticket="N2")
    db.session.add_all([mock_item_1, mock_item_2])
    db.session.commit()

    # Act
    resultado = consultar_noticias_municipio()

    # Assert
    assert "Aquí están las últimas noticias y eventos" in resultado
    assert "Noticia 1" in resultado
    assert "Resumen 1" in resultado
    assert "Evento 2" in resultado
    assert "Resumen 2" in resultado

def test_consultar_noticias_municipio_sin_noticias(app_context):
    """
    Tests the case where no news or events are found in the database.
    """
    # Act
    resultado = consultar_noticias_municipio()

    # Assert
    assert "No se encontraron noticias o eventos recientes" in resultado

@patch('services.herramientas_municipio.db.session.query')
def test_consultar_noticias_municipio_error(mock_query):
    """
    Tests the handling of a database error.
    """
    # Arrange
    mock_query.side_effect = Exception("Database connection failed")

    # Act
    resultado = consultar_noticias_municipio()

    # Assert
    assert "No pude obtener las últimas noticias en este momento" in resultado
    assert "https://www.juninmendoza.gov.ar/noticias/" in resultado


def test_generar_respuesta_audio_in_tool_registry():
    """
    Tests that the 'generar_respuesta_audio' tool is correctly registered.
    """
    from services.herramientas_municipio import TOOL_REGISTRY
    assert "generar_respuesta_audio" in TOOL_REGISTRY
    tool_info = TOOL_REGISTRY["generar_respuesta_audio"]
    assert "funcion" in tool_info
    assert "descripcion" in tool_info
    assert "parametros" in tool_info
    assert "text" in tool_info["parametros"]

@patch('services.herramientas_municipio.AddressResolver.resolve', side_effect=Exception("boom"))
def test_validar_y_formatear_direccion_error_api(mock_resolve):
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")
    assert resultado is None
