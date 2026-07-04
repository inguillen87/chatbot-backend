import unittest
from unittest.mock import patch
from app import create_app, db
from models import User, MunicipioTicket, Rubro, TicketComentario, ArchivoAdjunto, Conversacion
from config import TestConfig
from utils.roles import ROLE_EMPLEADO, canonical_role
import json

class TicketEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
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
        admin_pyme_alias_user = User(
            email='alias@junin.com',
            name='Alias Junin',
            rol='admin_pyme',
            municipio_id=1,
            rubro_id=municipio_rubro.id,
            tipo_chat='municipio'
        )
        admin_pyme_alias_user.set_password('adminpass')
        db.session.add(admin_pyme_alias_user)
        db.session.flush()

        employee_alias_user = User(
            email='employee@junin.com',
            name='Employee Junin',
            rol='employee',
            municipio_id=1,
            empresa_id=admin_user.id,
            rubro_id=municipio_rubro.id,
            tipo_chat='municipio',
            es_empleado=True,
            ticket_categorias='calle,alumbrado',
        )
        employee_alias_user.set_password('employeepass')
        db.session.add(employee_alias_user)

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

        ticket1.asignado_a_id = employee_alias_user.id
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

    def test_canonical_role_accepts_employee_aliases(self):
        self.assertEqual(canonical_role('employee'), ROLE_EMPLEADO)
        self.assertEqual(canonical_role('agent'), ROLE_EMPLEADO)

    def test_api_tickets_accepts_employee_role_alias(self):
        login_resp = self.client.post('/auth/login', json={
            'email': 'employee@junin.com',
            'password': 'employeepass'
        })
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']

        resp = self.client.get(
            '/api/tickets?include=compact',
            headers={'Authorization': f'Bearer {token}'},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type.split(';')[0], 'application/json')
        data = json.loads(resp.data)
        self.assertIn('tickets', data)
        self.assertEqual(len(data['tickets']), 2)
        self.assertEqual(data.get('pagination', {}).get('total_items'), 2)

    @patch('services.notification_dispatcher.dispatch_ticket_update', return_value={'email': False, 'sms': False, 'whatsapp': False})
    def test_employee_role_alias_can_reply_only_assigned_ticket(self, _mock_dispatch):
        login_resp = self.client.post('/auth/login', json={
            'email': 'employee@junin.com',
            'password': 'employeepass'
        })
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']
        headers = {'Authorization': f'Bearer {token}'}

        assigned_ticket = MunicipioTicket.query.filter_by(asunto='Bache en la calle').first()
        unassigned_ticket = MunicipioTicket.query.filter_by(asunto='Luz quemada').first()

        assigned_resp = self.client.post(
            f'/tickets/municipio/{assigned_ticket.id}/responder',
            json={'comentario': 'Lo revisamos desde la mesa operativa.'},
            headers=headers,
        )
        self.assertEqual(assigned_resp.status_code, 200)

        blocked_resp = self.client.post(
            f'/tickets/municipio/{unassigned_ticket.id}/responder',
            json={'comentario': 'No deberia poder responder.'},
            headers=headers,
        )
        self.assertEqual(blocked_resp.status_code, 403)

    def test_get_tickets_accepts_modern_admin_role_aliases(self):
        login_resp = self.client.post('/auth/admin/login', json={
            'email': 'alias@junin.com',
            'password': 'adminpass'
        })
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']

        headers = {'Authorization': f'Bearer {token}'}
        tickets_resp = self.client.get('/tickets', headers=headers)
        self.assertEqual(tickets_resp.status_code, 200)
        data = json.loads(tickets_resp.data)
        self.assertIn('tickets', data)
        self.assertEqual(len(data['tickets']), 2)

    def test_api_tickets_for_client_role_returns_json_not_redirect(self):
        cliente = User(
            email='vecino@junin.com',
            name='Vecino Junin',
            rol='usuario',
            rubro_id=Rubro.query.filter_by(clave='municipios').first().id,
            tipo_chat='municipio',
        )
        cliente.set_password('clientpass')
        db.session.add(cliente)
        db.session.flush()
        db.session.add(
            MunicipioTicket(
                municipio_id=1,
                user_id=cliente.id,
                asunto='Consulta propia',
                categoria='consulta',
                pregunta='Quiero ver mi reclamo.',
            )
        )
        db.session.commit()

        login_resp = self.client.post('/auth/login', json={
            'email': 'vecino@junin.com',
            'password': 'clientpass'
        })
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']

        resp = self.client.get(
            '/api/tickets',
            headers={'Authorization': f'Bearer {token}'},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type.split(';')[0], 'application/json')
        data = json.loads(resp.data)
        self.assertIn('tickets', data)
        self.assertEqual(len(data['tickets']), 1)

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

    @patch('routes.ticket.obtener_ruta', side_effect=RuntimeError('osrm unavailable'))
    def test_api_ticket_details_degrades_route_failures(self, _mock_route):
        login_resp = self.client.post('/auth/login', json={
            'email': 'admin@junin.com',
            'password': 'adminpass'
        })
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']
        headers = {'Authorization': f'Bearer {token}'}

        admin_user = User.query.filter_by(email='admin@junin.com').first()
        admin_user.latitud = -33.08
        admin_user.longitud = -68.47
        ticket = MunicipioTicket.query.filter_by(asunto='Bache en la calle').first()
        ticket.latitud = -33.09
        ticket.longitud = -68.48
        db.session.commit()

        resp = self.client.get(f'/api/tickets/municipio/{ticket.id}', headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type.split(';')[0], 'application/json')
        data = json.loads(resp.data)
        self.assertIsNone(data.get('ruta'))
        self.assertIn('ticket_route_unavailable', data.get('meta', {}).get('degraded_reasons', []))
        self.assertIsNone(data.get('nombre_y_avatar_whatsapp', {}).get('avatar_url'))
        self.assertFalse(data.get('nombre_y_avatar_whatsapp', {}).get('avatar_consent'))

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
        self.assertEqual(comment_with_attachment['texto'], "Test comment with attachment")
        self.assertIn('attachmentInfo', comment_with_attachment)
        self.assertIsNotNone(comment_with_attachment['attachmentInfo'])
        self.assertEqual(comment_with_attachment['attachmentInfo']['name'], "test_image.jpg")
        self.assertEqual(comment_with_attachment['attachmentInfo']['url'], "http://example.com/test.jpg")

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

    @patch('services.email_service.enviar_email_con_multiples_adjuntos')
    def test_send_ticket_history_handles_missing_dates(self, mock_send_email):
        mock_send_email.return_value = True

        login_resp = self.client.post('/auth/login', json={'email': 'admin@junin.com', 'password': 'adminpass'})
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']
        headers = {'Authorization': f'Bearer {token}'}

        admin_user = User.query.filter_by(email='admin@junin.com').first()
        ticket = MunicipioTicket(
            municipio_id=admin_user.municipio_id,
            user_id=admin_user.id,
            asunto='Sin fecha creada',
            categoria='prueba',
            pregunta='Necesitamos verificar formato de fecha.',
            fecha=None,
        )
        db.session.add(ticket)
        db.session.commit()

        comentario = TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario='Comentario sin fecha asignada',
            es_admin=False,
        )
        comentario.fecha = None
        db.session.add(comentario)
        db.session.commit()

        resp = self.client.post(f'/tickets/municipio/{ticket.id}/send-history', headers=headers)
        self.assertEqual(resp.status_code, 200)

        cuerpo_html = mock_send_email.call_args.kwargs['cuerpo_html']
        self.assertIn('Sin fecha', cuerpo_html)
        self.assertIn('Comentario sin fecha asignada', cuerpo_html)

    @patch('services.email_service.enviar_email_con_multiples_adjuntos')
    @patch('services.email_service.validar_configuracion_smtp')
    def test_send_ticket_history_returns_503_when_smtp_missing(self, mock_validar_smtp, mock_send_email):
        mock_validar_smtp.return_value = (False, 'Configuración SMTP incompleta: faltan host o puerto.')

        login_resp = self.client.post('/auth/login', json={'email': 'admin@junin.com', 'password': 'adminpass'})
        self.assertEqual(login_resp.status_code, 200)
        token = json.loads(login_resp.data)['token']
        headers = {'Authorization': f'Bearer {token}'}

        ticket = MunicipioTicket.query.first()

        resp = self.client.post(f'/tickets/municipio/{ticket.id}/send-history', headers=headers)
        self.assertEqual(resp.status_code, 503)

        data = json.loads(resp.data)
        self.assertIn('SMTP', data.get('error', ''))
        mock_send_email.assert_not_called()


if __name__ == '__main__':
    unittest.main()
