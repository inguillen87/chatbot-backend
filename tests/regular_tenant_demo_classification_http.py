"""Actual chat decorators/persistence on an isolated database, no providers."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__':
    prepare_process()
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch


class RegularTenantChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.temp = tempfile.TemporaryDirectory(prefix='chatboc-regular-chat-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.temp.cleanup()

    def setUp(self):
        from database import db
        from models import ChatSessionContext, Conversacion, Rubro, TenantProfile, User, TenantConfig
        self.client = self.app.test_client()
        self.resolved_owners = []
        self.app.config.update(DEMO_MAX_MESSAGES_PER_SESSION=2,
            DEMO_ANONYMOUS_MAX_MESSAGES_PER_SESSION=1,
            ANONYMOUS_MAX_MESSAGES_PER_SESSION=50,
            DEMO_RUBROS=[{'key': 'registered-demo', 'nombre': 'Registered demo',
                'tipo_chat': 'municipio', 'rubro_clave': 'regular-chat-a',
                'token': 'registered-demo-token'}])
        with self.app.app_context():
            ChatSessionContext.query.delete()
            Conversacion.query.delete()
            TenantConfig.query.filter_by(key='institutional_assistant').delete()
            for key, suffix in (('acceptance-a', 'a'), ('acceptance-b', 'b')):
                tenant = db.session.get(TenantProfile, self.accounts[key]['tenant_id'])
                owner = db.session.get(User, tenant.municipio_id or tenant.pyme_id)
                rubro = Rubro.query.filter_by(clave='regular-chat-' + suffix).first()
                if rubro is None:
                    rubro = Rubro(clave='regular-chat-' + suffix, nombre='Fixture ' + suffix, es_publico=True)
                    db.session.add(rubro); db.session.flush()
                tenant.tipo = 'municipio'; tenant.plan = 'full'; tenant.is_active = True
                tenant.configuracion = {}; tenant.municipio_id = owner.id; tenant.pyme_id = None
                owner.tipo_chat = 'municipio'; owner.plan = 'gratis'; owner.preguntas_usadas = 999
                owner.limite_preguntas = 50; owner.rubro_id = rubro.id; owner.token = 'regular-owner-' + suffix
            db.session.commit()

    def state(self, slug='acceptance-a', **changes):
        from database import db
        from models import TenantProfile, User
        with self.app.app_context():
            tenant = TenantProfile.query.filter_by(slug=slug).one()
            owner = db.session.get(User, tenant.municipio_id or tenant.pyme_id)
            for key, value in changes.items():
                target, field = key.split('__', 1)
                setattr(tenant if target == 'tenant' else owner, field, value)
            db.session.commit()
            return tenant.id, owner.id

    def context(self, data, *, tenant='acceptance-a', session='regular-session', anon='regular-anon'):
        from database import db
        from models import ChatSessionContext
        tenant_id = self.accounts[tenant]['tenant_id'] if tenant else None
        with self.app.app_context():
            db.session.add(ChatSessionContext(chat_session_id=session, tenant_id=tenant_id,
                anon_id=anon, context_data=deepcopy(data)))
            db.session.commit()

    def stored(self, session='regular-session'):
        from models import ChatSessionContext
        with self.app.app_context():
            row = ChatSessionContext.query.filter_by(chat_session_id=session).one()
            return row.tenant_id, deepcopy(row.context_data)

    def post(self, *, slug='acceptance-a', endpoint='/api/ask/municipio', session='regular-session',
             anon='regular-anon', question='consulta institucional', body=None, headers=None):
        payload = {'pregunta': question, 'tenant': slug, 'tenant_slug': slug,
            'session_id': session, 'tipo_chat': 'municipio' if 'municipio' in endpoint else 'pyme'}
        payload.update(body or {})
        request_headers = {'Origin': 'https://www.chatboc.ar', 'X-Anon-Id': anon}
        request_headers.update(headers or {})
        return self.client.post(endpoint, json=payload, headers=request_headers)

    def responder(self):
        def respond(*args, **kwargs):
            self.resolved_owners.append(kwargs['owner_user'].id)
            return {'message_body': 'Fixture response', 'message_type': 'text', 'fuente': 'local-fixture'}
        return patch('routes.chat.responder_chatboc', side_effect=respond)

    def test_full_canonical_tenant_normal_request_and_init_never_consume_demo_quota(self):
        for endpoint in ('/api/ask/municipio', '/ask/municipio'):
            with self.subTest(endpoint=endpoint), self.responder() as responder:
                response = self.post(endpoint=endpoint, session=endpoint, question='__INIT__')
                self.assertEqual(response.status_code, 200, response.get_json())
                for index in range(3):
                    response = self.post(endpoint=endpoint, session=endpoint, question='consulta ' + str(index))
                    self.assertEqual(response.status_code, 200, response.get_json())
                    self.assertIsNone(response.get_json()['limite_preguntas'])
                self.assertEqual(responder.call_count, 4)
                self.assertEqual(self.resolved_owners[-1], self.accounts['acceptance-a']['id'])
            tenant_id, data = self.stored(endpoint)
            self.assertEqual(tenant_id, self.accounts['acceptance-a']['tenant_id'])
            self.assertNotIn('demo_session', data)
            self.assertNotIn('demo_message_count', data)

    def test_proven_old_v2_context_clears_flags_but_preserves_historical_counter(self):
        data = {'demo_session': True, 'demo_session_source': 'v2_demo',
            'demo_key': 'acceptance-a', 'demo_message_count': 2,
            'demo_session_id': 'regular-session', 'operational_note': 'keep'}
        self.context(data)
        with self.responder() as responder:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.get_json())
        responder.assert_called_once()
        _, stored = self.stored()
        self.assertNotIn('demo_session', stored); self.assertNotIn('demo_key', stored)
        self.assertEqual(stored['demo_message_count'], 2)
        self.assertEqual(stored['operational_note'], 'keep')

    def test_explicit_demo_keeps_quota_even_for_full_tenant(self):
        self.context({'demo_session': True, 'demo_session_source': 'v2_demo',
            'demo_key': 'acceptance-a', 'demo_message_count': 2})
        with self.responder() as responder:
            response = self.post(body={'demo_mode': True})
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()['reason_code'], 'demo_message_limit_reached')
        responder.assert_not_called()
        self.assertEqual(self.stored()[1]['demo_message_count'], 3)

    def test_unproven_v2_demo_key_is_preserved_instead_of_cleared_as_contamination(self):
        self.state(owner__rubro_id=None)
        self.context({'demo_session': True, 'demo_session_source': 'v2_demo',
            'demo_key': None, 'demo_message_count': 2})
        with self.responder() as responder:
            response = self.post()
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()['reason_code'], 'demo_message_limit_reached')
        responder.assert_not_called()
        self.assertEqual(self.stored()[1]['demo_message_count'], 3)

    def test_signed_demo_credential_keeps_quota(self):
        from routes.v2.tenants import create_demo_session_token
        with self.app.app_context():
            token = create_demo_session_token(tenant_slug='acceptance-a', sector='municipio')
        self.context({'demo_session': True, 'demo_key': 'acceptance-a', 'demo_message_count': 2})
        with self.responder() as responder:
            response = self.post(body={'demo_session_id': token})
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()['reason_code'], 'demo_message_limit_reached')
        responder.assert_not_called()

    def test_server_owned_demo_flags_keep_demo_quota_for_ordinary_selectors(self):
        for flag in ('demo_mode', 'trial_mode', 'chatboc_demo_hub', 'is_demo_tenant'):
            self.state(tenant__configuracion={flag: True})
            self.context({'demo_session': True, 'demo_session_source': 'v2_demo',
                'demo_key': 'acceptance-a', 'demo_message_count': 2}, session=flag)
            with self.subTest(flag=flag), self.responder() as responder:
                response = self.post(session=flag)
                self.assertEqual(response.status_code, 403, response.get_json())
                self.assertEqual(response.get_json()['reason_code'], 'demo_message_limit_reached')
                responder.assert_not_called()
            self.assertTrue(self.stored(flag)[1]['demo_session'])
            self.assertEqual(self.stored(flag)[1]['demo_message_count'], 3)

    def test_registered_legacy_demo_keeps_quota_and_selection(self):
        _, owner_id = self.state(owner__token='registered-demo-token')
        self.context({'demo_session': True, 'demo_key': 'registered-demo',
            'demo_owner_user_id': owner_id, 'demo_message_count': 2})
        with self.responder() as responder:
            response = self.post(headers={'X-Token': 'registered-demo-token'})
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()['reason_code'], 'demo_message_limit_reached')
        responder.assert_not_called()
        self.assertEqual(self.stored()[1]['demo_key'], 'registered-demo')

    def test_regular_anonymous_limit_is_retained_and_not_marketing_demo_limit(self):
        from database import db
        from models import Conversacion
        self.app.config['ANONYMOUS_MAX_MESSAGES_PER_SESSION'] = 2
        with self.app.app_context():
            db.session.add(Conversacion(session_id='regular-session', pregunta='prior real question',
                respuesta='ok', fuente='fixture'))
            db.session.commit()
        with self.responder() as responder:
            first = self.post()
            self.assertEqual(first.status_code, 200, first.get_json())
            # Explicit history fixture: downstream CRM persistence is not the
            # quota boundary under test and its SQLite adapter is independent.
            with self.app.app_context():
                db.session.add(Conversacion(session_id='regular-session', pregunta='second prior real question',
                    respuesta='ok', fuente='fixture'))
                db.session.commit()
            second = self.post(question='new question after quota')
            self.assertEqual(second.status_code, 403, second.get_json())
            self.assertEqual(second.get_json()['reason_code'], 'anonymous_message_limit_reached')
            self.assertEqual(second.get_json()['trial_usage']['limit'], 2)
            self.assertEqual(responder.call_count, 1)

    def test_canonical_plan_free_pro_and_unknown_cannot_inherit_owner_full_or_payload_full(self):
        for plan, limit in (('free', 50), ('gratis', 50), ('pro', 250), ('unsupported', 50)):
            with self.subTest(plan=plan):
                self.state(tenant__plan=plan, owner__plan='full', owner__preguntas_usadas=limit)
                with self.responder() as responder:
                    response = self.post(session=plan, body={'plan': 'full', 'capabilities': ['*']})
                self.assertEqual(response.status_code, 403, response.get_json())
                self.assertEqual(response.get_json()['reason_code'], 'owner_plan_limit_reached')
                self.assertEqual(response.get_json()['trial_usage']['limit'], limit)
                responder.assert_not_called()

    def test_unknown_inactive_and_conflicting_tenant_fail_before_responder_or_cleanup(self):
        poisoned = {'demo_session': True, 'demo_session_source': 'v2_demo',
            'demo_key': 'acceptance-a', 'demo_message_count': 2}
        cases = [('missing-tenant', {}, {}), ('acceptance-a', {'tenant': 'acceptance-b'}, {}),
                 ('acceptance-a', {}, {'X-Tenant-Slug': 'acceptance-b'})]
        for index, (slug, body, headers) in enumerate(cases):
            session = 'negative-' + str(index)
            self.context(poisoned, session=session)
            with self.subTest(index=index), self.responder() as responder:
                response = self.post(slug=slug, session=session, body=body, headers=headers)
                self.assertEqual(response.status_code, 403, response.get_json())
                responder.assert_not_called()
                self.assertEqual(self.stored(session)[1], poisoned)
        self.state(tenant__is_active=False)
        with self.responder() as responder:
            response = self.post(session='inactive', body={'plan': 'full'})
        self.assertEqual(response.status_code, 403, response.get_json()); responder.assert_not_called()

    def test_canonical_pro_quota_allows_last_available_question_and_reports_same_limit(self):
        self.state(tenant__plan='pro', owner__plan='gratis', owner__preguntas_usadas=249)
        with self.responder() as responder:
            response = self.post(body={'plan': 'gratis'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['limite_preguntas'], 250)
        responder.assert_called_once()

    def test_generic_default_registered_demo_owner_does_not_turn_other_regular_tenant_into_demo(self):
        self.state(owner__token='registered-demo-token')
        self.context({'demo_session': True, 'demo_session_source': 'v2_demo',
            'demo_key': 'acceptance-b', 'demo_message_count': 2}, tenant='acceptance-b')
        with self.responder() as responder:
            response = self.post(slug='acceptance-b')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.resolved_owners[-1], self.accounts['acceptance-b']['id'])
        self.assertFalse(response.get_json()['ux_context']['demo_session'])
        self.assertEqual(self.stored()[1]['demo_message_count'], 2)

    def test_foreign_tenant_and_anonymous_session_cannot_clear_poison_or_rebind(self):
        data = {'demo_session': True, 'demo_session_source': 'v2_demo',
            'demo_key': 'acceptance-a', 'demo_message_count': 2}
        for session, tenant, anon in (('foreign-tenant', 'acceptance-b', 'regular-anon'),
                                     ('foreign-anon', 'acceptance-a', 'other-anon')):
            self.context(data, tenant=tenant, session=session, anon=anon)
            with self.subTest(session=session), self.responder() as responder:
                response = self.post(session=session)
                self.assertIn(response.status_code, (403, 409), response.get_json())
                responder.assert_not_called()
            self.assertEqual(self.stored(session), (self.accounts[tenant]['tenant_id'], data))

    def test_explicit_entity_owner_cannot_select_another_regular_tenant(self):
        with self.responder() as responder:
            response = self.post(slug='acceptance-b', headers={'X-Token': 'regular-owner-a'})
        self.assertEqual(response.status_code, 403, response.get_json()); responder.assert_not_called()

    def test_valid_widget_jwt_keeps_owner_binding_with_and_without_idempotency_and_pyme(self):
        from services.auth_session_lifecycle import issue_token
        from datetime import datetime, timedelta, timezone
        with self.app.app_context():
            token = issue_token({'user_id': self.accounts['acceptance-a']['id'],
                'session_kind': 'widget', 'exp': datetime.now(timezone.utc) + timedelta(hours=1)})
        for endpoint, extra in (('/api/ask/municipio', {}),
                                ('/api/ask/municipio', {'Idempotency-Key': 'widget-cross-scope'}),
                                ('/api/ask/pyme', {})):
            with self.subTest(endpoint=endpoint, keyed=bool(extra)), self.responder() as responder:
                response = self.post(slug='acceptance-b', endpoint=endpoint,
                    session=endpoint + str(bool(extra)), headers={'Authorization': 'Bearer ' + token, **extra})
                self.assertIn(response.status_code, (403, 409), response.get_json())
                responder.assert_not_called()
        with self.responder() as responder:
            response = self.post(headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.resolved_owners[-1], self.accounts['acceptance-a']['id'])

    def test_pyme_and_colegio_normal_contexts_use_same_bound_non_demo_path(self):
        from database import db
        from models import TenantProfile
        for tipo in ('pyme', 'colegio'):
            _, owner_id = self.state(tenant__tipo=tipo, owner__tipo_chat='pyme')
            with self.app.app_context():
                tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
                tenant.pyme_id = owner_id; tenant.municipio_id = None; db.session.commit()
            session = tipo + '-regular'
            self.context({'demo_session': True, 'demo_session_source': 'v2_demo',
                'demo_key': 'acceptance-a', 'demo_message_count': 2}, session=session)
            with self.subTest(tipo=tipo), self.responder() as responder:
                response = self.post(endpoint='/api/ask/pyme', session=session)
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(self.resolved_owners[-1], owner_id)
            self.assertEqual(self.stored(session)[1]['demo_message_count'], 2)
            self.assertNotIn('demo_session', self.stored(session)[1])

    def test_published_institutional_menu_real_pipeline_after_contaminated_demo_limit(self):
        from tests.test_institutional_assistant_content import sample
        from services.logic import responder_chatboc
        login = self.client.post('/auth/login', json={'email': self.accounts['acceptance-a']['email'], 'password': self.password})
        self.assertEqual(login.status_code, 200, login.get_json())
        url = '/api/admin/tenants/acceptance-a/institutional-assistant'
        imported = self.client.put(url, json={'operation': 'import', 'expected_revision': None,
            'bundle': sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')},
            headers={'X-Chatboc-Knowledge': '1'})
        self.assertEqual(imported.status_code, 200, imported.get_json())
        published = self.client.put(url, json={'operation': 'publish', 'expected_revision': imported.get_json()['revision']},
            headers={'X-Chatboc-Knowledge': '1'})
        self.assertEqual(published.status_code, 200, published.get_json())
        self.context({'demo_session': True, 'demo_session_source': 'v2_demo',
            'demo_key': 'acceptance-a', 'demo_message_count': 2,
            'rubro_tool_summary': {'tools': ['old-demo-tool']}})
        with patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
             patch('services.llm_utils.llamar_llm_para_json_estructurado', side_effect=AssertionError('menu cannot use model')):
            response = self.post(question='__INIT__', body={
                'demo_metadata': {'key': 'caller-cannot-force-demo'},
                'rubro_tool_summary': {'tools': ['caller-tool']},
                'chat_bootstrap': {'payload': {'rubro_tool_summary': {'tools': ['bootstrap-tool']}}}})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['fuente'], 'institutional_knowledge')
        self.assertEqual(response.get_json()['knowledge_tenant']['id'], self.accounts['acceptance-a']['tenant_id'])
        self.assertEqual(response.get_json()['context_revision'], published.get_json()['revision'])
        self.assertEqual(self.stored()[1]['demo_message_count'], 2)

    def published_knowledge_for_public_chat(self):
        from tests.test_institutional_assistant_content import sample
        from database import db
        from models import Rubro, TenantProfile, User
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, self.accounts['acceptance-a']['tenant_id'])
            owner = db.session.get(User, tenant.municipio_id)
            # The real TDF request reaches the municipal responder. Its
            # persisted sector key, rather than the unrelated "regular-chat-a"
            # fixture key, is the routing authority exercised here.
            rubro = Rubro.query.filter_by(clave='municipio').first()
            if rubro is None:
                rubro = Rubro(clave='municipio', nombre='Municipio', es_publico=True)
                db.session.add(rubro); db.session.flush()
            owner.rubro_id = rubro.id
            db.session.commit()
        login = self.client.post('/auth/login', json={
            'email': self.accounts['acceptance-a']['email'], 'password': self.password})
        self.assertEqual(login.status_code, 200, login.get_json())
        bundle = sample(self.accounts['acceptance-a']['tenant_id'], 'acceptance-a')
        bundle['nodes']['start']['actions'][0]['label'] = '🪪 Certificados: CUD y CMO'
        bundle['sources']['a'].update(document_visibility='private',
            origin_url='https://drive.google.com/file/d/private-fixture-original/view')
        url = '/api/admin/tenants/acceptance-a/institutional-assistant'
        imported = self.client.put(url, json={'operation': 'import', 'expected_revision': None,
            'bundle': bundle}, headers={'X-Chatboc-Knowledge': '1'})
        self.assertEqual(imported.status_code, 200, imported.get_json())
        published = self.client.put(url, json={'operation': 'publish',
            'expected_revision': imported.get_json()['revision']}, headers={'X-Chatboc-Knowledge': '1'})
        self.assertEqual(published.status_code, 200, published.get_json())
        # The embed's chat POST is anonymous even when an admin published it.
        self.client = self.app.test_client()
        return published.get_json(), url

    def assert_public_knowledge_answer(self, response, published):
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['fuente'], 'institutional_knowledge')
        self.assertEqual(payload['knowledge_tenant'], {
            'id': self.accounts['acceptance-a']['tenant_id'], 'slug': 'acceptance-a'})
        self.assertEqual(payload['context_revision'], published['revision'])
        self.assertEqual(payload['knowledge_nodes'][0]['id'], 'requirements')
        self.assertIn('Respuesta institucional de prueba.', payload['message_body'])
        self.assertTrue(payload['ux_context']['trusted_owner'])
        self.assertFalse(payload['ux_context']['demo_session'])
        self.assertFalse(payload['ux_context']['should_render_demo_shell'])
        for source in payload['knowledge_sources']:
            self.assertEqual(source['document_visibility'], 'private')
            self.assertFalse(source['delivery']['publicly_accessible'])
            self.assertNotIn('url', source); self.assertNotIn('origin_url', source)
        self.assertNotIn('private-fixture-original', str(payload))
        return payload

    def test_real_widget_knowledge_button_label_and_action_avoid_complaint_dispatch(self):
        from services.logic import responder_chatboc
        from services.constants import CONTEXTO_MUNICIPIO
        published, _ = self.published_knowledge_for_public_chat()
        operation = {'estado_conversacion': 'ESPERANDO_INFO_RECLAMO_LLM',
            'expected_fields_llm_reclamo': ['descripcion', 'email']}
        self.context({CONTEXTO_MUNICIPIO: operation})
        action = 'knowledge:' + published['revision'][:16] + ':requirements'
        with patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
             patch('services.llm_utils.llamar_llm_para_json_estructurado', side_effect=AssertionError('exact menu must not use model')), \
             patch('services.municipio_responder.responder_municipio', side_effect=AssertionError('knowledge action must not start complaint')):
            response = self.post(question='🪪 Certificados: CUD y CMO', body={
                'action': action, 'action_id': action, 'button_source': 'button',
                'contexto_previo': {'estado_conversacion': 'inicio', 'id_ticket_creado': None}})
        self.assert_public_knowledge_answer(response, published)
        self.assertEqual(self.stored()[1][CONTEXTO_MUNICIPIO], operation)

    def test_regular_question_after_general_history_uses_published_source_selector(self):
        from services.logic import responder_chatboc
        from services.constants import CONTEXTO_MUNICIPIO
        published, _ = self.published_knowledge_for_public_chat()
        general = {'estado_conversacion': 'CONVERSACION_GENERAL_LLM',
            'historial_conversacion_general_llm': [{'user': 'Consulta anterior de prueba'}]}
        self.context({CONTEXTO_MUNICIPIO: general})
        question = '¿Qué documentación puedo consultar sobre el CUD?'
        with patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
             patch('services.llm_utils.llamar_llm_para_json_estructurado', return_value={'node_ids': ['requirements']}) as selector, \
             patch('services.municipio_responder.responder_municipio', side_effect=AssertionError('matched source must not use free-form complaint handler')):
            response = self.post(question=question)
        self.assert_public_knowledge_answer(response, published)
        selector.assert_called_once()
        self.assertEqual(__import__('json').loads(selector.call_args.args[1])['question'], question)
        self.assertEqual(self.stored()[1][CONTEXTO_MUNICIPIO], general)

    def test_fresh_knowledge_navigation_then_freeform_uses_published_source_selector(self):
        from services.logic import responder_chatboc
        from services.constants import CONTEXTO_MUNICIPIO
        published, _ = self.published_knowledge_for_public_chat()
        revision = published['revision']
        with patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
             patch('services.llm_utils.llamar_llm_para_json_estructurado', side_effect=AssertionError('navigation must not query model')), \
             patch('services.municipio_responder.responder_municipio', side_effect=AssertionError('navigation must not start operation')):
            for index, node_id in enumerate(('requirements', 'start', 'requirements')):
                action = 'knowledge:' + revision[:16] + ':' + node_id
                response = self.post(question='Opción institucional de prueba ' + str(index), body={
                    'action': action, 'action_id': action, 'button_source': 'button',
                    'contexto_previo': {'estado_conversacion': 'inicio', 'datos_reclamo': {
                        'categoria': None, 'descripcion': None, 'ubicacion': None},
                        'historial_conversacion': [], 'id_ticket_creado': None}})
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()['fuente'], 'institutional_knowledge')
                self.assertEqual(response.get_json()['context_revision'], revision)
        before = self.stored()[1]
        self.assertEqual(before.get(CONTEXTO_MUNICIPIO), {})
        self.assertEqual(before['institutional_knowledge']['revision'], revision)
        self.assertEqual(before['institutional_knowledge']['node_id'], 'requirements')
        question = '¿Cómo pido requisitos de CUD en Río Grande?'
        with patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
             patch('services.llm_utils.llamar_llm_para_json_estructurado', return_value={'node_ids': ['requirements']}) as selector, \
             patch('services.municipio_responder.responder_municipio', side_effect=AssertionError('matched institutional question must not reach direct LLM')):
            response = self.post(question=question, body={'button_source': 'input',
                'contexto_previo': {'estado_conversacion': 'inicio', 'datos_reclamo': {
                    'categoria': None, 'descripcion': None, 'ubicacion': None},
                    'historial_conversacion': [{'role': 'assistant', 'text': 'Menú de prueba'}],
                    'id_ticket_creado': None}})
        self.assert_public_knowledge_answer(response, published)
        selector.assert_called_once()
        request = __import__('json').loads(selector.call_args.args[1])
        self.assertEqual(request['question'], question)
        self.assertEqual(request['current_node'], 'requirements')
        self.assertEqual(self.stored()[1].get(CONTEXTO_MUNICIPIO), {})

    def test_empty_institutional_selection_keeps_unknown_answer_and_published_choices(self):
        from services.logic import responder_chatboc
        from services.constants import CONTEXTO_MUNICIPIO
        from services.institutional_assistant import UI
        published, _ = self.published_knowledge_for_public_chat()
        revision = published['revision']
        self.context({'institutional_knowledge': {'revision': revision, 'node_id': 'requirements'},
            CONTEXTO_MUNICIPIO: {}})
        with patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
             patch('services.llm_utils.llamar_llm_para_json_estructurado', return_value={'node_ids': []}) as selector, \
             patch('services.municipio_responder.responder_municipio', return_value={
                 'message_body': 'Unfunded generic answer fixture', 'fuente': 'llm_respuesta_directa'}) as direct:
            response = self.post(question='¿Cómo pido requisitos de CUD en Río Grande?',
                body={'button_source': 'input', 'contexto_previo': {
                    'estado_conversacion': 'inicio', 'datos_reclamo': {'descripcion': None},
                    'id_ticket_creado': None}})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['fuente'], 'institutional_knowledge_unknown_question')
        self.assertEqual(payload['message_body'], UI['unknown'])
        self.assertNotIn('Unfunded generic answer fixture', str(payload))
        self.assertEqual(payload['ux_context']['tenant_slug'], 'acceptance-a')
        self.assertEqual(payload['ux_context']['owner_user_id'], self.accounts['acceptance-a']['id'])
        self.assertTrue(payload['ux_context']['trusted_owner'])
        self.assertEqual(payload['context_revision'], revision)
        self.assertEqual([(b['texto'], b['action_id']) for b in payload['botones']], [
            ('🪪 Certificados: CUD y CMO', 'knowledge:' + revision[:16] + ':requirements')])
        self.assertNotIn('knowledge_nodes', payload)
        self.assertNotIn('private-fixture-original', str(payload))
        selector.assert_called_once(); direct.assert_not_called()
        stored = self.stored()[1]
        self.assertEqual(stored[CONTEXTO_MUNICIPIO], {})
        self.assertEqual(stored['institutional_knowledge']['node_id'], 'start')

    def test_invalid_knowledge_action_never_falls_through_to_complaint(self):
        from services.logic import responder_chatboc
        published, _ = self.published_knowledge_for_public_chat()
        actions = ('knowledge:' + '0' * 16 + ':requirements',
            'knowledge:' + published['revision'][:16] + ':unknown-node')
        for index, action in enumerate(actions):
            with self.subTest(action=index), patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
                 patch('services.llm_utils.llamar_llm_para_json_estructurado', side_effect=AssertionError('invalid explicit menu must not query model')), \
                 patch('services.municipio_responder.responder_municipio', side_effect=AssertionError('invalid explicit menu must not start complaint')):
                response = self.post(session='invalid-knowledge-' + str(index), question='🪪 Certificados: CUD y CMO',
                    body={'action': action, 'action_id': action, 'button_source': 'button'})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['fuente'], 'institutional_knowledge_stale')

    def test_exact_municipal_command_keeps_operational_route_but_negations_and_questions_keep_corpus(self):
        from services.logic import responder_chatboc
        from services.constants import CONTEXTO_MUNICIPIO
        published, _ = self.published_knowledge_for_public_chat()
        with patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
             patch('services.llm_utils.llamar_llm_para_json_estructurado', side_effect=AssertionError('exact command must keep operational route')) as selector, \
             patch('services.municipio_responder.responder_municipio', return_value={
                 'message_body': 'Menú operacional de prueba', 'fuente': 'operational'}) as operational:
            response = self.post(session='exact-operational-command', question='iniciar un reclamo')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['fuente'], 'operational')
        selector.assert_not_called(); operational.assert_called_once()
        for index, question in enumerate(('No quiero iniciar un reclamo',
            '¿Cómo iniciar un reclamo?', '¿Cómo pido requisitos de CUD en Río Grande?')):
            session = 'informational-or-negated-' + str(index)
            with self.subTest(question=index), \
                 patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
                 patch('services.llm_utils.llamar_llm_para_json_estructurado', return_value={'node_ids': []}) as selector, \
                 patch('services.municipio_responder.responder_municipio', side_effect=AssertionError('question or negation must not start operation')) as operational:
                response = self.post(session=session, question=question)
            self.assertEqual(response.status_code, 200, response.get_json())
            payload = response.get_json()
            self.assertEqual(payload['fuente'], 'institutional_knowledge_unknown_question')
            self.assertEqual(payload['context_revision'], published['revision'])
            self.assertTrue(payload['botones'])
            selector.assert_called_once(); operational.assert_not_called()
            self.assertEqual(self.stored(session)[1][CONTEXTO_MUNICIPIO], {})

    def test_active_or_unknown_operation_and_explicit_business_action_keep_priority(self):
        from services.logic import responder_chatboc
        from services.constants import CONTEXTO_MUNICIPIO
        self.published_knowledge_for_public_chat()
        operations = ({'estado_conversacion': 'ESPERANDO_INFO_RECLAMO_LLM'},
            {'stage': 'location'}, {'estado_conversacion': 'CONVERSACION_GENERAL_LLM',
                'expected_fields_llm_reclamo': ['email']},
            {'estado_conversacion': 'CONVERSACION_GENERAL_LLM'})
        for index, operation in enumerate(operations):
            session = 'active-operation-' + str(index)
            self.context({CONTEXTO_MUNICIPIO: operation}, session=session)
            action = {'action': 'iniciar_reclamo', 'action_id': 'iniciar_reclamo'} if index == 3 else {}
            with self.subTest(operation=index), patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
                 patch('services.llm_utils.llamar_llm_para_json_estructurado', side_effect=AssertionError('active operation must keep control')), \
                 patch('services.municipio_responder.responder_municipio', return_value={
                    'message_body': 'Respuesta operacional de prueba', 'fuente': 'operational'}) as operational:
                response = self.post(session=session, question='¿Qué documentación puedo consultar sobre el CUD?', body=action)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['fuente'], 'operational')
            operational.assert_called_once()
            self.assertEqual(self.stored(session)[1][CONTEXTO_MUNICIPIO], operation)

    def test_legacy_human_handoff_without_top_level_ticket_keeps_priority(self):
        from services.logic import responder_chatboc
        from services.constants import CONTEXTO_MUNICIPIO
        self.published_knowledge_for_public_chat()
        markers = {'human_chat_in_progress': True, 'live_chat_ticket_id': 928,
            'live_chat_estado': 'waiting_agent', 'live_chat_socket_room': 'fixture-human-room',
            'live_chat_status': 'waiting_agent'}
        for scope in ('municipal', 'session'):
            for index, (marker, value) in enumerate(markers.items()):
                session = 'legacy-human-' + scope + '-' + str(index)
                general = {'estado_conversacion': 'CONVERSACION_GENERAL_LLM'}
                context = {CONTEXTO_MUNICIPIO: general}
                (general if scope == 'municipal' else context)[marker] = value
                self.context(context, session=session)
                with self.subTest(scope=scope, marker=marker), \
                     patch('routes.chat.responder_chatboc', wraps=responder_chatboc), \
                     patch('services.llm_utils.llamar_llm_para_json_estructurado', side_effect=AssertionError('human handoff must keep control')), \
                     patch('services.municipio_responder.responder_municipio', return_value={
                        'message_body': 'Atención humana de prueba', 'fuente': 'operational'}) as operational:
                    response = self.post(session=session, question='¿Qué documentación puedo consultar sobre el CUD?')
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()['fuente'], 'operational')
                operational.assert_called_once()
                stored = self.stored(session)[1]
                for key, expected in context.items():
                    self.assertEqual(stored[key], expected)
                self.assertNotIn('institutional_knowledge', stored)


if __name__ == '__main__':
    unittest.main(verbosity=2)
