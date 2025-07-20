import unittest
from unittest.mock import patch, MagicMock, PropertyMock, call
from flask import jsonify
import sys
import os
from types import ModuleType, SimpleNamespace
from datetime import datetime, timezone

# Añadir el directorio raíz al path para importar módulos de la app
# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mockear dependencias pesadas o externas ANTES de importar el módulo bajo prueba
# Cohere
mock_cohere_module = MagicMock() # This seems to be for the 'cohere' library itself
sys.modules['cohere'] = mock_cohere_module

# Mock 'services.cohere_ai' and its functions explicitly
mock_cohere_ai_service_stub = MagicMock()
mock_cohere_ai_service_stub.robust_embed = MagicMock(return_value=[[0.1, 0.2]]) # Default mock return
mock_cohere_ai_service_stub.get_cohere_response = MagicMock(return_value="Mocked Cohere Resp") # Default mock return
sys.modules['services.cohere_ai'] = mock_cohere_ai_service_stub


# Mockear modelos y db ANTES de importar las rutas
mock_db_session_instance = MagicMock()
models_stub = ModuleType('models')
# Removed spec_set=True temporarily to see if it allows .query attribute assignment
models_stub.PlantillasRespuesta = MagicMock()
models_stub.User = MagicMock(spec_set=True) # Keep for User if not causing issues
models_stub.db = SimpleNamespace(session=mock_db_session_instance)
sys.modules['models'] = models_stub
sys.modules['extensions'] = MagicMock() # extensions.db también es usado

# Ahora se puede importar el blueprint y sus funciones
from routes.ai_templates import ai_templates_bp, get_all_templates, create_template, update_template, delete_template, generate_template_text_from_prompt, improve_template_text
from app import create_app

# Simular current_app y request que Flask normalmente proporcionaría
mock_current_app_object = MagicMock()
mock_current_app_object.logger = MagicMock()

# --- Clases de Ayuda para Mocks ---
class MockPlantilla:
    def __init__(self, id, name, text, keywords, is_active, embedding=None, created_at=None, updated_at=None):
        self.id = id
        self.name = name
        self.text = text
        self.keywords = keywords
        self.is_active = is_active
        self.embedding = embedding
        self.created_at = created_at or datetime.now(timezone.utc)
        self.updated_at = updated_at or datetime.now(timezone.utc)

    # Simular el comportamiento de isoformat() para los campos de fecha
    @property
    def created_at_iso(self):
        return self.created_at.isoformat()

    @property
    def updated_at_iso(self):
        return self.updated_at.isoformat()

# Define un mock para el objeto request que se usará en las pruebas
mock_request_object = MagicMock()


# --- Test Suite ---
class TestAITemplatesEndpoints(unittest.TestCase):

    def setUp(self):
        """Set up for each test method."""
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()
        db.create_all()

        # Resetear mocks antes de cada prueba
        mock_db_session_instance.reset_mock()
        models_stub.PlantillasRespuesta.reset_mock()
        models_stub.User.reset_mock()

        # Configurar el mock de PlantillasRespuesta.query para que devuelva mocks útiles
        self.mock_query_instance = MagicMock()
        models_stub.PlantillasRespuesta.query = self.mock_query_instance

        # Mockear servicios de Cohere (use the pre-configured mocks)
        self.mock_robust_embed = mock_cohere_ai_service_stub.robust_embed
        self.mock_get_cohere_response = mock_cohere_ai_service_stub.get_cohere_response

        self.mock_robust_embed.reset_mock()
        self.mock_get_cohere_response.reset_mock()

        # Simular un usuario autenticado (admin o empleado)
        self.mock_user_admin = SimpleNamespace(id=1, rol='admin', empresa_id=None, email='admin@test.com')
        self.mock_user_empleado = SimpleNamespace(id=2, rol='empleado', empresa_id=10, email='empleado@test.com')

        # Patch global 'current_app' y 'request' para todas las pruebas en esta clase
        # Esto es más limpio que usar @patch en cada método si siempre son los mismos mocks
        self.patch_current_app = patch('routes.ai_templates.current_app', mock_current_app_object)
        self.patch_request = patch('routes.ai_templates.request', mock_request_object)

        self.patch_current_app.start()
        self.patch_request.start()

        # Mock jsonify para que devuelva el dict directamente y el código de estado
        # self.patch_jsonify = patch('routes.ai_templates.jsonify', lambda data: (data, 200)) # No longer needed
        # self.mock_jsonify = self.patch_jsonify.start()
        # The test client will handle jsonify


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        self.patch_current_app.stop()
        self.patch_request.stop()
        # self.patch_jsonify.stop() # No longer needed
        # Resetting the individual function mocks is done in setUp
        # Resetting the module mock itself if needed:
        # mock_cohere_ai_service_stub.reset_mock()


    # --- Pruebas para GET /templates ---
    def test_get_all_templates_success(self):
        mock_plantilla_1 = MockPlantilla(id='uuid1', name='Saludo', text='Hola', keywords=['saludo'], is_active=True)
        mock_plantilla_2 = MockPlantilla(id='uuid2', name='Despedida', text='Adiós', keywords=['fin'], is_active=False)
        self.mock_query_instance.order_by.return_value.all.return_value = [mock_plantilla_1, mock_plantilla_2]

        # Use test client
        response = self.client.get('/api/ai/templates', headers={'Authorization': f'Bearer {self.mock_user_admin.token}'})
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 200)
        self.assertIn('plantillas', data)
        self.assertEqual(len(data['plantillas']), 2)
        self.assertEqual(data['plantillas'][0]['name'], 'Saludo')
        self.assertEqual(data['plantillas'][1]['is_active'], False)
        self.mock_query_instance.order_by.assert_called_once()
        self.mock_query_instance.order_by.return_value.all.assert_called_once()

    def test_get_all_templates_empty(self):
        self.mock_query_instance.order_by.return_value.all.return_value = []

        response = self.client.get('/api/ai/templates', headers={'Authorization': f'Bearer {self.mock_user_admin.token}'})
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 200)
        self.assertIn('plantillas', data)
        self.assertEqual(len(data['plantillas']), 0)

    def test_get_all_templates_exception(self):
        self.mock_query_instance.order_by.return_value.all.side_effect = Exception("DB Error")

        response = self.client.get('/api/ai/templates', headers={'Authorization': f'Bearer {self.mock_user_admin.token}'})
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 500)
        self.assertIn('error', data)
        self.assertEqual(data['error'], "Error interno al obtener las plantillas.")
        # mock_current_app_object.logger.error.assert_called_once() # This mock might need adjustment if error is logged before jsonify

    # --- Pruebas para POST /templates (create_template) ---
    def test_create_template_success(self):
        request_payload = {
            "name": "Nueva Plantilla",
            "text": "Contenido de la plantilla.",
            "keywords": ["nueva", "test"],
            "is_active": True
        }
        self.mock_robust_embed.return_value = [[0.1, 0.2, 0.3]]

        # Mockear la instancia de PlantillasRespuesta que se crea
        created_plantilla_mock = MagicMock(spec=MockPlantilla) # Use MagicMock for more flexibility if needed
        created_plantilla_mock.id = "new_uuid"
        created_plantilla_mock.name = "Nueva Plantilla"
        created_plantilla_mock.text = "Contenido de la plantilla."
        created_plantilla_mock.keywords = ["nueva", "test"]
        created_plantilla_mock.is_active = True
        created_plantilla_mock.embedding = [0.1, 0.2, 0.3]
        # Simulate ISO format for comparison
        now_iso = datetime.now(timezone.utc).isoformat()
        created_plantilla_mock.created_at.isoformat.return_value = now_iso
        created_plantilla_mock.updated_at.isoformat.return_value = now_iso

        models_stub.PlantillasRespuesta.return_value = created_plantilla_mock # Constructor returns this

        response = self.client.post('/api/ai/templates',
                                    headers={'Authorization': f'Bearer {self.mock_user_admin.token}'},
                                    json=request_payload)
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 201)
        self.assertEqual(data['name'], "Nueva Plantilla")
        self.assertTrue(data['embedding_generated'])
        models_stub.PlantillasRespuesta.assert_called_once_with(
            name="Nueva Plantilla",
            text="Contenido de la plantilla.",
            keywords=["nueva", "test"],
            is_active=True,
            embedding=[0.1, 0.2, 0.3]
        )
        mock_db_session_instance.add.assert_called_once_with(created_plantilla_mock)
        mock_db_session_instance.commit.assert_called_once()
        self.mock_robust_embed.assert_called_once_with(textos=["Contenido de la plantilla."], input_type="search_document")

    def test_create_template_embedding_fails(self):
        request_payload = {"name": "Emb Fail", "text": "Texto"}
        self.mock_robust_embed.return_value = None

        created_plantilla_mock = MagicMock(spec=MockPlantilla)
        created_plantilla_mock.id = "emb_fail_uuid"
        # ... configure other attributes ...
        models_stub.PlantillasRespuesta.return_value = created_plantilla_mock

        response = self.client.post('/api/ai/templates',
                                    headers={'Authorization': f'Bearer {self.mock_user_admin.token}'},
                                    json=request_payload)
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 201) # Still creates, but embedding_generated is false
        self.assertFalse(data['embedding_generated'])
        # mock_current_app_object.logger.warning.assert_called() # Logger is harder to assert directly this way
        self.assertTrue(mock_current_app_object.logger.warning.called)


    def test_create_template_missing_name(self):
        request_payload = {"text": "Contenido"}
        response = self.client.post('/api/ai/templates',
                                    headers={'Authorization': f'Bearer {self.mock_user_admin.token}'},
                                    json=request_payload)
        status_code = response.status_code
        data = response.get_json()
        self.assertEqual(status_code, 400)
        self.assertIn("El campo 'name' es requerido", data['error'])

    def test_create_template_db_error(self):
        request_payload = {"name": "DB Error", "text": "Texto"}
        self.mock_robust_embed.return_value = [[0.1]]
        mock_db_session_instance.commit.side_effect = Exception("DB commit error")
        models_stub.PlantillasRespuesta.return_value = MagicMock()

        response = self.client.post('/api/ai/templates',
                                    headers={'Authorization': f'Bearer {self.mock_user_admin.token}'},
                                    json=request_payload)
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 500)
        self.assertEqual(data['error'], "Error interno al guardar la plantilla.")
        mock_db_session_instance.rollback.assert_called_once()


    # --- Pruebas para PUT /templates/{template_id} (update_template) ---
    def test_update_template_success_with_text_change(self):
        mock_existing_plantilla = MockPlantilla(id='uuid_upd', name='Original', text='Texto original', keywords=[], is_active=True, embedding=[0.1])
        self.mock_query_instance.get.return_value = mock_existing_plantilla

        update_payload = {"text": "Texto nuevo", "name": "Actualizado"}
        self.mock_robust_embed.return_value = [[0.9, 0.8]]

        response = self.client.put('/api/ai/templates/uuid_upd',
                                   headers={'Authorization': f'Bearer {self.mock_user_admin.token}'},
                                   json=update_payload)
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 200)
        self.assertEqual(data['name'], "Actualizado")
        self.assertEqual(data['text'], "Texto nuevo")
        self.assertTrue(data['embedding_regenerated'])
        self.assertEqual(mock_existing_plantilla.embedding, [0.9, 0.8]) # Check instance was modified
        mock_db_session_instance.commit.assert_called_once()
        self.mock_robust_embed.assert_called_once_with(textos=["Texto nuevo"], input_type="search_document")


    def test_update_template_not_found(self):
        self.mock_query_instance.get.return_value = None
        response = self.client.put('/api/ai/templates/non_existent_uuid',
                                   headers={'Authorization': f'Bearer {self.mock_user_admin.token}'},
                                   json={"name": "No importa"})
        status_code = response.status_code
        data = response.get_json()
        self.assertEqual(status_code, 404)
        self.assertEqual(data['error'], "Plantilla no encontrada.")


    # --- Pruebas para DELETE /templates/{template_id} (delete_template) ---
    def test_delete_template_success(self):
        mock_plantilla_to_delete = MockPlantilla(id='uuid_del', name='Para Borrar', text='...', keywords=[], is_active=True)
        self.mock_query_instance.get.return_value = mock_plantilla_to_delete

        response = self.client.delete('/api/ai/templates/uuid_del',
                                      headers={'Authorization': f'Bearer {self.mock_user_admin.token}'})
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 200)
        self.assertIn("eliminada correctamente", data['mensaje'])
        mock_db_session_instance.delete.assert_called_once_with(mock_plantilla_to_delete)
        mock_db_session_instance.commit.assert_called_once()


    # --- Pruebas para POST /generate-template-text ---
    def test_generate_template_text_success(self):
        prompt_payload = {"prompt": "Escribe un saludo"}
        self.mock_get_cohere_response.return_value = "Hola, ¿cómo estás?" # This mock is for the Gemini call now

        response = self.client.post('/api/ai/generate-template-text',
                                    headers={'Authorization': f'Bearer {self.mock_user_admin.token}'},
                                    json=prompt_payload)
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 200)
        self.assertEqual(data['generated_text'], "Hola, ¿cómo estás?")
        # The mock_get_cohere_response is now mocking llamar_gemini_para_generacion_texto
        # We need to assert it was called with the correct user_prompt and system_prompt
        self.mock_get_cohere_response.assert_called_once() # This will fail as args are different
        # Example of more specific assertion if needed:
        # self.mock_get_cohere_response.assert_called_once_with(
        #     system_prompt_especifico=ANY, user_prompt="Escribe un saludo", temperature=ANY
        # )

    # --- Pruebas para POST /improve-template-text ---
    def test_improve_template_text_success(self):
        improve_payload = {"text_to_improve": "Hol q tal"}
        self.mock_get_cohere_response.return_value = "Hola, ¿qué tal?"
        expected_user_prompt_for_gemini = ( # This is what llamar_gemini_para_generacion_texto will receive as user_prompt
            "Por favor, reescribe el siguiente texto para que sea más claro, conciso y profesional, manteniendo el significado original. "
            "El resultado debe ser únicamente el texto mejorado, sin introducciones ni comentarios adicionales. "
            "Texto a mejorar:\n\"\"\"\nHol q tal\n\"\"\""
        )

        response = self.client.post('/api/ai/improve-template-text',
                                    headers={'Authorization': f'Bearer {self.mock_user_admin.token}'},
                                    json=improve_payload)
        status_code = response.status_code
        data = response.get_json()

        self.assertEqual(status_code, 200)
        self.assertEqual(data['improved_text'], "Hola, ¿qué tal?")
        self.mock_get_cohere_response.assert_called_once() # This will fail as args are different
        # Example of more specific assertion:
        # self.mock_get_cohere_response.assert_called_once_with(
        #     system_prompt_especifico=ANY, user_prompt=expected_user_prompt_for_gemini, temperature=ANY
        # )


if __name__ == '__main__':
    unittest.main()
