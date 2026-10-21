"""Run production draft-handler bodies against real SQLAlchemy transactions.

Bootstrap/auth middleware are excluded explicitly. Draft/audit model classes and
handler functions are compiled unchanged from source, not rewritten as mocks.
Tenant/user fixtures and the authorization adapter are synthetic. PostgreSQL CI
is hard-bound to a disposable loopback database; no customer DSN is accepted.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker, relationship, backref
from flask import Flask, request, jsonify, abort
from services import message_templates as templates
from services.whatsapp_pack_transactions import (
    TemplatePackTransactionError, EVENT_TYPE, find_draft_receipt, key_digest, local_draft_transaction,
)
ROOT = Path(__file__).resolve().parents[1]
POSTGRES = os.environ.get('CHATBOC_DRAFT_TEST_POSTGRES') == '1'
Base = declarative_base()


class TenantProfile(Base):
    __tablename__ = 'tenant_profile'
    id = sa.Column(sa.Integer, primary_key=True)
    slug = sa.Column(sa.String(80), nullable=False)
    is_active = sa.Column(sa.Boolean, nullable=False, default=True)


class User(Base):
    __tablename__ = 'user'
    id = sa.Column(sa.Integer, primary_key=True)


Session = scoped_session(sessionmaker(expire_on_commit=False))
Base.query = Session.query_property()
db = SimpleNamespace(Model=Base, session=Session, relationship=relationship, backref=backref,
    **{name:getattr(sa,name) for name in ('Column','Integer','String','Boolean','ForeignKey','Text','DateTime','UniqueConstraint')})
namespace = dict(db=db, JSONType=JSONB().with_variant(sa.JSON(),'sqlite'),
    get_local_now=lambda:datetime.now(timezone.utc), TenantProfile=TenantProfile, User=User)
models = ast.parse((ROOT/'models.py').read_text(encoding='utf-8'))
selected = [node for node in models.body if isinstance(node,ast.ClassDef) and node.name in {'AuditEvent','MessageTemplateRegistry'}]
assert len(selected) == 2
exec(compile(ast.Module(body=selected,type_ignores=[]),'production-draft-models','exec'),namespace)
Audit, Registry = namespace['AuditEvent'], namespace['MessageTemplateRegistry']
def fixture_access(user, tenant, capability):
    if user.tenant_id != tenant.id or user.role != 'admin':
        abort(403)
    return {'whatsapp.templates.read','whatsapp.templates.manage'}


namespace.update(request=request, jsonify=jsonify, abort=abort, re=re, json=json,
    hashlib=hashlib, datetime=datetime, timezone=timezone,
    TemplatePackTransactionError=TemplatePackTransactionError, find_draft_receipt=find_draft_receipt,
    key_digest=key_digest, local_draft_transaction=local_draft_transaction,
    WHATSAPP_TEMPLATE_PACK_LOCAL_PROVIDER='chatboc', WHATSAPP_TEMPLATE_PACKS_READ='whatsapp.templates.read',
    WHATSAPP_TEMPLATE_PACKS_MANAGE='whatsapp.templates.manage',
    _template_pack_capabilities=lambda user:{'whatsapp.templates.read','whatsapp.templates.manage'},
    _require_template_pack_capability=fixture_access,
    _readiness_tenant_for_user=lambda user:Session.get(TenantProfile,user.tenant_id))
for name in ('WHATSAPP_TEMPLATE_PACK_CATALOG_VERSION','normalize_whatsapp_template_vertical',
        'whatsapp_template_definition_hash','whatsapp_template_lifecycle','whatsapp_template_pack','whatsapp_template_pack_catalog'):
    namespace[name] = getattr(templates,name)
names = {'_template_pack_registry_map','_template_pack_catalog_payload','_template_pack_idempotency_key',
    '_template_pack_request_fingerprint','_find_template_pack_idempotency_receipt',
    '_materialize_template_pack_drafts_locked','materialize_whatsapp_template_pack_drafts'}
tree = ast.parse((ROOT/'routes/whatsapp_rules.py').read_text(encoding='utf-8'))
functions = [node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
assert {node.name for node in functions} == names
for node in functions:
    node.decorator_list = []  # No claim of end-to-end auth/middleware coverage.
exec(compile(ast.Module(body=functions,type_ignores=[]),'production-draft-handlers','exec'),namespace)
class DraftTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='chatboc-draft-test-')
        url = 'sqlite:///'+str(Path(cls.directory.name)/'drafts.sqlite')
        if POSTGRES:
            url = sa.URL.create('postgresql+psycopg',username='postgres',password='local_only_test',
                host='127.0.0.1',port=5432,database='chatboc_draft_regression')
        cls.engine = sa.create_engine(url)
        Session.configure(bind=cls.engine)
        cls.app = Flask('draft-transaction-focal')
        cls.app.config.update(TESTING=True)
        def dispatch(vertical):
            user = SimpleNamespace(id=1, tenant_id=int(request.headers.get('X-Fixture-Tenant','1')),
                role=request.headers.get('X-Fixture-Role','admin'))
            return namespace['materialize_whatsapp_template_pack_drafts'](user,vertical)
        cls.app.add_url_rule('/packs/<vertical>',view_func=dispatch,methods=['POST'])
        cls.app.teardown_appcontext(lambda error:Session.remove())

    @classmethod
    def tearDownClass(cls):
        Session.remove(); Base.metadata.drop_all(cls.engine); cls.engine.dispose(); cls.directory.cleanup()

    def setUp(self):
        Session.remove(); Base.metadata.drop_all(self.engine); Base.metadata.create_all(self.engine)
        Session.add_all([TenantProfile(id=1,slug='tenant-a'),TenantProfile(id=2,slug='tenant-b'),User(id=1)])
        Session.commit(); Session.remove()

    def tearDown(self):
        Session.remove()
    def post(self, key='operation-one', vertical='municipio', tenant=1, **kwargs):
        headers={'Idempotency-Key':key,'X-Fixture-Tenant':str(tenant)}
        headers.update(kwargs.pop('headers',{}))
        kwargs.setdefault('json',{'pack_version':'1.0.0'})
        with self.app.test_client() as client:
            return client.post('/packs/'+vertical,headers=headers,**kwargs)

    def test_create_then_replay_has_one_audit_and_five_local_drafts(self):
        first=self.post(); again=self.post()
        self.assertEqual(first.status_code,201,first.get_data(as_text=True))
        self.assertEqual(again.status_code,200)
        self.assertTrue(again.json['idempotent_replay'])
        self.assertEqual(Session.query(Audit).count(),1)
        self.assertEqual(Session.query(Registry).count(),5)
        self.assertTrue(all(row.status=='local_draft' and not row.content_sid for row in Session.query(Registry)))

    def test_old_operation_remains_replayable_after_receipt_cache_eviction(self):
        self.assertEqual(self.post().status_code,201)
        for index in range(24):
            self.assertEqual(self.post(key=f'new-operation-{index}').status_code,200)
        audit_count=Session.query(Audit).count(); Session.remove()
        response=self.post()
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['idempotent_replay'])
        self.assertEqual(Session.query(Audit).count(),audit_count)
        self.assertTrue(all(len(row.metadata_json['materialization_receipts'])==20 for row in Session.query(Registry)))

    def test_new_receipts_do_not_persist_raw_operation_keys(self):
        self.post(); audit=Session.query(Audit).one()
        self.assertEqual(audit.details['idempotency_key_hash'],key_digest('operation-one'))
        self.assertNotIn('operation-one',json.dumps(audit.details))
    def test_legacy_audit_receipt_is_honored_without_metadata_cache(self):
        self.post(); audit=Session.query(Audit).one(); details=dict(audit.details)
        details.pop('idempotency_key_hash'); details['idempotency_key']='operation-one'; audit.details=details
        for row in Session.query(Registry):
            metadata=dict(row.metadata_json); metadata['materialization_receipts']=[]; row.metadata_json=metadata
        Session.commit(); Session.remove()
        response=self.post(); self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['idempotent_replay']); self.assertEqual(Session.query(Audit).count(),1)

    def test_legacy_metadata_receipt_without_audit_remains_compatible(self):
        self.post(); Session.query(Audit).delete()
        for row in Session.query(Registry):
            metadata=deepcopy(row.metadata_json); r=metadata['materialization_receipts'][0]
            r.pop('idempotency_key_hash'); r['idempotency_key']='operation-one'; row.metadata_json=metadata
        Session.commit(); Session.remove()
        response=self.post(); self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['idempotent_replay'])

    def test_key_reuse_with_another_pack_is_rejected(self):
        self.post(); response=self.post(vertical='colegio')
        self.assertEqual(response.status_code,409); self.assertEqual(Session.query(Registry).count(),5)
        self.assertEqual(Session.query(Audit).count(),1)

    def test_same_key_in_different_tenants_is_independent(self):
        self.assertEqual(self.post(tenant=1).status_code,201)
        self.assertEqual(self.post(tenant=2).status_code,201)
        self.assertEqual(Session.query(Audit).count(),2)
        self.assertEqual(Session.query(Registry).filter_by(tenant_id=2).count(),5)
    def test_deleted_draft_does_not_replay_false_success(self):
        self.post(); Session.delete(Session.query(Registry).first()); Session.commit(); Session.remove()
        response=self.post(); self.assertEqual(response.status_code,409)
        self.assertEqual(Session.query(Registry).count(),4); self.assertEqual(Session.query(Audit).count(),1)

    def test_changed_definition_does_not_replay_false_success(self):
        self.post(); row=Session.query(Registry).first(); data=deepcopy(row.metadata_json)
        data['template_pack']['definition_hash']='changed'; row.metadata_json=data
        Session.commit(); Session.remove()
        self.assertEqual(self.post().status_code,409)
        self.assertEqual(Session.query(Audit).count(),1)

    def test_invalid_json_unknown_fields_and_wrong_version_do_not_write(self):
        requests=[dict(json=['bad']),dict(json=None,data='{',content_type='application/json'),
            dict(json={'unknown':True}),dict(json={'pack_version':'9.0.0'}),
            dict(json={'pack_version':None}),dict(json={'pack_version':True}),
            dict(json={'idempotency_key':12345678}),dict(json={'pack_version':''})]
        for kwargs in requests:
            with self.subTest(kwargs=kwargs):
                self.assertIn(self.post(**kwargs).status_code,(400,409))
        self.assertEqual(Session.query(Registry).count(),0); self.assertEqual(Session.query(Audit).count(),0)

    def test_inactive_tenant_and_fixture_denial_prevent_writes(self):
        tenant=Session.get(TenantProfile,1); tenant.is_active=False; Session.commit(); Session.remove()
        self.assertEqual(self.post().status_code,403)
        self.assertEqual(self.post(tenant=2,headers={'X-Fixture-Role':'employee'}).status_code,403)
        self.assertEqual(Session.query(Registry).count(),0)

    def test_conflicting_audit_receipts_fail_closed(self):
        self.post(); details=deepcopy(Session.query(Audit).one().details); details['request_fingerprint']='conflict'
        Session.add(Audit(tenant_id=1,event_type=EVENT_TYPE,resource_type='whatsapp_template_pack',details=details))
        Session.commit(); Session.remove(); self.assertEqual(self.post().status_code,409)
    def test_late_database_failure_rolls_back_drafts_and_audit(self):
        def fail_audit(connection,cursor,statement,parameters,context,executemany):
            if statement.lstrip().upper().startswith('INSERT INTO AUDIT_EVENT'):
                raise sa.exc.OperationalError('injected-local-failure',{},RuntimeError('fixture'))
        event.listen(self.engine,'before_cursor_execute',fail_audit)
        try:
            self.assertEqual(self.post().status_code,503)
        finally:
            event.remove(self.engine,'before_cursor_execute',fail_audit)
        self.assertEqual(Session.query(Registry).count(),0); self.assertEqual(Session.query(Audit).count(),0)
        Session.remove(); self.assertEqual(self.post().status_code,201)

    def test_commit_failure_returns_retryable_error_without_partial_rows(self):
        with patch.object(Session.session_factory.class_,'commit',side_effect=sa.exc.SQLAlchemyError('fixture')):
            self.assertEqual(self.post().status_code,503)
        self.assertEqual(Session.query(Registry).count(),0); self.assertEqual(Session.query(Audit).count(),0)

    def test_commit_happens_after_catalog_response_construction(self):
        with patch.dict(namespace,{'_template_pack_catalog_payload':lambda *args:(_ for _ in ()).throw(RuntimeError('serialize-failed'))}):
            with self.assertRaisesRegex(RuntimeError,'serialize-failed'):
                self.post()
        self.assertEqual(Session.query(Registry).count(),0); self.assertEqual(Session.query(Audit).count(),0)

    def parallel(self,left,right):
        barrier=Barrier(2)
        def call(kwargs):
            barrier.wait(timeout=5)
            response=self.post(**kwargs)
            return response.status_code,response.json
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(call,item) for item in (left,right)]
            return [future.result(timeout=15) for future in futures]
    @unittest.skipUnless(POSTGRES,'row-lock concurrency requires disposable PostgreSQL')
    def test_simultaneous_same_key_has_one_commit_and_one_replay(self):
        responses=self.parallel({}, {})
        self.assertEqual(sorted(code for code,_ in responses),[200,201])
        self.assertEqual(sum(bool(body['idempotent_replay']) for _,body in responses),1)
        self.assertEqual(Session.query(Audit).count(),1); self.assertEqual(Session.query(Registry).count(),5)

    @unittest.skipUnless(POSTGRES,'row-lock concurrency requires disposable PostgreSQL')
    def test_simultaneous_same_key_different_pack_conflicts(self):
        responses=self.parallel({'vertical':'municipio'},{'vertical':'empresa'})
        self.assertEqual(sorted(code for code,_ in responses),[201,409])
        self.assertEqual(Session.query(Audit).count(),1); self.assertEqual(Session.query(Registry).count(),5)

    @unittest.skipUnless(POSTGRES,'row-lock concurrency requires disposable PostgreSQL')
    def test_simultaneous_different_keys_do_not_duplicate_draft_rows(self):
        responses=self.parallel({'key':'operation-left'},{'key':'operation-right'})
        self.assertEqual(sorted(code for code,_ in responses),[200,201])
        self.assertEqual(Session.query(Audit).count(),2); self.assertEqual(Session.query(Registry).count(),5)

    @unittest.skipUnless(POSTGRES,'row-lock concurrency requires disposable PostgreSQL')
    def test_simultaneous_tenants_do_not_share_receipts(self):
        responses=self.parallel({'tenant':1},{'tenant':2})
        self.assertEqual([code for code,_ in responses],[201,201])
        self.assertEqual({body['tenant']['id'] for _,body in responses},{1,2})
        self.assertEqual(Session.query(Audit).count(),2); self.assertEqual(Session.query(Registry).count(),10)

if __name__ == '__main__':
    unittest.main()
