import unittest
import json
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import jwt
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

    @patch("routes.chat.responder_chatboc")
    def test_attachment_payload_without_text_does_not_trigger_init(self, mock_responder):
        mock_responder.return_value = {"message_body": "ok"}

        payload = {
            "pregunta": "",
            "tipo_chat": "municipio",
            "attachmentInfo": {
                "id": 123,
                "url": "https://example.com/foto.jpg",
                "name": "foto.jpg",
                "mimeType": "image/jpeg",
                "size": 4,
                "thumbUrl": "https://example.com/foto_thumb.jpg",
            },
        }

        response = self.client.post("/ask/municipio", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertNotIn("error", data)

        mock_responder.assert_called_once()
        called_kwargs = mock_responder.call_args.kwargs
        self.assertEqual(called_kwargs.get("pregunta"), "")

        uploaded_info = called_kwargs.get("uploaded_file_info") or {}
        self.assertEqual(uploaded_info.get("id"), payload["attachmentInfo"]["id"])
        self.assertEqual(uploaded_info.get("source"), "web_upload")

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

    @patch('services.municipio_responder.llamar_gemini')
    def test_anonymous_chat_municipio_loads_default_owner(self, mock_llamar_gemini):
        """
        Tests that an anonymous request to /ask/municipio
        successfully loads a default owner user and returns a valid response.
        This test now also verifies the new keyword-based GreetingHandler.
        """
        mock_llamar_gemini.return_value = (
            {
                "accion_backend": "saludar",
                "message_body": "¡Hola! ...", # Mock message, will be replaced by handler
            },
            {}
        )

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

    @patch('services.tts_orchestrator.generar_audio', return_value=None)
    @patch('services.municipio_responder.llamar_gemini')
    def test_anonymous_chat_greets_with_saved_name(self, mock_llamar_gemini, mock_tts):
        """Ensure the greeting uses the stored profile name from a prior request."""
        mock_llamar_gemini.return_value = (
            {"accion_backend": "saludar"},
            {}
        )

        session_id = "test-session-name"
        save_resp = self.client.post(
            '/profile-name',
            json={"nombre_usuario": "Carlos"},
            headers={"X-Chat-Session-Id": session_id}
        )
        self.assertEqual(save_resp.status_code, 200)

        ctx = ChatSessionContext.query.get(session_id)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.context_data.get("profile_name"), "Carlos")

        chat_payload = {
            "pregunta": "Hola",
            "tipo_chat": "municipio",
        }
        response = self.client.post(
            '/ask/municipio',
            json=chat_payload,
            headers={"X-Chat-Session-Id": session_id}
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("Carlos", data.get("message_body", ""))

        # The response from the endpoint is a dictionary, and the welcome message is in 'message_body'.
        self.assertIn("Soy JUNI, tu Asistente Virtual", data.get("message_body", ""))
        self.assertIn("Soy JUNI", data.get("message_body", ""))  # Check for new welcome message
        self.assertIsNotNone(data.get("options_list"))  # The new format uses 'options_list' for buttons/menu items.

    @patch('services.tts_orchestrator.generar_audio', return_value=None)
    @patch('services.municipio_responder.llamar_gemini')
    def test_authenticated_chat_prefers_profile_name(self, mock_llamar_gemini, mock_tts):
        """An authenticated request should still greet with the stored profile name."""
        mock_llamar_gemini.return_value = (
            {"accion_backend": "saludar"},
            {},
        )

        session_id = "auth-session-name"
        save_resp = self.client.post(
            '/profile-name',
            json={"profile_name": "Carla"},
            headers={"X-Chat-Session-Id": session_id},
        )
        self.assertEqual(save_resp.status_code, 200)

        token = jwt.encode(
            {"user_id": self.default_municipio_user.id, "exp": datetime.utcnow() + timedelta(days=1)},
            self.app.config['SECRET_KEY'],
            algorithm="HS256",
        )

        chat_payload = {
            "pregunta": "Hola",
            "tipo_chat": "municipio",
        }
        response = self.client.post(
            '/ask/municipio',
            json=chat_payload,
            headers={
                "X-Chat-Session-Id": session_id,
                "Authorization": f"Bearer {token}",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("Carla", data.get("message_body", ""))
        self.assertNotIn(self.default_municipio_user.name, data.get("message_body", ""))

if __name__ == '__main__':
    unittest.main()
