import unittest
from unittest.mock import patch, MagicMock
from flask import Flask
from services.logic import responder_chatboc
from models import User, Rubro, ChatSessionContext

class LogicPhotoForwardingTest(unittest.TestCase):
    @patch('services.logic.responder_municipio')
    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat')
    @patch('requests.get')
    @patch('services.logic.db.session')
    def test_foto_url_forwarded_to_municipio(self, mock_db_session, mock_get, mock_interpretar, mock_responder):
        app = Flask(__name__)
        app.config['TWILIO_ACCOUNT_SID'] = 'sid'
        app.config['TWILIO_AUTH_TOKEN'] = 'token'

        mock_get.return_value = MagicMock(content=b'', raise_for_status=lambda: None)
        mock_interpretar.return_value = {"es_reclamo": True, "categoria_sugerida": "Arreglo de calle", "descripcion_sugerida": "Bache"}
        mock_responder.return_value = {"message_body": "ok"}

        owner_user = User(id=1, nombre_empresa="Municipio Test", tipo_chat="municipio")
        rubro = Rubro(id=2, nombre="municipio", es_publico=True)
        owner_user.rubro = rubro
        chat_context = ChatSessionContext(context_data={})

        uploaded = {"url": "http://example.com/img.jpg", "mime_type": "image/jpeg", "source": "whatsapp"}

        with app.app_context():
            responder_chatboc("", owner_user=owner_user, rubro_obj=rubro, chat_db_context=chat_context, uploaded_file_info=uploaded)

        mock_responder.assert_called_once()
        _, kwargs = mock_responder.call_args
        self.assertTrue(kwargs.get("es_foto"))
        self.assertEqual(kwargs.get("foto_url"), "http://example.com/img.jpg")

if __name__ == '__main__':
    unittest.main()
