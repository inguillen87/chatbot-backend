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

def test_full_claim_flow(test_app):
    with patch('services.municipios.llamar_gemini') as mock_llamar_gemini:
        # 1. User initiates a claim
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Claro, ¿cuál es el problema?",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {},
            "pedir_info": "descripcion"
        }
        owner_user = MagicMock()
        owner_user.id = 1
        rubro_obj = None
        viewer_user = None
        chat_db_context = MagicMock()
        chat_db_context.context_data = {}
        response = responder_municipio(
            pregunta_original="Quiero hacer un reclamo",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id"
        )
        assert "Para poder registrar tu reclamo" in response["message_body"]

        # 2. User provides description
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Entendido, un poste de luz roto. ¿Dónde ocurrió?",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"descripcion": "Poste de luz roto"},
            "pedir_info": "ubicacion"
        }
        response = responder_municipio(
            pregunta_original="Poste de luz roto",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id"
        )
        assert "Entendido, un poste de luz roto. ¿Dónde ocurrió?" in response["message_body"]

        # 3. User provides location
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Gracias. Para registrar el reclamo, necesito tu nombre completo.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"descripcion": "Poste de luz roto", "ubicacion": "Calle Falsa 123"},
            "pedir_info": "nombre_completo"
        }
        response = responder_municipio(
            pregunta_original="Calle Falsa 123",
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=viewer_user,
            chat_db_context=chat_db_context,
            anon_id="test_anon_id"
        )
        assert "Gracias. Para registrar el reclamo, necesito tu nombre completo." in response["message_body"]

if __name__ == '__main__':
    unittest.main()
