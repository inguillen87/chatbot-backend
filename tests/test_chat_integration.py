import unittest
import json
from unittest.mock import patch, MagicMock
from app import create_app, db
from models import User, Rubro, ArchivoAdjunto, AnalisisArchivo, ChatSessionContext
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    CELERY_TASK_ALWAYS_EAGER = True

class TestChatIntegration(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        rubro = Rubro(nombre="municipio", clave="municipio")
        rubro.es_publico = True
        db.session.add(rubro)
        db.session.commit()

        self.test_user = User(
            name="Test User",
            email="test@example.com",
            password_hash="password",
            rubro=rubro,
            tipo_chat="municipio",
        )
        db.session.add(self.test_user)

        # Add a default municipality user for anonymous tests
        self.default_municipio_user = User(
            name="Default Municipio",
            email="municipio@default.com",
            password_hash="password",
            rubro=rubro,
            tipo_chat="municipio",
            rol="admin"
        )
        db.session.add(self.default_municipio_user)
        db.session.commit()
        self.auth_headers = {'Authorization': f'Bearer {self.test_user.token}'}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    # @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat')
    # def test_chat_con_imagen_reclamo_exitoso(self, mock_interpretar_imagen_para_chat):
    #     mock_interpretar_imagen_para_chat.return_value = {
    #         'es_reclamo': True,
    #         'categoria_sugerida': 'Alumbrado Público',
    #         'descripcion_sugerida': 'Parece una luminaria rota en la calle.',
    #         'analisis_id': 1,
    #         'texto_ocr': 'Luz rota poste 123',
    #         'analisis_interno': {
    #             'tipo_analisis_sugerido': 'reclamo_municipal',
    #             'vision_inferred_category': 'Alumbrado Público',
    #             'llm_complaint_extraction_from_image': {
    #                 'tipo_problema': 'Alumbrado Público',
    #                 'descripcion_problema': 'Parece una luminaria rota en la calle.'
    #             }
    #         }
    #     }

    #     archivo_adj = ArchivoAdjunto(
    #         user_id=self.test_user.id,
    #         filename="test_luminaria.jpg",
    #         nombre_original="luminaria.jpg",
    #         url="/archivos/test_luminaria.jpg",
    #         mime="image/jpeg",
    #         tamano=12345,
    #         tipo="chat"
    #     )
    #     db.session.add(archivo_adj)
    #     db.session.commit()
    #     archivo_id = archivo_adj.id

    #     from services.tasks import tarea_analizar_contenido_archivo
    #     with self.app.app_context():
    #         tarea_analizar_contenido_archivo(archivo_id)

    #     analisis_obj = AnalisisArchivo.query.filter_by(archivo_adjunto_id=archivo_id).first()
    #     self.assertIsNotNone(analisis_obj)
    #     self.assertEqual(analisis_obj.estado_analisis, "completado")
    #     self.assertEqual(analisis_obj.tipo_analisis, "reclamo_municipal")
    #     self.assertEqual(json.loads(analisis_obj.datos_estructurados).get("llm_complaint_extraction_from_image").get("tipo_problema"), "Alumbrado Público")

    #     chat_payload = {
    #         "pregunta": "Adjunté una foto de un problema.",
    #         "tipo_chat": "municipio",
    #         "rubro_clave": "municipio",
    #         "uploaded_file_info": {
    #             "id": archivo_id,
    #             "name": "luminaria.jpg",
    #             "url": "/archivos/test_luminaria.jpg"
    #         }
    #     }

    #     response = self.client.post('/ask/municipio', json=chat_payload, headers=self.auth_headers)
    #     self.assertEqual(response.status_code, 200)
    #     data = response.get_json()

    #     self.assertIn("He analizado la imagen que subiste.", data["message_body"])
    #     self.assertIn("Parece ser un problema de 'Alumbrado Público'", data["message_body"])
    #     self.assertIn("Parece una luminaria rota en la calle.", data["message_body"])
    #     self.assertIn("¿Es esto correcto?", data["message_body"])

    #     self.assertTrue(any(b["texto"] == "Sí, es correcto" for b in data.get("options_list", [])))
    #     self.assertTrue(any(b["texto"] == "No, quiero describirlo yo" for b in data.get("options_list", [])))

    #     contexto_actualizado = data.get("contexto_actualizado", {}).get("contexto_municipio", {})
    #     self.assertEqual(contexto_actualizado.get("estado_conversacion"), "ESPERANDO_CONFIRMACION_RECLAMO_IMAGEN")
    #     self.assertEqual(contexto_actualizado.get("tipo_sugerido_imagen"), "Alumbrado Público")
    #     self.assertEqual(contexto_actualizado.get("archivo_id_reclamo_actual"), archivo_id)

    def test_anonymous_chat_municipio_loads_default_owner(self):
        """
        Tests that an anonymous request to /ask/municipio
        successfully loads a default owner user and returns a valid response.
        This test now also verifies the new keyword-based GreetingHandler.
        """
        chat_payload = {
            "pregunta": "Hola",
            "tipo_chat": "municipio",
        }

        # Note: No auth headers are sent. The new logic should catch "Hola"
        # and trigger the GreetingHandler directly, bypassing the LLM.
        response = self.client.post('/ask/municipio', json=chat_payload)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        # Check that we don't get a JSON error response.
        self.assertNotIn("error", data)

        # Check for the new, enhanced welcome message from GreetingHandler.
        # The response format from responder_municipio wraps the handler's response.
        # The key for the text is 'respuesta_usuario' inside the handler's dict.
        self.assertIn("Soy JUNI, tu Asistente Virtual", data.get("respuesta_usuario", ""))
        self.assertIn("Mandar una foto", data.get("respuesta_usuario", "")) # Check for feature explanation
        self.assertIsNotNone(data.get("categorias")) # Check for the new categorized buttons

if __name__ == '__main__':
    unittest.main()
