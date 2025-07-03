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
mock_cohere_module = MagicMock()
sys.modules['cohere'] = mock_cohere_module
sys.modules.setdefault('services.cohere_ai', MagicMock())


# Mockear modelos y db ANTES de importar las rutas
mock_db_session_instance = MagicMock()
models_stub = ModuleType('models')
models_stub.PlantillasRespuesta = MagicMock(spec_set=True) # spec_set para mayor rigor en los mocks
models_stub.User = MagicMock(spec_set=True)
models_stub.db = SimpleNamespace(session=mock_db_session_instance)
sys.modules['models'] = models_stub
sys.modules['extensions'] = MagicMock() # extensions.db también es usado

# Ahora se puede importar el blueprint y sus funciones
from routes.ai_templates import ai_templates_bp, get_all_templates, create_template, update_template, delete_template, generate_template_text_from_prompt, improve_template_text

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
        # Resetear mocks antes de cada prueba
        mock_db_session_instance.reset_mock()
        models_stub.PlantillasRespuesta.reset_mock()
        models_stub.User.reset_mock()

        # Configurar el mock de PlantillasRespuesta.query para que devuelva mocks útiles
        self.mock_query_instance = MagicMock()
        models_stub.PlantillasRespuesta.query = self.mock_query_instance

        # Mockear servicios de Cohere (ya mockeados al inicio del archivo a nivel de módulo)
        self.mock_robust_embed = sys.modules['services.cohere_ai'].robust_embed
        self.mock_get_cohere_response = sys.modules['services.cohere_ai'].get_cohere_response
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
        self.patch_jsonify = patch('routes.ai_templates.jsonify', lambda data: (data, 200)) # Default 200
        self.mock_jsonify = self.patch_jsonify.start()


    def tearDown(self):
        self.patch_current_app.stop()
        self.patch_request.stop()
        self.patch_jsonify.stop()
        sys.modules['services.cohere_ai'].reset_mock()

    # --- Pruebas para GET /templates ---
    def test_get_all_templates_success(self):
        mock_plantilla_1 = MockPlantilla(id='uuid1', name='Saludo', text='Hola', keywords=['saludo'], is_active=True)
        mock_plantilla_2 = MockPlantilla(id='uuid2', name='Despedida', text='Adiós', keywords=['fin'], is_active=False)
        self.mock_query_instance.order_by.return_value.all.return_value = [mock_plantilla_1, mock_plantilla_2]

        self.mock_jsonify.side_effect = lambda data: (data, 200)

        response, status_code = get_all_templates(self.mock_user_admin)

        self.assertEqual(status_code, 200)
        self.assertIn('plantillas', response)
        self.assertEqual(len(response['plantillas']), 2)
        self.assertEqual(response['plantillas'][0]['name'], 'Saludo')
        self.assertEqual(response['plantillas'][1]['is_active'], False)
        self.mock_query_instance.order_by.assert_called_once()
        self.mock_query_instance.order_by.return_value.all.assert_called_once()

    def test_get_all_templates_empty(self):
        self.mock_query_instance.order_by.return_value.all.return_value = []
        self.mock_jsonify.side_effect = lambda data: (data, 200)

        response, status_code = get_all_templates(self.mock_user_admin)
        self.assertEqual(status_code, 200)
        self.assertIn('plantillas', response)
        self.assertEqual(len(response['plantillas']), 0)

    def test_get_all_templates_exception(self):
        self.mock_query_instance.order_by.return_value.all.side_effect = Exception("DB Error")
        self.mock_jsonify.side_effect = lambda data: (data, 500)

        response, status_code = get_all_templates(self.mock_user_admin)
        self.assertEqual(status_code, 500)
        self.assertIn('error', response)
        self.assertEqual(response['error'], "Error interno al obtener las plantillas.")
        mock_current_app_object.logger.error.assert_called_once()

    # --- Pruebas para POST /templates (create_template) ---
    def test_create_template_success(self):
        mock_request_object.get_json.return_value = {
            "name": "Nueva Plantilla",
            "text": "Contenido de la plantilla.",
            "keywords": ["nueva", "test"],
            "is_active": True
        }
        self.mock_robust_embed.return_value = [[0.1, 0.2, 0.3]] # Simula embedding exitoso

        # Mockear la instancia de PlantillasRespuesta que se crea
        created_plantilla_mock = MagicMock(spec=MockPlantilla)
        created_plantilla_mock.id = "new_uuid"
        created_plantilla_mock.name = "Nueva Plantilla"
        created_plantilla_mock.text = "Contenido de la plantilla."
        created_plantilla_mock.keywords = ["nueva", "test"]
        created_plantilla_mock.is_active = True
        created_plantilla_mock.embedding = [0.1, 0.2, 0.3]
        created_plantilla_mock.created_at = datetime.now(timezone.utc)
        created_plantilla_mock.updated_at = datetime.now(timezone.utc)
        # Hacer que el constructor de PlantillasRespuesta devuelva este mock
        models_stub.PlantillasRespuesta.return_value = created_plantilla_mock

        self.mock_jsonify.side_effect = lambda data: (data, 201)

        response, status_code = create_template(self.mock_user_admin)

        self.assertEqual(status_code, 201)
        self.assertEqual(response['name'], "Nueva Plantilla")
        self.assertTrue(response['embedding_generated'])
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
        mock_request_object.get_json.return_value = {"name": "Emb Fail", "text": "Texto"}
        self.mock_robust_embed.return_value = None # Simula fallo de embedding

        created_plantilla_mock = MagicMock(spec=MockPlantilla)
        created_plantilla_mock.id = "emb_fail_uuid"
        # ... (otros atributos)
        models_stub.PlantillasRespuesta.return_value = created_plantilla_mock
        self.mock_jsonify.side_effect = lambda data: (data, 201)

        response, status_code = create_template(self.mock_user_admin)

        self.assertEqual(status_code, 201)
        self.assertFalse(response['embedding_generated'])
        mock_current_app_object.logger.warning.assert_called()
        models_stub.PlantillasRespuesta.assert_called_once_with(
            name="Emb Fail", text="Texto", keywords=[], is_active=True, embedding=None
        )

    def test_create_template_missing_name(self):
        mock_request_object.get_json.return_value = {"text": "Contenido"}
        self.mock_jsonify.side_effect = lambda data: (data, 400)
        response, status_code = create_template(self.mock_user_admin)
        self.assertEqual(status_code, 400)
        self.assertIn("El campo 'name' es requerido", response['error'])

    def test_create_template_db_error(self):
        mock_request_object.get_json.return_value = {"name": "DB Error", "text": "Texto"}
        self.mock_robust_embed.return_value = [[0.1]]
        mock_db_session_instance.commit.side_effect = Exception("DB commit error")

        # Mockear la instancia para que la llamada al constructor no falle antes del commit
        models_stub.PlantillasRespuesta.return_value = MagicMock()
        self.mock_jsonify.side_effect = lambda data: (data, 500)

        response, status_code = create_template(self.mock_user_admin)
        self.assertEqual(status_code, 500)
        self.assertEqual(response['error'], "Error interno al guardar la plantilla.")
        mock_db_session_instance.rollback.assert_called_once()

    # TODO: Más pruebas para create_template (keywords inválidas, is_active inválido, etc.)

    # --- Pruebas para PUT /templates/{template_id} (update_template) ---
    def test_update_template_success_with_text_change(self):
        mock_existing_plantilla = MockPlantilla(id='uuid_upd', name='Original', text='Texto original', keywords=[], is_active=True, embedding=[0.1])
        self.mock_query_instance.get.return_value = mock_existing_plantilla

        mock_request_object.get_json.return_value = {"text": "Texto nuevo", "name": "Actualizado"}
        self.mock_robust_embed.return_value = [[0.9, 0.8]] # Nuevo embedding
        self.mock_jsonify.side_effect = lambda data: (data, 200)

        response, status_code = update_template(self.mock_user_admin, 'uuid_upd')

        self.assertEqual(status_code, 200)
        self.assertEqual(response['name'], "Actualizado")
        self.assertEqual(response['text'], "Texto nuevo")
        self.assertTrue(response['embedding_regenerated'])
        self.assertEqual(mock_existing_plantilla.embedding, [0.9, 0.8])
        mock_db_session_instance.commit.assert_called_once()
        self.mock_robust_embed.assert_called_once_with(textos=["Texto nuevo"], input_type="search_document")
        # Verificar que flag_modified fue llamado para embedding
        mock_db_session_instance.add.assert_not_called() # No se añade, se modifica
        # Para verificar flag_modified, necesitaríamos mockearlo desde sqlalchemy.orm.attributes
        # from sqlalchemy.orm.attributes import flag_modified (esto estaría en el módulo de rutas)
        # con patch('routes.ai_templates.flag_modified') as mock_flag_modified:
        #    ...
        #    mock_flag_modified.assert_any_call(mock_existing_plantilla, "embedding")

    def test_update_template_not_found(self):
        self.mock_query_instance.get.return_value = None
        self.mock_jsonify.side_effect = lambda data: (data, 404)
        response, status_code = update_template(self.mock_user_admin, 'non_existent_uuid')
        self.assertEqual(status_code, 404)
        self.assertEqual(response['error'], "Plantilla no encontrada.")

    # TODO: Más pruebas para update_template (sin cambios, cambio solo keywords, error de embedding, etc.)

    # --- Pruebas para DELETE /templates/{template_id} (delete_template) ---
    def test_delete_template_success(self):
        mock_plantilla_to_delete = MockPlantilla(id='uuid_del', name='Para Borrar', text='...', keywords=[], is_active=True)
        self.mock_query_instance.get.return_value = mock_plantilla_to_delete
        self.mock_jsonify.side_effect = lambda data: (data, 200)

        response, status_code = delete_template(self.mock_user_admin, 'uuid_del')

        self.assertEqual(status_code, 200)
        self.assertIn("eliminada correctamente", response['mensaje'])
        mock_db_session_instance.delete.assert_called_once_with(mock_plantilla_to_delete)
        mock_db_session_instance.commit.assert_called_once()

    # TODO: Pruebas para delete_template (no encontrada, error DB)

    # --- Pruebas para POST /generate-template-text ---
    def test_generate_template_text_success(self):
        mock_request_object.get_json.return_value = {"prompt": "Escribe un saludo"}
        self.mock_get_cohere_response.return_value = "Hola, ¿cómo estás?"
        self.mock_jsonify.side_effect = lambda data: (data, 200)

        response, status_code = generate_template_text_from_prompt(self.mock_user_admin)

        self.assertEqual(status_code, 200)
        self.assertEqual(response['generated_text'], "Hola, ¿cómo estás?")
        self.mock_get_cohere_response.assert_called_once_with(message="Escribe un saludo")

    # TODO: Pruebas para generate_template_text (prompt vacío, error Cohere, prompt muy largo)

    # --- Pruebas para POST /improve-template-text ---
    def test_improve_template_text_success(self):
        mock_request_object.get_json.return_value = {"text_to_improve": "Hol q tal"}
        self.mock_get_cohere_response.return_value = "Hola, ¿qué tal?"
        expected_prompt_to_cohere = (
            "Por favor, reescribe el siguiente texto para que sea más claro, conciso y profesional, manteniendo el significado original. "
            "El resultado debe ser únicamente el texto mejorado, sin introducciones ni comentarios adicionales. "
            "Texto a mejorar:\n\"\"\"\nHol q tal\n\"\"\""
        )
        self.mock_jsonify.side_effect = lambda data: (data, 200)

        response, status_code = improve_template_text(self.mock_user_admin)

        self.assertEqual(status_code, 200)
        self.assertEqual(response['improved_text'], "Hola, ¿qué tal?")
        self.mock_get_cohere_response.assert_called_once_with(message=expected_prompt_to_cohere)

    # TODO: Pruebas para improve_template_text (texto vacío, error Cohere, texto muy largo)

if __name__ == '__main__':
    unittest.main()
