from unittest.mock import patch
from uuid import uuid4


class RolloutInvariantTests:
    def test_rollout_selects_one_organization_without_enabling_its_neighbour(self):
        original=self.app.config['CRM_TASKS_TENANT_IDS']
        self.app.config['CRM_TASKS_TENANT_IDS']=str(self.accounts['acceptance-a']['tenant_id'])
        try:
            code,selected=self.call('GET','/api/admin/tenants/acceptance-a/crm/tasks/capabilities')
            self.assertEqual(code,200);self.assertTrue(selected['available'])
            code,other=self.call('GET','/api/admin/tenants/acceptance-b/crm/tasks/capabilities',actor='acceptance-b')
            self.assertEqual(code,200);self.assertFalse(other['available'])
            self.assertEqual(other['rollout']['reason_code'],'tenant_not_selected')
            self.assertNotIn('can_create',other);self.assertNotIn('tenant_ids',str(other))
            path=self.url(slug='acceptance-b',contact=self.contacts['acceptance-b'])
            self.assertEqual(self.call('POST',path,{'title':'Should not exist'},actor='acceptance-b',key=str(uuid4()))[0],503)
            self.assertEqual(self.call('GET',path,actor='acceptance-b')[0],503)
        finally:self.app.config['CRM_TASKS_TENANT_IDS']=original

    def test_rollout_revocation_blocks_writes_without_losing_saved_history(self):
        task=self.create();original=self.app.config['CRM_TASKS_TENANT_IDS']
        self.app.config['CRM_TASKS_TENANT_IDS']=''
        try:
            payload={'expected_revision':1,'status':'done','reason':'Must not happen'}
            self.assertEqual(self.call('PATCH',self.url(task['id']),payload,key=str(uuid4()))[0],503)
            self.assertEqual(self.call('GET',self.url(task['id']))[0],503)
        finally:self.app.config['CRM_TASKS_TENANT_IDS']=original
        code,detail=self.call('GET',self.url(task['id']))
        self.assertEqual(code,200);self.assertEqual(detail['task']['revision'],1)
        self.assertEqual(detail['task']['status'],'todo');self.assertEqual(len(detail['events']),1)

    def test_rollout_denied_tenants_do_not_probe_the_task_schema(self):
        original=self.app.config['CRM_TASKS_TENANT_IDS']
        self.app.config['CRM_TASKS_TENANT_IDS']=''
        try:
            with patch('services.crm_task_schema.task_schema_ready',side_effect=AssertionError('should not inspect')) as inspect:
                code,body=self.call('GET','/api/admin/tenants/acceptance-a/crm/tasks/capabilities')
                self.assertEqual(code,200);self.assertFalse(body['available']);inspect.assert_not_called()
        finally:self.app.config['CRM_TASKS_TENANT_IDS']=original

    def test_rollout_malformed_selection_fails_closed_everywhere(self):
        original=self.app.config['CRM_TASKS_TENANT_IDS']
        self.app.config['CRM_TASKS_TENANT_IDS']=str(self.accounts['acceptance-a']['tenant_id'])+',*'
        try:
            code,body=self.call('GET','/api/admin/tenants/acceptance-a/crm/tasks/capabilities')
            self.assertEqual(code,200);self.assertFalse(body['available'])
            self.assertEqual(body['rollout']['reason_code'],'invalid_rollout_config')
            self.assertEqual(self.call('POST',self.url(),{'title':'No activation'},key=str(uuid4()))[0],503)
        finally:self.app.config['CRM_TASKS_TENANT_IDS']=original

    def test_rollout_cannot_be_selected_via_client_headers(self):
        original=self.app.config['CRM_TASKS_TENANT_IDS'];self.app.config['CRM_TASKS_TENANT_IDS']=''
        try:
            headers={**self.headers['acceptance-a'],'CRM_TASKS_ENABLED':'true','CRM_TASKS_TENANT_IDS':str(self.accounts['acceptance-a']['tenant_id'])}
            response=self.app.test_client().get('/api/admin/tenants/acceptance-a/crm/tasks/capabilities?CRM_TASKS_ENABLED=true',headers=headers)
            self.assertEqual(response.status_code,200);self.assertFalse(response.get_json()['available'])
        finally:self.app.config['CRM_TASKS_TENANT_IDS']=original

    def test_guard_name_with_no_effect_does_not_make_schema_ready(self):
        from database import db
        from sqlalchemy import text
        with self.app.app_context(),db.engine.begin() as connection:
            original=connection.execute(text("SELECT sql FROM sqlite_master WHERE name='crm_task_event_no_delete'")).scalar_one()
            connection.execute(text('DROP TRIGGER crm_task_event_no_delete'))
            connection.execute(text('CREATE TRIGGER crm_task_event_no_delete BEFORE DELETE ON crm_task_event BEGIN SELECT 1; END'))
        try:
            code,body=self.call('GET','/api/admin/tenants/acceptance-a/crm/tasks/capabilities')
            self.assertEqual(code,200);self.assertFalse(body['available'])
            self.assertEqual(body['rollout']['reason_code'],'schema_not_ready')
        finally:
            with self.app.app_context(),db.engine.begin() as connection:
                connection.execute(text('DROP TRIGGER crm_task_event_no_delete'));connection.execute(text(original))

    def test_ordinary_task_read_does_not_issue_any_ddl_or_change_rows(self):
        from database import db
        from sqlalchemy import event
        from models_crm_tasks import CrmTask,CrmTaskEvent
        task=self.create();statements=[]
        def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement.strip().split()[0].upper())
        with self.app.app_context():
            engine=db.engine;before=(CrmTask.query.count(),CrmTaskEvent.query.count())
        event.listen(engine,'before_cursor_execute',capture)
        try:self.assertEqual(self.call('GET',self.url(task['id']))[0],200)
        finally:event.remove(engine,'before_cursor_execute',capture)
        self.assertFalse({'CREATE','ALTER','DROP','UPDATE','INSERT','DELETE'}&set(statements))
        with self.app.app_context():self.assertEqual((CrmTask.query.count(),CrmTaskEvent.query.count()),before)
