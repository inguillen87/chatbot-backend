import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Add project root to system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipios import responder_municipio

class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        os.environ["GOOGLE_PROJECT_ID"] = "test-project"
        from app import create_app
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()

        self.owner_user = MagicMock()
        self.owner_user.id = 1
        self.rubro_obj = None
        self.viewer_user = None
        self.chat_db_context = MagicMock()
        self.chat_db_context.context_data = {}

    def tearDown(self):
        self.app_context.pop()

    @patch('services.gemini_bridge._llamar_gemini_impl')
    @patch('services.interpretacion_imagen_service._descargar_imagen')
    @patch('services.interpretacion_imagen_service.analyze_image_from_content')
    @patch('services.interpretacion_imagen_service.extract_complaint_details_llm')
    @patch('services.municipios.servicio_tickets.crear_nuevo_ticket')
    def test_end_to_end_pothole_complaint(self, mock_crear_ticket, mock_extract_complaint_details_llm, mock_analyze_image_from_content, mock_descargar_imagen, mock_llamar_gemini_impl):
        mock_llamar_gemini_impl.return_value = {
            "respuesta_usuario": "He recibido tu foto. Para continuar con el reclamo, por favor, decime la dirección del problema.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "target": "municipio",
                "categoria": "Arreglo de calle",
                "descripcion": "Bache en la calle.",
            },
            "pedir_info": "direccion",
            "botones": [],
        }
        mock_descargar_imagen.return_value = b'dummy_image_content'
        mock_analyze_image_from_content.return_value = {
            "labels": [{"description": "Pothole", "confidence": 0.9}],
            "objects": [],
            "full_text_annotation": None,
        }
        mock_extract_complaint_details_llm.return_value = {
            "tipo_problema": "Arreglo de calle",
            "descripcion_problema": "Bache en la calle.",
        }
        mock_crear_ticket.return_value = MagicMock(nro_ticket=12345)

        # 1. User sends image
        pregunta_original = {
            "pregunta": "",
            "es_foto": True,
            "foto_url": "http://example.com/pothole.jpg",
            "uploaded_file_info_whatsapp": {
                "url": "http://example.com/pothole.jpg",
                "mime_type": "image/jpeg",
                "source": "whatsapp",
            },
            "intencion": "iniciar_reclamo",
        }
        response = responder_municipio(pregunta_original, self.owner_user, self.rubro_obj, self.viewer_user, self.chat_db_context, "test_anon_id", "whatsapp")
        self.assertIn("He recibido tu foto", response["message_body"])
        self.assertIn("Arreglo de calle", response["message_body"])

        # 2. User sends address
        pregunta_original = {"pregunta": "Calle Falsa 123"}
        response = responder_municipio(pregunta_original, self.owner_user, self.rubro_obj, self.viewer_user, self.chat_db_context, "test_anon_id", "whatsapp")
        self.assertIn("registrada", response["message_body"])

        # 3. User sends name
        pregunta_original = {"pregunta": "Juan Perez"}
        response = responder_municipio(pregunta_original, self.owner_user, self.rubro_obj, self.viewer_user, self.chat_db_context, "test_anon_id", "whatsapp")
        self.assertIn("Gracias, Juan", response["message_body"])

        # 4. User sends phone
        pregunta_original = {"pregunta": "1122334455"}
        response = responder_municipio(pregunta_original, self.owner_user, self.rubro_obj, self.viewer_user, self.chat_db_context, "test_anon_id", "whatsapp")
        self.assertIn("Ya casi terminamos", response["message_body"])

        # 5. User sends email
        pregunta_original = {"pregunta": "juan.perez@example.com"}
        response = responder_municipio(pregunta_original, self.owner_user, self.rubro_obj, self.viewer_user, self.chat_db_context, "test_anon_id", "whatsapp")
        self.assertIn("Por favor, revisá los datos", response["message_body"])

        # 6. User confirms
        pregunta_original = {"pregunta": "si"}
        response = responder_municipio(pregunta_original, self.owner_user, self.rubro_obj, self.viewer_user, self.chat_db_context, "test_anon_id", "whatsapp")
        self.assertIn("Tu reclamo ha sido registrado con el número de ticket: **M-12345**", response["message_body"])

if __name__ == '__main__':
    unittest.main()
