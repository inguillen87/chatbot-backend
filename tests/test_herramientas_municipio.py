import pytest
from unittest.mock import patch, Mock
from services.herramientas_municipio import validar_y_formatear_direccion, consultar_noticias_municipio
from models import MunicipioTicket, db
from app import create_app
from config import TestConfig
from datetime import datetime

@pytest.fixture
def app_context():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()

@patch('services.herramientas_municipio.Maps_API_KEY', 'fake_api_key')
@patch('services.herramientas_municipio.requests.get')
def test_validar_y_formatear_direccion_exitosa(mock_get):
    # Arrange
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {
                "formatted_address": "Av. Siempreviva 742, Springfield, EE. UU.",
                "geometry": {
                    "location": {
                        "lat": 40.7128,
                        "lng": -74.0060
                    }
                }
            }
        ],
        "status": "OK"
    }
    mock_get.return_value = mock_response
    mock_config = {"ciudad": "Junin", "provincia": "Mendoza"}

    # Act
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742", municipio_config=mock_config)

    # Assert
    assert resultado is not None
    assert resultado["formatted_address"] == "Av. Siempreviva 742, Springfield, EE. UU."
    assert resultado["lat"] == 40.7128
    assert resultado["lng"] == -74.0060

    # Assert that the requests.get mock was called with the correct biasing components
    mock_get.assert_called_once()
    call_kwargs = mock_get.call_args.kwargs
    assert 'params' in call_kwargs
    assert call_kwargs['params']['components'] == 'country:AR|administrative_area:Mendoza|locality:Junin'


@patch('services.herramientas_municipio.Maps_API_KEY', 'fake_api_key')
@patch('services.herramientas_municipio.requests.get')
def test_validar_y_formatear_direccion_invalida(mock_get):
    # Arrange
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [],
        "status": "ZERO_RESULTS"
    }
    mock_get.return_value = mock_response

    # Act
    resultado = validar_y_formatear_direccion("una dirección inválida", municipio_config=None)

    # Assert
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

@patch('services.herramientas_municipio.Maps_API_KEY', 'fake_api_key')
@patch('services.herramientas_municipio.requests.get')
def test_validar_y_formatear_direccion_error_api(mock_get):
    # Arrange
    mock_response = Mock()
    mock_response.status_code = 500
    mock_get.return_value = mock_response

    # Act
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742", municipio_config=None)

    # Assert
    assert resultado is None
