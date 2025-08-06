import pytest
from unittest.mock import patch, MagicMock
from app import create_app, db
from config import TestingConfig

@pytest.fixture(scope='session')
def app():
    """Create a new app instance for each test session."""
    app = create_app(TestingConfig)
    return app

@pytest.fixture(scope='function')
def client(app):
    """A test client for the app."""
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            yield client
            db.session.remove()
            db.drop_all()

@pytest.fixture(autouse=True, scope='session')
def mock_google_cloud_services():
    """
    Mocks all Google Cloud service clients to prevent real API calls during tests.
    This is now more robust by patching the client classes themselves.
    """
    with patch('google.cloud.vision.ImageAnnotatorClient'), \
         patch('google.cloud.documentai.DocumentProcessorServiceClient'), \
         patch('google.cloud.speech_v1.SpeechClient'), \
         patch('google.cloud.texttospeech.TextToSpeechClient'), \
         patch('googlemaps.Client'), \
         patch('geopy.geocoders.GoogleV3'), \
         patch('services.gemini_bridge.llamar_gemini') as mock_llamar_gemini_func:

        # The higher-level mock for tests that use it directly
        mock_llamar_gemini_func.return_value = {
            "respuesta_usuario": "Respuesta mock de llamar_gemini.",
            "accion_backend": "responder_directamente"
        }

        yield

@pytest.fixture
def mock_responder_municipio():
    with patch('services.logic.responder_municipio') as mock:
        yield mock

@pytest.fixture
def mock_responder_pyme():
    with patch('services.logic.responder_pyme') as mock:
        yield mock
