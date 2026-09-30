from copy import deepcopy
import unittest
from services.institutional_assistant_content import normalize_bundle, select_nodes, overview, materialize_node, ContentError

def sample(tenant_id=701, slug='qa-knowledge'):
    return {'contract_version':'chatboc.institutional_guide.composed.v1','version':'1.0','tenant':{'id':tenant_id,'slug':slug},'start':'start',
        'sources':{'a':{'id':'a','title':'Documento de prueba','sha256':'a'*64,'page_count':3,'file_path':'PRIVATE/path.pdf'}},
        'nodes':{'start':{'id':'start','title':'Inicio','text':'Elegí una consulta.','actions':[{'code':'1','label':'Requisitos','target':'requirements'}]},
                 'requirements':{'id':'requirements','title':'Requisitos','text':'Respuesta institucional de prueba.','actions':[{'code':'9','label':'Volver','target':'start'}]}},
        'node_evidence':{'start':[{'source_id':'a','pages':[1]}],'requirements':[{'source_id':'a','page':2,'quote':'Respuesta institucional de prueba.'}]},
        'policy':{key:False for key in ['accepts_personal_data','creates_real_cases','queries_official_records','sends_notifications','stores_feedback']}}

class InstitutionalContentTests(unittest.TestCase):
    def test_canonical_text_and_pages_without_private_path(self):
        result=normalize_bundle(sample(),701,'qa-knowledge')
        self.assertEqual(result['nodes']['requirements']['text'],'Respuesta institucional de prueba.')
        self.assertEqual(result['nodes']['requirements']['sources'][0]['pages'],[2])
        self.assertNotIn('PRIVATE',str(result))
    def test_same_answer_for_selection_and_navigation(self):
        result=normalize_bundle(sample(),701,'qa-knowledge')
        selected=select_nodes(result,'¿Qué hace falta?','start',lambda *args:{'node_ids':['requirements']})
        self.assertEqual(selected,[result['nodes']['requirements']])
    def test_no_model_for_menu_and_no_fake_answer_for_unknown(self):
        result=normalize_bundle(sample(),701,'qa-knowledge')
        self.assertEqual(overview(result)['node_count'],2)
        self.assertEqual(select_nodes(result,'no cubierto','start',lambda *args:{'node_ids':[]}),[])
    def test_rejects_wrong_tenant(self):
        with self.assertRaises(ContentError): normalize_bundle(sample(),702,'other')
    def test_rejects_missing_evidence(self):
        data=sample();data['node_evidence'].pop('requirements')
        with self.assertRaises(ContentError):normalize_bundle(data,701,'qa-knowledge')
    def test_rejects_bad_pages_and_duplicate_actions(self):
        for mutation in ['page','choice','graph','policy']:
            data=sample()
            if mutation=='page':data['node_evidence']['start'][0]['pages']=[4]
            if mutation=='choice':data['nodes']['start']['actions']*=2
            if mutation=='graph':data['nodes']['start']['actions'][0]['target']='missing'
            if mutation=='policy':data['policy']['creates_real_cases']=True
            with self.subTest(mutation=mutation),self.assertRaises(ContentError):normalize_bundle(data,701,'qa-knowledge')
    def test_model_cannot_provide_text_links_or_unknown_ids(self):
        bundle=normalize_bundle(sample(),701,'qa-knowledge')
        for response in [{'node_ids':['missing']},{'node_ids':['start'],'text':'invented'},{'node_ids':['start','start']},None]:
            with self.subTest(response=response),self.assertRaises(ContentError):select_nodes(bundle,'consulta','start',lambda *args:response)
    def test_links_require_exact_reviewed_https_and_expire(self):
        data=sample();data['reference_links']={'tenant':data['tenant'],'entries':[{'id':'ref','label':'Referencia','status':'verified_reference','url':'https://example.org/info','review_after':'2000-01-01T00:00:00Z','node_ids':['requirements']}]}
        result=normalize_bundle(data,701,'qa-knowledge');self.assertEqual(materialize_node(result['nodes']['requirements'])['links'],[])
        data['reference_links']['entries'][0]['url']='javascript:alert(1)'
        with self.assertRaises(ContentError):normalize_bundle(data,701,'qa-knowledge')
    def test_source_pages_are_combined_without_losing_quotes(self):
        data=sample();data['node_evidence']['requirements'].append({'source_id':'a','page':3,'quote':'Otro fragmento.'})
        refs=normalize_bundle(data,701,'qa-knowledge')['nodes']['requirements']['sources']
        self.assertEqual(len(refs),1);self.assertEqual(refs[0]['pages'],[2,3]);self.assertEqual(len(refs[0]['excerpts']),2)
    def test_exact_source_ids_are_required(self):
        data=sample();data['sources']['a']['id']='b'
        with self.assertRaises(ContentError):normalize_bundle(data,701,'qa-knowledge')

    def test_evidence_quote_page_must_belong_to_validated_source_pages(self):
        for quoted_page in [999, 3, True, None, '2']:
            data=sample();data['node_evidence']['requirements']=[{'source_id':'a','pages':[2],'page':quoted_page,'quote':'Fragmento.'}]
            with self.subTest(page=quoted_page),self.assertRaises(ContentError):
                normalize_bundle(data,701,'qa-knowledge')
    def test_quote_without_page_is_only_derived_from_one_unambiguous_page(self):
        data=sample();data['node_evidence']['requirements']=[{'source_id':'a','pages':[2],'quote':'Fragmento.'}]
        normalized=normalize_bundle(data,701,'qa-knowledge')
        self.assertEqual(normalized['nodes']['requirements']['sources'][0]['excerpts'][0]['page'],2)
        data['node_evidence']['requirements'][0]['pages']=[1,2]
        with self.assertRaises(ContentError):normalize_bundle(data,701,'qa-knowledge')

class SelectorCapacityTests(unittest.TestCase):
    def test_import_rejects_a_corpus_that_cannot_be_queried(self):
        data=sample()
        for index in range(12):
            key='large-'+str(index)
            data['nodes'][key]={'id':key,'title':key,'text':'x'*11000,'actions':[]}
            data['node_evidence'][key]=[{'source_id':'a','page':1}]
            data['nodes']['start']['actions'].append({'code':key,'label':key,'target':key})
        with self.assertRaises(ContentError) as failure:
            normalize_bundle(data,701,'qa-knowledge')
        self.assertEqual(failure.exception.code,'knowledge_context_too_large')

    def test_import_reserves_space_for_the_largest_allowed_question(self):
        data=sample()
        normalized=normalize_bundle(data,701,'qa-knowledge')
        result=select_nodes(normalized,'\U0010ffff'*1800,'requirements',lambda *args:{'node_ids':['requirements']})
        self.assertEqual(result[0]['id'],'requirements')
