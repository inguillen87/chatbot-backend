
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
    @patch('services.pymes.handle_image_payload') # Mock handling of image
    def test_responder_pyme_with_image(self, mock_handle_image, mock_llm):
        # We need handle_image_payload to NOT return a flow result so that responder_pyme proceeds to LLM
        # OR we check that it calls handle_image_payload correctly.
        # But actually, responder_pyme calls handle_image_payload and returns EARLY if it returns something.

        # Scenario 1: Image handled by logic (e.g. Visual Search) -> Early Return
        mock_flow_result = MagicMock()
        mock_flow_result.message_body = "Encontré estos productos similares"
        mock_flow_result.source = "visual_search"
        mock_flow_result.data = {}
        mock_flow_result.options_list = []
        mock_flow_result.message_type = "text"
        mock_flow_result.audio_url = None
        mock_flow_result.audio_text = None
        mock_flow_result.delayed_payload = None
        mock_flow_result.delay_seconds = 20

        mock_handle_image.return_value = mock_flow_result

        owner_user = MagicMock()
        owner_user.rubro.slug = "general"
        chat_db_context = MagicMock()
        chat_db_context.context_data = {CONTEXTO_PYME: {}}

        uploaded_info = {
            "url": "http://example.com/image.jpg",
            "mime_type": "image/jpeg"
        }

        with patch('services.pymes.load_catalog', return_value=[]):
             response = responder_pyme(
                pregunta_original="",
                owner_user=owner_user,
                rubro_obj=owner_user.rubro,
                chat_db_context=chat_db_context,
                uploaded_file_info=uploaded_info
            )

        self.assertIn("Encontré estos productos", response['message_body'])
        mock_handle_image.assert_called_once()
        mock_llm.assert_not_called() # Should return early

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
        # Mock tenant_profile_pyme to prevent NoneType error in slug resolution
        tenant_profile = MagicMock()
        tenant_profile.slug = "test_tenant"
        owner_user.tenant_profile_pyme = tenant_profile

        chat_db_context = MagicMock()
        chat_db_context.context_data = {CONTEXTO_PYME: {}}

        # Simulate attachment info without mime_type to trigger the fallback logic
        uploaded_info = {
            "url": "http://example.com/file.xyz",
            "name": "file.xyz",
            # No mime_type or unknown
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
        # args[1] is message_usuario
        # args[0] is app
        if len(args) > 1:
            mensaje_usuario = args[1]
            self.assertIn("El usuario adjuntó un archivo", mensaje_usuario)
        else:
             # Depending on how it was called (kwargs vs args)
             call_kwargs = mock_llm.call_args.kwargs
             mensaje_usuario = call_kwargs.get('mensaje_usuario')
             self.assertIn("El usuario adjuntó un archivo", mensaje_usuario)

if __name__ == '__main__':
    unittest.main()
