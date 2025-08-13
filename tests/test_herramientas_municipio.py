import pytest
from unittest.mock import patch, Mock
from services.herramientas_municipio import validar_y_formatear_direccion

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

    # Act
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")

    # Assert
    assert resultado is not None
    assert resultado["formatted_address"] == "Av. Siempreviva 742, Springfield, EE. UU."
    assert resultado["lat"] == 40.7128
    assert resultado["lng"] == -74.0060

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
    resultado = validar_y_formatear_direccion("una dirección inválida")

    # Assert
    assert resultado is None

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
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")

    # Assert
    assert resultado is None
