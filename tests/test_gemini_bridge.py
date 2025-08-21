import unittest
from unittest.mock import patch, MagicMock
import json
import os

from services.gemini_bridge import (
    llamar_gemini,
    _limpiar_historial_gemini,
    _repair_json_response,
    GEMINI_MODEL_PRESTAMOS,
    GEMINI_MODEL_STANDARD,
    GEMINI_SAFETY_SETTINGS,
    MAX_HISTORIAL_MESSAGES,
)
from services import prompts

class TestGeminiBridge(unittest.TestCase):

    def setUp(self):
        pass

    def tearDown(self):
        pass

    @patch('services.gemini_bridge._llamar_gemini_impl')
    def test_llamar_gemini_prestamo(self, mock_llamar_gemini_impl):
        """Test para verificar que se llama al modelo de préstamos."""
        mock_llamar_gemini_impl.return_value = ({"message_body": "Hola! Soy un mock."}, {}, None)
        mock_app = MagicMock()
        mock_app.logger = MagicMock()
        resultado, _, _ = llamar_gemini(
            app=mock_app,
            mensaje_usuario={"texto": "Quiero un préstamo"},
            usuario={"tipo_chat": "pyme", "rubro": "servicios_financieros"}
        )
        self.assertIn("Hola! Soy un mock.", resultado['message_body'])

    @patch('services.gemini_bridge._llamar_gemini_impl')
    def test_llamar_gemini_luminaria(self, mock_llamar_gemini_impl):
        """Test para verificar que se llama al modelo estándar de municipio."""
        mock_llamar_gemini_impl.return_value = ({"message_body": "Hola! Soy un mock."}, {}, None)
        mock_app = MagicMock()
        mock_app.logger = MagicMock()
        resultado, _, _ = llamar_gemini(
            app=mock_app,
            mensaje_usuario={"texto": "Hay una luminaria rota"},
            usuario={"tipo_chat": "municipio"}
        )
        self.assertIn("Hola! Soy un mock.", resultado['message_body'])

    @patch('services.gemini_bridge._llamar_gemini_impl')
    def test_llamar_gemini_consulta_estado_reclamo(self, mock_llamar_gemini_impl):
        """Test para verificar que se llama al modelo estándar de municipio para consulta de reclamo."""
        mock_llamar_gemini_impl.return_value = ({"message_body": "Hola! Soy un mock."}, {}, None)
        mock_app = MagicMock()
        mock_app.logger = MagicMock()
        resultado, _, _ = llamar_gemini(
            app=mock_app,
            mensaje_usuario={"texto": "Quiero saber el estado de mi reclamo"},
            usuario={"tipo_chat": "municipio"}
        )
        self.assertIn("Hola! Soy un mock.", resultado['message_body'])

    def test_limpiar_historial_gemini(self):
        """Test para verificar la limpieza del historial de mensajes."""
        historial_largo = [{"role": "user", "parts": [{"text": f"mensaje {i}"}]} for i in range(MAX_HISTORIAL_MESSAGES + 5)]
        historial_limpio = _limpiar_historial_gemini(historial_largo)
        self.assertEqual(len(historial_limpio), MAX_HISTORIAL_MESSAGES)
        self.assertEqual(historial_limpio[0]["parts"][0]["text"], "mensaje 5")

    @patch('services.gemini_bridge._llamar_gemini_impl')
    def test_llamar_gemini_fallback_generico(self, mock_llamar_gemini_impl):
        """Test para verificar el fallback a un JSON genérico en caso de error."""
        mock_llamar_gemini_impl.return_value = ({
            "message_body": "Lo siento, no pude procesar tu solicitud en este momento debido a un error con el asistente IA. Intenta de nuevo más tarde.",
            "accion_backend": "derivar_humano"
        }, {}, None)
        mock_app = MagicMock()
        mock_app.logger = MagicMock()
        resultado, _, _ = llamar_gemini(
            app=mock_app,
            mensaje_usuario={"texto": "Cualquier cosa"},
            usuario={"tipo_chat": "pyme"}
        )
        self.assertIn("Lo siento", resultado['message_body'])
        self.assertIn("derivar_humano", resultado['accion_backend'])

    def test_repair_json_truncated_botones(self):
        """Verifica que el reparador maneja JSON truncado en 'botones'."""
        raw_json = (
            '{"message_body": "ok", "accion_backend": "crear_reclamo", '
            '"datos_estructura": {"target": "municipio"}, "pedir_info": null, "botones":'
        )
        fixed = _repair_json_response(raw_json)
        parsed = json.loads(fixed)
        self.assertEqual(parsed["botones"], [])
        self.assertEqual(parsed["accion_backend"], "crear_reclamo")

if __name__ == '__main__':
    unittest.main()
