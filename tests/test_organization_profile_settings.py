"""Production profile service with real SQL transactions and source model columns.

Auth/session middleware is not booted. Fixture membership is explicit; external
network and customer databases are not used. PostgreSQL target is loopback-only.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from flask import Flask, jsonify, request
from services.organization_profile_settings import (
    build_profile_settings, save_profile_settings, ProfileSettingsError, DAYS, HOURS_KEY,
)
ROOT=Path(__file__).resolve().parents[1]
POSTGRES=os.environ.get('CHATBOC_DRAFT_TEST_POSTGRES')=='1'
Base=declarative_base()
Session=scoped_session(sessionmaker(expire_on_commit=False))
Base.query=Session.query_property()
db=SimpleNamespace(Model=Base,session=Session,**{key:getattr(sa,key) for key in
    ('Column','Integer','String','Boolean','ForeignKey','Float','Text','DateTime')})
namespace={'db':db,'json':json,'JSONType':JSONB().with_variant(sa.JSON(),'sqlite'),
    'get_local_now':lambda:datetime.now(timezone.utc)}
tree=ast.parse((ROOT/'models.py').read_text(encoding='utf-8'))
selections={
 'User':{'id','rol','tenant_id','tenant_slug','name','email','nombre_empresa','telefono','direccion',
         'ciudad','provincia','pais','latitud','longitud','link_web','logo_url','horario'},
 'TenantProfile':{'id','slug','nombre','tipo','municipio_id','pyme_id','logo_url','configuracion','is_active'},
}
for name,columns in selections.items():
    original=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==name)
    body=[n for n in original.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in columns|{'__tablename__'} for t in n.targets)]
    if name=='User': body.extend(n for n in original.body if isinstance(n,ast.FunctionDef) and n.name=='horario_json')
    new=ast.ClassDef(name=name,bases=[ast.Attribute(value=ast.Name(id='db',ctx=ast.Load()),attr='Model',ctx=ast.Load())],keywords=[],body=body,decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[new],type_ignores=[])),'production-profile-columns','exec'),namespace)
audit=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='AuditEvent')
exec(compile(ast.Module(body=[audit],type_ignores=[]),'production-audit-model','exec'),namespace)
User,Tenant,Audit=(namespace[key] for key in ('User','TenantProfile','AuditEvent'))
# Named fixture constraints allow deterministic teardown of the source's cyclic FKs.
for table in Base.metadata.tables.values():
    for index,constraint in enumerate(table.foreign_key_constraints):
        if constraint.name is None:constraint.name=f'fixture_{table.name}_fk_{index}'


def allowed(actor,tenant):
    return actor.rol=='admin' and actor.tenant_id==tenant.id

def hours():
    return [{'dia':day,'abre':'09:00','cierra':'16:00','cerrado':False} for day in DAYS]
class OrganizationProfileSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory(prefix='profile-settings-')
        url='sqlite:///'+str(Path(cls.directory.name)/'settings.sqlite')
        if POSTGRES:
            url=sa.URL.create('postgresql+psycopg',username='postgres',password='local_only_test',
                host='127.0.0.1',port=5432,database='chatboc_draft_regression')
        cls.engine=sa.create_engine(url);Session.configure(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Session.remove();Base.metadata.drop_all(cls.engine);cls.engine.dispose();cls.directory.cleanup()

    def setUp(self):
        Session.remove();Base.metadata.drop_all(self.engine);Base.metadata.create_all(self.engine)
        Session.add_all([User(id=1,name='Owner',email='owner@example.test',rol='admin',tenant_slug='tenant-a'),
            User(id=2,name='Operator',email='operator@example.test',rol='admin',tenant_slug='tenant-a'),
            User(id=3,name='Other',email='other@example.test',rol='admin',tenant_slug='tenant-b')])
        Session.flush()
        Session.add_all([Tenant(id=1,slug='tenant-a',nombre='Organización A',tipo='municipio',municipio_id=1,is_active=True,configuracion={'keep':True}),
            Tenant(id=2,slug='tenant-b',nombre='Organización B',tipo='empresa',pyme_id=3,is_active=True)])
        Session.flush()
        for identifier,tenant_id in ((1,1),(2,1),(3,2)):Session.get(User,identifier).tenant_id=tenant_id
        Session.commit();Session.remove()
        self.initial=self.read();Session.remove()

    def tearDown(self): Session.remove()
    def read(self,tenant_id=1):
        tenant=Session.get(Tenant,tenant_id);owner=Session.get(User,tenant.municipio_id or tenant.pyme_id)
        return build_profile_settings(tenant,owner,can_edit=True)
    def save(self,changes=None,revision=None,actor=2,tenant=1):
        return save_profile_settings(Session,Tenant,User,Audit,tenant_id=tenant,actor_id=actor,
            data={'organization_profile':changes or {'nombre_empresa':'Nueva organización'},
                'expected_revision':revision or self.initial['revision']},authorize=allowed)

    def test_institutional_fields_persist_and_personal_operator_is_unchanged(self):
        result=self.save({'nombre_empresa':'Nombre nuevo','telefono':'+5492615550101',
            'direccion':'Calle 100','ciudad':'Ciudad','provincia':'Provincia','pais':'Argentina',
            'latitud':-33.0,'longitud':-68.0,'link_web':'https://example.test/info','logo_url':'https://example.test/logo.png','horario_json':hours()})
        self.assertTrue(result['saved']);Session.remove();stored=self.read()
        self.assertEqual(stored['values']['nombre_empresa'],'Nombre nuevo')
        self.assertEqual(stored['values']['horario_json'],hours())
        self.assertEqual(Session.get(User,2).name,'Operator');self.assertIsNone(Session.get(User,2).telefono)
        self.assertEqual(Session.get(Tenant,1).configuracion['keep'],True)
        self.assertIsNone(Session.get(User,1).horario)
        self.assertNotIn('email',stored['values'])
        self.assertEqual(Session.query(Audit).count(),1)
        self.assertNotIn('+5492615550101',json.dumps(Session.query(Audit).one().details))

    def test_stale_revision_cannot_overwrite_another_administrator(self):
        self.save();Session.remove()
        with self.assertRaises(ProfileSettingsError) as error:self.save({'nombre_empresa':'Old overwrite'})
        self.assertEqual(error.exception.status,412)
        self.assertEqual(self.read()['values']['nombre_empresa'],'Nueva organización')
        self.assertEqual(Session.query(Audit).count(),1)

    def test_verified_reread_allows_an_explicit_new_save(self):
        self.save();Session.remove();new=self.read();Session.remove()
        self.assertTrue(self.save({'telefono':'12345'},revision=new['revision'])['saved'])
    def test_unchanged_values_do_not_add_audit(self):
        result=self.save({'nombre_empresa':self.initial['values']['nombre_empresa']})
        self.assertFalse(result['saved']);self.assertEqual(Session.query(Audit).count(),0)
        self.assertEqual(result['profile']['revision'],self.initial['revision'])

    def test_roles_accounts_and_provider_fields_are_rejected(self):
        for field in ('rol','email','password','plan','dominio','whatsapp_sender_id','avatar_url'):
            with self.subTest(field=field),self.assertRaises(ProfileSettingsError):self.save({field:'not-allowed'})
        self.assertEqual(Session.query(Audit).count(),0)

    def test_invalid_payload_never_partially_updates_name(self):
        values=[{'nombre_empresa':'Good','telefono':'x'*21},{'nombre_empresa':''},
            {'latitud':float('nan'),'longitud':1},{'latitud':True},{'latitud':10},
            {'logo_url':'https://user:pass@example.test/logo'},{'link_web':'http://127.0.0.1/a'},
            {'link_web':'javascript:alert(1)'},{'horario_json':'not-array'},{'horario_json':[{}]}]
        for changes in values:
            with self.subTest(changes=changes),self.assertRaises(ProfileSettingsError):self.save(changes)
        self.assertEqual(self.read()['values']['nombre_empresa'],'Organización A')
        self.assertEqual(Session.query(Audit).count(),0)

    def test_foreign_admin_and_revoked_role_are_rejected(self):
        with self.assertRaises(ProfileSettingsError) as error:self.save(actor=3)
        self.assertEqual(error.exception.status,403)
        operator=Session.get(User,2);operator.rol='empleado';Session.commit();Session.remove()
        with self.assertRaises(ProfileSettingsError):self.save()
        self.assertEqual(Session.query(Audit).count(),0)
    def test_inactive_tenant_is_rechecked(self):
        tenant=Session.get(Tenant,1);tenant.is_active=False;Session.commit();Session.remove()
        with self.assertRaises(ProfileSettingsError):self.save()
        self.assertEqual(Session.query(Audit).count(),0)

    def test_same_values_in_another_tenant_have_a_distinct_revision(self):
        tenant=Session.get(Tenant,2);tenant.nombre='Organización A';Session.commit()
        self.assertNotEqual(self.read(2)['revision'],self.initial['revision'])

    def test_legacy_owner_change_invalidates_revision(self):
        owner=Session.get(User,1);owner.telefono='changed-elsewhere';Session.commit();Session.remove()
        with self.assertRaises(ProfileSettingsError) as error:self.save()
        self.assertEqual(error.exception.status,412)

    def test_audit_failure_rolls_back_tenant_owner_and_hours(self):
        def fail(connection,cursor,statement,parameters,context,executemany):
            if statement.lstrip().upper().startswith('INSERT INTO AUDIT_EVENT'):
                raise sa.exc.OperationalError('fixture',{},RuntimeError('fixture'))
        sa.event.listen(self.engine,'before_cursor_execute',fail)
        try:
            with self.assertRaises(ProfileSettingsError) as error:self.save({'nombre_empresa':'Changed','horario_json':hours()})
            self.assertEqual(error.exception.status,503)
        finally:sa.event.remove(self.engine,'before_cursor_execute',fail)
        self.assertEqual(self.read()['values'],self.initial['values']);self.assertEqual(Session.query(Audit).count(),0)

    def test_commit_failure_is_not_success(self):
        with patch.object(Session.session_factory.class_,'commit',side_effect=sa.exc.SQLAlchemyError('fixture')):
            with self.assertRaises(ProfileSettingsError):self.save()
        self.assertEqual(self.read()['values'],self.initial['values'])
    @unittest.skipUnless(POSTGRES,'concurrent row locks are checked in PostgreSQL CI')
    def test_two_admins_with_same_revision_have_one_save_and_one_conflict(self):
        barrier=Barrier(2)
        def worker(name):
            barrier.wait(timeout=5)
            try:return self.save({'nombre_empresa':name})['ok']
            except ProfileSettingsError as error:return error.status
            finally:Session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(worker,['Choice A','Choice B']))
        self.assertEqual(sorted(results,key=str),sorted([True,412],key=str))
        self.assertEqual(Session.query(Audit).count(),1)

    @unittest.skipUnless(POSTGRES,'concurrent row locks are checked in PostgreSQL CI')
    def test_independent_organizations_can_save_concurrently(self):
        other=self.read(2);Session.remove();barrier=Barrier(2)
        def worker(tenant):
            barrier.wait(timeout=5)
            try:return self.save({'ciudad':'City'},actor=2 if tenant==1 else 3,tenant=tenant,
                revision=self.initial['revision'] if tenant==1 else other['revision'])['tenant']['id']
            finally:Session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(set(pool.map(worker,[1,2])),{1,2})
        self.assertEqual(Session.query(Audit).count(),2)

    def test_existing_config_route_returns_a_verified_receipt(self):
        source=ast.parse((ROOT/'routes/admin_tenant.py').read_text(encoding='utf-8'))
        handler=next(node for node in source.body if isinstance(node,ast.FunctionDef) and node.name=='update_tenant_config_bundle')
        handler.decorator_list=[]
        context={'request':request,'jsonify':jsonify,'save_profile_settings':save_profile_settings,
            'ProfileSettingsError':ProfileSettingsError,'db':db,'TenantProfile':Tenant,'User':User,'AuditEvent':Audit,
            '_resolve_admin_tenant':lambda user,slug:Session.query(Tenant).filter_by(slug=slug).one_or_none(),
            '_is_authorized_for_tenant':allowed,'can_manage_tenant_control_plane':allowed}
        exec(compile(ast.Module(body=[handler],type_ignores=[]),'production-config-route','exec'),context)
        app=Flask('config-route-fixture')
        with app.test_request_context('/api/admin/tenants/tenant-a/config',method='PUT',json={
            'organization_profile':{'nombre_empresa':'Saved through endpoint'},'expected_revision':self.initial['revision']}):
            response,status=context['update_tenant_config_bundle'](Session.get(User,2),'tenant-a')
            self.assertEqual(status,200)
            self.assertEqual(response.json['profile']['values']['nombre_empresa'],'Saved through endpoint')
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        Session.remove()
        self.assertEqual(self.read()['values']['nombre_empresa'],'Saved through endpoint')

    def test_shared_legacy_owner_is_not_updated_across_organizations(self):
        other=Session.get(Tenant,2);other.pyme_id=1;Session.commit();Session.remove()
        with self.assertRaises(ProfileSettingsError) as error:self.save()
        self.assertEqual(error.exception.code,'profile_owner_shared')
        self.assertEqual(self.read()['values'],self.initial['values'])


    def test_optional_read_contract_handles_ambiguous_owner_without_exception(self):
        tenant=Session.get(Tenant,1);owner=Session.get(User,1)
        tenant.pyme_id=3
        self.assertIsNone(build_profile_settings(tenant,owner,can_edit=True))
        tenant.pyme_id=None;tenant.municipio_id=None
        self.assertIsNone(build_profile_settings(tenant,owner,can_edit=True))
        Session.rollback()

    def test_optional_read_contract_rejects_nonfinite_coordinates(self):
        tenant=Session.get(Tenant,1);owner=Session.get(User,1)
        owner.latitud=float('inf')
        self.assertIsNone(build_profile_settings(tenant,owner,can_edit=True))
        Session.rollback()

    def test_missing_version_never_commits(self):
        with self.assertRaises(ProfileSettingsError) as error:
            save_profile_settings(Session,Tenant,User,Audit,tenant_id=1,actor_id=2,
                data={'organization_profile':{'nombre_empresa':'Unexpected'}},authorize=allowed)
        self.assertEqual(error.exception.status,428)
        self.assertEqual(self.read()['values'],self.initial['values'])

    def test_legacy_string_schedule_is_preserved_without_synthesizing_hours(self):
        owner=Session.get(User,1);owner.horario='Consultar disponibilidad';Session.commit();Session.remove()
        baseline=self.read();Session.remove()
        result=self.save({'ciudad':'Ciudad cambiada'},revision=baseline['revision'])
        self.assertEqual(result['profile']['values']['horario_json'],[])
        self.assertEqual(Session.get(User,1).horario,'Consultar disponibilidad')


    def test_editability_reason_distinguishes_maintenance_and_permissions(self):
        tenant=Session.get(Tenant,1);owner=Session.get(User,1)
        readonly=build_profile_settings(tenant,owner,can_edit=False)
        self.assertEqual(readonly['editability']['reason_code'],'tenant_admin_required')
        maintenance=build_profile_settings(tenant,owner,can_edit=True,writes_blocked=True)
        self.assertFalse(maintenance['can_edit'])
        self.assertEqual(maintenance['editability']['reason_code'],'maintenance')
        self.assertIn('Tus permisos no cambiaron',maintenance['editability']['message'])

    def test_editability_change_does_not_change_content_revision(self):
        tenant=Session.get(Tenant,1);owner=Session.get(User,1)
        editable=build_profile_settings(tenant,owner,can_edit=True)
        maintenance=build_profile_settings(tenant,owner,can_edit=True,writes_blocked=True)
        readonly=build_profile_settings(tenant,owner,can_edit=False)
        self.assertEqual(editable['revision'],maintenance['revision'])
        self.assertEqual(editable['revision'],readonly['revision'])
        self.assertEqual(editable['editability']['reason_code'],'ready')

    def test_editability_does_not_coerce_string_permissions_to_true(self):
        tenant=Session.get(Tenant,1);owner=Session.get(User,1)
        result=build_profile_settings(tenant,owner,can_edit='true')
        self.assertFalse(result['can_edit'])
        self.assertEqual(result['editability']['mode'],'read_only')

if __name__=='__main__':unittest.main()
