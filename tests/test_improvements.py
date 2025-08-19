import unittest
from unittest.mock import Mock, patch
import os
import sys

# Add the project root to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from flask import Flask
from services.municipios import responder_municipio, ConversationState, CONTEXTO_MUNICIPIO
from models import db, User, Rubro, ChatSessionContext

class TestMunicipioImprovements(unittest.TestCase):

    def setUp(self):
        """Set up a test client and initialize the database."""
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

        db.init_app(self.app)

        with self.app.app_context():
            db.create_all()
            # Create dummy data for testing
            owner_user = User(id=1, name='test_owner', email='owner@test.com', password_hash='test')
            viewer_user = User(id=2, name='test_viewer', email='viewer@test.com', password_hash='test')
            rubro = Rubro(id=1, clave='municipios', nombre='municipios')
            db.session.add(owner_user)
            db.session.add(viewer_user)
            db.session.add(rubro)
            db.session.commit()

    def tearDown(self):
        """Clean up the database after each test."""
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_type_error_fix_in_normalizar_str(self):
        """
        Test that the TypeError in normalizar_str is fixed.
        This is tested by calling responder_municipio with a list in 'pedir_info'
        which previously caused the error.
        """
        with self.app.app_context():
            owner_user = User.query.get(1)
            viewer_user = User.query.get(2)
            rubro = Rubro.query.get(1)

            chat_session = ChatSessionContext(
                chat_session_id='test_session_type_error',
                user_id=owner_user.id,
                context_data={
                    CONTEXTO_MUNICIPIO: {
                        'estado_conversacion': ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name
                    }
                }
            )
            db.session.add(chat_session)
            db.session.commit()

            # Mock the Gemini call to return a response that includes a list in 'pedir_info'
            with patch('services.municipios.llamar_gemini') as mock_llamar_gemini:
                mock_llamar_gemini.return_value = {
                    "message_body": "Some response",
                    "accion_backend": "iniciar_reclamo",
                    "datos_estructura": {},
                    "pedir_info": ["una descripción del problema", "tu nombre"],
                    "botones": []
                }

                # The call to responder_municipio should not raise a TypeError
                try:
                    responder_municipio(
                        pregunta_original="confirmar",
                        owner_user=owner_user,
                        rubro_obj=rubro,
                        viewer_user=viewer_user,
                        chat_db_context=chat_session,
                        anon_id=None,
                        channel="whatsapp"
                    )
                except TypeError as e:
                    self.fail(f"responder_municipio raised TypeError unexpectedly: {e}")


if __name__ == '__main__':
    unittest.main()
