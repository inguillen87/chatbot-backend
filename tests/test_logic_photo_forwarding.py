import unittest
from unittest.mock import patch, MagicMock
from flask import Flask
from services.logic import responder_chatboc
from models import User, Rubro, ChatSessionContext
from services.constants import CONTEXTO_MUNICIPIO

class LogicPhotoForwardingTest(unittest.TestCase):
    @patch('services.municipio_responder.responder_municipio')
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

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.interpretacion_imagen_service.interpretar_imagen_para_chat')
    @patch('requests.get')
    @patch('services.logic.db.session')
    def test_skips_vision_during_reclamo_flow(self, mock_db_session, mock_get, mock_interpretar, mock_responder):
        app = Flask(__name__)
        app.config['TWILIO_ACCOUNT_SID'] = 'sid'
        app.config['TWILIO_AUTH_TOKEN'] = 'token'

        mock_get.return_value = MagicMock(content=b'', raise_for_status=lambda: None)
        mock_responder.return_value = {"message_body": "ok"}

        owner_user = User(id=1, nombre_empresa="Municipio Test", tipo_chat="municipio")
        rubro = Rubro(id=2, nombre="municipio", es_publico=True)
        owner_user.rubro = rubro
        chat_context = ChatSessionContext(context_data={
            CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": {"state": "ESPERANDO_FOTO", "datos_reclamo": {}}}
        })

        uploaded = {"url": "http://example.com/img.jpg", "mime_type": "image/jpeg", "source": "whatsapp"}

        with app.app_context():
            responder_chatboc("", owner_user=owner_user, rubro_obj=rubro, chat_db_context=chat_context, uploaded_file_info=uploaded)

        mock_interpretar.assert_not_called()
        mock_get.assert_not_called()

    @patch('services.municipio_responder.responder_municipio')
    @patch('services.logic.db.session')
    def test_web_upload_photo_sets_context(self, mock_db_session, mock_responder):
        app = Flask(__name__)

        owner_user = User(id=5, nombre_empresa="Municipio Test", tipo_chat="municipio")
        rubro = Rubro(id=6, nombre="municipio", es_publico=True)
        owner_user.rubro = rubro
        chat_context = ChatSessionContext(context_data={})

        uploaded = {
            "id": 555,
            "url": "http://example.com/web_photo.jpg",
            "mime_type": "image/jpeg",
            "name": "web_photo.jpg",
            "source": "web_upload",
        }

        with app.app_context():
            responder_chatboc(
                "",
                owner_user=owner_user,
                rubro_obj=rubro,
                chat_db_context=chat_context,
                uploaded_file_info=uploaded,
            )

        mock_responder.assert_called_once()
        _, kwargs = mock_responder.call_args
        self.assertTrue(kwargs.get("es_foto"))
        self.assertTrue(kwargs.get("es_archivo"))
        self.assertEqual(kwargs.get("foto_url"), "http://example.com/web_photo.jpg")
        self.assertEqual(kwargs.get("archivo_url"), "http://example.com/web_photo.jpg")
        self.assertEqual(kwargs.get("archivo_id_para_asociar"), 555)

if __name__ == '__main__':
    unittest.main()
