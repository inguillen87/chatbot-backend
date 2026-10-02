"""Real application routes/models on a disposable database; no customer credentials."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__': prepare_process()
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch
from tests.test_institutional_assistant_content import sample
from tests.institutional_assistant_routing_cases import ExistingResponderCases

class KnowledgeHTTPTests(ExistingResponderCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.temp=tempfile.TemporaryDirectory(prefix='chatboc-knowledge-')
        import os
        os.environ['CORS_ALLOWED_ORIGINS']='https://panel.example.invalid'
        cls.app,cls.accounts,cls.password=create_disposable_app(cls.temp.name)
    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():db.session.remove();db.engine.dispose()
        cls.temp.cleanup()
    def setUp(self):
        from database import db
        from models import TenantConfig,AuditEvent,TenantProfile
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED']=False
        with self.app.app_context():
            TenantConfig.query.filter_by(key='institutional_assistant').delete()
            AuditEvent.query.filter(AuditEvent.event_type.like('institutional_knowledge.%')).delete()
            for key in ('acceptance-a','acceptance-b'):
                tenant=db.session.get(TenantProfile,self.accounts[key]['tenant_id']);tenant.is_active=True
            db.session.commit()
    def login(self,key='acceptance-a'):
        client=self.app.test_client()
        response=client.post('/auth/login',json={'email':self.accounts[key]['email'],'password':self.password})
        self.assertEqual(response.status_code,200,response.get_json());return client
    def url(self,public=False):return '/api/'+('public' if public else 'admin')+'/tenants/acceptance-a/institutional-assistant'
    def get(self,client):
        response=client.get(self.url());self.assertEqual(response.status_code,200,response.get_json());return response.get_json()
    def put(self,client,operation,revision,bundle=None):
        data={'operation':operation,'expected_revision':revision}
        if bundle is not None:data['bundle']=bundle
        return client.put(self.url(),json=data,headers={'X-Chatboc-Knowledge':'1'})
    def seed(self,client):
        response=self.put(client,'import',None,sample(self.accounts['acceptance-a']['tenant_id'],'acceptance-a'))
        self.assertEqual(response.status_code,200,response.get_json());return response.get_json()
    def node_url(self,revision,node='requirements',public=False):
        return self.url(public)+'/nodes/'+node+'?revision='+revision
    def test_canonical_get_matches_menu_post_without_model_or_database_commit(self):
        from database import db
        from models import TenantConfig,AuditEvent
        client=self.login();state=self.seed(client)
        with self.app.app_context():
            before=deepcopy(TenantConfig.query.filter_by(tenant_id=self.accounts['acceptance-a']['tenant_id'],key='institutional_assistant').one().json_value)
            audit_before=AuditEvent.query.count()
        with patch('services.institutional_assistant.select_nodes',side_effect=AssertionError('canonical GET must not call selection')),patch.object(db.session,'commit',side_effect=AssertionError('canonical GET must not commit')):
            response=client.get(self.node_url(state['revision']))
        self.assertEqual(response.status_code,200,response.get_json())
        payload=response.get_json()
        self.assertFalse(payload['selection_performed']);self.assertFalse(payload['business_writes_performed'])
        self.assertEqual(payload['text'],'Respuesta institucional de prueba.')
        self.assertEqual(payload['nodes'][0]['sources'][0]['pages'],[2])
        self.assertNotIn('PRIVATE',str(payload))
        self.assertEqual(response.headers['Cache-Control'],'private, no-store')
        old=client.post(self.url()+'/answer',json={'revision':state['revision'],'node_id':'requirements'})
        self.assertEqual(old.status_code,200,old.get_json());self.assertEqual(payload,old.get_json())
        with self.app.app_context():
            self.assertEqual(TenantConfig.query.filter_by(tenant_id=self.accounts['acceptance-a']['tenant_id'],key='institutional_assistant').one().json_value,before)
            self.assertEqual(AuditEvent.query.count(),audit_before)
    def test_canonical_get_rejects_noncanonical_queries_and_foreign_private_access(self):
        client=self.login();state=self.seed(client);path=self.node_url(state['revision'])
        for suffix in ('&question=consulta','&revision='+state['revision'],'&node_id=start','&unknown=1','&tenant=acceptance-b'):
            with self.subTest(suffix=suffix):
                self.assertEqual(client.get(path+suffix).status_code,400)
        for revision in ('','not-a-revision','A'*64):
            self.assertEqual(client.get(self.node_url(revision)).status_code,400)
        self.assertEqual(client.get(self.url()+'/nodes/requirements').status_code,400)
        self.assertEqual(client.get(path,json={'question':'consulta'}).status_code,400)
        self.assertEqual(client.get(path,headers={'X-Tenant':'acceptance-b'}).status_code,400)
        self.assertEqual(client.get(self.node_url(state['revision'],'missing')).status_code,400)
        self.assertEqual(client.get(self.node_url('0'*64)).status_code,412)
        self.assertIn(self.app.test_client().get(path).status_code,(401,403))
        for account in ('acceptance-b','viewer'):
            self.assertEqual(self.login(account).get(path).status_code,403)
        self.assertEqual(client.get(path.replace('/acceptance-a/','/missing-tenant/')).status_code,404)
    def test_public_canonical_get_exposes_only_current_published_state(self):
        client=self.login();state=self.seed(client);public=self.app.test_client()
        self.assertEqual(public.get(self.node_url(state['revision'],public=True)).status_code,404)
        published=self.put(client,'publish',state['revision']).get_json()
        response=public.get(self.node_url(published['revision'],public=True))
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertFalse(response.get_json()['selection_performed'])
        self.assertFalse(response.get_json()['business_writes_performed'])
        self.assertEqual(public.get(self.node_url(state['revision'],public=True)).status_code,412)
        self.assertEqual(public.get(self.node_url(published['revision'],public=True)+'&question=consulta').status_code,400)
        self.assertEqual(self.put(client,'retire',published['revision']).status_code,200)
        self.assertEqual(public.get(self.node_url(published['revision'],public=True)).status_code,404)
    def test_canonical_get_real_http_login_isolation_and_publication(self):
        import requests
        from threading import Thread
        from werkzeug.serving import make_server
        client=self.login();state=self.seed(client)
        server=make_server('127.0.0.1',0,self.app,threaded=True)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        owner=requests.Session();foreign=requests.Session();anonymous=requests.Session()
        try:
            for session,account in ((owner,'acceptance-a'),(foreign,'acceptance-b')):
                login=session.post(base+'/auth/login',json={'email':self.accounts[account]['email'],'password':self.password},timeout=10)
                self.assertEqual(login.status_code,200)
            path=self.node_url(state['revision'])
            own=owner.get(base+path,timeout=10);self.assertEqual(own.status_code,200)
            self.assertEqual(own.json()['tenant']['slug'],'acceptance-a')
            self.assertEqual(foreign.get(base+path,timeout=10).status_code,403)
            self.assertIn(anonymous.get(base+path,timeout=10).status_code,(401,403))
            self.assertEqual(owner.get(base+self.node_url('0'*64),timeout=10).status_code,412)
            self.assertEqual(anonymous.get(base+self.node_url(state['revision'],public=True),timeout=10).status_code,404)
            published=self.put(client,'publish',state['revision']).get_json()
            self.assertEqual(anonymous.get(base+self.node_url(published['revision'],public=True),timeout=10).status_code,200)
        finally:
            owner.close();foreign.close();anonymous.close();server.shutdown();thread.join(timeout=5);server.server_close()
    def test_empty_workspace_is_truthful(self):
        state=self.get(self.login());self.assertIsNone(state['knowledge']);self.assertIsNone(state['revision'])
        self.assertEqual(self.app.test_client().get(self.url(True)).status_code,404)
    def test_normal_login_profile_capabilities_match_private_knowledge_authorization(self):
        for account in ('acceptance-a', 'acceptance-b', 'second', 'delegated'):
            with self.subTest(account=account):
                client=self.login(account)
                profile=client.get('/api/me')
                self.assertEqual(profile.status_code,200,profile.get_json())
                data=profile.get_json()
                slug='acceptance-b' if account=='acceptance-b' else 'acceptance-a'
                self.assertEqual(data['tenant_slug'],slug)
                self.assertEqual(data['organization_workspace']['tenant']['slug'],slug)
                for key in ('capabilities','permissions','scopes'):
                    self.assertIn('knowledge.read',data[key])
                    self.assertIn('knowledge.write',data[key])
                path='/api/admin/tenants/'+slug+'/institutional-assistant'
                self.assertEqual(client.get(path).status_code,200)
                other='acceptance-a' if slug=='acceptance-b' else 'acceptance-b'
                self.assertEqual(client.get('/api/admin/tenants/'+other+'/institutional-assistant').status_code,403)
                for path in ('/auth/session/bootstrap','/auth/me/dashboard'):
                    response=client.get(path)
                    self.assertEqual(response.status_code,200,response.get_json())
                    payload=response.get_json()
                    scopes=payload.get('user',payload)['capabilities']
                    self.assertIn('knowledge.read',scopes)
                    self.assertIn('knowledge.write',scopes)
    def test_profile_does_not_inherit_public_or_other_tenant_selectors(self):
        client=self.login()
        for path,headers in [('/api/me?tenant=acceptance-b',{}),('/api/me',{'X-Tenant-Slug':'acceptance-b'}),('/api/me',{'Referer':'https://panel.example.invalid/municipio/acceptance-b'})]:
            with self.subTest(path=path,headers=headers):
                response=client.get(path,headers=headers)
                self.assertEqual(response.status_code,200,response.get_json())
                data=response.get_json()
                self.assertEqual(data['tenant_slug'],'acceptance-a')
                self.assertEqual(data['organization_workspace']['tenant']['slug'],'acceptance-a')
    def test_employee_metadata_cannot_advertise_private_knowledge_access(self):
        from database import db
        from models import User
        with self.app.app_context():
            user=db.session.get(User,self.accounts['viewer']['id']);original=deepcopy(user.accesibilidad)
            user.accesibilidad={'capabilities':['knowledge.read','knowledge.write','settings.tenant.write']};db.session.commit()
        try:
            client=self.login('viewer');response=client.get('/api/me')
            self.assertEqual(response.status_code,200,response.get_json())
            for key in ('capabilities','permissions','scopes'):
                self.assertNotIn('knowledge.read',response.get_json()[key])
                self.assertNotIn('knowledge.write',response.get_json()[key])
                self.assertNotIn('settings.tenant.write',response.get_json()[key])
            self.assertEqual(client.get(self.url()).status_code,403)
            self.assertEqual(client.post(self.url()+'/answer',json={'revision':None,'node_id':'start'}).status_code,403)
            self.assertEqual(self.put(client,'publish',None).status_code,403)
        finally:
            with self.app.app_context():
                user=db.session.get(User,self.accounts['viewer']['id']);user.accesibilidad=original;db.session.commit()
    def test_login_selector_cannot_rebind_existing_tenant_membership(self):
        from database import db
        from models import User
        with self.app.app_context():
            user=db.session.get(User,self.accounts['viewer']['id']);original=user.municipio_id
            user.municipio_id=None;db.session.commit()
        try:
            client=self.app.test_client()
            response=client.post('/auth/login',json={'email':self.accounts['viewer']['email'],'password':self.password,'tenant_slug':'acceptance-b'})
            self.assertEqual(response.status_code,200,response.get_json())
            self.assertEqual(response.get_json()['tenant_slug'],'acceptance-a')
            with self.app.app_context():
                user=db.session.get(User,self.accounts['viewer']['id'])
                self.assertEqual(user.tenant_id,self.accounts['acceptance-a']['tenant_id'])
                self.assertEqual(user.tenant_slug,'acceptance-a')
        finally:
            with self.app.app_context():
                user=db.session.get(User,self.accounts['viewer']['id']);user.municipio_id=original;db.session.commit()
    def test_global_superadmin_capabilities_require_the_email_allowlist(self):
        from routes.auth import _profile_capabilities_for_user
        from models import User
        with self.app.test_request_context('/api/me'),patch.dict('os.environ',{'CLERK_SUPERADMIN_EMAILS':'allowed@example.invalid'}):
            for email,allowed in [('allowed@example.invalid',True),('outside@example.invalid',False)]:
                with self.subTest(email=email):
                    user=User(id=990001,name='Platform account',email=email,rol='super_admin')
                    capabilities=_profile_capabilities_for_user(user)
                    for capability in ('*','knowledge.read','knowledge.write','settings.tenant.write'):
                        self.assertEqual(capability in capabilities,allowed)
    def test_conflicting_membership_denies_direct_private_knowledge_routes(self):
        from database import db
        from models import User
        from utils.tenant_admin_access import can_manage_tenant_control_plane
        from models import TenantProfile
        cases=[('second',{'tenant_slug':'acceptance-b'}),
            ('acceptance-a',{'tenant_id':self.accounts['acceptance-b']['tenant_id'],'tenant_slug':'acceptance-b'}),
            ('second',{'municipio_id':self.accounts['acceptance-b']['id']}),
            ('delegated',{'empresa_id':self.accounts['acceptance-b']['id']})]
        for account,changes in cases:
            with self.subTest(account=account,changes=changes):
                client=self.login(account)
                with self.app.app_context():
                    user=db.session.get(User,self.accounts[account]['id'])
                    original={key:getattr(user,key) for key in changes}
                    for key,value in changes.items():setattr(user,key,value)
                    db.session.commit()
                try:
                    with self.app.app_context():
                        user=db.session.get(User,self.accounts[account]['id'])
                        for slug in ('acceptance-a','acceptance-b'):
                            tenant=db.session.get(TenantProfile,self.accounts[slug]['tenant_id'])
                            self.assertFalse(can_manage_tenant_control_plane(user,tenant))
                    for slug in ('acceptance-a','acceptance-b'):
                        path='/api/admin/tenants/'+slug+'/institutional-assistant'
                        self.assertEqual(client.get(path).status_code,403)
                        self.assertEqual(client.post(path+'/answer',json={'revision':None,'node_id':'start'}).status_code,403)
                        self.assertEqual(client.put(path,json={'operation':'publish','expected_revision':None},headers={'X-Chatboc-Knowledge':'1'}).status_code,403)
                    profile=client.get('/api/me')
                    self.assertEqual(profile.status_code,200,profile.get_json())
                    self.assertNotIn('tenant_slug',profile.get_json())
                    self.assertNotIn('organization_workspace',profile.get_json())
                    for capability in ('knowledge.read','knowledge.write','settings.tenant.write'):
                        self.assertNotIn(capability,profile.get_json()['capabilities'])
                finally:
                    with self.app.app_context():
                        user=db.session.get(User,self.accounts[account]['id'])
                        for key,value in original.items():setattr(user,key,value)
                        db.session.commit()
    def test_consistent_legacy_owner_membership_keeps_private_knowledge_access(self):
        from database import db
        from models import User,TenantProfile
        from utils.tenant_admin_access import can_manage_tenant_control_plane
        client=self.login('second')
        with self.app.app_context():
            user=db.session.get(User,self.accounts['second']['id'])
            original={key:getattr(user,key) for key in ('tenant_id','tenant_slug','empresa_id','municipio_id','pyme_id')}
        try:
            for legacy_field in ('municipio_id','pyme_id'):
                with self.subTest(legacy_field=legacy_field),self.app.app_context():
                    user=db.session.get(User,self.accounts['second']['id'])
                    for field in original:setattr(user,field,None)
                    setattr(user,legacy_field,self.accounts['acceptance-a']['id']);db.session.commit()
                    own=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
                    other=db.session.get(TenantProfile,self.accounts['acceptance-b']['tenant_id'])
                    self.assertTrue(can_manage_tenant_control_plane(user,own))
                    self.assertFalse(can_manage_tenant_control_plane(user,other))
                    self.assertEqual(client.get(self.url()).status_code,200)
        finally:
            with self.app.app_context():
                user=db.session.get(User,self.accounts['second']['id'])
                for key,value in original.items():setattr(user,key,value)
                db.session.commit()
    def test_invalid_legacy_owner_reference_does_not_select_the_other_valid_owner(self):
        from database import db
        from models import User
        client=self.login('second')
        with self.app.app_context():
            user=db.session.get(User,self.accounts['second']['id'])
            original={key:getattr(user,key) for key in ('tenant_id','tenant_slug','empresa_id','municipio_id','pyme_id')}
        try:
            with self.app.app_context():
                user=db.session.get(User,self.accounts['second']['id'])
                user.tenant_id=None;user.tenant_slug=None;user.empresa_id=None
                user.municipio_id=self.accounts['acceptance-a']['id'];user.pyme_id=99999999;db.session.commit()
            response=client.get('/api/me')
            self.assertEqual(response.status_code,200,response.get_json())
            self.assertNotIn('tenant_slug',response.get_json())
            self.assertNotIn('organization_workspace',response.get_json())
            self.assertNotIn('knowledge.read',response.get_json()['capabilities'])
            bootstrap=client.get('/auth/session/bootstrap')
            self.assertEqual(bootstrap.status_code,200,bootstrap.get_json())
            self.assertIsNone(bootstrap.get_json()['user']['tenant_slug'])
            self.assertEqual(client.get(self.url()).status_code,403)
            self.assertEqual(client.post(self.url()+'/answer',json={'revision':None,'node_id':'start'}).status_code,403)
        finally:
            with self.app.app_context():
                user=db.session.get(User,self.accounts['second']['id'])
                for key,value in original.items():setattr(user,key,value)
                db.session.commit()
    def test_import_persists_and_publication_is_explicit(self):
        client=self.login();state=self.seed(client)
        self.assertEqual(state['visibility'],'private');self.assertEqual(self.get(client)['revision'],state['revision'])
        self.assertEqual(self.app.test_client().get(self.url(True)).status_code,404)
        published=self.put(client,'publish',state['revision']);self.assertEqual(published.status_code,200,published.get_json())
        response=self.app.test_client().get(self.url(True));self.assertEqual(response.status_code,200)
        self.assertFalse(response.get_json()['can_edit']);self.assertIn('no-store',response.headers['Cache-Control'])
    def test_menu_and_provider_selection_use_same_canonical_text(self):
        client=self.login();state=self.seed(client);body={'revision':state['revision'],'node_id':'requirements'}
        menu=client.post(self.url()+'/answer',json=body);self.assertEqual(menu.status_code,200,menu.get_json())
        with patch('services.llm_utils.llamar_llm_para_json_estructurado',return_value={'node_ids':['requirements']}) as model:
            response=client.post(self.url()+'/answer',json={**body,'question':'¿Qué hace falta?'})
            self.assertEqual(response.status_code,200,response.get_json());self.assertEqual(response.get_json()['nodes'],menu.get_json()['nodes']);model.assert_called_once()
    def test_retired_source_and_old_revision_are_rejected(self):
        client=self.login();state=self.seed(client);published=self.put(client,'publish',state['revision']).get_json()
        self.assertEqual(self.put(client,'publish',state['revision']).status_code,412)
        retired=self.put(client,'retire',published['revision']);self.assertEqual(retired.status_code,200)
        self.assertEqual(self.app.test_client().post(self.url(True)+'/answer',json={'revision':published['revision'],'node_id':'start'}).status_code,404)
    def test_other_tenant_cannot_read_or_change_private_content(self):
        client=self.login();state=self.seed(client);other=self.login('acceptance-b')
        self.assertEqual(other.get(self.url()).status_code,403)
        self.assertEqual(self.put(other,'publish',state['revision']).status_code,403)
        wrong=sample(self.accounts['acceptance-b']['tenant_id'],'acceptance-b')
        self.assertEqual(self.put(client,'import',state['revision'],wrong).status_code,422)
        self.assertEqual(self.get(client)['revision'],state['revision'])
    def test_untrusted_origin_and_missing_confirmation_do_not_write(self):
        client=self.login();state=self.seed(client);body={'operation':'publish','expected_revision':state['revision']}
        self.assertEqual(client.put(self.url(),json=body).status_code,400)
        self.assertEqual(client.put(self.url(),json=body,headers={'X-Chatboc-Knowledge':'1','Origin':'https://foreign.example.invalid'}).status_code,403)
        self.assertEqual(self.get(client)['revision'],state['revision'])
    def test_read_scopes_and_strict_json(self):
        client=self.login();self.assertEqual(client.get(self.url()+'?tenant=acceptance-b').status_code,400)
        self.assertEqual(client.get(self.url(),headers={'X-Tenant':'acceptance-b'}).status_code,400)
        response=client.put(self.url(),data='{"operation":"import","operation":"publish"}',content_type='application/json',headers={'X-Chatboc-Knowledge':'1'})
        self.assertEqual(response.status_code,400)
    def test_existing_chat_handler_consumes_published_knowledge(self):
        from database import db
        from models import TenantProfile,User
        from services.logic import responder_chatboc
        client=self.login();state=self.seed(client);published=self.put(client,'publish',state['revision']).get_json()
        with self.app.app_context():
            tenant=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
            owner=db.session.get(User,tenant.municipio_id or tenant.pyme_id)
            answer=responder_chatboc('knowledge:'+published['revision'][:16]+':requirements',owner_user=owner,tipo_chat='municipio',channel='web')
            self.assertEqual(answer['fuente'],'institutional_knowledge');self.assertIn('Respuesta institucional de prueba.',answer['message_body'])
    def test_answer_revoked_during_interpretation_is_not_returned(self):
        from database import db
        from models import TenantConfig
        from services.institutional_assistant_content import digest
        client=self.login();state=self.seed(client);published=self.put(client,'publish',state['revision']).get_json()
        def withdraw(*args):
            row=TenantConfig.query.filter_by(tenant_id=self.accounts['acceptance-a']['tenant_id'],key='institutional_assistant').one()
            value=deepcopy(row.json_value);value['visibility']='private';value['generation']+=1
            value['revision']=digest({key:value[key] for key in ('bundle_hash','visibility','generation')});row.json_value=value;db.session.commit()
            return {'node_ids':['requirements']}
        with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=withdraw):
            response=self.app.test_client().post(self.url(True)+'/answer',json={'revision':published['revision'],'question':'consulta'})
        self.assertEqual(response.status_code,404);self.assertNotIn('Respuesta institucional',response.get_data(as_text=True))
    def test_no_scope_bypass_for_disabled_tenant_or_cutover(self):
        client=self.login();state=self.seed(client)
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED']=True
        self.assertEqual(self.put(client,'publish',state['revision']).status_code,503)
        self.assertEqual(self.get(client)['revision'],state['revision'])
    def test_source_payload_is_not_exposed_by_general_configuration(self):
        client=self.login();self.seed(client)
        response=client.get('/api/admin/tenants/acceptance-a/config')
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertNotIn('Respuesta institucional de prueba.',response.get_data(as_text=True))
    def test_model_failure_does_not_fabricate_answer(self):
        client=self.login();state=self.seed(client)
        with patch('services.llm_utils.llamar_llm_para_json_estructurado',return_value=None):
            response=client.post(self.url()+'/answer',json={'revision':state['revision'],'question':'consulta'})
        self.assertEqual(response.status_code,503);self.assertNotIn('nodes',response.get_json())

    def test_private_answer_rechecks_actor_after_model_selection(self):
        from database import db
        from models import User
        client=self.login();state=self.seed(client)
        with self.app.app_context(): original_role=db.session.get(User,self.accounts['acceptance-a']['id']).rol
        def lose_role(*args):
            user=db.session.get(User,self.accounts['acceptance-a']['id']);user.rol='ciudadano';db.session.commit()
            return {'node_ids':['requirements']}
        try:
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=lose_role):
                response=client.post(self.url()+'/answer',json={'revision':state['revision'],'question':'consulta'})
            self.assertEqual(response.status_code,403,response.get_json());self.assertNotIn('nodes',response.get_json())
        finally:
            with self.app.app_context():
                user=db.session.get(User,self.accounts['acceptance-a']['id']);user.rol=original_role;db.session.commit()

    def test_private_answer_rejects_disable_during_selection_without_version_change(self):
        from copy import deepcopy
        from database import db
        from models import User
        from utils.auth_helpers import auth_session_version
        client=self.login();state=self.seed(client)
        with self.app.app_context():
            user=db.session.get(User,self.accounts['acceptance-a']['id'])
            original=deepcopy(user.accesibilidad)
            version=auth_session_version(user)
        def disable(*args):
            user=db.session.get(User,self.accounts['acceptance-a']['id'])
            metadata=deepcopy(user.accesibilidad or {})
            metadata['auth']={**metadata.get('auth',{}),'disabled':True}
            user.accesibilidad=metadata;db.session.commit()
            self.assertEqual(auth_session_version(user),version)
            return {'node_ids':['requirements']}
        try:
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=disable):
                response=client.post(self.url()+'/answer',json={'revision':state['revision'],'question':'consulta'})
            self.assertEqual(response.status_code,403,response.get_json())
            self.assertNotIn('nodes',response.get_json())
            self.assertNotIn('Respuesta institucional',response.get_data(as_text=True))
        finally:
            with self.app.app_context():
                user=db.session.get(User,self.accounts['acceptance-a']['id'])
                user.accesibilidad=original;db.session.commit()

    def test_exact_auth_session_retirement_during_selection_releases_no_answer(self):
        from database import db
        from models import User
        from services.auth_session_lifecycle import retire_session
        from utils.auth_helpers import auth_session_version
        from uuid import uuid4
        client=self.login();state=self.seed(client)
        descriptor=client.get('/auth/me').get_json()['session_retirement']
        with self.app.app_context():
            before=auth_session_version(db.session.get(User,self.accounts['acceptance-a']['id']))
        def retire_exact_session(*args):
            receipt=retire_session(descriptor['proof'],uuid4().hex)
            self.assertTrue(receipt['local_revoked'])
            self.assertEqual(auth_session_version(db.session.get(User,self.accounts['acceptance-a']['id'])),before)
            return {'node_ids':['requirements']}
        with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=retire_exact_session):
            response=client.post(self.url()+'/answer',json={'revision':state['revision'],'question':'consulta'})
        self.assertEqual(response.status_code,403,response.get_json())
        self.assertEqual(response.get_json(),{'reason_code':'knowledge_forbidden'})
        self.assertNotIn('nodes',response.get_json())
        self.assertNotIn('Respuesta institucional',response.get_data(as_text=True))

    def test_chat_navigation_uses_the_existing_interactive_contract(self):
        from database import db
        from models import TenantProfile,User
        from services.logic import responder_chatboc
        client=self.login();private=self.seed(client);state=self.put(client,'publish',private['revision']).get_json()
        with self.app.app_context():
            tenant=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id']);owner=db.session.get(User,tenant.municipio_id)
            result=responder_chatboc('menu',owner_user=owner,tipo_chat='municipio',channel='web')
            self.assertEqual(result['fuente'],'institutional_knowledge')
            self.assertEqual(result['message_type'],'interactive_buttons')
            self.assertTrue(result['options_list'])
            self.assertEqual(result['botones'],result['options_list'])
            self.assertTrue(all(option['action_id'].startswith('knowledge:'+state['revision'][:16]+':') for option in result['options_list']))

    def test_legacy_owner_resolves_exact_institution_without_tenant_id(self):
        from database import db
        from models import TenantProfile,User
        from services.institutional_assistant import maybe_handle_institutional_question
        client=self.login();private=self.seed(client);self.put(client,'publish',private['revision'])
        with self.app.app_context():
            tenant=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id']);owner=db.session.get(User,tenant.municipio_id)
            old=owner.tenant_id
            try:
                owner.tenant_id=None;db.session.commit()
                self.assertEqual(maybe_handle_institutional_question('menu',owner)['fuente'],'institutional_knowledge')
            finally:owner.tenant_id=old;db.session.commit()

    def test_contradictory_owner_tenant_is_not_replaced_by_a_fallback(self):
        from database import db
        from models import TenantProfile,User
        from services.institutional_assistant import maybe_handle_institutional_question
        client=self.login();private=self.seed(client);self.put(client,'publish',private['revision'])
        with self.app.app_context():
            tenant=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id']);owner=db.session.get(User,tenant.municipio_id);old=owner.tenant_id
            try:
                owner.tenant_id=self.accounts['acceptance-b']['tenant_id'];db.session.commit()
                self.assertIsNone(maybe_handle_institutional_question('menu',owner))
            finally:owner.tenant_id=old;db.session.commit()

    def test_knowledge_confirmation_header_is_allowed_by_preflight(self):
        response=self.app.test_client().options(self.url(),headers={'Origin':'https://panel.example.invalid','Access-Control-Request-Method':'PUT','Access-Control-Request-Headers':'content-type,x-chatboc-knowledge'})
        self.assertIn(response.status_code,[200,204])
        self.assertIn('x-chatboc-knowledge',response.headers.get('Access-Control-Allow-Headers','').lower())
        self.assertEqual(response.headers.get('Access-Control-Allow-Origin'),'https://panel.example.invalid')

    def test_published_questions_do_not_require_credentialed_origin_allowlist(self):
        client=self.login();private=self.seed(client);published=self.put(client,'publish',private['revision']).get_json()
        response=self.app.test_client().post(self.url(True)+'/answer',json={'revision':published['revision'],'node_id':'requirements'},headers={'Origin':'https://institution.example.invalid'})
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertNotEqual(response.headers.get('Access-Control-Allow-Credentials'),'true')

if __name__ == '__main__': unittest.main(verbosity=2)
