import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import create_app, db
from config import TestConfig
from models import MunicipioTicket, TenantProfile, User, TicketComentario
from utils.auth_helpers import generar_token
from utils.time_utils import datetime_to_iso_utc


class TicketPublicEndpointConfig(TestConfig):
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"
    TRACKING_FAILURE_RATE_LIMIT = "2 per minute"
    TRACKING_SUBJECT_FAILURE_RATE_LIMIT = "3 per minute"


class TicketPublicEndpointTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TicketPublicEndpointConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()
        # create minimal user to satisfy foreign key
        user = User(name='Admin', email='admin@example.com', rol='admin', tipo_chat='municipio')
        user.set_password('pass')
        db.session.add(user)
        db.session.flush()
        self.user = user
        self.tenant = TenantProfile(
            slug='municipio-principal',
            nombre='Municipio principal',
            tipo='municipio',
            municipio_id=user.id,
        )
        db.session.add(self.tenant)
        db.session.flush()
        user.tenant_id = self.tenant.id

        # create sample ticket linked to the user
        ticket = MunicipioTicket(
            nro_ticket='123456',
            municipio_id=user.id,
            tenant_id=self.tenant.id,
            pregunta='p',
            consulta_pin='654321',
            canal_ingreso='web',
            ultima_actividad=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        db.session.add(ticket)
        db.session.commit()
        self.ticket_id = ticket.id
        self.ticket_ultima = ticket.ultima_actividad

    def assert_private_no_store(self, response):
        cache_control = response.headers.get('Cache-Control', '').lower()
        self.assertIn('private', cache_control)
        self.assertIn('no-store', cache_control)
        self.assertEqual(response.headers.get('Pragma'), 'no-cache')

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_public_lookup_by_number(self):
        # add timeline data
        comentario = TicketComentario(municipio_ticket_id=self.ticket_id, comentario='primer mensaje')
        cambio_estado = TicketComentario(
            municipio_ticket_id=self.ticket_id,
            comentario="Estado actualizado a 'en progreso'",
            es_admin=True,
            origen='sistema',
            estado_ticket='en progreso'
        )
        db.session.add_all([comentario, cambio_estado])
        db.session.commit()

        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['id_ticket'], 'M-123456')
        self.assertEqual(len(data['timeline']), 3)
        self.assertEqual(data['timeline'][0]['tipo'], 'ticket_creado')
        self.assertEqual(data['timeline'][0]['estado'], 'nuevo')
        self.assertEqual(data['timeline'][1]['tipo'], 'comentario')
        self.assertEqual(data['timeline'][2]['estado'], 'en progreso')
        self.assertEqual(data['channel'], 'web')
        self.assertEqual(data['canal_ingreso'], 'web')
        self.assertEqual(data['ultima_actualizacion'], datetime_to_iso_utc(self.ticket_ultima))
        self.assert_private_no_store(resp)

    def test_timeline_maps_cerrado_to_resuelto(self):
        cambio_estado = TicketComentario(
            municipio_ticket_id=self.ticket_id,
            comentario="Estado actualizado a 'cerrado'",
            es_admin=True,
            origen='sistema',
            estado_ticket='cerrado'
        )
        db.session.add(cambio_estado)
        db.session.commit()

        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['timeline'][1]['estado'], 'resuelto')

    @patch('routes.ticket.verify_recaptcha', return_value=False)
    def test_public_lookup_invalid_recaptcha(self, mock_recaptcha):
        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321&recaptcha_token=test')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertEqual(data["error"], "Verificación reCAPTCHA fallida.")
        self.assert_private_no_store(resp)

    def test_public_lookup_ignores_undefined_recaptcha(self):
        resp = self.client.get('/tickets/municipio/por_numero/123456?pin=654321&recaptcha_token=undefined')
        self.assertEqual(resp.status_code, 200)
        self.assert_private_no_store(resp)

    def test_public_lookup_requires_pin(self):
        resp = self.client.get('/tickets/municipio/por_numero/123456')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertEqual(data["error"], "PIN requerido.")
        self.assert_private_no_store(resp)

    def test_authenticated_lookup_without_recaptcha_or_pin(self):
        token = generar_token(self.user.id, self.user.rol, self.user.tipo_chat, self.user.municipio_id, self.user.pyme_id)
        headers = {'Authorization': f'Bearer {token}'}
        resp = self.client.get('/tickets/municipio/por_numero/123456', headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assert_private_no_store(resp)

    def test_public_pin_failures_share_rate_limit_across_aliases_and_exempt_jwt(self):
        remote_addr = '198.51.100.40'
        attempts = [
            self.client.get(
                '/tickets/municipio/por_numero/123456?pin=000000&kind=claim',
                environ_base={'REMOTE_ADDR': remote_addr},
            ),
            self.client.get(
                '/api/tickets/municipio/por_numero/M-123456?pin=111111&kind=order',
                environ_base={'REMOTE_ADDR': remote_addr},
            ),
            self.client.get(
                '/tickets/municipio/por_numero/123456?pin=222222&kind=rotated-bucket',
                environ_base={'REMOTE_ADDR': remote_addr},
            ),
        ]

        self.assertEqual([response.status_code for response in attempts], [404, 404, 429])
        self.assertEqual(
            [response.get_json() for response in attempts[:2]],
            [{'error': 'Ticket no encontrado.'}] * 2,
        )
        self.assertEqual(attempts[-1].get_json()['reason_code'], 'tracking_rate_limited')
        self.assertTrue(attempts[-1].headers.get('Retry-After'))
        for response in attempts:
            self.assert_private_no_store(response)

        token = generar_token(
            self.user.id,
            self.user.rol,
            self.user.tipo_chat,
            self.user.municipio_id,
            self.user.pyme_id,
        )
        authenticated = self.client.get(
            '/api/tickets/municipio/por_numero/123456',
            headers={'Authorization': f'Bearer {token}'},
            environ_base={'REMOTE_ADDR': remote_addr},
        )
        self.assertEqual(authenticated.status_code, 200)
        self.assert_private_no_store(authenticated)

    def test_public_pin_limit_does_not_reveal_ticket_existence_and_resists_ip_rotation(self):
        same_ip_attempts = [
            self.client.get(
                f'/api/tickets/municipio/por_numero/999999?pin={pin}',
                environ_base={'REMOTE_ADDR': '198.51.100.41'},
            )
            for pin in ('000000', '111111', '222222')
        ]
        self.assertEqual(
            [response.status_code for response in same_ip_attempts],
            [404, 404, 429],
        )
        self.assertEqual(
            [response.get_json() for response in same_ip_attempts[:2]],
            [{'error': 'Ticket no encontrado.'}] * 2,
        )

        rotating_ip_attempts = [
            self.client.get(
                f'/tickets/municipio/por_numero/999998?pin={index:06d}',
                environ_base={'REMOTE_ADDR': f'198.51.100.{50 + index}'},
            )
            for index in range(4)
        ]
        self.assertEqual(
            [response.status_code for response in rotating_ip_attempts],
            [404, 404, 404, 429],
        )
        for response in (*same_ip_attempts, *rotating_ip_attempts):
            self.assertNotIn('123456', response.get_data(as_text=True))
            self.assert_private_no_store(response)

    def test_authenticated_lookup_rejects_cross_tenant_ticket_without_leaking_pii(self):
        other_admin = User(
            name='Other Admin',
            email='other-admin@example.com',
            rol='admin',
            tipo_chat='municipio',
        )
        other_admin.set_password('pass')
        db.session.add(other_admin)
        db.session.flush()

        tenant_b = TenantProfile(
            slug='municipio-b',
            nombre='Municipio B',
            tipo='municipio',
            municipio_id=other_admin.id,
        )
        db.session.add(tenant_b)
        db.session.flush()
        other_admin.tenant_id = tenant_b.id

        ticket = db.session.get(MunicipioTicket, self.ticket_id)
        ticket.tenant_id = self.tenant.id
        # Conflicting legacy owner data must never override authoritative tenant_id.
        ticket.municipio_id = other_admin.id
        ticket.nombre_vecino = 'Vecina Privada'
        ticket.email_vecino = 'vecina-privada@example.com'
        ticket.dni_vecino = '30111222'
        db.session.commit()

        token = generar_token(
            other_admin.id,
            other_admin.rol,
            other_admin.tipo_chat,
            other_admin.municipio_id,
            other_admin.pyme_id,
        )
        for path in (
            '/tickets/municipio/por_numero/123456',
            '/tickets/municipio/por_numero/123456?pin=654321',
            '/api/tickets/municipio/por_numero/123456',
        ):
            with self.subTest(path=path):
                response = self.client.get(
                    path,
                    headers={'Authorization': f'Bearer {token}'},
                )

                self.assertEqual(response.status_code, 404)
                payload = response.get_json()
                self.assertEqual(payload, {'error': 'Ticket no encontrado.'})
                serialized = response.get_data(as_text=True)
                self.assertNotIn('Vecina Privada', serialized)
                self.assertNotIn('vecina-privada@example.com', serialized)
                self.assertNotIn('30111222', serialized)
                self.assert_private_no_store(response)

    def test_authenticated_ticket_owner_can_lookup_without_pin(self):
        citizen = User(
            name='Ticket Owner',
            email='ticket-owner@example.com',
            rol='usuario',
            tipo_chat='municipio',
        )
        citizen.set_password('pass')
        db.session.add(citizen)
        db.session.flush()

        ticket = db.session.get(MunicipioTicket, self.ticket_id)
        ticket.user_id = citizen.id
        db.session.commit()

        token = generar_token(
            citizen.id,
            citizen.rol,
            citizen.tipo_chat,
            citizen.municipio_id,
            citizen.pyme_id,
        )
        response = self.client.get(
            '/tickets/municipio/por_numero/123456',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['id_ticket'], 'M-123456')

    def test_authenticated_end_user_cannot_bypass_pin_with_municipio_id(self):
        citizen = User(
            name='Citizen',
            email='citizen@example.com',
            rol='usuario',
            tipo_chat='municipio',
            municipio_id=self.user.id,
        )
        citizen.set_password('pass')
        db.session.add(citizen)
        db.session.commit()

        token = generar_token(
            citizen.id,
            citizen.rol,
            citizen.tipo_chat,
            citizen.municipio_id,
            citizen.pyme_id,
        )
        response = self.client.get(
            '/tickets/municipio/por_numero/123456',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {'error': 'Ticket no encontrado.'})

    def test_authenticated_employee_can_only_lookup_assigned_ticket(self):
        employee = User(
            name='Municipal Agent',
            email='municipal-agent@example.com',
            rol='empleado',
            tipo_chat='municipio',
            empresa_id=self.user.id,
            municipio_id=self.user.id,
            es_empleado=True,
            tenant_id=self.tenant.id,
            accesibilidad={
                'employee_scope': {
                    'categorias': ['Limpieza'],
                }
            },
        )
        employee.set_password('pass')
        db.session.add(employee)
        db.session.commit()

        token = generar_token(
            employee.id,
            employee.rol,
            employee.tipo_chat,
            employee.municipio_id,
            employee.pyme_id,
        )
        headers = {'Authorization': f'Bearer {token}'}

        ticket = db.session.get(MunicipioTicket, self.ticket_id)
        ticket.user_id = employee.id
        ticket.categoria = 'Limpieza'
        db.session.commit()

        unassigned = self.client.get(
            '/tickets/municipio/por_numero/123456',
            headers=headers,
        )
        self.assertEqual(unassigned.status_code, 404)

        ticket.asignado_a_id = employee.id
        db.session.commit()

        assigned = self.client.get(
            '/tickets/municipio/por_numero/123456',
            headers=headers,
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.get_json()['id_ticket'], 'M-123456')

    def test_legacy_employee_flag_cannot_bypass_assignment_as_ticket_owner(self):
        employee = User(
            name='Legacy Municipal Agent',
            email='legacy-municipal-agent@example.com',
            rol='usuario',
            tipo_chat='municipio',
            empresa_id=self.user.id,
            municipio_id=self.user.id,
            es_empleado=True,
        )
        employee.set_password('pass')
        db.session.add(employee)
        db.session.flush()

        ticket = db.session.get(MunicipioTicket, self.ticket_id)
        ticket.user_id = employee.id
        ticket.asignado_a_id = None
        db.session.commit()

        token = generar_token(
            employee.id,
            employee.rol,
            employee.tipo_chat,
            employee.municipio_id,
            employee.pyme_id,
        )
        response = self.client.get(
            '/tickets/municipio/por_numero/123456',
            headers={'Authorization': f'Bearer {token}'},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {'error': 'Ticket no encontrado.'})

if __name__ == '__main__':
    unittest.main()
