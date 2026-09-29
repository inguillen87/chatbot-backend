"""Real application routes/models on a disposable database; no customer credentials."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__': prepare_process()
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch
from tests.test_institutional_assistant_content import sample

class KnowledgeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.temp=tempfile.TemporaryDirectory(prefix='chatboc-knowledge-')
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
    def test_empty_workspace_is_truthful(self):
        state=self.get(self.login());self.assertIsNone(state['knowledge']);self.assertIsNone(state['revision'])
        self.assertEqual(self.app.test_client().get(self.url(True)).status_code,404)
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

if __name__ == '__main__': unittest.main(verbosity=2)
