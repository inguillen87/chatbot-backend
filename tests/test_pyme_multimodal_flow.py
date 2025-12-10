
import unittest
from unittest.mock import patch, MagicMock
from app import create_app, db
from config import TestConfig
from services.pymes import responder_pyme, CONTEXTO_PYME

class PymeMultimodalTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.pymes.llamar_llm_con_fallback')
    def test_responder_pyme_with_attachment_no_text(self, mock_llm):
        # Mock LLM response
        mock_llm.return_value = ({
            "accion_backend": "responder_directamente",
            "message_body": "Recibido el archivo."
        }, None)

        owner_user = MagicMock()
        owner_user.id = 1
        owner_user.rubro.nombre = "general"
        owner_user.rubro.slug = "general"

        chat_db_context = MagicMock()
        chat_db_context.context_data = {CONTEXTO_PYME: {}}

        # Simulate attachment info without mime_type to trigger the fallback I added
        uploaded_info = {
            "url": "http://example.com/file.xyz",
            "name": "file.xyz"
        }

        responder_pyme(
            pregunta_original="",
            owner_user=owner_user,
            rubro_obj=owner_user.rubro,
            chat_db_context=chat_db_context,
            uploaded_file_info=uploaded_info
        )

        # Check what was sent to LLM
        args, _ = mock_llm.call_args
        mensaje_usuario = args[1] # mensaje_usuario is the 2nd arg in llamar_llm_con_fallback(app, mensaje_usuario, ...)

        self.assertIn("El usuario adjuntó un archivo", mensaje_usuario)
        self.assertNotIn("sin texto adicional", mensaje_usuario)

    @patch('services.pymes.llamar_llm_con_fallback')
    def test_responder_pyme_with_image(self, mock_llm):
        mock_llm.return_value = ({"accion_backend": "responder", "message_body": "ok"}, None)

        owner_user = MagicMock()
        owner_user.rubro.slug = "general"
        chat_db_context = MagicMock()
        chat_db_context.context_data = {CONTEXTO_PYME: {}}

        uploaded_info = {
            "url": "http://example.com/image.jpg",
            "mime_type": "image/jpeg"
        }

        # Mock load_catalog to avoid DB lookups
        with patch('services.pymes.load_catalog', return_value=[]):
             responder_pyme(
                pregunta_original="",
                owner_user=owner_user,
                rubro_obj=owner_user.rubro,
                chat_db_context=chat_db_context,
                uploaded_file_info=uploaded_info
            )

        # Check LLM message
        args, _ = mock_llm.call_args
        mensaje_usuario = args[1]
        self.assertIn("El usuario envió una imagen", mensaje_usuario)

if __name__ == '__main__':
    unittest.main()
