"""Actual task service/model/migration on disposable PostgreSQL.
Parent account/contact tables are minimal fixtures; this does not test authentication.
The full application/authentication acceptance is tests.crm_tasks_http_acceptance.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from uuid import uuid4
from types import SimpleNamespace
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.dialects.postgresql import JSONB
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.crm_task_pg_rollout_invariants import PostgresReadinessTests
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module

class TaskPostgresTests(PostgresReadinessTests,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn=os.environ.get('CHATBOC_TASKS_QA_DSN','')
        url=make_url(dsn)
        if url.host not in ('127.0.0.1','localhost') or url.database!='chatboc_tasks_qa':
            raise RuntimeError('Use only the explicitly named loopback disposable QA database')
        cls.schema='tasks_qa_'+uuid4().hex
        cls.admin=create_engine(dsn)
        with cls.admin.begin() as connection:connection.execute(text('CREATE SCHEMA '+cls.schema))
        cls.app=Flask(__name__)
        cls.app.config.update(SQLALCHEMY_DATABASE_URI=dsn,SQLALCHEMY_ENGINE_OPTIONS={'connect_args':{'options':'-csearch_path='+cls.schema}})
        cls.db=db=SQLAlchemy(cls.app)
        class ParentTenant(db.Model):
            __tablename__='tenant_profile'
            id=db.Column(db.Integer,primary_key=True)
        class ParentUser(db.Model):
            __tablename__='user'
            id=db.Column(db.Integer,primary_key=True)
            tenant_id=db.Column(db.Integer,nullable=False)
            rol=db.Column(db.String(32));name=db.Column(db.String(64));email=db.Column(db.String(128))
            is_active=db.Column(db.Boolean,nullable=False,default=True)
        class ParentContact(db.Model):
            __tablename__='contact'
            id=db.Column(db.String(36),primary_key=True)
            tenant_id=db.Column(db.Integer,nullable=False)
        cls.User=ParentUser
        database=types.ModuleType('database');database.db=db;sys.modules['database']=database
        models=types.ModuleType('models');models.User=ParentUser;models.JSONType=db.JSON().with_variant(JSONB(),'postgresql');sys.modules['models']=models
        memory=types.ModuleType('models_memory');memory.Contact=ParentContact;sys.modules['models_memory']=memory
        utils=types.ModuleType('utils');utils.__path__=[str(ROOT/'utils')];sys.modules['utils']=utils
        load('utils.roles','utils/roles.py')
        cls.task_models=load('models_crm_tasks','models_crm_tasks.py')
        cls.service=load('task_service_pg_test','services/crm_tasks.py')
        cls.readiness=load('task_schema_pg_test','services/crm_task_schema.py')
        migration=load('task_pg_migration','migrations/pending/20260924_crm_tasks_v1.py')
        cls.contact=str(uuid4());cls.tenant=SimpleNamespace(id=1,slug='task-pg-qa',municipio_id=None,pyme_id=None)
        with cls.app.app_context():
            with db.engine.connect() as connection:
                cls.actual_pg_version=connection.execute(text('SHOW server_version')).scalar()
                expected=os.environ.get('EXPECTED_POSTGRES_VERSION')
                if expected and cls.actual_pg_version.split(' ',1)[0]!=expected:
                    raise RuntimeError('PostgreSQL test version mismatch')
            db.metadata.create_all(db.engine,tables=[ParentTenant.__table__,ParentUser.__table__,ParentContact.__table__])
            with db.engine.begin() as connection:
                migration.op=Operations(MigrationContext.configure(connection));migration.upgrade()
            db.session.add_all([ParentTenant(id=1),ParentUser(id=1,tenant_id=1,rol='admin',name='QA',email='qa@example.invalid'),ParentContact(id=cls.contact,tenant_id=1)])
            db.session.commit()
    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():cls.db.session.remove();cls.db.engine.dispose()
        with cls.admin.begin() as connection:connection.execute(text('DROP SCHEMA '+cls.schema+' CASCADE'))
        cls.admin.dispose()
    def command(self,payload,task_id=None,key=None):
        with self.app.app_context():
            actor=self.db.session.get(self.User,1)
            return self.service.mutate(self.tenant,actor,self.contact,payload,key or str(uuid4()),task_id)
    def create(self):return self.command({'title':'PostgreSQL QA task'})['task']
    def state(self,task):
        with self.app.app_context():
            row=self.db.session.get(self.task_models.CrmTask,task['id'])
            return self.service.snapshot(row),self.task_models.CrmTaskEvent.query.filter_by(task_id=row.id).count()
    def test_01_migrated_schema_is_ready(self):
        with self.app.app_context():self.assertTrue(self.readiness.task_schema_ready())
    def test_02_simultaneous_edits_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from unittest.mock import patch
        task=self.create();barrier=Barrier(2);normalize=self.service.normalized_changes
        def synchronize(*args,**kwargs):
            value=normalize(*args,**kwargs);barrier.wait(timeout=10);return value
        def edit(title):
            try:return self.command({'title':title,'expected_revision':1,'reason':'Concurrent PostgreSQL test'},task['id'])
            except self.service.TaskError as error:return error.status
        with patch.object(self.service,'normalized_changes',synchronize),ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(edit,['Writer A','Writer B']))
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertEqual([result for result in results if isinstance(result,int)],[409])
        stored,count=self.state(task);self.assertEqual(stored['revision'],2);self.assertEqual(count,2)
    def test_03_concurrent_identical_creation_reuses_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from unittest.mock import patch
        barrier=Barrier(2);normalize=self.service.normalized_changes;key=str(uuid4())
        def synchronize(*args,**kwargs):
            result=normalize(*args,**kwargs);barrier.wait(timeout=10);return result
        with patch.object(self.service,'normalized_changes',synchronize),ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.command({'title':'Same operation'},key=key),[1,2]))
        self.assertEqual(results[0]['task']['id'],results[1]['task']['id'])
        self.assertEqual(sorted(result['receipt']['replayed'] for result in results),[False,True])
        self.assertEqual(self.state(results[0]['task'])[1],1)
    def test_04_history_failure_reverts_task(self):
        from unittest.mock import patch
        task=self.create()
        with patch.object(self.service,'append_event',side_effect=RuntimeError('Synthetic audit failure')):
            with self.assertRaises(RuntimeError):self.command({'title':'Not committed','expected_revision':1,'reason':'Failure test'},task['id'])
        stored,count=self.state(task);self.assertEqual(stored['revision'],1);self.assertEqual(count,1)
    def test_05_database_rejects_event_updates_and_deletes(self):
        task=self.create()
        for operation in ['UPDATE crm_task_event SET reason=\'forbidden\'','DELETE FROM crm_task_event']:
            with self.assertRaises(DBAPIError):
                with self.app.app_context(),self.db.engine.begin() as connection:
                    connection.execute(text(operation+' WHERE task_id=:task'),{'task':task['id']})
        self.assertEqual(self.state(task)[1],1)
    def test_06_receipt_replay_after_later_update_preserves_current_task(self):
        task=self.create();key=str(uuid4());payload={'title':'First update','expected_revision':1,'reason':'Original operation'}
        first=self.command(payload,task['id'],key)
        self.command({'title':'Later update','expected_revision':2,'reason':'Later operation'},task['id'])
        recovered=self.command(payload,task['id'],key)
        self.assertEqual(recovered['receipt']['event_id'],first['receipt']['event_id'])
        stored,count=self.state(task);self.assertEqual(stored['title'],'Later update');self.assertEqual(count,3)
    def test_07_key_conflict_cannot_change_existing_record(self):
        key=str(uuid4());saved=self.command({'title':'Original'},key=key)
        with self.assertRaises(self.service.TaskError) as error:self.command({'title':'Other content'},key=key)
        self.assertEqual(error.exception.status,409);self.assertEqual(self.state(saved['task'])[1],1)

if __name__=='__main__':
    folder=Path(os.environ.get('TASK_QA_EVIDENCE','task-postgres-evidence'));folder.mkdir(parents=True,exist_ok=True)
    with (folder/'postgres-tests.log').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TaskPostgresTests))
    report={'tests':result.testsRun,'errors':len(result.errors),'failures':len(result.failures),'success':result.wasSuccessful(),'database':'disposable PostgreSQL','server_version':getattr(TaskPostgresTests,'actual_pg_version',None),'original_task_service':True,'original_task_models':True,'original_migration':True,'parent_models':'minimal fixtures','http_authentication_tested':False}
    (folder/'postgres-results.json').write_text(json.dumps(report),encoding='utf-8');print(json.dumps(report))
    raise SystemExit(0 if result.wasSuccessful() else 1)
