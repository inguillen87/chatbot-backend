import pytest
from unittest.mock import patch
from app import create_app, db
from config import TestConfig

@pytest.fixture(scope='function')
def test_app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
    return app

@pytest.fixture(scope='function')
def test_client(test_app):
    return test_app.test_client()

@pytest.fixture(scope="session", autouse=True)
def mock_llamar_gemini_pymes_session():
    with patch('services.pymes.llamar_gemini') as mock_gemini:
        yield mock_gemini

@pytest.fixture(scope="session", autouse=True)
def mock_llamar_gemini_municipios_session():
    with patch('services.municipios.llamar_gemini') as mock_gemini:
        yield mock_gemini
