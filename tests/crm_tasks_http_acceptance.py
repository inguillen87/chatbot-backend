"""Original application, auth, task migration and persistence on a disposable DB."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from tests.profile_acceptance_runtime import prepare_process, create_disposable_app


from tests.crm_task_invariants import TaskInvariantTests

class CrmTaskHttpTests(TaskInvariantTests, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='chatboc-task-qa-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)
        cls.app.config['CRM_TASKS_ENABLED'] = True
        from database import db
        from models_memory import Contact
        with cls.app.app_context():
            assert 'chatboc-task-qa-' in db.engine.url.database
            db.create_all()
            migration_path = Path(__file__).resolve().parents[1] / 'migrations/pending/20260924_crm_tasks_v1.py'
            spec = importlib.util.spec_from_file_location('task_migration_test', migration_path)
            cls.migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.migration)
            from alembic.migration import MigrationContext
            from alembic.operations import Operations
            with db.engine.begin() as connection:
                cls.migration.op = Operations(MigrationContext.configure(connection))
                cls.migration.upgrade()
            cls.contacts = {}
            for slug in ('acceptance-a','acceptance-b'):
                cid = str(uuid4()); cls.contacts[slug] = cid
                db.session.add(Contact(id=cid, tenant_id=cls.accounts[slug]['tenant_id'], name='Contacto tareas QA', preferences={'next_action_at':'2026-10-01T12:00:00Z','owner_notes':'No modificar'}))
            db.session.commit()
        cls.headers = {}
        for account in ('acceptance-a','acceptance-b','second','viewer'):
            client = cls.app.test_client()
            result = client.post('/auth/login', json={'email':cls.accounts[account]['email'],'password':cls.password})
            assert result.status_code == 200
            cls.headers[account] = {'Authorization':'Bearer '+result.get_json()['token']}

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove(); db.engine.dispose()
        cls.directory.cleanup()

    def url(self, task=None, slug='acceptance-a', contact=None):
        path = f"/api/admin/tenants/{slug}/contacts/{contact or self.contacts['acceptance-a']}/tasks"
        return path + ('/'+task if task else '')

    def call(self, method, path, body=None, actor='acceptance-a', key=None):
        headers = dict(self.headers[actor]) if actor else {}
        if key is not None: headers['Idempotency-Key'] = key
        response = self.app.test_client().open(path, method=method, json=body, headers=headers)
        return response.status_code, response.get_json()

    def create(self, **fields):
        code, body = self.call('POST',self.url(), {'title':'Tarea QA',**fields}, key=str(uuid4()))
        self.assertEqual(code,201,body)
        return body['task']

    def test_multiple_tasks_are_independent(self):
        first = self.create(assignee_id=self.accounts['viewer']['id'], due_at='2026-10-01T12:30:00-03:00')
        second = self.create(title='Segunda tarea de prueba')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['due_at'], '2026-10-01T15:30:00Z')
        self.assertEqual(first['revision'], 1)
        code, body = self.call('GET', self.url(first['id']))
        self.assertEqual(code, 200)
        self.assertEqual(body['events'][0]['operation'], 'created')
        from database import db
        from models_memory import Contact
        with self.app.app_context():
            contact = db.session.get(Contact, self.contacts['acceptance-a'])
            self.assertEqual(contact.preferences['owner_notes'], 'No modificar')

    def test_stale_version_returns_conflict(self):
        task = self.create()
        change = {'expected_revision':1, 'title':'Cambio guardado', 'reason':'Revisión'}
        self.assertEqual(self.call('PATCH', self.url(task['id']), change, key=str(uuid4()))[0], 200)
        code, body = self.call('PATCH', self.url(task['id']), {**change, 'title':'Versión anterior'}, actor='second', key=str(uuid4()))
        self.assertEqual(code, 409)
        self.assertEqual(body['error']['code'], 'stale_revision')
        _, body = self.call('GET', self.url(task['id']))
        self.assertEqual(body['task']['title'], 'Cambio guardado')
        self.assertEqual(len(body['events']), 2)

    def test_creation_request_is_idempotent(self):
        key = str(uuid4()); payload = {'title':'Solicitud de prueba'}
        first = self.call('POST', self.url(), payload, key=key)
        again = self.call('POST', self.url(), payload, key=key)
        self.assertEqual(first[0], 201)
        self.assertEqual(again[0], 200)
        self.assertEqual(first[1]['task']['id'], again[1]['task']['id'])
        self.assertTrue(again[1]['receipt']['replayed'])
        self.assertEqual(self.call('POST', self.url(), {'title':'Otro contenido'}, key=key)[0], 409)

    def test_inactive_module_has_no_task_actions(self):
        self.app.config['CRM_TASKS_ENABLED'] = False
        try:
            path = '/api/admin/tenants/acceptance-a/crm/tasks/capabilities'
            code, body = self.call('GET', path)
            self.assertEqual(code, 200)
            self.assertFalse(body['available'])
            self.assertEqual(self.call('GET', self.url())[0], 503)
        finally:
            self.app.config['CRM_TASKS_ENABLED'] = True

    def test_revision_is_required(self):
        task = self.create()
        code, body = self.call('PATCH', self.url(task['id']), {'title':'Cambio','reason':'QA'}, key=str(uuid4()))
        self.assertEqual(code, 428)
        self.assertEqual(body['error']['code'], 'revision_required')

    def test_assignment_uses_tenant_members(self):
        invalid_values = [self.accounts['acceptance-b']['id'], True, '2', -1, 999999]
        for value in invalid_values:
            with self.subTest(value=value):
                payload = {'title':'Asignación de prueba','assignee_id':value}
                self.assertEqual(self.call('POST',self.url(),payload,key=str(uuid4()))[0],400)

    def test_employee_progress_permissions(self):
        task = self.create(assignee_id=self.accounts['viewer']['id'])
        change = {'expected_revision':1,'status':'in_progress','reason':'Inicio de actividad'}
        self.assertEqual(self.call('PATCH',self.url(task['id']),change,actor='viewer',key=str(uuid4()))[0],200)
        edit = {'expected_revision':2,'title':'Cambio de título','reason':'Prueba'}
        self.assertEqual(self.call('PATCH',self.url(task['id']),edit,actor='viewer',key=str(uuid4()))[0],403)
        other = self.create()
        self.assertEqual(self.call('PATCH',self.url(other['id']),change,actor='viewer',key=str(uuid4()))[0],403)

    def test_event_failure_reverts_the_task_transaction(self):
        from unittest.mock import patch
        task = self.create()
        update = {'expected_revision':1,'title':'Intento de cambio','reason':'QA'}
        with patch('services.crm_tasks.append_event', side_effect=RuntimeError('Synthetic audit failure')):
            self.assertEqual(self.call('PATCH',self.url(task['id']),update,key=str(uuid4()))[0],503)
        _, detail = self.call('GET',self.url(task['id']))
        self.assertEqual(detail['task']['revision'],1)
        self.assertEqual(detail['task']['title'],'Tarea QA')
        self.assertEqual(len(detail['events']),1)

    def test_invalid_calendar_and_unknown_fields(self):
        for fields in [{'due_at':'2026-02-30T10:00:00Z'},{'due_at':'2026-10-01T10:00'},{'priority':[]},{'unexpected':True}]:
            with self.subTest(fields=fields):
                self.assertEqual(self.call('POST',self.url(),{'title':'Tarea de prueba',**fields},key=str(uuid4()))[0],400)

    def test_concurrent_edits_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from unittest.mock import patch
        from services.crm_tasks import normalized_changes
        task = self.create(); barrier = Barrier(2)
        def synchronized(*args, **kwargs):
            result = normalized_changes(*args, **kwargs)
            barrier.wait(timeout=5)
            return result
        def edit(label):
            return self.call('PATCH',self.url(task['id']),{'expected_revision':1,'title':label,'reason':'Concurrent QA'},key=str(uuid4()))
        with patch('services.crm_tasks.normalized_changes', synchronized):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(edit, ['Primera edición','Segunda edición']))
        self.assertEqual(sorted(code for code,_ in results),[200,409])
        _, detail = self.call('GET',self.url(task['id']))
        self.assertEqual(detail['task']['revision'],2)
        self.assertEqual(len(detail['events']),2)

    def test_future_revision_does_not_validate_against_an_older_loaded_state(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from services.crm_tasks import normalized_changes
        task = self.create()
        def concurrent_change(tenant, actor, loaded, payload):
            result = normalized_changes(tenant, actor, loaded, payload)
            if payload.get('expected_revision') == 2:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    changed = pool.submit(self.call, 'PATCH', self.url(task['id']),
                        {'expected_revision':1,'status':'done','reason':'Concurrent close'},
                        key=str(uuid4())).result(timeout=5)
                self.assertEqual(changed[0],200,changed)
            return result
        with patch('services.crm_tasks.normalized_changes', concurrent_change):
            code, result = self.call('PATCH', self.url(task['id']),
                {'expected_revision':2,'status':'in_progress','reason':'Future version probe'},key=str(uuid4()))
        self.assertEqual(code,409,result)
        _, current = self.call('GET',self.url(task['id']))
        self.assertNotEqual(current['task']['status'],'in_progress')

    def test_migrated_history_rejects_in_place_edits(self):
        from database import db
        from models_crm_tasks import CrmTaskEvent
        from sqlalchemy.exc import IntegrityError
        task = self.create()
        with self.app.app_context():
            event = CrmTaskEvent.query.filter_by(task_id=task['id']).one()
            original = event.reason
            event.reason = 'Synthetic edit that must be rejected'
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            self.assertEqual(CrmTaskEvent.query.filter_by(task_id=task['id']).one().reason, original)

    def test_browser_roundtrip_with_original_routes(self):
        frontend = globals().get('QA_FRONTEND')
        if not frontend:
            self.skipTest('Provide --frontend to run the integrated browser')
        import subprocess, threading
        from werkzeug.serving import make_server
        from database import db
        from models_memory import Contact
        from models_crm_tasks import CrmTask, CrmTaskEvent
        contacts = [str(uuid4()) for _ in range(3)]
        with self.app.app_context():
            for cid in contacts:
                db.session.add(Contact(id=cid,tenant_id=self.accounts['acceptance-a']['tenant_id'],name='UI task fixture',preferences={}))
            db.session.commit()
        server = make_server('127.0.0.1',0,self.app,threaded=True)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            settings = {'origin':f'http://127.0.0.1:{server.server_port}','authorization':self.headers['acceptance-a']['Authorization'],
                'contacts':contacts,'assigneeId':self.accounts['viewer']['id'],'evidence':str(QA_EVIDENCE)}
            result = subprocess.run(['node',str(frontend/'tests/crm-tasks-full.browser.mjs')],cwd=frontend,input=json.dumps(settings),text=True,encoding='utf-8',capture_output=True,timeout=180)
            (QA_EVIDENCE/'browser-runtime.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
            self.assertEqual(result.returncode,0,'See browser-runtime.log')
            with self.app.app_context():
                for cid in contacts:
                    tasks = CrmTask.query.filter_by(contact_id=cid).all()
                    self.assertEqual(len(tasks),2)
                    primary = next(task for task in tasks if task.title=='Tarea principal QA')
                    self.assertEqual(primary.status,'done'); self.assertEqual(primary.revision,2)
                    self.assertEqual(primary.assignee_id,self.accounts['viewer']['id'])
                    self.assertEqual(CrmTaskEvent.query.filter_by(task_id=primary.id).count(),2)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == '__main__':
    prepare_process()
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend', type=Path)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    QA_FRONTEND = args.frontend.resolve() if args.frontend else None
    QA_EVIDENCE = args.evidence.resolve()
    QA_EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (QA_EVIDENCE/'backend-tests.log').open('w',encoding='utf-8') as output:
        result = unittest.TextTestRunner(stream=output,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CrmTaskHttpTests))
    (QA_EVIDENCE/'backend-tests.json').write_text(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
        'skipped':len(result.skipped),'success':result.wasSuccessful(),'database':'disposable SQLite','external_network':'blocked'}),encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
