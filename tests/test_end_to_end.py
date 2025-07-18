import unittest
from unittest.mock import patch, MagicMock
from app import create_app, db
from config import TestConfig
from models import User, Rubro, WhatsappNumero, Ticket, ArchivoAdjunto

class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create a user and other necessary data
        rubro = Rubro(nombre='municipio')
        db.session.add(rubro)
        db.session.commit()
        user = User(email='test@example.com', password='password', rubro_id=rubro.id, tipo_chat='municipio')
        db.session.add(user)
        db.session.commit()
        whatsapp_numero = WhatsappNumero(numero_whatsapp='+1234567890', user_id=user.id)
        db.session.add(whatsapp_numero)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('routes.whatsapp_webhook.responder_chatboc')
    @patch('routes.whatsapp_webhook.validator')
    def test_create_ticket_with_image_whatsapp(self, mock_validator, mock_responder_chatboc):
        mock_validator.validate.return_value = True
        mock_responder_chatboc.return_value = {
            "respuesta": "Su reclamo ha sido creado con el número 123.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "categoria": "Alumbrado Público",
                "descripcion": "Luz quemada",
                "ubicacion": "Mitre y Belgrano"
            }
        }

        response = self.client.post('/webhook/whatsapp', data={
            'To': 'whatsapp:+1234567890',
            'From': 'whatsapp:+9876543210',
            'Body': 'test message',
            'MediaUrl0': 'http://example.com/image.jpg',
            'MediaContentType0': 'image/jpeg'
        })

        self.assertEqual(response.status_code, 200)

        ticket = Ticket.query.first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.categoria, 'Alumbrado Público')

        archivo = ArchivoAdjunto.query.first()
        self.assertIsNotNone(archivo)
        self.assertEqual(archivo.ticket_id, ticket.id)
        self.assertEqual(archivo.url, 'http://example.com/image.jpg')

    @patch('routes.chat.responder_chatboc')
    def test_create_ticket_with_pdf_webchat(self, mock_responder_chatboc):
        mock_responder_chatboc.return_value = {
            "respuesta": "Su reclamo ha sido creado con el número 124.",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "categoria": "Recolección de Residuos",
                "descripcion": "Basura acumulada",
                "ubicacion": "Calle Falsa 123"
            }
        }

        with open('tests/test_files/dummy.pdf', 'rb') as pdf:
            response = self.client.post('/chat/upload', data={'file': (pdf, 'dummy.pdf')}, content_type='multipart/form-data')

        self.assertEqual(response.status_code, 200)

        ticket = Ticket.query.first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.categoria, 'Recolección de Residuos')

        archivo = ArchivoAdjunto.query.first()
        self.assertIsNotNone(archivo)
        self.assertEqual(archivo.ticket_id, ticket.id)
        self.assertTrue(archivo.url.endswith('dummy.pdf'))

if __name__ == '__main__':
    unittest.main()
