import unittest
from unittest.mock import patch, MagicMock
import json
import os

from services.gemini_bridge import llamar_gemini, _limpiar_historial_gemini, GEMINI_MODEL_PRESTAMOS, GEMINI_MODEL_STANDARD, GEMINI_SAFETY_SETTINGS, MAX_HISTORIAL_MESSAGES
from services import prompts

class TestGeminiBridge(unittest.TestCase):

    def setUp(self):
        pass

    def tearDown(self):
        pass

    @patch('services.gemini_bridge._llamar_gemini_impl')
    def test_llamar_gemini_prestamo(self, mock_llamar_gemini_impl):
        """Test para verificar que se llama al modelo de préstamos."""
        mock_llamar_gemini_impl.return_value = {"respuesta_usuario": "Hola! Soy un mock."}
        resultado = llamar_gemini(
            mensaje_usuario={"texto": "Quiero un préstamo"},
            usuario={"tipo_chat": "pyme", "rubro": "servicios_financieros"}
        )
        self.assertIn("Hola! Soy un mock.", resultado['respuesta_usuario'])

    @patch('services.gemini_bridge._llamar_gemini_impl')
    def test_llamar_gemini_luminaria(self, mock_llamar_gemini_impl):
        """Test para verificar que se llama al modelo estándar de municipio."""
        mock_llamar_gemini_impl.return_value = {"respuesta_usuario": "Hola! Soy un mock."}
        resultado = llamar_gemini(
            mensaje_usuario={"texto": "Hay una luminaria rota"},
            usuario={"tipo_chat": "municipio"}
        )
        self.assertIn("Hola! Soy un mock.", resultado['respuesta_usuario'])

    @patch('services.gemini_bridge._llamar_gemini_impl')
    def test_llamar_gemini_consulta_estado_reclamo(self, mock_llamar_gemini_impl):
        """Test para verificar que se llama al modelo estándar de municipio para consulta de reclamo."""
        mock_llamar_gemini_impl.return_value = {"respuesta_usuario": "Hola! Soy un mock."}
        resultado = llamar_gemini(
            mensaje_usuario={"texto": "Quiero saber el estado de mi reclamo"},
            usuario={"tipo_chat": "municipio"}
        )
        self.assertIn("Hola! Soy un mock.", resultado['respuesta_usuario'])

    def test_limpiar_historial_gemini(self):
        """Test para verificar la limpieza del historial de mensajes."""
        historial_largo = [{"role": "user", "parts": [{"text": f"mensaje {i}"}]} for i in range(MAX_HISTORIAL_MESSAGES + 5)]
        historial_limpio = _limpiar_historial_gemini(historial_largo)
        self.assertEqual(len(historial_limpio), MAX_HISTORIAL_MESSAGES)
        self.assertEqual(historial_limpio[0]["parts"][0]["text"], "mensaje 5")

    @patch('services.gemini_bridge._llamar_gemini_impl')
    def test_llamar_gemini_fallback_generico(self, mock_llamar_gemini_impl):
        """Test para verificar el fallback a un JSON genérico en caso de error."""
        mock_llamar_gemini_impl.return_value = {
            "respuesta_usuario": "Lo siento, no pude procesar tu solicitud en este momento debido a un error con el asistente IA. Intenta de nuevo más tarde.",
            "accion_backend": "derivar_humano"
        }
        resultado = llamar_gemini(
            mensaje_usuario={"texto": "Cualquier cosa"},
            usuario={"tipo_chat": "pyme"}
        )
        self.assertIn("Lo siento", resultado['respuesta_usuario'])
        self.assertIn("derivar_humano", resultado['accion_backend'])

if __name__ == '__main__':
    unittest.main()
