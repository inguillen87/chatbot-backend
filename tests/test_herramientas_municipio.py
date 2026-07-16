import pytest
from unittest.mock import patch
from services.herramientas_municipio import validar_y_formatear_direccion, consultar_noticias_municipio
from models import MunicipioPost, db
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

@patch('services.herramientas_municipio._load_camaras_referencia', return_value=[])
@patch('services.herramientas_municipio.geocodificar_texto_llm')
def test_validar_y_formatear_direccion_exitosa(mock_geocodificar, _mock_camaras):
    mock_geocodificar.return_value = {
        "normalized_query": "Av. Siempreviva 742, Junin, Mendoza",
        "lat": 40.7128,
        "lon": -74.0060,
        "confidence": 0.91,
    }
    mock_config = {"ciudad": "Junin", "provincia": "Mendoza"}

    # Act
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742", municipio_config=mock_config)

    # Assert
    assert resultado is not None
    assert resultado["formatted_address"] == "Av. Siempreviva 742, Junin, Mendoza"
    assert resultado["lat"] == 40.7128
    assert resultado["lng"] == -74.0060
    assert resultado["source"] == "openai_llm"
    mock_geocodificar.assert_called_once_with(
        "Av. Siempreviva 742",
        puntos_referencia=[],
        localidad_predeterminada="Junin",
    )


@patch('services.herramientas_municipio._load_camaras_referencia', return_value=[])
@patch('services.herramientas_municipio.geocodificar_texto_llm', return_value=None)
def test_validar_y_formatear_direccion_invalida(_mock_geocodificar, _mock_camaras):

    # Act
    resultado = validar_y_formatear_direccion("una dirección inválida", municipio_config=None)

    # Assert
    assert resultado is None

@patch('services.herramientas_municipio.MUNICIPIO_ID', 1)
@patch(
    'services.herramientas_municipio.CONFIG_MUNICIPIO',
    {"nombre": "Municipalidad de Junin"},
)
def test_consultar_noticias_municipio_exitosa(app_context):
    """
    Tests the successful retrieval of news and events from the database.
    """
    # Arrange
    mock_item_1 = MunicipioPost(
        titulo="Noticia 1",
        descripcion="Resumen 1",
        tipo_post="noticia",
        fecha_publicacion=datetime.now(),
        municipio_id=1,
    )
    mock_item_2 = MunicipioPost(
        titulo="Información 2",
        descripcion="Resumen 2",
        tipo_post="informacion",
        fecha_publicacion=datetime.now(),
        municipio_id=1,
    )
    db.session.add_all([mock_item_1, mock_item_2])
    db.session.commit()

    # Act
    resultado = consultar_noticias_municipio()

    # Assert
    assert "Aquí están las últimas novedades de Municipalidad de Junin" in resultado
    assert "Noticia 1" in resultado
    assert "Resumen 1" in resultado
    assert "Información 2" in resultado
    assert "Resumen 2" in resultado

@patch('services.herramientas_municipio.MUNICIPIO_ID', 1)
def test_consultar_noticias_municipio_sin_noticias(app_context):
    """
    Tests the case where no news or events are found in the database.
    """
    # Act
    resultado = consultar_noticias_municipio()

    # Assert
    assert "No se encontraron noticias o eventos recientes" in resultado

@patch('services.herramientas_municipio.MUNICIPIO_ID', 1)
@patch('services.herramientas_municipio.MunicipioPost.query')
def test_consultar_noticias_municipio_error(mock_query, app_context):
    """
    Tests the handling of a database error.
    """
    # Arrange
    mock_query.filter.side_effect = Exception("Database connection failed")

    # Act
    resultado = consultar_noticias_municipio()

    # Assert
    assert "No pude obtener las últimas noticias en este momento" in resultado
    assert "sitio web oficial" in resultado


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

@patch('services.herramientas_municipio._load_camaras_referencia', return_value=[])
@patch('services.herramientas_municipio.geocodificar_texto_llm', return_value=None)
def test_validar_y_formatear_direccion_sin_resultado(_mock_geocodificar, _mock_camaras):

    # Act
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742", municipio_config=None)

    # Assert
    assert resultado is None
