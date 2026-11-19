"""Anonymous question-outcome metrics, mixed into the disposable full-application HTTP suite."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

MODEL = 'services.llm_utils.llamar_llm_para_json_estructurado'
# A marker that cannot appear in any fixture, identifier or generated value.
SECRET = 'Zeta-Quark-7731 mi vecina Ofelia no puede caminar'


class QuestionMetricsCases:
    def metrics_events(self):
        from models import AnalyticsEventV2
        with self.app.app_context():
            return [(row.channel, dict(row.metadata_payload), row) for row in AnalyticsEventV2.query.filter_by(
                tenant_id=self.accounts['acceptance-a']['tenant_id'],
                event_name='institutional_assistant.question').order_by(AnalyticsEventV2.ts).all()]

    def clear_metrics(self):
        from database import db
        from extensions import limiter
        from models import AnalyticsEventV2
        with self.app.app_context():
            AnalyticsEventV2.query.filter_by(event_name='institutional_assistant.question').delete()
            db.session.commit()
            # These cases ask many public questions from one address; leave the
            # shared per-address budget as the other cases of the suite expect it.
            limiter.reset()
        self.addCleanup(self.reset_limits)

    def reset_limits(self):
        from extensions import limiter
        with self.app.app_context(): limiter.reset()

    def published(self):
        client = self.login(); state = self.seed(client)
        self.clear_metrics()
        return client, self.put(client, 'publish', state['revision']).get_json()

    def ask(self, published, question=None, node='start', client=None, public=True):
        body = {'revision': published['revision'], 'node_id': node}
        if question is not None: body['question'] = question
        return (client or self.app.test_client()).post(self.url(public) + '/answer', json=body)

    def metrics(self, client, query=''):
        return client.get(self.url() + '/metrics' + query)

    def test_public_question_records_only_canonical_identifiers(self):
        from database import db
        _, published = self.published()
        with patch(MODEL, return_value={'node_ids': ['requirements']}):
            response = self.ask(published, SECRET)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['selection_performed'])
        self.assertFalse(response.get_json()['business_writes_performed'])
        events = self.metrics_events()
        self.assertEqual(len(events), 1)
        channel, payload, row = events[0]
        self.assertEqual(channel, 'web_reader')
        self.assertEqual(payload, {'outcome': 'answered', 'node': 'start', 'selected': ['requirements'],
                                   'revision': published['revision'][:16], 'selection_performed': True})
        for field in ('user_id', 'anon_id', 'session_id', 'lat', 'lng', 'entity_ref'):
            self.assertIsNone(getattr(row, field), field)
        # Nothing the person wrote is persisted anywhere in the database.
        from sqlalchemy import inspect, text
        with self.app.app_context():
            tables = inspect(db.engine).get_table_names()
            self.assertIn('analytics_events_v2', tables)
            for table in tables:
                for stored in db.session.execute(text('SELECT * FROM "' + table.replace('"', '""') + '"')).all():
                    content = repr(tuple(stored))
                    for fragment in (SECRET, 'Zeta-Quark', 'Ofelia', 'vecina'):
                        self.assertNotIn(fragment, content, table)

    def test_each_typed_outcome_is_classified_once(self):
        from services.institutional_assistant_content import ContentError
        _, published = self.published()
        with patch(MODEL, return_value={'node_ids': []}):
            uncovered = self.ask(published, 'Consulta sin cobertura', node='requirements')
        self.assertEqual(uncovered.status_code, 200, uncovered.get_json()); self.assertEqual(uncovered.get_json()['nodes'], [])
        with patch(MODEL, side_effect=AssertionError('exact commands must not call the model')):
            menu = self.ask(published, 'volver al menú', node='requirements')
            typed = self.ask(published, 'Requisitos')
            sources = self.ask(published, 'fuentes', node='requirements')
        for response in (menu, typed, sources): self.assertEqual(response.status_code, 200, response.get_json())
        with patch('services.institutional_assistant.select_nodes',
                   side_effect=ContentError('knowledge_interpretation_unavailable', 503)):
            unavailable = self.ask(published, 'Consulta con el intérprete caído', node='requirements')
        self.assertEqual(unavailable.status_code, 503); self.assertNotIn('nodes', unavailable.get_json())
        prefix = published['revision'][:16]
        self.assertEqual([payload for _, payload, _ in self.metrics_events()], [
            {'outcome': 'uncovered', 'node': 'requirements', 'selected': [], 'revision': prefix, 'selection_performed': True},
            {'outcome': 'typed_navigation', 'node': 'requirements', 'selected': ['start'], 'revision': prefix, 'selection_performed': False},
            {'outcome': 'typed_navigation', 'node': 'start', 'selected': ['requirements'], 'revision': prefix, 'selection_performed': False},
            {'outcome': 'sources', 'node': 'requirements', 'selected': [], 'revision': prefix, 'selection_performed': False},
            {'outcome': 'unavailable', 'node': 'requirements', 'selected': [], 'revision': prefix, 'selection_performed': True},
        ])

    def test_navigation_previews_and_rejected_requests_are_not_counted(self):
        client, published = self.published()
        public = self.app.test_client()
        self.assertEqual(public.get(self.url(True)).status_code, 200)
        self.assertEqual(public.get(self.node_url(published['revision'], public=True)).status_code, 200)
        self.assertEqual(self.ask(published).status_code, 200)
        with patch(MODEL, return_value={'node_ids': ['requirements']}):
            self.assertEqual(self.ask(published, 'Vista previa del equipo', client=client, public=False).status_code, 200)
            self.assertEqual(self.ask({'revision': '0' * 64}, 'Revisión vieja').status_code, 412)
            self.assertEqual(self.ask(published, 'Nodo inexistente', node='missing').status_code, 400)
        self.assertEqual(self.metrics_events(), [])

    def test_metric_failure_never_changes_the_answer(self):
        from database import db
        from sqlalchemy.exc import OperationalError
        _, published = self.published()
        with patch(MODEL, return_value={'node_ids': ['requirements']}):
            expected = self.ask(published, 'Consulta de referencia').get_json()
            with patch('services.analytics.ingestor.analytics_ingestor.track', side_effect=RuntimeError('synthetic')):
                failed = self.ask(published, 'Consulta de referencia')
            with patch.object(db.session, 'commit', side_effect=OperationalError('synthetic', {}, Exception('down'))):
                unavailable = self.ask(published, 'Consulta de referencia')
        for response in (failed, unavailable):
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json(), expected)
        self.assertEqual(len(self.metrics_events()), 1)
        # The session stays usable after a failed metric write.
        with patch(MODEL, return_value={'node_ids': ['requirements']}):
            self.assertEqual(self.ask(published, 'Consulta posterior').status_code, 200)
        self.assertEqual(len(self.metrics_events()), 2)

    def test_cutover_fence_answers_no_question_and_counts_nothing(self):
        from services.institutional_assistant_metrics import record_question_outcomes
        _, published = self.published()
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
        with patch(MODEL, side_effect=AssertionError('fenced request must not reach the model')):
            self.assertEqual(self.ask(published, 'Consulta en mantenimiento').status_code, 503)
        with self.app.app_context():
            stored = record_question_outcomes(self.accounts['acceptance-a']['tenant_id'], 'municipio', 'web_reader', [
                {'outcome': 'answered', 'node': 'start', 'selected': ['requirements'], 'revision': published['revision'][:16]}])
        self.assertEqual(stored, 0)
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = False
        self.assertEqual(self.metrics_events(), [])

    def test_only_validated_canonical_values_are_stored(self):
        from services.institutional_assistant_metrics import record_question_outcomes
        _, published = self.published()
        tenant_id, prefix = self.accounts['acceptance-a']['tenant_id'], published['revision'][:16]
        valid = {'outcome': 'answered', 'node': 'start', 'selected': ['requirements'], 'revision': prefix}
        rejected = [
            {**valid, 'outcome': 'other'}, {**valid, 'node': 'texto libre con espacios'},
            {**valid, 'selected': ['a', 'b', 'c', 'd']}, {**valid, 'selected': [SECRET]},
            {**valid, 'revision': published['revision']}, {**valid, 'revision': 'Z' * 16}, 'answered', None,
        ]
        with self.app.app_context():
            self.assertEqual(record_question_outcomes(tenant_id, 'municipio', 'web_reader', rejected), 0)
            self.assertEqual(record_question_outcomes(tenant_id, 'municipio', 'unknown_channel', [valid]), 0)
            self.assertEqual(record_question_outcomes(None, 'municipio', 'web_reader', [valid]), 0)
            self.assertEqual(record_question_outcomes(tenant_id, 'municipio', 'web_reader', [{**valid, 'question': SECRET}]), 1)
        events = self.metrics_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0][1]), {'outcome', 'node', 'selected', 'revision', 'selection_performed'})

    def test_metrics_endpoint_aggregates_for_tenant_administrators_only(self):
        from database import db
        from models import AnalyticsEventV2
        client, published = self.published()
        tenant_id, prefix = self.accounts['acceptance-a']['tenant_id'], published['revision'][:16]
        with patch(MODEL, return_value={'node_ids': []}):
            for _ in range(2): self.assertEqual(self.ask(published, 'Sin cobertura', node='requirements').status_code, 200)
            self.assertEqual(self.ask(published, 'Sin cobertura desde el inicio').status_code, 200)
        with patch(MODEL, return_value={'node_ids': ['requirements']}):
            self.assertEqual(self.ask(published, 'Con respuesta').status_code, 200)
        now = datetime.now(timezone.utc)
        with self.app.app_context():
            db.session.add_all([
                # Outside the requested window, another tenant, another event and a damaged payload.
                AnalyticsEventV2(tenant_id=tenant_id, event_name='institutional_assistant.question', channel='web_reader',
                    ts=now - timedelta(days=40), metadata_payload={'outcome': 'uncovered', 'node': 'requirements', 'selected': [], 'revision': prefix, 'selection_performed': True}),
                AnalyticsEventV2(tenant_id=self.accounts['acceptance-b']['tenant_id'], event_name='institutional_assistant.question', channel='web_reader',
                    ts=now, metadata_payload={'outcome': 'uncovered', 'node': 'requirements', 'selected': [], 'revision': prefix, 'selection_performed': True}),
                AnalyticsEventV2(tenant_id=tenant_id, event_name='page_view', channel='web_reader', ts=now, metadata_payload={'outcome': 'uncovered'}),
                AnalyticsEventV2(tenant_id=tenant_id, event_name='institutional_assistant.question', channel='web_reader', ts=now, metadata_payload={'outcome': 'uncovered', 'node': SECRET}),
                AnalyticsEventV2(tenant_id=tenant_id, event_name='institutional_assistant.question', channel='web_reader', ts=now,
                    metadata_payload={'outcome': 'uncovered', 'node': 'retired-node', 'selected': [], 'revision': 'f' * 16, 'selection_performed': True}),
            ]); db.session.commit()
        with patch.object(db.session, 'commit', side_effect=AssertionError('metrics are read-only')):
            response = self.metrics(client)
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        self.assertEqual(data['contract_version'], 'chatboc.institutional_assistant_metrics.v1')
        self.assertEqual(data['tenant'], {'id': tenant_id, 'slug': 'acceptance-a'})
        self.assertEqual((data['days'], data['utc_offset_minutes'], data['truncated']), (30, 0, False))
        self.assertEqual(data['totals'], {'answered': 1, 'uncovered': 4, 'typed_navigation': 0, 'sources': 0, 'unavailable': 0})
        self.assertEqual(data['uncovered_rate'], 0.8)
        self.assertEqual(data['by_channel'], {'web_reader': data['totals']})
        self.assertEqual(len(data['by_day']), 30)
        self.assertEqual(data['by_day'][-1]['date'], data['to'])
        self.assertEqual(sum(day['uncovered'] for day in data['by_day']), 4)
        self.assertEqual(data['uncovered_by_node'], [
            {'node_id': 'requirements', 'count': 2, 'title': 'Requisitos', 'is_start': False},
            {'node_id': 'retired-node', 'count': 1, 'title': None, 'is_start': False},
            {'node_id': 'start', 'count': 1, 'title': 'Inicio', 'is_start': True}])
        self.assertEqual(data['answered_by_node'], [{'node_id': 'requirements', 'count': 1, 'title': 'Requisitos', 'is_start': False}])
        self.assertNotIn(SECRET, response.get_data(as_text=True))
        # The panel wording travels with the data; every label is non-empty text.
        self.assertEqual(set(data['ui']), {
            'title', 'description', 'period', 'answered', 'uncovered', 'typed_navigation', 'sources',
            'unavailable', 'uncovered_rate', 'uncovered_by_node', 'answered_by_node', 'node', 'count',
            'retired_node', 'empty', 'privacy', 'truncated'})
        self.assertTrue(all(isinstance(text, str) and text.strip() and len(text) <= 200 for text in data['ui'].values()))
        self.assertIn('{days}', data['ui']['period'])
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        wide = self.metrics(client, '?days=90&utc_offset_minutes=-180').get_json()
        self.assertEqual((wide['days'], wide['utc_offset_minutes'], len(wide['by_day'])), (90, -180, 90))
        self.assertEqual(wide['totals']['uncovered'], 5)
        for query in ('?days=0', '?days=91', '?days=abc', '?days=7&days=7', '?utc_offset_minutes=841',
                      '?utc_offset_minutes=1.5', '?unknown=1', '?tenant=acceptance-b', '?days='):
            with self.subTest(query=query): self.assertEqual(self.metrics(client, query).status_code, 400)
        self.assertEqual(client.get(self.url() + '/metrics', headers={'X-Tenant': 'acceptance-b'}).status_code, 400)
        self.assertIn(self.metrics(self.app.test_client()).status_code, (401, 403))
        for account in ('acceptance-b', 'viewer'):
            self.assertEqual(self.metrics(self.login(account)).status_code, 403)
        self.assertEqual(client.get(self.url().replace('/acceptance-a/', '/missing-tenant/') + '/metrics').status_code, 404)
        self.assertEqual(client.post(self.url() + '/metrics', json={}).status_code, 405)

    def test_metrics_are_available_before_any_knowledge_or_question(self):
        client = self.login(); self.clear_metrics()
        data = self.metrics(client, '?days=1').get_json()
        self.assertIsNone(data['revision']); self.assertIsNone(data['uncovered_rate'])
        self.assertEqual(len(data['by_day']), 1)
        self.assertEqual(data['uncovered_by_node'], []); self.assertEqual(data['answered_by_node'], [])
