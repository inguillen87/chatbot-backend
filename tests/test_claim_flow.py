import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import json

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from config import Config
from services.municipios import responder_municipio

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False

@patch('services.municipios.responder_municipio')
def test_full_claim_flow(mock_responder_municipio):
    # 1. User initiates a claim
    mock_responder_municipio.return_value = {
        "message_body": "Claro, ¿cuál es el problema?",
        "pedir_info": "descripcion"
    }
    response = mock_responder_municipio(pregunta_original="Quiero hacer un reclamo")
    assert "Claro, ¿cuál es el problema?" in response["message_body"]

    # 2. User provides description
    mock_responder_municipio.return_value = {
        "message_body": "Entendido, un poste de luz roto. ¿Dónde ocurrió?",
        "pedir_info": "ubicacion"
    }
    response = mock_responder_municipio(pregunta_original="Poste de luz roto")
    assert "Entendido, un poste de luz roto. ¿Dónde ocurrió?" in response["message_body"]

    # 3. User provides location
    mock_responder_municipio.return_value = {
        "message_body": "Gracias. Para registrar el reclamo, necesito tu nombre completo.",
        "pedir_info": "nombre_completo"
    }
    response = mock_responder_municipio(pregunta_original="Calle Falsa 123")
    assert "Gracias. Para registrar el reclamo, necesito tu nombre completo." in response["message_body"]

if __name__ == '__main__':
    unittest.main()
