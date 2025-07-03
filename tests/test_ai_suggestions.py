import unittest
import json
from unittest.mock import patch, MagicMock

# Asumiendo que app y db están configurados de una manera estándar accesible para las pruebas
# Esto podría necesitar ajustarse según la estructura real del proyecto de pruebas
# from app import create_app, db  # O la forma en que se crea la app para pruebas
# from models import User, PlantillasRespuesta

# Placeholder para la creación de la app y db si no se puede importar directamente
# Se necesitaría una configuración de prueba similar a otros archivos de test del proyecto.

class TestAISuggestions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Configuración inicial para todas las pruebas de la clase."""
        # cls.app = create_app(config_class='config.TestingConfig') # Ajustar según el proyecto
        # cls.client = cls.app.test_client()
        # cls.db = db # Ajustar
        # with cls.app.app_context():
        #     cls.db.create_all()
        pass # Reemplazar con la configuración real

    @classmethod
    def tearDownClass(cls):
        """Limpieza final después de todas las pruebas."""
        # with cls.app.app_context():
        #     cls.db.drop_all()
        pass # Reemplazar con la limpieza real

    def setUp(self):
        """Configuración antes de cada prueba individual."""
        # self.app_context = self.app.app_context()
        # self.app_context.push()
        # self.db.session.begin_nested() # Para rollback después de cada test

        # Mock de usuario (admin) - esto debe ajustarse a cómo se maneja la autenticación en las pruebas
        self.admin_user_mock = MagicMock()
        self.admin_user_mock.id = 1
        self.admin_user_mock.rol = 'admin'

        # Mock de plantillas
        self.plantilla1_embedding = [0.1] * 1024 # Ejemplo de embedding (longitud de Cohere v3)
        self.plantilla2_embedding = [0.2] * 1024
        self.plantilla3_embedding = [0.3] * 1024
        self.plantilla_sin_embedding = [0.4] * 1024 # Se usará para plantilla sin embedding en BD
        self.plantilla_baja_sim = [0.9] * 1024


        # Estas plantillas se crearían en la BD de prueba si estuviera disponible
        self.mock_plantillas_db = [
            {'id': 1, 'name': 'Plantilla Alta Sim', 'text': 'Texto plantilla 1', 'embedding': self.plantilla1_embedding, 'is_active': True},
            {'id': 2, 'name': 'Plantilla Media Sim', 'text': 'Texto plantilla 2', 'embedding': self.plantilla2_embedding, 'is_active': True},
            {'id': 3, 'name': 'Plantilla Inactiva', 'text': 'Texto plantilla 3', 'embedding': self.plantilla3_embedding, 'is_active': False},
            {'id': 4, 'name': 'Plantilla Sin Embedding Valido', 'text': 'Texto plantilla 4', 'embedding': None, 'is_active': True},
            {'id': 5, 'name': 'Plantilla Baja Sim', 'text': 'Texto plantilla 5', 'embedding': self.plantilla_baja_sim, 'is_active': True},
        ]

        # Mock del token_requerido y require_role para que pasen y devuelvan el usuario mock
        # Esto es crucial y debe hacerse según cómo funcionen los decoradores en el proyecto
        self.token_patcher = patch('routes.ai.token_requerido', lambda func: lambda *args, **kwargs: func(self.admin_user_mock, *args, **kwargs))
        self.role_patcher = patch('routes.ai.require_role', lambda *roles: lambda func: func) # Asume que pasa si está mockeado así

        self.mock_token_required = self.token_patcher.start()
        self.mock_require_role = self.role_patcher.start()


    def tearDown(self):
        """Limpieza después de cada prueba individual."""
        # self.db.session.rollback() # Rollback para aislar pruebas
        # self.app_context.pop()
        self.token_patcher.stop()
        self.role_patcher.stop()
        pass


    @patch('routes.ai.robust_embed')
    @patch('routes.ai.PlantillasRespuesta.query')
    def test_suggest_templates_success(self, mock_query, mock_robust_embed):
        """Prueba de éxito para sugerencia de plantillas."""

        # Configurar mocks
        # 1. Mock para robust_embed (cuando se llama para la consulta del ticket)
        mock_consulta_embedding = [0.11] * 1024 # Similar a plantilla1
        mock_robust_embed.return_value = [mock_consulta_embedding]

        # 2. Mock para PlantillasRespuesta.query.filter().all()
        # Devolver solo las plantillas relevantes para este test (activas con embedding)
        plantillas_para_db_mock = []
        for p_data in self.mock_plantillas_db:
            if p_data['is_active'] and p_data['embedding'] is not None:
                mock_p = MagicMock()
                mock_p.id = p_data['id']
                mock_p.name = p_data['name']
                mock_p.text = p_data['text']
                mock_p.embedding = p_data['embedding']
                mock_p.is_active = p_data['is_active']
                plantillas_para_db_mock.append(mock_p)

        mock_filter = MagicMock()
        mock_filter.all.return_value = plantillas_para_db_mock
        mock_query.filter.return_value = mock_filter

        # Simular llamada al endpoint (necesita el cliente de Flask configurado)
        # response = self.client.post('/api/ai/suggest-templates',
        #                             json={'asunto': 'Problema con mi cuenta', 'top_n': 2},
        #                             headers={'Authorization': 'Bearer testtoken'}) # O como se maneje el token
        # data = json.loads(response.data.decode())

        # --- Simulación sin cliente Flask (para que el código se pueda escribir) ---
        # Esto es una aproximación. Idealmente, se usaría self.client.post()
        from routes.ai import suggest_templates_route, MIN_SIMILARITY_THRESHOLD
        with patch('routes.ai.current_app.logger'): # Mockear logger para evitar errores si no está config.
            # Necesitamos simular el objeto request de Flask
            mock_request = MagicMock()
            mock_request.get_json.return_value = {'asunto': 'Problema con mi cuenta', 'top_n': 2}

            with patch('flask.request', mock_request):
                 # La ruta espera current_user como primer argumento debido al decorador mockeado
                response_tuple = suggest_templates_route(self.admin_user_mock)

            response_data_str, status_code = response_tuple
            data = json.loads(response_data_str.get_data(as_text=True))


        self.assertEqual(status_code, 200)
        self.assertIn('sugerencias', data)
        sugerencias = data['sugerencias']

        # Verificaciones (dependen de la implementación de cosine_similarity y los embeddings mock)
        # Asumiendo que plantilla1 ([0.1]*1024) es la más similar a la consulta ([0.11]*1024)
        # y plantilla2 ([0.2]*1024) es la segunda.
        # plantilla_baja_sim ([0.9]*1024) debería tener score bajo.

        # La similaridad coseno entre [0.1]*N y [0.11]*N será alta y positiva.
        # La similaridad coseno entre [0.1]*N y [0.2]*N será más baja pero aún positiva.
        # La similaridad coseno entre [0.1]*N y [0.9]*N será mucho más baja.

        # Esperamos 2 sugerencias
        self.assertEqual(len(sugerencias), 2)

        # La primera debería ser 'Plantilla Alta Sim'
        self.assertEqual(sugerencias[0]['name'], 'Plantilla Alta Sim')
        self.assertEqual(sugerencias[0]['id_plantilla'], '1')
        self.assertTrue(sugerencias[0]['score'] > MIN_SIMILARITY_THRESHOLD)

        # La segunda debería ser 'Plantilla Media Sim'
        self.assertEqual(sugerencias[1]['name'], 'Plantilla Media Sim')
        self.assertEqual(sugerencias[1]['id_plantilla'], '2')
        self.assertTrue(sugerencias[1]['score'] > MIN_SIMILARITY_THRESHOLD)

        # Asegurar que los scores están en orden descendente
        self.assertTrue(sugerencias[0]['score'] >= sugerencias[1]['score'])

        mock_robust_embed.assert_called_once_with(textos=['Problema con mi cuenta'], input_type="search_query")

    def test_suggest_templates_missing_asunto(self):
        """Prueba de error cuando falta el campo 'asunto'."""
        # response = self.client.post('/api/ai/suggest-templates', json={})
        # data = json.loads(response.data.decode())

        from routes.ai import suggest_templates_route
        with patch('routes.ai.current_app.logger'):
            mock_request = MagicMock()
            mock_request.get_json.return_value = {}
            with patch('flask.request', mock_request):
                response_tuple = suggest_templates_route(self.admin_user_mock)
            response_data_str, status_code = response_tuple
            data = json.loads(response_data_str.get_data(as_text=True))

        self.assertEqual(status_code, 400)
        self.assertIn('error', data)
        self.assertEqual(data['error'], "El campo 'asunto' es obligatorio.")

    @patch('routes.ai.robust_embed')
    @patch('routes.ai.PlantillasRespuesta.query')
    def test_suggest_templates_no_active_templates(self, mock_query, mock_robust_embed):
        """Prueba cuando no hay plantillas activas con embeddings."""
        mock_consulta_embedding = [0.11] * 1024
        mock_robust_embed.return_value = [mock_consulta_embedding]

        mock_filter = MagicMock()
        mock_filter.all.return_value = [] # No hay plantillas
        mock_query.filter.return_value = mock_filter

        from routes.ai import suggest_templates_route
        with patch('routes.ai.current_app.logger'):
            mock_request = MagicMock()
            mock_request.get_json.return_value = {'asunto': 'Test', 'top_n': 3}
            with patch('flask.request', mock_request):
                response_tuple = suggest_templates_route(self.admin_user_mock)
            response_data_str, status_code = response_tuple
            data = json.loads(response_data_str.get_data(as_text=True))

        self.assertEqual(status_code, 200)
        self.assertIn('sugerencias', data)
        self.assertEqual(len(data['sugerencias']), 0)
        self.assertIn('message', data)
        self.assertEqual(data['message'], "No hay plantillas de respuesta activas configuradas con embeddings.")

    @patch('routes.ai.robust_embed')
    def test_suggest_templates_cohere_embed_fails(self, mock_robust_embed):
        """Prueba cuando robust_embed no devuelve un embedding para la consulta."""
        mock_robust_embed.return_value = None # Falla al generar embedding

        from routes.ai import suggest_templates_route
        with patch('routes.ai.current_app.logger'):
            mock_request = MagicMock()
            mock_request.get_json.return_value = {'asunto': 'Test Cohere Falla'}
            with patch('flask.request', mock_request):
                response_tuple = suggest_templates_route(self.admin_user_mock)
            response_data_str, status_code = response_tuple
            data = json.loads(response_data_str.get_data(as_text=True))

        self.assertEqual(status_code, 500)
        self.assertIn('error', data)
        self.assertEqual(data['error'], "Error al generar el embedding para la consulta del ticket.")


# Esto es necesario si se quiere ejecutar el archivo directamente con python -m unittest tests.test_ai_suggestions
# pero usualmente se usa un test runner como 'flask test' o 'pytest'.
# if __name__ == '__main__':
#     unittest.main()

# Nota: Para que estas pruebas funcionen, se necesita:
# 1. Una forma de crear una instancia de la app Flask para pruebas (create_app).
# 2. Configuración de base de datos de prueba (TestingConfig).
# 3. Inicialización de `db` con la app de prueba.
# 4. La función `cosine_similarity` debe estar disponible y funcionar correctamente.
# 5. Los decoradores `@token_requerido` y `@require_role` deben ser mockeables o
#    se debe proveer un token válido de un usuario admin/empleado en los headers.
#    El mocking de los decoradores es una forma común de aislarlos.
#
# Debido a las limitaciones del entorno actual, no puedo ejecutar esto.
# El usuario necesitará integrar esto en su sistema de pruebas.
# Los mocks para `self.client.post` se han reemplazado con llamadas directas
# a la función de la ruta, lo cual es una simplificación y no prueba la capa HTTP completa.
# Idealmente, se usaría `self.client.post`.
