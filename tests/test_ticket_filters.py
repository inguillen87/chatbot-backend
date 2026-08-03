import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os
from app import create_app, db
from config import Config

# Añadir el directorio raíz del proyecto al sys.path
project_root_ticket_filters = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_ticket_filters not in sys.path:
    sys.path.insert(0, project_root_ticket_filters)

from routes.ticket import get_tickets_del_usuario_logic

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class DummyQuery:
    def __init__(self, items):
        self.items = list(items)
    def filter_by(self, **kwargs):
        self.items = [i for i in self.items if all(getattr(i, k) == v for k, v in kwargs.items())]
        return self
    def filter(self, *criterion):
        for c in criterion:
            try:
                if isinstance(c, tuple):
                    key, val = c
                else:
                    key = c.left.name
                    val = c.right.value
                self.items = [i for i in self.items if getattr(i, key) == val]
            except Exception:
                pass
        return self
    def order_by(self, *args):
        return self
    def all(self):
        return self.items
    def offset(self, *args):
        return self
    def limit(self, *args):
        return self

from models import User, MunicipioTicket, PymeTicket, TicketComentario, TicketRealtimeState, TenantProfile
from datetime import datetime, timedelta
from utils.time_utils import get_local_now

class TicketFiltersTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _create_municipal_admin(self, *, email: str, name: str):
        admin_user = User(
            email=email,
            name=name,
            rol='admin',
            tipo_chat='municipio',
        )
        admin_user.set_password('password')
        db.session.add(admin_user)
        db.session.flush()
        tenant = TenantProfile(
            slug=f'ticket-filters-municipio-{admin_user.id}',
            nombre=name,
            tipo='municipio',
            municipio_id=admin_user.id,
            is_active=True,
        )
        db.session.add(tenant)
        db.session.flush()
        admin_user.municipio_id = admin_user.id
        admin_user.tenant_id = tenant.id
        admin_user.tenant_slug = tenant.slug
        db.session.commit()
        return admin_user, tenant

    def test_estado_filter_municipio(self):
        admin_user, tenant = self._create_municipal_admin(
            email='admin@test.com',
            name='Admin Test',
        )

        t1 = MunicipioTicket(id=1, nro_ticket='1', estado='abierto', fecha=datetime.now(), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=admin_user.id, tenant_id=tenant.id)
        t2 = MunicipioTicket(id=2, nro_ticket='2', estado='cerrado', fecha=datetime.now(), categoria='A', direccion=None, latitud=None, longitud=None, municipio_id=admin_user.id, tenant_id=tenant.id)
        db.session.add_all([t1, t2])
        db.session.commit()

        with self.app.test_request_context('?estado=abierto'), patch(
            'routes.ticket.get_current_tenant_profile', return_value=tenant
        ):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['id'], 1)

    def test_invalid_explicit_tenant_slug_does_not_fall_back_to_first_tenant(self):
        owner_like_employee = User(
            id=20,
            email='owner-employee@test.com',
            name='Owner Employee',
            rol='empleado',
            tipo_chat='municipio',
            municipio_id=None,
        )
        owner_like_employee.set_password('password')
        db.session.add(owner_like_employee)
        db.session.commit()

        first_tenant = TenantProfile(
            slug='primer-tenant',
            nombre='Primer tenant',
            tipo='municipio',
            municipio_id=owner_like_employee.id,
        )
        ticket = MunicipioTicket(
            id=70,
            nro_ticket='M-FIRST',
            estado='abierto',
            fecha=datetime.now(),
            categoria='A',
            direccion=None,
            latitud=None,
            longitud=None,
            municipio_id=owner_like_employee.id,
        )
        db.session.add_all([first_tenant, ticket])
        db.session.commit()

        with self.app.test_request_context(headers={'X-Tenant-Slug': 'tenant-inexistente'}):
            response = get_tickets_del_usuario_logic(owner_like_employee)

        self.assertEqual(response.status_code, 403)
        data = response.get_json()
        self.assertEqual(data["access_contract"]["contract_version"], "tickets.access.v1")
        self.assertEqual(data["reason_code"], "missing_municipal_scope")
        self.assertIsNone(data["current_scope"]["tenant_slug"])

    def test_estado_filter_uses_filtered_pagination_total(self):
        admin_user, tenant = self._create_municipal_admin(
            email='admin-pagination@test.com',
            name='Admin Pagination',
        )

        tickets = [
            MunicipioTicket(id=11, nro_ticket='P-11', estado='abierto', fecha=datetime.now(), categoria='A', municipio_id=admin_user.id, tenant_id=tenant.id),
            MunicipioTicket(id=12, nro_ticket='P-12', estado='abierto', fecha=datetime.now(), categoria='A', municipio_id=admin_user.id, tenant_id=tenant.id),
            MunicipioTicket(id=13, nro_ticket='P-13', estado='cerrado', fecha=datetime.now(), categoria='A', municipio_id=admin_user.id, tenant_id=tenant.id),
        ]
        db.session.add_all(tickets)
        db.session.commit()

        with self.app.test_request_context('?estado=abierto&per_page=1'), patch('routes.ticket.get_current_tenant_profile', return_value=tenant):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['pagination']['total_items'], 2)
            self.assertEqual(data['pagination']['total_pages'], 2)
            self.assertTrue(data['pagination']['has_next'])
            self.assertEqual(data['summary']['total'], 3)

    def test_search_finds_ticket_without_user_join_match(self):
        admin_user, tenant = self._create_municipal_admin(
            email='admin-search@test.com',
            name='Admin Search',
        )

        ticket = MunicipioTicket(id=21, nro_ticket='M-ANON-777', estado='nuevo', fecha=datetime.now(), categoria='Luminaria', municipio_id=admin_user.id, tenant_id=tenant.id, user_id=None)
        db.session.add(ticket)
        db.session.commit()

        with self.app.test_request_context('?q=ANON-777'), patch('routes.ticket.get_current_tenant_profile', return_value=tenant):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 21)

    def test_channel_and_agent_filters_municipio(self):
        admin_user, tenant = self._create_municipal_admin(
            email='admin-agent@test.com',
            name='Admin Agent',
        )
        assigned_user = User(
            email='agent@test.com',
            name='Agent Test',
            rol='empleado',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            empresa_id=admin_user.id,
            tipo_chat='municipio',
        )
        assigned_user.set_password('password')
        db.session.add(assigned_user)
        db.session.commit()

        assigned_ticket = MunicipioTicket(
            id=31,
            nro_ticket='M-ASSIGNED',
            estado='nuevo',
            fecha=datetime.now(),
            categoria='A',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
            canal_ingreso='whatsapp',
            asignado_a_id=assigned_user.id,
        )
        unassigned_ticket = MunicipioTicket(
            id=32,
            nro_ticket='M-UNASSIGNED',
            estado='nuevo',
            fecha=datetime.now(),
            categoria='A',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
            canal_ingreso='web',
            asignado_a_id=None,
        )
        db.session.add_all([assigned_ticket, unassigned_ticket])
        db.session.commit()

        with self.app.test_request_context(f'?channel=whatsapp&assigned_agent={assigned_user.id}'), patch('routes.ticket.get_current_tenant_profile', return_value=tenant):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 31)

        with self.app.test_request_context('?unassigned=true'), patch('routes.ticket.get_current_tenant_profile', return_value=tenant):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 32)

    def test_facets_are_global_to_scope_not_current_page(self):
        admin_user, tenant = self._create_municipal_admin(
            email='admin-facets@test.com',
            name='Admin Facets',
        )
        assigned_user = User(
            email='agent-facets@test.com',
            name='Agent Facets',
            rol='empleado',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            empresa_id=admin_user.id,
            tipo_chat='municipio',
        )
        assigned_user.set_password('password')
        db.session.add(assigned_user)
        db.session.commit()

        tickets = [
            MunicipioTicket(
                id=71,
                nro_ticket='M-FACET-WA',
                estado='nuevo',
                fecha=get_local_now() - timedelta(minutes=1),
                categoria='Luminaria',
                municipio_id=admin_user.id,
                tenant_id=tenant.id,
                canal_ingreso='whatsapp',
                asignado_a_id=assigned_user.id,
            ),
            MunicipioTicket(
                id=72,
                nro_ticket='M-FACET-WEB',
                estado='nuevo',
                fecha=get_local_now() - timedelta(minutes=2),
                categoria='Arbolado',
                municipio_id=admin_user.id,
                tenant_id=tenant.id,
                canal_ingreso='web',
                asignado_a_id=None,
            ),
            MunicipioTicket(
                id=73,
                nro_ticket='M-FACET-CLOSED',
                estado='cerrado',
                fecha=get_local_now() - timedelta(minutes=3),
                categoria='Luminaria',
                municipio_id=admin_user.id,
                tenant_id=tenant.id,
                canal_ingreso='whatsapp',
                asignado_a_id=assigned_user.id,
            ),
        ]
        db.session.add_all(tickets)
        db.session.commit()

        with self.app.test_request_context('?estado=nuevo&per_page=1&include=compact'), patch('routes.ticket.get_current_tenant_profile', return_value=tenant):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()

            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['pagination']['total_items'], 2)
            self.assertEqual(data['facets']['contract_version'], 'tickets.facets.v1')
            self.assertEqual(data['facets']['total_scoped'], 3)
            self.assertEqual(data['facets']['total_filtered'], 2)

            channels = {item['value']: item['count'] for item in data['facets']['channels']}
            self.assertEqual(channels['whatsapp'], 1)
            self.assertEqual(channels['web'], 1)

            statuses = {item['value']: item['count'] for item in data['facets']['statuses']}
            self.assertEqual(statuses['nuevo'], 2)
            self.assertEqual(statuses['cerrado'], 1)

            categories = {item['value']: item['count'] for item in data['facets']['categories']}
            self.assertEqual(categories['Luminaria'], 1)
            self.assertEqual(categories['Arbolado'], 1)

            agents = {item['value']: item['count'] for item in data['facets']['agents']}
            self.assertEqual(agents[str(assigned_user.id)], 1)
            self.assertEqual(agents['unassigned'], 1)

    def test_operational_sla_filter_is_applied_before_pagination(self):
        admin_user, tenant = self._create_municipal_admin(
            email='admin-sla@test.com',
            name='Admin SLA',
        )

        recent_ticket = MunicipioTicket(
            id=41,
            nro_ticket='M-RECENT',
            estado='nuevo',
            fecha=get_local_now(),
            categoria='A',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
            asignado_a_id=None,
        )
        risk_ticket = MunicipioTicket(
            id=42,
            nro_ticket='M-RISK',
            estado='nuevo',
            fecha=get_local_now() - timedelta(hours=10),
            ultima_actividad=get_local_now() - timedelta(hours=10),
            categoria='A',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
            asignado_a_id=None,
        )
        db.session.add_all([recent_ticket, risk_ticket])
        db.session.commit()

        with self.app.test_request_context('?sla=risk&per_page=1&include=compact'), patch('routes.ticket.get_current_tenant_profile', return_value=tenant):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 42)
            self.assertEqual(data['tickets'][0]['sla_status'], 'por_vencer')

    def test_unread_filter_is_applied_before_pagination(self):
        admin_user, tenant = self._create_municipal_admin(
            email='admin-unread-filter@test.com',
            name='Admin Unread Filter',
        )

        newest_ticket = MunicipioTicket(
            id=51,
            nro_ticket='M-NEWEST',
            estado='nuevo',
            fecha=get_local_now(),
            categoria='A',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
        )
        unread_ticket = MunicipioTicket(
            id=52,
            nro_ticket='M-UNREAD',
            estado='nuevo',
            fecha=get_local_now() - timedelta(hours=2),
            categoria='A',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
        )
        db.session.add_all([newest_ticket, unread_ticket])
        db.session.commit()

        comment = TicketComentario(
            municipio_ticket_id=unread_ticket.id,
            comentario='Vecino agrego informacion',
            user_id=admin_user.id,
            es_admin=False,
        )
        db.session.add(comment)
        db.session.flush()
        db.session.add(
            TicketRealtimeState(
                ticket_type='municipio',
                ticket_id=unread_ticket.id,
                viewer_key=f'user:{admin_user.id}',
                viewer_user_id=admin_user.id,
                viewer_role='admin',
                presence_status='active',
                last_presence_at=get_local_now(),
                last_read_comment_id=0,
            )
        )
        db.session.commit()

        with self.app.test_request_context('?unread=unread&per_page=1&include=compact'), patch('routes.ticket.get_current_tenant_profile', return_value=tenant):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 52)
            self.assertEqual(data['tickets'][0]['collaboration_state']['unread_viewer_count'], 1)

    def test_priority_filter_uses_ai_details_payload(self):
        admin_user, tenant = self._create_municipal_admin(
            email='admin-priority@test.com',
            name='Admin Priority',
        )

        normal_ticket = MunicipioTicket(
            id=61,
            nro_ticket='M-NORMAL',
            estado='nuevo',
            fecha=get_local_now(),
            categoria='A',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
        )
        high_priority_ticket = MunicipioTicket(
            id=62,
            nro_ticket='M-HIGH',
            estado='nuevo',
            fecha=get_local_now() - timedelta(minutes=10),
            categoria='A',
            municipio_id=admin_user.id,
            tenant_id=tenant.id,
            detalles=json.dumps({
                'prioridad_sugerida': 'alta',
                'prioridad_confianza': 0.87,
                'prioridad_provider': 'huggingface',
            }),
        )
        db.session.add_all([normal_ticket, high_priority_ticket])
        db.session.commit()

        with self.app.test_request_context('?priority=alta&per_page=1&include=compact'), patch('routes.ticket.get_current_tenant_profile', return_value=tenant):
            response = get_tickets_del_usuario_logic(admin_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data['pagination']['total_items'], 1)
            self.assertEqual(data['tickets'][0]['id'], 62)
            self.assertEqual(data['tickets'][0]['priority'], 'alta')
            self.assertEqual(data['tickets'][0]['priority_score'], 0.87)
            self.assertEqual(data['tickets'][0]['priority_breakdown']['provider'], 'huggingface')

    def test_categoria_filter_pyme(self):
        pyme_user = User(email='pyme@test.com', name='Pyme Test', rol='admin', rubro_id=7, tipo_chat='pyme')
        pyme_user.set_password('password')
        db.session.add(pyme_user)
        db.session.flush()

        tenant = TenantProfile(
            slug='pyme-filter-test',
            nombre='Pyme Filter Test',
            tipo='pyme',
            pyme_id=pyme_user.id,
            configuracion={},
        )
        db.session.add(tenant)
        db.session.flush()

        t1 = PymeTicket(id=1, nro_ticket=1, estado='abierto', fecha=datetime.now(), categoria='X', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7, tenant_id=tenant.id, pregunta="pregunta de prueba 1")
        t2 = PymeTicket(id=2, nro_ticket=2, estado='abierto', fecha=datetime.now(), categoria='Y', telefono=None, email=None, dni=None, estado_cliente=None, direccion=None, latitud=None, longitud=None, rubro_id=7, tenant_id=tenant.id, pregunta="pregunta de prueba 2")
        db.session.add_all([t1, t2])
        db.session.commit()

        with self.app.test_request_context('?categoria=X'), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(pyme_user)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['id'], 1)

    def test_municipio_user_without_id_returns_error(self):
        user = User(id=9, rol='empleado', municipio_id=None, tipo_chat='municipio')
        with self.app.test_request_context(), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(user)
            self.assertEqual(response.status_code, 403)
            data = response.get_json()
            self.assertEqual(data["access_contract"]["contract_version"], "tickets.access.v1")
            self.assertEqual(data["action_hint"], "repair_ticket_scope")
            self.assertEqual(data["reason_code"], "missing_municipal_scope")
            self.assertIn("tickets.read", data["required_capabilities"])
            self.assertEqual(data["current_scope"]["tipo_chat"], "municipio")
            self.assertEqual(response.headers.get("X-Request-Id"), data["request_id"])

    def test_pyme_user_without_scope_returns_repair_contract(self):
        user = User(id=10, rol='admin', municipio_id=None, rubro_id=None, tipo_chat='pyme')
        with self.app.test_request_context(headers={"X-Request-Id": "req-ticket-scope"}), patch('routes.ticket.get_current_tenant_profile', return_value=None):
            response = get_tickets_del_usuario_logic(user)
            self.assertEqual(response.status_code, 403)
            data = response.get_json()
            self.assertEqual(data["access_contract"]["contract_version"], "tickets.access.v1")
            self.assertEqual(data["action_hint"], "repair_ticket_scope")
            self.assertEqual(data["reason_code"], "missing_pyme_scope")
            self.assertEqual(data["request_id"], "req-ticket-scope")
            self.assertIn("reclamos.read", data["required_capabilities"])
            self.assertEqual(data["current_scope"]["tipo_chat"], "pyme")

if __name__ == '__main__':
    unittest.main()
