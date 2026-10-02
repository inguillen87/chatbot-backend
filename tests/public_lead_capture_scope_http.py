"""Public contact capture preserves anonymous chat and account boundaries."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__':
    prepare_process()

import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch


class PublicLeadCaptureScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.temp = tempfile.TemporaryDirectory(prefix='chatboc-public-lead-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)
        from database import db
        from models import User
        with cls.app.app_context():
            admin = User(name='Synthetic administrator', email='known-admin@example.invalid',
                rol='super_admin', tenant_id=cls.accounts['acceptance-b']['tenant_id'],
                tenant_slug='acceptance-b', telefono='synthetic-admin-phone', anon_id='admin-own-anon')
            admin.set_password(cls.password)
            db.session.add(admin); db.session.commit()
            cls.admin_id = admin.id

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove(); db.engine.dispose()
        cls.temp.cleanup()

    def setUp(self):
        from database import db
        from models import AnalyticsEventV2, ChatSessionContext, Conversacion, TenantConfig, TenantTicket, User
        self.client = self.app.test_client()
        with self.app.app_context():
            ChatSessionContext.query.delete(); Conversacion.query.delete()
            AnalyticsEventV2.query.delete(); TenantTicket.query.delete()
            TenantConfig.query.filter_by(key='institutional_assistant').delete()
            admin = db.session.get(User, self.admin_id)
            admin.name = 'Synthetic administrator'; admin.tenant_id = self.accounts['acceptance-b']['tenant_id']
            admin.tenant_slug = 'acceptance-b'; admin.telefono = 'synthetic-admin-phone'
            db.session.commit()

    def context(self, *, session='lead-scope-session', slug='acceptance-a',
                anon='lead-scope-anon', user_id=None):
        from database import db
        from models import ChatSessionContext
        with self.app.app_context():
            db.session.add(ChatSessionContext(chat_session_id=session,
                tenant_id=self.accounts[slug]['tenant_id'] if slug else None,
                anon_id=anon, user_id=user_id,
                context_data={'institutional_knowledge': {'revision': 'retained-revision'},
                    'operational_marker': {'ticket': 'retained'}}))
            db.session.commit()

    def post(self, *, body=None, headers=None, query=''):
        payload = {'tenant_slug': 'acceptance-a', 'chat_session_id': 'lead-scope-session',
            'anon_id': 'lead-scope-anon', 'name': 'Synthetic visitor',
            'email': 'known-admin@example.invalid', 'phone': 'synthetic-visitor-phone',
            'interest': 'Contactos por ciudad', 'message': 'Consulta de contacto sintética',
            'idempotency_key': 'lead-scope-key'}
        payload.update(body or {})
        request_headers = {'X-Anon-Id': 'lead-scope-anon',
            'X-Chat-Session-Id': 'lead-scope-session', 'X-Tenant-Slug': 'acceptance-a'}
        request_headers.update(headers or {})
        return self.client.post('/api/public/lead-capture' + query, json=payload, headers=request_headers)

    def snapshot(self):
        from models import AnalyticsEventV2, ChatSessionContext, Conversacion, TenantTicket, User
        with self.app.app_context():
            return {
                'users': [{column.name: deepcopy(getattr(user, column.name))
                    for column in User.__table__.columns} for user in User.query.order_by(User.id).all()],
                'contexts': [(row.chat_session_id, row.tenant_id, row.user_id, row.anon_id,
                    deepcopy(row.context_data)) for row in ChatSessionContext.query.order_by(ChatSessionContext.chat_session_id).all()],
                'counts': (TenantTicket.query.count(), Conversacion.query.count(), AnalyticsEventV2.query.count()),
            }

    @contextmanager
    def recorded_dml(self):
        from database import db
        from sqlalchemy import event
        statements = []
        def record(_connection, _cursor, statement, _parameters, _context, _many):
            verb = statement.lstrip().split(None, 1)[0].upper()
            if verb in {'INSERT', 'UPDATE', 'DELETE'}:
                statements.append(verb)
        with self.app.app_context():
            engine = db.engine
            event.listen(engine, 'before_cursor_execute', record)
        try:
            yield statements
        finally:
            event.remove(engine, 'before_cursor_execute', record)

    def assert_scope_rejected_without_dml(self, *, body=None, headers=None, query=''):
        before = self.snapshot()
        with self.recorded_dml() as dml:
            response = self.post(body=body, headers=headers, query=query)
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()['reason_code'], 'public_lead_capture_scope_conflict')
        self.assertEqual(dml, [])
        self.assertEqual(self.snapshot(), before)

    def test_known_administrator_email_is_contact_data_and_never_claims_anonymous_chat(self):
        self.context()
        before = self.snapshot()
        response = self.post()
        self.assertEqual(response.status_code, 200, response.get_json())
        after = self.snapshot()
        self.assertEqual(after['users'], before['users'])
        from models import AnalyticsEventV2, ChatSessionContext, Conversacion, TenantTicket
        with self.app.app_context():
            row = ChatSessionContext.query.get('lead-scope-session')
            self.assertIsNone(row.user_id)
            self.assertEqual(row.anon_id, 'lead-scope-anon')
            self.assertEqual(row.tenant_id, self.accounts['acceptance-a']['tenant_id'])
            self.assertEqual(row.context_data['institutional_knowledge'], {'revision': 'retained-revision'})
            self.assertEqual(row.context_data['operational_marker'], {'ticket': 'retained'})
            ticket = TenantTicket.query.one()
            self.assertIsNone(ticket.user_id)
            self.assertEqual(ticket.datos_extra['lead_profile']['email'], 'known-admin@example.invalid')
            self.assertIsNone(Conversacion.query.one().user_id)
            self.assertIsNone(AnalyticsEventV2.query.one().user_id)

    def test_explicit_contact_retry_preserves_crm_profile_and_idempotency(self):
        self.context()
        first = self.post(body={'email': 'new-contact@example.invalid'})
        self.assertEqual(first.status_code, 200, first.get_json())
        retry = self.post(body={'email': 'new-contact@example.invalid'})
        self.assertEqual(retry.status_code, 200, retry.get_json())
        self.assertEqual(retry.get_json()['ticket_id'], first.get_json()['ticket_id'])
        self.assertTrue(retry.get_json()['deduplicated'])
        from models import TenantTicket, ChatSessionContext
        with self.app.app_context():
            self.assertEqual(TenantTicket.query.count(), 1)
            ticket = TenantTicket.query.one()
            self.assertEqual(ticket.datos_extra['lead_profile']['nombre'], 'Synthetic visitor')
            self.assertEqual(ticket.datos_extra['lead_profile']['telefono'], 'synthetic-visitor-phone')
            self.assertIsNone(ChatSessionContext.query.get('lead-scope-session').user_id)

    def test_other_tenant_context_cannot_be_written_by_public_capture(self):
        self.context(slug='acceptance-b')
        self.assert_scope_rejected_without_dml()

    def test_other_anonymous_context_cannot_be_written_by_public_capture(self):
        self.context(anon='another-anon')
        self.assert_scope_rejected_without_dml()

    def test_identified_context_cannot_be_reused_even_with_matching_anonymous_identifier(self):
        self.context(user_id=self.admin_id)
        self.assert_scope_rejected_without_dml()

    def test_body_header_anonymous_identity_conflict_is_rejected_before_dml(self):
        self.context()
        self.assert_scope_rejected_without_dml(body={'anon_id': 'another-anon'})

    def test_body_header_session_conflict_is_rejected_before_dml(self):
        self.context()
        self.assert_scope_rejected_without_dml(body={'chat_session_id': 'another-session'})

    def test_body_header_tenant_conflict_is_rejected_before_dml(self):
        self.context()
        self.assert_scope_rejected_without_dml(headers={'X-Tenant-Slug': 'acceptance-b'})

    def test_same_idempotency_key_cannot_replay_another_anonymous_contact(self):
        self.context()
        first = self.post()
        self.assertEqual(first.status_code, 200, first.get_json())
        self.context(session='another-session', anon='another-anon')
        self.assert_scope_rejected_without_dml(
            body={'chat_session_id': 'another-session', 'anon_id': 'another-anon'},
            headers={'X-Chat-Session-Id': 'another-session', 'X-Anon-Id': 'another-anon'})

    def test_unknown_tenant_never_creates_contact_records(self):
        before = self.snapshot()
        with self.recorded_dml() as dml:
            response = self.post(body={'tenant_slug': 'unknown'}, headers={'X-Tenant-Slug': 'unknown'})
        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertEqual(dml, [])
        self.assertEqual(self.snapshot(), before)

    def test_contact_capture_cannot_interrupt_next_published_institutional_action(self):
        from database import db
        from models import Rubro, TenantProfile, User
        from tests.test_institutional_assistant_content import sample
        from services.logic import responder_chatboc
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
            tenant.plan = 'full'
            owner = db.session.get(User, tenant.municipio_id)
            rubro = Rubro.query.filter_by(clave='municipio').first()
            if rubro is None:
                rubro = Rubro(clave='municipio', nombre='Municipio', es_publico=True)
                db.session.add(rubro); db.session.flush()
            owner.rubro_id = rubro.id
            db.session.commit()
        publisher = self.app.test_client()
        login = publisher.post('/auth/login', json={'email': self.accounts['acceptance-a']['email'],
            'password': self.password})
        self.assertEqual(login.status_code, 200, login.get_json())
        bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        bundle['nodes']['human'] = deepcopy(bundle['nodes']['requirements'])
        bundle['nodes']['human'].update(id='human', title='Contactos de prueba')
        bundle['nodes']['start']['actions'].append({'code': '2', 'label': 'Contactos por ciudad', 'target': 'human'})
        bundle['node_evidence']['human'] = deepcopy(bundle['node_evidence']['requirements'])
        bundle['sources']['a']['document_visibility'] = 'private'
        url = '/api/admin/tenants/acceptance-a/institutional-assistant'
        imported = publisher.put(url, json={'operation': 'import', 'expected_revision': None, 'bundle': bundle},
            headers={'X-Chatboc-Knowledge': '1'})
        self.assertEqual(imported.status_code, 200, imported.get_json())
        published = publisher.put(url, json={'operation': 'publish',
            'expected_revision': imported.get_json()['revision']}, headers={'X-Chatboc-Knowledge': '1'})
        self.assertEqual(published.status_code, 200, published.get_json())
        revision = published.get_json()['revision']

        def chat(node, key):
            action = 'knowledge:' + revision[:16] + ':' + node
            return self.client.post('/api/ask/municipio', json={
                'tenant': 'acceptance-a', 'tenant_slug': 'acceptance-a',
                'session_id': 'lead-scope-session', 'tipo_chat': 'municipio',
                'pregunta': 'Contactos por ciudad' if node == 'human' else 'Consulta de prueba',
                'action': action, 'action_id': action, 'button_source': 'button'},
                headers={'X-Anon-Id': 'lead-scope-anon', 'Idempotency-Key': key})

        with patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
             patch('services.llm_utils.llamar_llm_para_json_estructurado', side_effect=AssertionError('explicit menu must not use model')), \
             patch('services.municipio_responder.responder_municipio', side_effect=AssertionError('explicit menu must not use generic flow')):
            for index in range(3):
                response = chat('requirements', 'lead-chat-' + str(index))
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()['fuente'], 'institutional_knowledge')
            captured = self.post()
            self.assertEqual(captured.status_code, 200, captured.get_json())
            response = chat('human', 'lead-chat-contact')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['fuente'], 'institutional_knowledge')
        self.assertEqual(response.get_json()['knowledge_nodes'][0]['id'], 'human')
        self.assertEqual(response.get_json()['knowledge_tenant'], {
            'id': self.accounts['acceptance-a']['tenant_id'], 'slug': 'acceptance-a'})
        self.assertEqual(response.get_json()['context_revision'], revision)

    def test_same_key_with_changed_contact_details_is_rejected_without_dml(self):
        self.context()
        first = self.post()
        self.assertEqual(first.status_code, 200, first.get_json())
        before = self.snapshot()
        with self.recorded_dml() as dml:
            response = self.post(body={'email': 'changed-contact@example.invalid'})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()['reason_code'], 'public_lead_capture_idempotency_conflict')
        self.assertEqual(dml, [])
        self.assertEqual(self.snapshot(), before)

    def test_conflicting_body_header_idempotency_keys_are_rejected_without_dml(self):
        self.context()
        self.assert_scope_rejected_without_dml(headers={'Idempotency-Key': 'different-key'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
