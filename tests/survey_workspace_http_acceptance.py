"""Dedicated full-app HTTP acceptance. Disposable identities and database only."""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == '__main__':
    prepare_process()

from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
import json
import re
import secrets
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor

ADMIN_PATH = '/api/admin/encuestas'
ALIASES = ('/api/encuestas', '/admin/encuestas', ADMIN_PATH,
           '/api/municipal/encuestas', '/api/admin/surveys',
           '/admin/surveys', '/api/municipal/surveys')


class Browser:
    def __init__(self, origin):
        self.origin = origin
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, method, path, data=None, headers=None):
        request = Request(self.origin + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = self.opener.open(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            body = json.loads(raw) if raw and 'json' in response.headers.get('Content-Type', '') else None
            return response.status, body


class SurveyAcceptanceServer:
    """All fault controls are installed in this test process, not product routes."""
    def __init__(self):
        from tests.profile_acceptance_runtime import create_disposable_app
        from database import db
        from flask import request, jsonify
        from models import TenantProfile
        from werkzeug.serving import make_server
        self.temp = tempfile.TemporaryDirectory(prefix='chatboc-survey-http-')
        self.app, self.accounts, self.password = create_disposable_app(self.temp.name)
        self.token = secrets.token_urlsafe(32)
        self.mode = 'normal'
        self.mutations = []
        self.reads = []
        with self.app.app_context():
            for account in ('acceptance-a', 'acceptance-b'):
                tenant = db.session.get(TenantProfile, self.accounts[account]['tenant_id'])
                tenant.plan = 'full'
            db.session.commit()
        self.cases = {}
        for index, kind in enumerate(('close', 'delete', 'close-recovery', 'delete-recovery')):
            record = self.create_survey('publicada' if kind.startswith('close') else 'borrador',
                title=f'QA integrado {index + 1} {kind}', responses=1 if kind.startswith('close') else 0)
            self.cases[kind] = record

        @self.app.before_request
        def fault_injection():
            if request.method == 'GET' and request.path.rstrip('/') in ALIASES:
                self.reads.append(request.path)
                if self.mode == 'readback-unavailable':
                    return jsonify({'error': 'Lectura temporalmente no disponible en aceptación'}), 503
            return None

        @self.app.after_request
        def observe_and_arm(response):
            path = request.path.rstrip('/')
            mutation = (request.method == 'DELETE' and any(re.fullmatch(re.escape(base)+r'/\d+', path) for base in ALIASES)) or (
                request.method == 'POST' and re.fullmatch(r'/api/v2/surveys/\d+/close', path))
            if mutation:
                self.mutations.append({'method': request.method, 'path': path, 'status': response.status_code})
                if 200 <= response.status_code < 300 and self.mode == 'fail-next-readback':
                    self.mode = 'readback-unavailable'
            return response

        @self.app.post('/__survey_acceptance__/control')
        def control():
            if request.remote_addr != '127.0.0.1' or not secrets.compare_digest(
                request.headers.get('X-Acceptance-Token', ''), self.token):
                return jsonify({'error': 'forbidden'}), 403
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or set(body) != {'mode'} or body['mode'] not in ('normal', 'fail-next-readback'):
                return jsonify({'error': 'invalid'}), 400
            self.mode = body['mode']
            return jsonify({'mode': self.mode, 'mutations': self.mutations, 'reads': len(self.reads)})

        self.server = make_server('127.0.0.1', 0, self.app, threaded=True)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def create_survey(self, state='borrador', *, account='acceptance-a', responses=0, origin='real', title=None):
        from database import db
        from models import EncEncuesta, EncPregunta, EncRespuesta
        now = datetime.now(timezone.utc)
        with self.app.app_context():
            tenant_id = self.accounts[account]['tenant_id']
            item = EncEncuesta(tenant_id=tenant_id, slug='qa-' + secrets.token_hex(8),
                titulo=title or 'Instrumento desechable', descripcion='Datos sintéticos de aceptación',
                tipo='opinion', estado=state, politica_unicidad='libre', anonimo_permitido=True,
                inicio_at=now-timedelta(hours=1), fin_at=now+timedelta(days=1))
            db.session.add(item)
            db.session.flush()
            db.session.add(EncPregunta(encuesta_id=item.id, orden=1, tipo='abierta', texto='Consulta de prueba', obligatoria=False))
            for _ in range(responses):
                db.session.add(EncRespuesta(encuesta_id=item.id, tenant_id=tenant_id,
                    huella_unica=secrets.token_hex(16), canal='web', response_origin=origin, submitted_at=now))
            db.session.commit()
            return {'id': item.id, 'title': item.titulo, 'tenant_id': tenant_id, 'responses': responses}

    def read_storage(self, survey_id):
        from database import db
        from models import EncEncuesta, EncRespuesta
        with self.app.app_context():
            item = db.session.get(EncEncuesta, survey_id)
            return {'exists': item is not None, 'state': item.estado if item else None,
                'responses': EncRespuesta.query.filter_by(encuesta_id=survey_id).count()}

    def close(self):
        from database import db
        self.mode = 'normal'
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.temp.cleanup()


class SurveyWorkspaceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.runtime = SurveyAcceptanceServer()
    @classmethod
    def tearDownClass(cls): cls.runtime.close()
    def setUp(self): self.runtime.mode = 'normal'
    def tearDown(self): self.runtime.mode = 'normal'

    def login(self, account='acceptance-a'):
        browser = Browser(self.runtime.origin)
        status, data = browser.request('POST', '/auth/login', {
            'email': self.runtime.accounts[account]['email'], 'password': self.runtime.password})
        self.assertEqual(status, 200)
        self.assertTrue(data.get('token'))
        return browser

    def url(self, survey_id=None, account='acceptance-a', base=ADMIN_PATH):
        return base + (f'/{survey_id}' if survey_id else '') + '?tenant_slug=' + account

    def test_list_is_scoped_and_uses_original_session(self):
        foreign = self.runtime.create_survey(account='acceptance-b')
        browser = self.login()
        status, body = browser.request('GET', self.url())
        self.assertEqual(status, 200)
        self.assertEqual(body['contract_version'], 'surveys.admin_list.v2')
        self.assertEqual(body['tenant']['slug'], 'acceptance-a')
        self.assertNotIn(foreign['id'], [row['id'] for row in body['encuestas']])
        self.assertEqual(browser.request('GET', '/api/me?tenant_slug=acceptance-a')[0], 200)

    def test_close_persists_and_preserves_responses(self):
        item = self.runtime.create_survey('publicada', responses=2)
        browser = self.login()
        path = f"/api/v2/surveys/{item['id']}/close?tenant_slug=acceptance-a"
        status, body = browser.request('POST', path)
        self.assertEqual(status, 200)
        self.assertEqual(body['estado'], 'cerrada')
        self.assertEqual(self.runtime.read_storage(item['id']), {'exists': True, 'state': 'cerrada', 'responses': 2})
        self.assertEqual(browser.request('POST', path)[0], 200)
        status, body = browser.request('GET', self.url(item['id']))
        self.assertEqual(status, 200)
        self.assertFalse(body['admin_lifecycle']['capabilities']['can_close'])

    def test_delete_empty_draft_is_persisted(self):
        item = self.runtime.create_survey()
        browser = self.login()
        self.assertEqual(browser.request('DELETE', self.url(item['id']))[0], 200)
        self.assertEqual(self.runtime.read_storage(item['id']), {'exists': False, 'state': None, 'responses': 0})
        self.assertEqual(browser.request('GET', self.url(item['id']))[0], 404)

    def test_every_admin_alias_rejects_deleting_a_live_instrument(self):
        item = self.runtime.create_survey('publicada', responses=1)
        browser = self.login()
        for base in ALIASES:
            with self.subTest(base=base):
                status, body = browser.request('DELETE', self.url(item['id'], base=base))
                self.assertEqual(status, 409)
                self.assertEqual(body['reason_code'], 'survey_delete_requires_draft')
        self.assertEqual(self.runtime.read_storage(item['id']), {'exists': True, 'state': 'publicada', 'responses': 1})

    def test_empty_live_closed_and_archived_instruments_are_not_deleted(self):
        browser = self.login()
        for state in ('publicada', 'cerrada', 'archivada'):
            item = self.runtime.create_survey(state)
            with self.subTest(state=state):
                self.assertEqual(browser.request('DELETE', self.url(item['id']))[0], 409)
                self.assertTrue(self.runtime.read_storage(item['id'])['exists'])

    def test_all_recorded_responses_protect_a_draft(self):
        browser = self.login()
        for origin in ('real', 'synthetic_demo'):
            item = self.runtime.create_survey(responses=1, origin=origin)
            with self.subTest(origin=origin):
                status, body = browser.request('DELETE', self.url(item['id']))
                self.assertEqual(status, 409)
                self.assertEqual(body['reason_code'], 'survey_delete_has_responses')
                self.assertEqual(self.runtime.read_storage(item['id'])['responses'], 1)

    def test_foreign_and_anonymous_deletions_are_denied(self):
        item = self.runtime.create_survey()
        self.assertEqual(Browser(self.runtime.origin).request('DELETE', self.url(item['id']))[0], 401)
        self.assertEqual(self.login('acceptance-b').request('DELETE', self.url(item['id'], account='acceptance-b'))[0], 403)
        self.assertTrue(self.runtime.read_storage(item['id'])['exists'])

    def test_foreign_close_is_denied(self):
        item = self.runtime.create_survey('publicada', responses=1)
        path = f"/api/v2/surveys/{item['id']}/close?tenant_slug=acceptance-b"
        self.assertEqual(self.login('acceptance-b').request('POST', path)[0], 403)
        self.assertEqual(self.runtime.read_storage(item['id'])['state'], 'publicada')

    def test_employee_cannot_delete_a_draft(self):
        item = self.runtime.create_survey()
        self.assertEqual(self.login('viewer').request('DELETE', self.url(item['id']))[0], 403)
        self.assertTrue(self.runtime.read_storage(item['id'])['exists'])

    def test_missing_id_and_failed_close_leave_the_list_readable(self):
        browser = self.login()
        self.assertEqual(browser.request('DELETE', self.url(2147483000))[0], 404)
        item = self.runtime.create_survey()
        path = f"/api/v2/surveys/{item['id']}/close?tenant_slug=acceptance-a"
        self.assertEqual(browser.request('POST', path)[0], 409)
        self.assertEqual(browser.request('GET', self.url())[0], 200)

    def test_readback_fault_does_not_undo_committed_delete(self):
        item = self.runtime.create_survey()
        browser = self.login()
        self.runtime.mode = 'fail-next-readback'
        self.assertEqual(browser.request('DELETE', self.url(item['id']))[0], 200)
        self.assertEqual(browser.request('GET', self.url())[0], 503)
        self.assertFalse(self.runtime.read_storage(item['id'])['exists'])
        self.runtime.mode = 'normal'
        self.assertEqual(browser.request('GET', self.url())[0], 200)

    def test_fault_control_requires_its_private_disposable_token(self):
        status, _ = Browser(self.runtime.origin).request('POST', '/__survey_acceptance__/control', {'mode': 'fail-next-readback'})
        self.assertEqual(status, 403)
        self.assertEqual(self.runtime.mode, 'normal')


if __name__ == '__main__':
    unittest.main(verbosity=2)
