import unittest
import json
from services.gemini_bridge import llamar_gemini, JULES_SYSTEM_PROMPT

class TestGeminiBridge(unittest.TestCase):

    def test_llamar_gemini_prestamo(self):
        mensaje_usuario = "necesito un préstamo para mi emprendimiento"
        usuario_info = {"nombre": "Emprendedor Test", "tipo_entidad": "pyme"}
        historial = []

        respuesta = llamar_gemini(mensaje_usuario, usuario_info, historial)

        self.assertIn("respuesta_usuario", respuesta)
        self.assertEqual(respuesta["accion_backend"], "consulta_credito")
        self.assertIn("datos_estructura", respuesta)
        self.assertEqual(respuesta["datos_estructura"]["target"], "pyme")
        self.assertEqual(respuesta["datos_estructura"]["usuario"], "Emprendedor Test")
        self.assertEqual(respuesta["pedir_info"], "monto")
        self.assertTrue(len(respuesta["botones"]) > 0)
        self.assertIn("Te ayudo a gestionar tu pedido de crédito", respuesta["respuesta_usuario"])

    def test_llamar_gemini_luminaria(self):
        mensaje_usuario = "se quemó la luz en la calle Falsa 123"
        usuario_info = {"nombre": "Vecino Test", "tipo_entidad": "municipio", "contacto": {"telefono": "123456789"}}
        historial = [{"role": "user", "parts": [{"text": "Hola"}]}, {"role": "model", "parts": [{"text": "Hola Vecino Test"}]}]

        respuesta = llamar_gemini(mensaje_usuario, usuario_info, historial)

        self.assertIn("respuesta_usuario", respuesta)
        self.assertEqual(respuesta["accion_backend"], "crear_reclamo")
        self.assertIn("datos_estructura", respuesta)
        self.assertEqual(respuesta["datos_estructura"]["target"], "municipio")
        self.assertEqual(respuesta["datos_estructura"]["categoria"], "Alumbrado Público")
        self.assertEqual(respuesta["datos_estructura"]["usuario"], "Vecino Test")
        self.assertEqual(respuesta["datos_estructura"]["telefono"], "123456789")
        self.assertIsNone(respuesta["pedir_info"])
        self.assertTrue(len(respuesta["botones"]) > 0)
        self.assertIn("Registré tu reclamo por luminaria quemada", respuesta["respuesta_usuario"])

    def test_llamar_gemini_consulta_estado_reclamo(self):
        mensaje_usuario = "quiero saber el estado de mi reclamo"
        usuario_info = {"nombre": "Consultador Test", "tipo_entidad": "municipio"}
        historial = []

        respuesta = llamar_gemini(mensaje_usuario, usuario_info, historial)

        self.assertEqual(respuesta["accion_backend"], "consulta_estado_reclamo")
        self.assertEqual(respuesta["pedir_info"], "id_reclamo")
        self.assertIn("Para consultar el estado de tu reclamo", respuesta["respuesta_usuario"])

    def test_llamar_gemini_fallback_generico(self):
        mensaje_usuario = "información sobre mariposas"
        usuario_info = {"nombre": "Curioso Test", "tipo_entidad": "municipio"}
        historial = []

        respuesta = llamar_gemini(mensaje_usuario, usuario_info, historial)

        self.assertEqual(respuesta["accion_backend"], "derivar_humano")
        self.assertEqual(respuesta["pedir_info"], "aclaracion")
        self.assertEqual(respuesta["datos_estructura"]["target"], "municipio") # Default
        self.assertIn("No entendí bien tu consulta", respuesta["respuesta_usuario"])

    def test_jules_system_prompt_presente_y_valido(self):
        # Solo una verificación básica de que el prompt existe y es un string no vacío.
        self.assertTrue(isinstance(JULES_SYSTEM_PROMPT, str))
        self.assertTrue(len(JULES_SYSTEM_PROMPT) > 100) # Asumimos que un prompt real tiene cierta longitud
        # Podríamos incluso intentar parsear los ejemplos JSON dentro del prompt si quisiéramos ser más exhaustivos
        # pero para una prueba unitaria del bridge, esto es suficiente.

if __name__ == '__main__':
    unittest.main()
