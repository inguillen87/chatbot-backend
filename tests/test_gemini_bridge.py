import unittest
import json
from unittest.mock import patch, MagicMock
import os
from services.gemini_bridge import llamar_gemini, JULES_SYSTEM_PROMPT

# Helper to create a mock Gemini response object
def create_mock_gemini_response(json_string_payload: str):
    mock_response = MagicMock()
    mock_candidate = MagicMock()
    mock_part = MagicMock()
    mock_part.text = json_string_payload
    mock_candidate.content.parts = [mock_part]
    mock_response.candidates = [mock_candidate]
    # Mock the prompt_feedback attribute for safety, in case it's accessed in error paths
    mock_response.prompt_feedback = MagicMock()
    mock_response.prompt_feedback.block_reason = None
    mock_response.prompt_feedback.block_reason_message = None
    return mock_response

class TestGeminiBridge(unittest.TestCase):

    @patch('services.gemini_bridge.os.environ.get')
    @patch('vertexai.init')
    @patch('vertexai.generative_models.GenerativeModel') # Patch the class
    def test_llamar_gemini_prestamo(self, mock_generative_model_class, mock_vertex_init, mock_os_environ_get):
        def environ_get_side_effect(key, default=None):
            if key == "GOOGLE_PROJECT_ID": return "test-project-id"
            if key == "GOOGLE_LOCATION": return "us-central1"
            return default
        mock_os_environ_get.side_effect = environ_get_side_effect

        mensaje_usuario = "necesito un préstamo para mi emprendimiento"
        usuario_info = {"nombre": "Emprendedor Test", "tipo_entidad": "pyme"}
        historial = []

        mock_response_json = {
            "respuesta_usuario": "Claro, te ayudaré con tu préstamo (real). ¿Monto y destino?",
            "accion_backend": "consulta_credito",
            "datos_estructura": {"categoria": "Crédito PyME", "descripcion": mensaje_usuario, "usuario": "Emprendedor Test", "target": "pyme"},
            "pedir_info": "monto",
            "botones": [ { "texto": "Solicitar préstamo" } ]
        }
        mock_model_instance = mock_generative_model_class.return_value
        mock_model_instance.generate_content.return_value = create_mock_gemini_response(json.dumps(mock_response_json))

        respuesta = llamar_gemini(mensaje_usuario, usuario_info, historial)

        mock_vertex_init.assert_called_once_with(project="test-project-id", location="us-central1")
        mock_generative_model_class.assert_called_once()
        mock_model_instance.generate_content.assert_called_once()
        self.assertIn("respuesta_usuario", respuesta)
        self.assertEqual(respuesta["accion_backend"], "consulta_credito")
        self.assertEqual(respuesta["datos_estructura"]["target"], "pyme")
        self.assertEqual(respuesta["datos_estructura"]["usuario"], "Emprendedor Test")
        self.assertEqual(respuesta["pedir_info"], "monto")
        self.assertIn("Claro, te ayudaré con tu préstamo (real)", respuesta["respuesta_usuario"])

    @patch('services.gemini_bridge.os.environ.get')
    @patch('vertexai.init')
    @patch('vertexai.generative_models.GenerativeModel')
    def test_llamar_gemini_luminaria(self, mock_generative_model_class, mock_vertex_init, mock_os_environ_get):
        def environ_get_side_effect(key, default=None):
            if key == "GOOGLE_PROJECT_ID": return "test-project-id"
            if key == "GOOGLE_LOCATION": return "us-central1"
            return default
        mock_os_environ_get.side_effect = environ_get_side_effect

        mensaje_usuario = "se quemó la luz en la calle Falsa 123"
        usuario_info = {"nombre": "Vecino Test", "tipo_entidad": "municipio", "contacto": {"telefono": "123456789"}}
        historial = [{"role": "user", "parts": [{"text": "Hola"}]}, {"role": "model", "parts": [{"text": "Hola Vecino Test"}]}]

        mock_response_json = {
            "respuesta_usuario": "Registré tu reclamo por luminaria (real).",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {"categoria": "Alumbrado Público", "descripcion": mensaje_usuario, "ubicacion": "calle Falsa 123", "usuario": "Vecino Test", "telefono": "123456789", "target": "municipio"},
            "pedir_info": None,
            "botones": [ { "texto": "Consultar estado" } ]
        }
        mock_model_instance = mock_generative_model_class.return_value
        mock_model_instance.generate_content.return_value = create_mock_gemini_response(json.dumps(mock_response_json))

        respuesta = llamar_gemini(mensaje_usuario, usuario_info, historial)

        mock_vertex_init.assert_called_once_with(project="test-project-id", location="us-central1")
        mock_generative_model_class.assert_called_once()
        mock_model_instance.generate_content.assert_called_once()
        self.assertEqual(respuesta["accion_backend"], "crear_reclamo")
        self.assertEqual(respuesta["datos_estructura"]["categoria"], "Alumbrado Público")
        self.assertIn("Registré tu reclamo por luminaria (real)", respuesta["respuesta_usuario"])

    @patch('services.gemini_bridge.os.environ.get')
    @patch('vertexai.init')
    @patch('vertexai.generative_models.GenerativeModel')
    def test_llamar_gemini_consulta_estado_reclamo(self, mock_generative_model_class, mock_vertex_init, mock_os_environ_get):
        def environ_get_side_effect(key, default=None):
            if key == "GOOGLE_PROJECT_ID": return "test-project-id"
            if key == "GOOGLE_LOCATION": return "us-central1"
            return default
        mock_os_environ_get.side_effect = environ_get_side_effect

        mensaje_usuario = "quiero saber el estado de mi reclamo"
        usuario_info = {"nombre": "Consultador Test", "tipo_entidad": "municipio"}
        historial = []
        mock_response_json = {
            "respuesta_usuario": "Para consultar el estado de tu reclamo (real), necesito el número de ticket.",
            "accion_backend": "consulta_estado_ticket",
            "datos_estructura": {"target": "municipio"},
            "pedir_info": "id_reclamo",
            "botones": []
        }
        mock_model_instance = mock_generative_model_class.return_value
        mock_model_instance.generate_content.return_value = create_mock_gemini_response(json.dumps(mock_response_json))

        respuesta = llamar_gemini(mensaje_usuario, usuario_info, historial)

        mock_vertex_init.assert_called_once_with(project="test-project-id", location="us-central1")
        mock_generative_model_class.assert_called_once()
        mock_model_instance.generate_content.assert_called_once()
        self.assertEqual(respuesta["accion_backend"], "consulta_estado_ticket")
        self.assertEqual(respuesta["pedir_info"], "id_reclamo")
        self.assertIn("Para consultar el estado de tu reclamo (real)", respuesta["respuesta_usuario"])

    @patch('services.gemini_bridge.os.environ.get')
    @patch('vertexai.init')
    @patch('vertexai.generative_models.GenerativeModel')
    def test_llamar_gemini_fallback_generico(self, mock_generative_model_class, mock_vertex_init, mock_os_environ_get):
        # Test 1: GOOGLE_PROJECT_ID is None (should hit EnvironmentError fallback)
        def environ_get_side_effect_no_project(key, default=None):
            if key == "GOOGLE_PROJECT_ID": return None
            if key == "GOOGLE_LOCATION": return "us-central1"
            return default
        mock_os_environ_get.side_effect = environ_get_side_effect_no_project

        mensaje_usuario = "información sobre mariposas"
        usuario_info = {"nombre": "Curioso Test", "tipo_entidad": "municipio"}
        historial = []

        respuesta_error_env = llamar_gemini(mensaje_usuario, usuario_info, historial)
        self.assertEqual(respuesta_error_env["accion_backend"], "derivar_humano")
        self.assertIn("Error de configuración del servicio de IA (entorno)", respuesta_error_env["respuesta_usuario"])
        self.assertIn("GOOGLE_PROJECT_ID no configurado", respuesta_error_env["datos_estructura"]["error_detalle"])
        self.assertIsNone(respuesta_error_env["pedir_info"]) # Check pedir_info for error case

        # Reset mocks for the next path in the same test
        mock_os_environ_get.reset_mock()
        mock_vertex_init.reset_mock()
        mock_generative_model_class.reset_mock() # Reset the class mock
        mock_model_instance = mock_generative_model_class.return_value # Get a fresh instance for the next call
        mock_model_instance.generate_content.reset_mock()


        # Test 2: GOOGLE_PROJECT_ID is set, but Gemini returns a generic/unhelpful response
        def environ_get_side_effect_with_project(key, default=None):
            if key == "GOOGLE_PROJECT_ID": return "test-project-id"
            if key == "GOOGLE_LOCATION": return "us-central1"
            return default
        mock_os_environ_get.side_effect = environ_get_side_effect_with_project

        mock_response_json_fallback = {
            "respuesta_usuario": "No sé de qué hablas.",
            "accion_backend": "derivar_humano",
            "datos_estructura": {"target": "municipio"},
            "pedir_info": "aclaracion",
            "botones": []
        }
        # mock_model_instance is already mock_generative_model_class.return_value
        mock_model_instance.generate_content.return_value = create_mock_gemini_response(json.dumps(mock_response_json_fallback))

        respuesta = llamar_gemini(mensaje_usuario, usuario_info, historial)

        mock_vertex_init.assert_called_with(project="test-project-id", location="us-central1")
        mock_generative_model_class.assert_called_with("gemini-1.5-flash-preview-0514", system_instruction=[JULES_SYSTEM_PROMPT])
        mock_model_instance.generate_content.assert_called_once()
        self.assertEqual(respuesta["accion_backend"], "derivar_humano")
        self.assertEqual(respuesta["pedir_info"], "aclaracion")
        self.assertEqual(respuesta["datos_estructura"]["target"], "municipio")
        self.assertIn("No sé de qué hablas.", respuesta["respuesta_usuario"])

    def test_jules_system_prompt_presente_y_valido(self):
        self.assertTrue(isinstance(JULES_SYSTEM_PROMPT, str))
        self.assertTrue(len(JULES_SYSTEM_PROMPT) > 100)

if __name__ == '__main__':
    unittest.main()
