from copy import deepcopy
from types import SimpleNamespace
import json
import unittest
from services.organization_setup_journey import build_organization_setup_journey
from services.tenant_implementation_journey import build_implementation_journey

IDS=('institutional_branding','whatsapp','widget','templates','live_chat','knowledge_content',
    'catalog_marketplace','payments_checkout','team_routing','crm','identity_auth',
    'accessibility','territorial_intelligence','public_intake_security','analytics_surveys')
def tenant(kind='municipio', **changes):
    return SimpleNamespace(**({'id':1,'slug':'tenant-a','tipo':kind,'is_active':True,'whatsapp_sender_id':'registered-private-id'}|changes))
def channels():
    return [{'id':key,'label':key,'status':'ready','ready':True,'locked':False,'actions':[]} for key in IDS]
def mark(items,key,status):
    row=next(item for item in items if item['id']==key)
    row.update(status=status,ready=status=='ready',locked=status=='locked',
        actions=[{'id':'open_'+key,'label':'Revisar configuración','href':'/perfil?section=channels','kind':'link','primary':True}])

class SetupJourneyTests(unittest.TestCase):
    def test_known_types_have_server_labels_and_five_steps(self):
        for kind in ('municipio','gobierno','colegio','empresa','pyme'):
            with self.subTest(kind=kind):
                result=build_organization_setup_journey(tenant(kind),channels())
                self.assertEqual(result['organization_type'],kind)
                self.assertEqual(result['tenant'],{'id':1,'slug':'tenant-a'})
                self.assertEqual(len(result['stages']),5)
                self.assertFalse(result['writes_performed']);self.assertFalse(result['provider_calls_performed'])
    def test_commercial_steps_do_not_require_government_territory_or_surveys(self):
        source=[item for item in channels() if item['id'] not in ('territorial_intelligence','analytics_surveys')]
        for kind in ('empresa','pyme','colegio'):
            result=build_organization_setup_journey(tenant(kind),source)
            self.assertFalse(result['government_setup'])
            self.assertEqual(result['summary']['progress'],100)
            self.assertIn('payments_checkout',result['stages'][2]['source_ids'])
    def test_government_does_not_require_commercial_checkout(self):
        source=[item for item in channels() if item['id'] not in ('catalog_marketplace','payments_checkout')]
        result=build_organization_setup_journey(tenant(),source)
        self.assertTrue(result['government_setup']); self.assertEqual(result['summary']['progress'],100)
    def test_commercial_missing_payments_remains_unpublished(self):
        result=build_organization_setup_journey(tenant('pyme'),[item for item in channels() if item['id']!='payments_checkout'])
        self.assertEqual(result['stages'][2]['status'],'not_published')
        self.assertIsNone(result['stages'][2]['primary_action'])
    def test_current_step_and_plan_lock_come_from_source_status(self):
        source=channels();mark(source,'whatsapp','locked')
        result=build_organization_setup_journey(tenant(),source)
        self.assertEqual(result['summary']['current_stage_id'],'channels')
        self.assertEqual(result['summary']['blocked'],1)
        self.assertEqual(result['summary']['progress'],80)
        self.assertEqual(result['summary']['next_action']['id'],'open_whatsapp')
    def test_registered_sender_is_not_copied_or_treated_as_connected(self):
        source=channels();mark(source,'whatsapp','pending')
        result=build_organization_setup_journey(tenant(),source)
        self.assertNotIn('registered-private-id',json.dumps(result))
        self.assertIn('Ya existe',result['continuity_note'])
        self.assertFalse(result['stages'][1]['ready'])
    def test_unknown_type_is_neutral_and_inactive_identity_is_absent(self):
        result=build_organization_setup_journey(tenant('unknown'),channels())
        self.assertEqual(result['organization_type'],'organizacion');self.assertFalse(result['government_setup'])
        for changes in ({'id':True},{'slug':'../bad'},{'is_active':False}):
            self.assertIsNone(build_organization_setup_journey(tenant(**changes),channels()))
    def test_duplicate_source_is_not_resolved_by_last_write_wins(self):
        source=channels();source.append(deepcopy(source[0]))
        result=build_organization_setup_journey(tenant(),source)
        self.assertEqual(result['stages'][0]['status'],'not_published')
    def test_absent_sources_do_not_invent_progress(self):
        result=build_organization_setup_journey(tenant(),[])
        self.assertEqual(result['summary']['progress'],0);self.assertIsNone(result['summary']['next_action'])
    def test_unsafe_or_foreign_links_are_omitted(self):
        for link in ('https://outside.invalid','//outside.invalid','/%2foutside.invalid','/api/private',
                '/perfil?tenant_slug=other','/t/other/chat','/e/other/cart','/perfil?tenant_slug='):
            source=channels();mark(source,'whatsapp','pending');source[1]['actions'][0]['href']=link
            result=build_organization_setup_journey(tenant(),source)
            self.assertIsNone(result['summary']['next_action'],link)
    def test_inputs_and_legacy_contract_remain_unchanged(self):
        source=channels();before=deepcopy(source);legacy=build_implementation_journey(source)
        build_organization_setup_journey(tenant('empresa'),source)
        self.assertEqual(source,before);self.assertEqual(build_implementation_journey(source),legacy)
    def test_unrelated_fields_are_not_exposed(self):
        source=channels();source[0]['secret']='private-not-for-client'
        result=build_organization_setup_journey(tenant(),source)
        self.assertNotIn('private-not-for-client',json.dumps(result));self.assertIn('No certifica',result['readiness_note'])

    def test_contradictory_ready_and_locked_evidence_is_not_success(self):
        source=channels();source[0]['locked']=True
        result=build_organization_setup_journey(tenant(),source)
        self.assertEqual(result['stages'][0]['status'],'not_published')

if __name__=='__main__': unittest.main()
