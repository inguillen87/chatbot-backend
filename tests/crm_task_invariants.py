"""Additional task transaction invariants, used by the isolated HTTP suite."""
from uuid import uuid4
from unittest.mock import patch

class TaskInvariantTests:
    def test_same_key_concurrent_creation_has_one_task_and_event(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from services.crm_tasks import normalized_changes
        barrier=Barrier(2);key=str(uuid4());payload={'title':'Same request concurrently'}
        def synchronize(*args,**kwargs):
            result=normalized_changes(*args,**kwargs);barrier.wait(timeout=10);return result
        with patch('services.crm_tasks.normalized_changes',synchronize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:self.call('POST',self.url(),payload,key=key),[0,1]))
        self.assertEqual(sorted(code for code,_ in results),[200,201],results)
        self.assertEqual(results[0][1]['task']['id'],results[1][1]['task']['id'])
        _,detail=self.call('GET',self.url(results[0][1]['task']['id']))
        self.assertEqual(len(detail['events']),1)

    def test_replay_after_later_revisions_recovers_the_original_receipt(self):
        task=self.create();key=str(uuid4());change={'title':'Original saved title','expected_revision':1,'reason':'Original change'}
        _,saved=self.call('PATCH',self.url(task['id']),change,key=key)
        self.assertEqual(self.call('PATCH',self.url(task['id']),{'title':'Later title','expected_revision':2,'reason':'New change'},key=str(uuid4()))[0],200)
        code,recovered=self.call('PATCH',self.url(task['id']),change,key=key)
        self.assertEqual(code,200);self.assertTrue(recovered['receipt']['replayed'])
        self.assertEqual(recovered['receipt']['event_id'],saved['receipt']['event_id'])
        self.assertEqual(recovered['task']['revision'],2)
        _,current=self.call('GET',self.url(task['id']))
        self.assertEqual(current['task']['revision'],3);self.assertEqual(current['task']['title'],'Later title')

    def test_receipt_is_bound_to_the_supplied_operation_key(self):
        from hashlib import sha256
        key=str(uuid4());code,body=self.call('POST',self.url(),{'title':'Key binding'},key=key)
        self.assertEqual(code,201);self.assertEqual(body['receipt']['key_hash'],sha256(key.encode()).hexdigest())

    def test_failed_creation_rolls_back_both_task_and_event(self):
        from database import db
        from models_crm_tasks import CrmTask,CrmTaskEvent
        with self.app.app_context(): before=(CrmTask.query.count(),CrmTaskEvent.query.count())
        with patch('services.crm_tasks.append_event',side_effect=RuntimeError('Synthetic failure')):
            self.assertEqual(self.call('POST',self.url(),{'title':'Not persisted'},key=str(uuid4()))[0],503)
        with self.app.app_context(): self.assertEqual((CrmTask.query.count(),CrmTaskEvent.query.count()),before)

    def test_anonymous_and_foreign_tenants_cannot_read_or_mutate(self):
        task=self.create()
        for method in ['GET','PATCH']:
            data={'title':'Foreign','expected_revision':1,'reason':'Forbidden'} if method=='PATCH' else None
            self.assertEqual(self.call(method,self.url(task['id']),data,actor=None,key=str(uuid4()))[0],401)
            self.assertIn(self.call(method,self.url(task['id']),data,actor='acceptance-b',key=str(uuid4()))[0],[403,404])
            self.assertEqual(self.call(method,self.url(task['id'],slug='acceptance-b'),data,actor='acceptance-b',key=str(uuid4()))[0],404)

    def test_terminal_task_requires_reopening_before_editing(self):
        task=self.create();self.assertEqual(self.call('PATCH',self.url(task['id']),{'status':'done','expected_revision':1,'reason':'Finished'},key=str(uuid4()))[0],200)
        self.assertEqual(self.call('PATCH',self.url(task['id']),{'title':'Forbidden edit','expected_revision':2,'reason':'Attempt'},key=str(uuid4()))[0],403)
        self.assertEqual(self.call('PATCH',self.url(task['id']),{'status':'todo','expected_revision':2,'reason':'Reopen'},key=str(uuid4()))[0],200)

    def test_missing_history_guard_keeps_module_unavailable(self):
        from database import db
        from sqlalchemy import text
        with self.app.app_context(),db.engine.begin() as connection:
            connection.execute(text('DROP TRIGGER crm_task_event_no_delete'))
        try:
            code,body=self.call('GET','/api/admin/tenants/acceptance-a/crm/tasks/capabilities')
            self.assertEqual(code,200);self.assertFalse(body['available'])
            self.assertEqual(self.call('POST',self.url(),{'title':'Disabled'},key=str(uuid4()))[0],503)
        finally:
            with self.app.app_context(),db.engine.begin() as connection:
                connection.execute(text("CREATE TRIGGER crm_task_event_no_delete BEFORE DELETE ON crm_task_event BEGIN SELECT RAISE(ABORT,'crm_task_event_append_only'); END"))

    def test_history_delete_and_foreign_contact_write_are_rejected_by_database(self):
        from database import db
        from models_crm_tasks import CrmTask,CrmTaskEvent
        from sqlalchemy.exc import IntegrityError
        task=self.create()
        with self.app.app_context():
            event=CrmTaskEvent.query.filter_by(task_id=task['id']).one()
            db.session.delete(event)
            with self.assertRaises(IntegrityError):db.session.commit()
            db.session.rollback()
            stored=db.session.get(CrmTask,task['id']);stored.contact_id=self.contacts['acceptance-b']
            with self.assertRaises(IntegrityError):db.session.commit()
            db.session.rollback()
            self.assertEqual(db.session.get(CrmTask,task['id']).contact_id,self.contacts['acceptance-a'])

    def test_schema_inspection_failure_returns_unavailable_without_leaking_details(self):
        with patch('services.crm_task_schema.task_schema_ready',side_effect=RuntimeError('private connection detail')):
            code,body=self.call('GET','/api/admin/tenants/acceptance-a/crm/tasks/capabilities')
            self.assertEqual(code,200);self.assertFalse(body['available'])
            self.assertNotIn('private connection detail',str(body))
