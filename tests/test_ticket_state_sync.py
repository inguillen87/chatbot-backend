import unittest
import json
import os
from unittest.mock import patch

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import User, MunicipioTicket, PymeTicket, Rubro, TenantProfile


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
        self.pyme_admin = pyme_admin
        self.pyme_tenant = pyme_tenant

        # Create a municipal ticket
        muni_ticket = MunicipioTicket(nro_ticket='111111', municipio_id=1, pregunta='p', consulta_pin='222222', user_id=muni_admin.id)
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

    def test_admin_response_updates_public_state(self):
        res_login = self.client.post('/auth/login', data=json.dumps({'email': 'muni@example.com', 'password': 'pass'}), content_type='application/json')
        token = json.loads(res_login.data)['token']
        with patch(
            'services.notification_dispatcher.dispatch_ticket_update',
            return_value={'email': False, 'sms': False, 'whatsapp': True},
        ), patch('routes.ticket.emit_ticket_update') as emit_update, patch(
            'routes.ticket.emit_ticket_comment'
        ) as emit_comment, patch('routes.ticket.emit_ticket_unread_changed'):
            res = self.client.post(f'/tickets/municipio/{self.muni_ticket.id}/responder',
                                   headers={'Authorization': f'Bearer {token}'},
                                   data=json.dumps({'comentario': 'Hola'}),
                                   content_type='application/json')
        self.assertEqual(res.status_code, 200)
        payload = json.loads(res.data)
        self.assertEqual(payload['delivery']['contract_version'], 'tickets.agent_reply_delivery.v1')
        self.assertEqual(payload['delivery']['mode'], 'real_message')
        self.assertEqual(payload['delivery']['channel'], 'whatsapp')
        self.assertEqual(payload['delivery']['reply_status'], 'sent_to_contact')
        self.assertTrue(payload['delivery']['external_dispatch'])
        self.assertTrue(payload['delivery']['socket_emitted'])
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
        self.assertEqual(payload['delivery']['contract_version'], 'tickets.agent_reply_delivery.v1')

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
