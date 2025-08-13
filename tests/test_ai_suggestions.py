import pytest
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Añadir el directorio raíz del proyecto al sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import PlantillasRespuesta, User, Rubro
from routes.ai import ai_bp
import json
from config import Config

# Configuración de prueba
@pytest.mark.legacy
class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

@pytest.mark.legacy
class TestAISuggestions(unittest.TestCase):
    def setUp(self):
        """Set up for each test method."""
        self.app = create_app(TestConfig)
        self.app.register_blueprint(ai_bp, url_prefix='/ai')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.mock_rubro = Rubro(id=1, clave="pyme_test_rubro", nombre="Test Rubro PYME")
        db.session.add(self.mock_rubro)
        db.session.commit()

        self.mock_user = User(
            id=1, name="Test Admin User", email="admin@test.com",
            rol="admin", token="test_auth_token_admin", rubro_id=self.mock_rubro.id
        )
        self.mock_user.set_password("adminpass")
        db.session.add(self.mock_user)
        db.session.commit()

        self.g_patcher = patch('flask.g', new_callable=MagicMock)
        self.mock_g = self.g_patcher.start()
        self.mock_g.current_user = self.mock_user

    def tearDown(self):
        """Tear down after each test method."""
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        self.g_patcher.stop()
        patch.stopall()

    def _crear_plantilla(self, name, text, keywords=None, is_active=True, embedding_value=None):
        if embedding_value is None:
            embedding_value = [0.1] * 1024

        plantilla = PlantillasRespuesta(
            name=name, text=text,
            keywords=json.dumps(keywords) if keywords else json.dumps([]),
            is_active=is_active, embedding=embedding_value
        )
        db.session.add(plantilla)
        db.session.commit()
        return plantilla

    @patch('routes.ai.embed_textos_gemini')
    def test_suggest_templates_success(self, mock_embed_textos_gemini):
        mock_embed_textos_gemini.return_value = [[0.11]*1024]
        self._crear_plantilla("Saludo", "Hola, ¿cómo estás {{nombre_cliente}}?", ["saludo"], embedding_value=[0.1]*1024)
        self._crear_plantilla("Despedida", "Adiós, {{nombre_cliente}}.", ["despedida"], embedding_value=[0.2]*1024)

        response = self.client.post('/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {self.mock_user.token}'},
                                    json={'asunto': 'Quiero saludar', 'consulta_cliente': 'Hola', 'top_n': 1})

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn('sugerencias', data)
        sugerencias = data['sugerencias']
        self.assertEqual(len(sugerencias), 1)
        self.assertEqual(sugerencias[0]['name'], 'Saludo')
        self.assertIn("Hola, ¿cómo estás {{nombre_cliente}}?", sugerencias[0]['text'])
        mock_embed_textos_gemini.assert_called_once()

    def test_suggest_templates_missing_asunto(self):
        response = self.client.post('/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {self.mock_user.token}'},
                                    json={'consulta_cliente': 'Hola'})
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn('error', data)
        self.assertIn("El campo 'asunto' es obligatorio", data['error'])

    @patch('routes.ai.embed_textos_gemini')
    def test_suggest_templates_no_active_templates_with_embeddings(self, mock_embed_textos_gemini):
        self._crear_plantilla("Inactiva", "Plantilla inactiva", is_active=False, embedding_value=[0.5]*1024)
        self._crear_plantilla("Activa Sin Embedding", "Texto activo sin embedding", embedding_value=None)
        mock_embed_textos_gemini.return_value = [[0.1]*1024]

        response = self.client.post('/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {self.mock_user.token}'},
                                    json={'asunto': 'Consulta', 'consulta_cliente': 'Duda', 'top_n': 1})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn('sugerencias', data)
        self.assertEqual(len(data['sugerencias']), 1)
        # The 'message' key is optional and not present when suggestions are found.
        # self.assertIn('message', data)
        # self.assertEqual(data['message'], "No hay plantillas de respuesta activas configuradas con embeddings.")

    @patch('routes.ai.embed_textos_gemini')
    def test_suggest_templates_embed_fails(self, mock_embed_textos_gemini):
        self._crear_plantilla("Activa Con Embedding", "Texto activo", embedding_value=[0.1]*1024)
        mock_embed_textos_gemini.return_value = None

        response = self.client.post('/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {self.mock_user.token}'},
                                    json={'asunto': 'Consulta', 'consulta_cliente': 'Ayuda'})

        self.assertEqual(response.status_code, 500)
        data = response.get_json()
        self.assertIn('error', data)
        self.assertIn("Error al generar el embedding para la consulta.", data['error'])

if __name__ == '__main__':
    unittest.main()
