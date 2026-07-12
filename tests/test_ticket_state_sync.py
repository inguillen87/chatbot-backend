import unittest
import json
import os
from unittest.mock import patch

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import User, MunicipioTicket, PymeTicket, Rubro, TenantProfile
from services.ticket_realtime_state import mark_ticket_read


class TicketStateSyncTestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class TicketStateSyncTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TicketStateSyncTestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create municipality admin user
        muni_admin = User(name='Muni Admin', email='muni@example.com', rol='admin', tipo_chat='municipio', municipio_id=1)
        muni_admin.set_password('pass')
        db.session.add(muni_admin)

        citizen = User(name='Citizen', email='citizen@example.com', rol='usuario', tipo_chat='municipio', municipio_id=1)
        citizen.set_password('pass')
        db.session.add(citizen)

        # Create pyme admin user and rubro
        rubro = Rubro(clave='r1', nombre='R1')
        db.session.add(rubro)
        db.session.commit()
        pyme_admin = User(name='Pyme Admin', email='pyme@example.com', rol='admin', tipo_chat='pyme', rubro_id=rubro.id)
        pyme_admin.set_password('pass')
        db.session.add(pyme_admin)
        db.session.flush()
        pyme_tenant = TenantProfile(
            slug='pyme-state-sync',
            nombre='Pyme State Sync',
            tipo='pyme',
            pyme_id=pyme_admin.id,
        )
        db.session.add(pyme_tenant)
        db.session.flush()
        pyme_admin.tenant_id = pyme_tenant.id
        pyme_admin.tenant_slug = pyme_tenant.slug
        db.session.commit()

        self.muni_admin = muni_admin
        self.citizen = citizen
        self.pyme_admin = pyme_admin
        self.pyme_tenant = pyme_tenant

        # Create a municipal ticket
        muni_ticket = MunicipioTicket(nro_ticket='111111', municipio_id=1, pregunta='p', consulta_pin='222222', user_id=citizen.id)
        db.session.add(muni_ticket)
        db.session.commit()
        self.muni_ticket = muni_ticket

        # Create a pyme ticket
        pyme_ticket = PymeTicket(
            nro_ticket=1,
            tenant_id=pyme_tenant.id,
            rubro_id=rubro.id,
            pregunta='p',
            user_id=pyme_admin.id,
        )
        db.session.add(pyme_ticket)
        db.session.commit()
        self.pyme_ticket = pyme_ticket

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _login(self, email):
        response = self.client.post(
            '/auth/login',
            data=json.dumps({'email': email, 'password': 'pass'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        return json.loads(response.data)['token']

    def _set_municipio_presence(self, token, status='active'):
        response = self.client.post(
            f'/tickets/municipio/{self.muni_ticket.id}/presence',
            headers={'Authorization': f'Bearer {token}'},
            data=json.dumps({'presence_status': status}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def _reply_to_municipio(self, token, *, notification_results=None, emit_comment_side_effect=None):
        notification_results = notification_results or {'email': False, 'sms': False, 'whatsapp': False}
        with patch(
            'services.notification_dispatcher.dispatch_ticket_update',
            return_value=notification_results,
        ), patch('routes.ticket.emit_ticket_update') as emit_update, patch(
            'routes.ticket.emit_ticket_comment',
            side_effect=emit_comment_side_effect,
        ) as emit_comment, patch('routes.ticket.emit_ticket_unread_changed'):
            response = self.client.post(
                f'/tickets/municipio/{self.muni_ticket.id}/responder',
                headers={'Authorization': f'Bearer {token}'},
                data=json.dumps({'comentario': 'Hola'}),
                content_type='application/json',
            )
        return response, emit_update, emit_comment

    def test_admin_response_updates_public_state(self):
        token = self._login('muni@example.com')
        res, emit_update, emit_comment = self._reply_to_municipio(
            token,
            notification_results={'email': False, 'sms': False, 'whatsapp': True},
        )
        self.assertEqual(res.status_code, 200)
        payload = json.loads(res.data)
        self.assertEqual(payload['delivery']['contract_version'], 'tickets.agent_reply_delivery.v2')
        self.assertEqual(payload['delivery']['legacy_contract_version'], 'tickets.agent_reply_delivery.v1')
        self.assertEqual(payload['delivery']['mode'], 'real_message')
        self.assertEqual(payload['delivery']['channel'], 'whatsapp')
        self.assertEqual(payload['delivery']['reply_status'], 'sent_to_contact')
        self.assertTrue(payload['delivery']['external_dispatch'])
        self.assertTrue(payload['delivery']['socket_emitted'])
        self.assertTrue(payload['delivery']['recipient_room_emitted'])
        self.assertFalse(payload['delivery']['recipient_presence_confirmed'])
        self.assertFalse(payload['delivery']['recipient_read_confirmed'])
        self.assertEqual(
            payload['delivery']['delivery_results'],
            {'email': False, 'sms': False, 'whatsapp': True, 'socket': True},
        )
        emit_update.assert_called_once()
        emit_comment.assert_called_once()

        resp_public = self.client.get(f'/tickets/municipio/por_numero/{self.muni_ticket.nro_ticket}?pin={self.muni_ticket.consulta_pin}')
        self.assertEqual(resp_public.status_code, 200)
        data = json.loads(resp_public.data)
        self.assertEqual(data['estado_ticket'], 'en_proceso')

    def test_reply_without_presence_is_queued_even_when_socket_is_emitted(self):
        token = self._login('muni@example.com')

        response, _, _ = self._reply_to_municipio(token)

        self.assertEqual(response.status_code, 200)
        delivery = response.get_json()['delivery']
        self.assertTrue(delivery['socket_emitted'])
        self.assertTrue(delivery['recipient_room_emitted'])
        self.assertFalse(delivery['recipient_presence_confirmed'])
        self.assertFalse(delivery['recipient_read_confirmed'])
        self.assertEqual(delivery['mode'], 'timeline_only')
        self.assertEqual(delivery['status'], 'queued')
        self.assertEqual(delivery['channel'], 'crm')
        self.assertEqual(delivery['reply_status'], 'saved_to_timeline')
        self.assertEqual(delivery['reason'], 'recipient_presence_not_confirmed')

    def test_admin_presence_alone_does_not_confirm_citizen_delivery(self):
        token = self._login('muni@example.com')
        self._set_municipio_presence(token)

        response, _, _ = self._reply_to_municipio(token)

        self.assertEqual(response.status_code, 200)
        delivery = response.get_json()['delivery']
        self.assertTrue(delivery['socket_emitted'])
        self.assertTrue(delivery['recipient_room_emitted'])
        self.assertFalse(delivery['recipient_presence_confirmed'])
        self.assertFalse(delivery['recipient_read_confirmed'])
        self.assertEqual(delivery['status'], 'queued')
        self.assertEqual(delivery['reply_status'], 'saved_to_timeline')

    def test_active_citizen_presence_confirms_live_chat_delivery_not_read(self):
        citizen_token = self._login('citizen@example.com')
        admin_token = self._login('muni@example.com')
        self._set_municipio_presence(citizen_token)

        response, _, _ = self._reply_to_municipio(admin_token)

        self.assertEqual(response.status_code, 200)
        delivery = response.get_json()['delivery']
        self.assertTrue(delivery['socket_emitted'])
        self.assertTrue(delivery['recipient_room_emitted'])
        self.assertTrue(delivery['recipient_presence_confirmed'])
        self.assertFalse(delivery['recipient_read_confirmed'])
        self.assertEqual(delivery['mode'], 'real_message')
        self.assertEqual(delivery['status'], 'sent')
        self.assertEqual(delivery['channel'], 'live_socket')
        self.assertEqual(delivery['reply_status'], 'sent_to_live_chat')
        self.assertEqual(delivery['reason'], 'recipient_presence_confirmed')

    def test_admin_room_emission_is_not_delivery_when_public_room_emit_fails(self):
        citizen_token = self._login('citizen@example.com')
        admin_token = self._login('muni@example.com')
        self._set_municipio_presence(citizen_token)

        response, emit_update, emit_comment = self._reply_to_municipio(
            admin_token,
            emit_comment_side_effect=RuntimeError('public room unavailable'),
        )

        self.assertEqual(response.status_code, 200)
        emit_update.assert_called_once()
        self.assertEqual(emit_comment.call_count, 2)
        delivery = response.get_json()['delivery']
        self.assertTrue(delivery['socket_emitted'])
        self.assertFalse(delivery['recipient_room_emitted'])
        self.assertTrue(delivery['recipient_presence_confirmed'])
        self.assertFalse(delivery['recipient_read_confirmed'])
        self.assertEqual(delivery['status'], 'queued')
        self.assertEqual(delivery['reply_status'], 'saved_to_timeline')
        self.assertEqual(delivery['reason'], 'recipient_room_dispatch_failed')

    def test_citizen_read_ack_is_separate_from_presence_and_socket_emission(self):
        admin_token = self._login('muni@example.com')

        def acknowledge_comment(payload):
            comment = payload.get('comment') or payload.get('comentario') or {}
            mark_ticket_read(
                ticket_type='municipio',
                ticket_id=self.muni_ticket.id,
                viewer_key=f'user:{self.citizen.id}',
                last_read_comment_id=int(comment['id']),
                viewer_user_id=self.citizen.id,
                viewer_role='usuario',
                active_session_id='citizen-read-test',
            )
            db.session.commit()

        response, _, emit_comment = self._reply_to_municipio(
            admin_token,
            emit_comment_side_effect=acknowledge_comment,
        )

        self.assertEqual(response.status_code, 200)
        emit_comment.assert_called_once()
        delivery = response.get_json()['delivery']
        self.assertTrue(delivery['socket_emitted'])
        self.assertTrue(delivery['recipient_room_emitted'])
        self.assertTrue(delivery['recipient_presence_confirmed'])
        self.assertTrue(delivery['recipient_read_confirmed'])
        self.assertEqual(delivery['reply_comment_ids'], [delivery['latest_reply_comment_id']])
        self.assertEqual(delivery['status'], 'sent')
        self.assertEqual(delivery['reply_status'], 'sent_to_live_chat')
        self.assertEqual(delivery['reason'], 'recipient_read_confirmed')

    def test_estado_change_syncs_cliente(self):
        res_login = self.client.post('/auth/login', data=json.dumps({'email': 'pyme@example.com', 'password': 'pass'}), content_type='application/json')
        token = json.loads(res_login.data)['token']
        res = self.client.put(f'/tickets/pyme/{self.pyme_ticket.id}/estado',
                              headers={'Authorization': f'Bearer {token}'},
                              data=json.dumps({'estado': 'cerrado'}),
                              content_type='application/json')
        self.assertEqual(res.status_code, 200)
        actualizado = PymeTicket.query.get(self.pyme_ticket.id)
        self.assertEqual(actualizado.estado, 'cerrado')
        self.assertEqual(actualizado.estado_cliente, 'cerrado')

    def test_tenant_scoped_pyme_admin_can_view_and_reply_without_rubro_id(self):
        tenant_admin = User(
            name='Tenant Pyme Admin',
            email='tenant-pyme@example.com',
            rol='admin',
            tipo_chat='pyme',
            tenant_slug='tenant-pyme',
        )
        tenant_admin.set_password('pass')
        db.session.add(tenant_admin)
        db.session.commit()

        tenant = TenantProfile(
            slug='tenant-pyme',
            nombre='Tenant Pyme',
            tipo='pyme',
            pyme_id=tenant_admin.id,
        )
        db.session.add(tenant)
        db.session.commit()
        tenant_admin.tenant_id = tenant.id

        ticket = PymeTicket(
            nro_ticket=2,
            tenant_id=tenant.id,
            rubro_id=999,
            pregunta='consulta tenant',
            user_id=tenant_admin.id,
        )
        db.session.add(ticket)
        db.session.commit()

        res_login = self.client.post(
            '/auth/login',
            data=json.dumps({'email': 'tenant-pyme@example.com', 'password': 'pass'}),
            content_type='application/json',
        )
        token = json.loads(res_login.data)['token']
        headers = {
            'Authorization': f'Bearer {token}',
            'X-Tenant-Slug': 'tenant-pyme',
        }

        detail = self.client.get(f'/tickets/pyme/{ticket.id}', headers=headers)
        self.assertEqual(detail.status_code, 200)

        with patch(
            'services.notification_dispatcher.dispatch_ticket_update',
            return_value={'email': False, 'sms': False, 'whatsapp': False},
        ), patch('routes.ticket.emit_ticket_update'), patch(
            'routes.ticket.emit_ticket_comment'
        ), patch('routes.ticket.emit_ticket_unread_changed'):
            reply = self.client.post(
                f'/tickets/pyme/{ticket.id}/responder',
                headers=headers,
                data=json.dumps({'comentario': 'Respuesta desde tenant'}),
                content_type='application/json',
            )

        self.assertEqual(reply.status_code, 200)
        payload = json.loads(reply.data)
        self.assertEqual(payload['delivery']['contract_version'], 'tickets.agent_reply_delivery.v2')

    def test_get_ticket_estados_endpoint(self):
        res_login = self.client.post(
            '/auth/login',
            data=json.dumps({'email': 'pyme@example.com', 'password': 'pass'}),
            content_type='application/json'
        )
        token = json.loads(res_login.data)['token']
        res = self.client.get('/tickets/estados', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIn('estados', data)
        self.assertIn('en_proceso', data['estados'])

    def test_invalid_estado_rejected(self):
        res_login = self.client.post(
            '/auth/login',
            data=json.dumps({'email': 'pyme@example.com', 'password': 'pass'}),
            content_type='application/json'
        )
        token = json.loads(res_login.data)['token']
        res = self.client.put(
            f'/tickets/pyme/{self.pyme_ticket.id}/estado',
            headers={'Authorization': f'Bearer {token}'},
            data=json.dumps({'estado': 'invalido'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 400)

if __name__ == '__main__':
    unittest.main()
