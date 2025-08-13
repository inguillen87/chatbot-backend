import pytest
from unittest.mock import patch
from app import create_app, db
from models import User, Rubro
from config import TestingConfig

@pytest.fixture(scope='session')
def app():
    """Create a new app instance for each test session."""
    _app = create_app(TestingConfig)
    return _app

@pytest.fixture(scope='function')
def client(app):
    """A test client for the app."""
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            yield client
            db.session.remove()
            db.drop_all()

@pytest.fixture(scope='function')
def init_database(client):
    """Create initial data for the database."""
    rubro = Rubro(nombre="municipio", clave="municipio", es_publico=True)
    db.session.add(rubro)

    owner_user = User(name="Test Municipio", email="test@municipio.com", rubro=rubro, tipo_chat="municipio")
    owner_user.set_password("testpassword")
    db.session.add(owner_user)

    viewer_user = User(name="Test User", email="test@user.com")
    viewer_user.set_password("testpassword")
    db.session.add(viewer_user)

    db.session.commit()
    yield db

@pytest.fixture(scope='function')
def owner_user(init_database):
    return User.query.filter_by(email="test@municipio.com").first()

@pytest.fixture(scope='function')
def viewer_user(init_database):
    return User.query.filter_by(email="test@user.com").first()

@pytest.fixture(scope='function')
def rubro(init_database):
    return Rubro.query.filter_by(clave="municipio").first()

@pytest.fixture(autouse=True)
def mock_google_tts_and_gemini(mocker):
    """
    Automatically mocks the Google Text-to-Speech and Gemini clients for all tests.
    Prevents actual API calls and DefaultCredentialsError.
    This mock is intentionally generic. Tests that require specific LLM
    responses should patch 'services.municipio_responder.llamar_llm_para_json_estructurado'
    or the equivalent function in the module under test.
    """
    # Mock Text-to-Speech
    mock_tts_client = mocker.patch('google.cloud.texttospeech.TextToSpeechClient')
    mock_tts_instance = mock_tts_client.return_value
    mock_tts_instance.synthesize_speech.return_value = mocker.MagicMock(audio_content=b'fake-audio-content')

    # Mock Gemini
    mock_gemini_model = mocker.patch('vertexai.generative_models.GenerativeModel')
    mock_gemini_instance = mock_gemini_model.return_value

    # Create the full nested mock structure for the response
    mock_response = mocker.MagicMock()
    mock_candidate = mocker.MagicMock()
    mock_content = mocker.MagicMock()
    mock_part = mocker.MagicMock()

    # The final piece of data is the text, which must be a JSON string.
    mock_part.text = '{"message_body": "Mocked Gemini Response from conftest"}'

    # Assemble the structure from the inside out
    mock_content.parts = [mock_part]
    mock_candidate.content = mock_content
    mock_response.candidates = [mock_candidate]

    # Also handle the .text attribute for simpler access patterns
    type(mock_response).text = mocker.PropertyMock(return_value='{"message_body": "Mocked Gemini Response from conftest"}')

    mock_gemini_instance.generate_content.return_value = mock_response

    yield mock_tts_instance, mock_gemini_instance
