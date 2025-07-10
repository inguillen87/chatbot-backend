import unittest
from unittest.mock import patch, MagicMock, PropertyMock
from flask import Flask, g
from config import Config
from models import db, PlantillasRespuesta, User, Rubro # Ensure User and Rubro are imported
from routes.ai import ai_bp # Assuming the blueprint is named ai_bp in routes/ai.py
import json

# Configuración de prueba
class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:" # Use in-memory SQLite for tests
    WTF_CSRF_ENABLED = False
    COHERE_API_KEY = "test_cohere_key" # Mock key, not actually used if functions are patched

class TestAISuggestions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Set up the Flask application for all tests in this class."""
        cls.app = Flask(__name__) # Create a new Flask app instance
        cls.app.config.from_object(TestConfig)
        db.init_app(cls.app)
        cls.app.register_blueprint(ai_bp, url_prefix='/ai') # Register the blueprint

    def setUp(self):
        """Set up for each test method."""
        self.app_context = self.app.app_context()
        self.app_context.push() # Push an application context
        db.create_all() # Create database tables
        self.client = self.app.test_client() # Create a test client

        # Mock user and rubro for authentication context
        self.mock_rubro = Rubro(id=1, clave="pyme_test_rubro", nombre="Test Rubro PYME")
        db.session.add(self.mock_rubro)
        db.session.commit()

        self.mock_user = User(
            id=1, name="Test Admin User", email="admin@test.com",
            rol="admin", token="test_auth_token_admin", rubro_id=self.mock_rubro.id
        )
        self.mock_user.set_password("adminpass") # Assuming User model has set_password
        db.session.add(self.mock_user)
        db.session.commit()

        # Patch flask.g to simulate an authenticated user
        # This is common if your @token_requerido sets g.current_user
        self.g_patcher = patch('flask.g', new_callable=MagicMock)
        self.mock_g = self.g_patcher.start()
        self.mock_g.current_user = self.mock_user

        # Corrected patch targets for Cohere functions
        self.embed_patcher = patch('services.cohere_ai.embed_textos')
        self.mock_embed_textos = self.embed_patcher.start()

        self.chat_patcher = patch('services.cohere_ai.get_cohere_response')
        self.mock_get_cohere_response = self.chat_patcher.start()


    def tearDown(self):
        """Tear down after each test method."""
        db.session.remove()
        db.drop_all() # Drop all tables
        self.app_context.pop() # Pop the application context

        self.g_patcher.stop()
        self.embed_patcher.stop()
        self.chat_patcher.stop()

    def _crear_plantilla(self, name, text, keywords=None, is_active=True, embedding_value=None):
        """Helper to create and save a PlantillasRespuesta instance."""
        if embedding_value is None:
            # Ensure embedding dimension matches what your Cohere model outputs (e.g., 1024)
            embedding_value = [0.1] * 1024

        plantilla = PlantillasRespuesta(
            name=name, text=text,
            keywords=json.dumps(keywords) if keywords else json.dumps([]), # Store keywords as JSON string
            is_active=is_active, embedding=embedding_value
        )
        db.session.add(plantilla)
        db.session.commit()
        return plantilla

    def test_suggest_templates_success(self):
        """Prueba de éxito para sugerencia de plantillas."""
        self._crear_plantilla("Saludo", "Hola, ¿cómo estás {{nombre_cliente}}?", ["saludo"], embedding_value=[0.1]*1024)
        self._crear_plantilla("Despedida", "Adiós, {{nombre_cliente}}.", ["despedida"], embedding_value=[0.2]*1024)

        self.mock_embed_textos.return_value = [[0.11]*1024] # Embedding for query, close to "Saludo"

        response = self.client.post('/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {self.mock_user.token}'},
                                    json={'asunto': 'Quiero saludar', 'consulta_cliente': 'Hola', 'top_n': 1})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        # The route now returns a dict with a 'sugerencias' key
        self.assertIn('sugerencias', data)
        sugerencias = data['sugerencias']
        self.assertEqual(len(sugerencias), 1)
        self.assertEqual(sugerencias[0]['name'], 'Saludo')
        self.assertIn("Hola, ¿cómo estás {{nombre_cliente}}?", sugerencias[0]['text'])
        self.mock_embed_textos.assert_called_once_with(textos=['Quiero saludar'], input_type='search_query')


    def test_suggest_templates_missing_asunto(self):
        """Prueba de error cuando falta el campo 'asunto'."""
        response = self.client.post('/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {self.mock_user.token}'},
                                    json={'consulta_cliente': 'Hola'}) # Missing 'asunto'
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn('error', data)
        self.assertIn("El campo 'asunto' es obligatorio", data['error'])

    def test_suggest_templates_no_active_templates_with_embeddings(self):
        """Prueba cuando no hay plantillas activas con embeddings."""
        self._crear_plantilla("Inactiva", "Plantilla inactiva", is_active=False, embedding_value=[0.5]*1024)
        self._crear_plantilla("Activa Sin Embedding", "Texto activo sin embedding", embedding_value=None)

        self.mock_embed_textos.return_value = [[0.1]*1024] # Query embedding

        response = self.client.post('/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {self.mock_user.token}'},
                                    json={'asunto': 'Consulta', 'consulta_cliente': 'Duda', 'top_n': 1})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertIn('sugerencias', data)
        self.assertEqual(len(data['sugerencias']), 0)
        self.assertIn('message', data) # Expect a message indicating no templates found
        self.assertEqual(data['message'], "No hay plantillas de respuesta activas configuradas con embeddings.")


    def test_suggest_templates_cohere_embed_fails(self):
        """Prueba cuando embed_textos (para la consulta) no devuelve un embedding."""
        self._crear_plantilla("Activa Con Embedding", "Texto activo", embedding_value=[0.1]*1024)
        self.mock_embed_textos.return_value = None # Simular fallo de Cohere al obtener embedding para la consulta

        response = self.client.post('/ai/suggest-templates',
                                    headers={'Authorization': f'Bearer {self.mock_user.token}'},
                                    json={'asunto': 'Consulta', 'consulta_cliente': 'Ayuda'})

        self.assertEqual(response.status_code, 500)
        data = json.loads(response.data)
        self.assertIn('error', data)
        self.assertIn("Error al generar el embedding para la consulta del ticket", data['error'])

if __name__ == '__main__':
    unittest.main()
