import unittest
from unittest.mock import patch
from app import create_app, db
from models import User, MunicipioTicket, Rubro, TicketComentario, ArchivoAdjunto, Conversacion
from config import TestConfig
import json

class TicketEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Crear un rubro para municipio
        municipio_rubro = Rubro(nombre='municipios', clave='municipios')
        db.session.add(municipio_rubro)
        db.session.commit() # Commit para que el rubro tenga id

        # Crear usuario admin de municipio
        admin_user = User(
            email='admin@junin.com',
            name='Admin Junin',
            rol='admin',
            municipio_id=1, # Junín
            rubro_id=municipio_rubro.id,
            tipo_chat='municipio'
        )
        admin_user.set_password('adminpass')
        db.session.add(admin_user)

        # Crear tickets para el municipio 1
        ticket1 = MunicipioTicket(
            municipio_id=1,
            user_id=admin_user.id,
            asunto='Bache en la calle',
            categoria='calle',
            pregunta='Hay un bache grande en la calle principal.',
            anon_id='session123'
        )
        ticket2 = MunicipioTicket(
            municipio_id=1,
            user_id=admin_user.id,
            asunto='Luz quemada',
            categoria='alumbrado',
            pregunta='El poste de luz de la esquina no funciona.'
        )
        # Crear un segundo usuario para el municipio 2
        user_municipio_2 = User(email='admin@otro.com', name='Admin Otro', rol='admin', municipio_id=2, rubro_id=municipio_rubro.id, tipo_chat='municipio')
        user_municipio_2.set_password('adminpass')
        db.session.add(user_municipio_2)
        db.session.commit()

        # Ticket de otro municipio
        ticket3 = MunicipioTicket(
            municipio_id=2,
            user_id=user_municipio_2.id,
            asunto='Arbol caido',
            categoria='espacios verdes',
            pregunta='Un arbol se cayo sobre la vereda.'
        )
        db.session.add_all([ticket1, ticket2, ticket3])
        db.session.commit()

        # Conversación asociada al ticket1
        db.session.add(Conversacion(session_id='session123', pregunta='Hola', respuesta='Hola, ¿en qué puedo ayudarte?', fuente='chat'))
        db.session.commit()


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_get_tickets_del_usuario_admin_municipio(self):
        login_resp = self.client.post('/auth/login', json={
            'email': 'admin@junin.com',
            'password': 'adminpass'
        })
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']

        headers = {'Authorization': f'Bearer {token}'}
        tickets_resp = self.client.get('/tickets', headers=headers)
        self.assertEqual(tickets_resp.status_code, 200)

        # Verificar que la respuesta contiene los tickets correctos
        data = json.loads(tickets_resp.data)
        self.assertIn('tickets', data)
        tickets = data['tickets']
        self.assertIsInstance(tickets, list)
        self.assertEqual(len(tickets), 2) # Solo los tickets del municipio 1

        asuntos = {t['asunto'] for t in tickets}
        self.assertIn('Bache en la calle', asuntos)
        self.assertIn('Luz quemada', asuntos)
        self.assertNotIn('Arbol caido', asuntos)

        # Check for new fields
        self.assertIn('id', tickets[0])
        self.assertIn('nro_ticket', tickets[0])

        ticket_map = {t['asunto']: t for t in tickets}
        self.assertIn('historial_chat', ticket_map['Bache en la calle'])
        self.assertEqual(len(ticket_map['Bache en la calle']['historial_chat']), 2)
        self.assertEqual(ticket_map['Bache en la calle']['historial_chat'][0]['texto'], 'Hola')
        self.assertEqual(ticket_map['Bache en la calle']['historial_chat'][0]['autor'], 'vecino')

    def test_ticket_details_includes_chat_history(self):
        login_resp = self.client.post('/auth/login', json={
            'email': 'admin@junin.com',
            'password': 'adminpass'
        })
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']
        headers = {'Authorization': f'Bearer {token}'}

        ticket = MunicipioTicket.query.filter_by(asunto='Bache en la calle').first()
        resp = self.client.get(f'/tickets/municipio/{ticket.id}', headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn('historial_chat', data)
        self.assertEqual(len(data['historial_chat']), 2)
        self.assertEqual(data['historial_chat'][0]['texto'], 'Hola')
        self.assertEqual(data['historial_chat'][0]['autor'], 'vecino')

    def test_get_chat_mensajes_with_attachments(self):
        # 1. Login to get token
        login_resp = self.client.post('/auth/login', json={'email': 'admin@junin.com', 'password': 'adminpass'})
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']
        headers = {'Authorization': f'Bearer {token}'}

        # 2. Create ticket, comment, and attachment
        ticket = MunicipioTicket.query.first()
        attachment = ArchivoAdjunto(
            municipio_ticket_id=ticket.id,
            filename="test.jpg",
            nombre_original="test_image.jpg",
            mime="image/jpeg",
            url="http://example.com/test.jpg"
        )
        db.session.add(attachment)
        db.session.commit()

        comment = TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario="Test comment with attachment",
            archivo_adjunto_id=attachment.id,
            es_admin=True
        )
        db.session.add(comment)
        db.session.commit()

        # 3. Call the endpoint
        resp = self.client.get(f'/tickets/chat/{ticket.id}/mensajes', headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)

        # 4. Assertions
        self.assertIn('mensajes', data)
        self.assertGreater(len(data['mensajes']), 0)

        comment_with_attachment = data['mensajes'][0]
        self.assertEqual(comment_with_attachment['comentario'], "Test comment with attachment")
        self.assertIn('attachment_info', comment_with_attachment)
        self.assertIsNotNone(comment_with_attachment['attachment_info'])
        self.assertEqual(comment_with_attachment['attachment_info']['name'], "test_image.jpg")
        self.assertEqual(comment_with_attachment['attachment_info']['url'], "http://example.com/test.jpg")

    @patch('services.email_service.enviar_email_con_multiples_adjuntos')
    def test_send_ticket_history_email(self, mock_send_email):
        mock_send_email.return_value = True
        # 1. Login to get token
        login_resp = self.client.post('/auth/login', json={'email': 'admin@junin.com', 'password': 'adminpass'})
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']
        headers = {'Authorization': f'Bearer {token}'}

        # 2. Get a ticket to send
        ticket = MunicipioTicket.query.first()

        # 3. Call the new endpoint
        resp = self.client.post(f'/tickets/municipio/{ticket.id}/send-history', headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data['success'])

        # 4. Assert that the mock was called
        mock_send_email.assert_called_once()

        # 5. Assert call arguments
        args, kwargs = mock_send_email.call_args
        self.assertIn('destinos', kwargs)
        self.assertIn('asunto', kwargs)
        self.assertIn('cuerpo_html', kwargs)
        self.assertIn('adjuntos', kwargs)

        self.assertIn('admin@junin.com', kwargs['destinos']) # Admin email
        self.assertIn(f'Ticket #{ticket.nro_ticket}', kwargs['asunto'])
        self.assertIn('<h1>Historial de Conversación</h1>', kwargs['cuerpo_html'])


if __name__ == '__main__':
    unittest.main()
