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
    def test_document_visibility_is_explicit_and_does_not_publish_private_links(self):
        data = sample()
        data['sources']['a'].update(document_visibility='private',
            official_url='https://example.org/source.pdf',
            origin_url='https://drive.google.com/file/d/private-source/view')
        bundle = normalize_bundle(data, 701, 'qa-knowledge')
        before = deepcopy(bundle)
        for source in (overview(bundle, public=True)['sources'][0],
                       materialize_node(bundle['nodes']['start'], public=True)['sources'][0]):
            self.assertEqual(source['document_visibility'], 'private')
            self.assertEqual(source['delivery']['document_visibility'], 'private')
            self.assertFalse(source['delivery']['publicly_accessible'])
            self.assertNotIn('url', source)
            self.assertNotIn('origin_url', source)
        private_source = overview(bundle)['sources'][0]
        self.assertEqual(private_source['url'], 'https://example.org/source.pdf')
        self.assertIn('origin_url', private_source)
        self.assertEqual(bundle, before)
        data['sources']['a']['document_visibility'] = 'public'
        public_source = overview(normalize_bundle(data, 701, 'qa-knowledge'), public=True)['sources'][0]
        self.assertTrue(public_source['delivery']['publicly_accessible'])
        self.assertEqual(public_source['url'], 'https://example.org/source.pdf')

    def test_document_visibility_rejects_unknown_values_and_legacy_is_unchanged(self):
        for invalid in (None, True, False, 'PUBLIC', '', 'internal', {}, []):
            data = sample(); data['sources']['a']['document_visibility'] = invalid
            with self.subTest(value=invalid), self.assertRaises(ContentError):
                normalize_bundle(data, 701, 'qa-knowledge')
        legacy = normalize_bundle(sample(), 701, 'qa-knowledge')['sources']['a']
        self.assertNotIn('document_visibility', legacy)
        self.assertNotIn('document_visibility', legacy['delivery'])
        self.assertNotIn('publicly_accessible', legacy['delivery'])

    def test_editorial_metadata_survives_without_import_approval_or_drive_link(self):
        data = sample()
        metadata = {'source_authority': 'project', 'format': 'pdf', 'mime_type': 'application/pdf',
                    'pagination': 'native', 'byte_size': 205, 'printed_year': 2025,
                    'origin_url': 'https://drive.google.com/file/d/source-fixture/view',
                    'native_revision': 'provider-revision-fixture', 'modified_at': '2025-03-28T10:00:00Z',
                    'review_status': 'needs_review', 'current_validity': 'not_verified',
                    'provenance': 'Documento institucional aportado; revisión pendiente',
                    'evaluation_only': True, 'approval_status': 'pending_institutional_approval'}
        data['sources']['a'].update(metadata)
        data.update(evaluation_only=True, approval_status='pending_institutional_approval')
        normalized = normalize_bundle(data, 701, 'qa-knowledge')
        self.assertTrue(normalized['evaluation_only'])
        self.assertEqual(normalized['approval_status'], 'pending_institutional_approval')
        source = normalized['sources']['a']
        for name, expected in metadata.items():
            self.assertEqual(source[name], expected)
        self.assertIsNone(source['url'])
        self.assertNotIn('file_path', source)
        self.assertNotIn('available', source['delivery'])
        self.assertEqual(source['delivery']['format'], 'pdf')
        for delivered in (overview(normalized)['sources'][0], materialize_node(normalized['nodes']['start'])['sources'][0]):
            for name, expected in metadata.items():
                self.assertEqual(delivered[name], expected)
            self.assertEqual(delivered['delivery'], source['delivery'])

    def test_text_and_image_snapshots_have_one_explicit_logical_page(self):
        for format_name, mime, extension in [('jpeg', 'image/jpeg', 'jpg'), ('text', 'text/plain', 'txt')]:
            data = sample()
            data['sources']['a'].update(format=format_name, mime_type=mime, page_count=1)
            for refs in data['node_evidence'].values():
                for ref in refs:
                    ref['pages'] = [1]; ref['page'] = 1
            result = normalize_bundle(data, 701, 'qa-knowledge')['sources']['a']
            self.assertEqual(result['pagination'], 'logical_snapshot')
            self.assertEqual(result['delivery']['mime_type'], mime)
            self.assertTrue(result['delivery']['filename'].endswith('.' + extension))
            for mutation in ({'page_count': 2}, {'pagination': 'native'}, {'mime_type': 'text/html'}):
                with self.subTest(format=format_name, mutation=mutation), self.assertRaises(ContentError):
                    invalid_data = deepcopy(data)
                    invalid_data['sources']['a'].update(mutation)
                    normalize_bundle(invalid_data, 701, 'qa-knowledge')

    def test_public_source_projection_hides_private_origin_without_mutating_evidence(self):
        from services.institutional_assistant_content import digest
        data = sample()
        metadata = {'origin_url': 'https://drive.google.com/file/d/private-source-fixture/view',
                    'official_url': 'https://example.org/official.pdf',
                    'source_authority': 'operational_document', 'provenance': 'Fuente aportada',
                    'review_status': 'needs_review', 'evaluation_only': True}
        data['sources']['a'].update(metadata)
        bundle = normalize_bundle(data, 701, 'qa-knowledge')
        before = digest(bundle)
        for private, public in (
            (overview(bundle)['sources'][0], overview(bundle, public=True)['sources'][0]),
            (materialize_node(bundle['nodes']['start'])['sources'][0],
             materialize_node(bundle['nodes']['start'], public=True)['sources'][0]),
        ):
            self.assertEqual(private['origin_url'], metadata['origin_url'])
            self.assertNotIn('origin_url', public)
            self.assertEqual(public['url'], metadata['official_url'])
            self.assertEqual({key: value for key, value in private.items() if key != 'origin_url'}, public)
        self.assertEqual(digest(bundle), before)

    def test_metadata_invalid_types_dates_enums_and_bounds_are_rejected(self):
        for field, invalid in [('source_authority', 'approved'), ('review_status', 'approved'),
                               ('current_validity', 'vigente'), ('printed_year', True),
                               ('printed_year', 1800), ('byte_size', True), ('byte_size', None),
                               ('byte_size', 8 * 1024 * 1024 + 1), ('native_revision', {}),
                               ('provenance', 'x' * 501), ('modified_at', '2025-01-01'),
                               ('modified_at', '2025-01-01 10:00:00+00:00'),
                               ('modified_at', 'not-a-date'), ('format', 'html'), ('format', {})]:
            data = sample(); data['sources']['a'][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ContentError):
                normalize_bundle(data, 701, 'qa-knowledge')

    def test_drive_origin_is_never_an_official_public_source_link(self):
        for url in ('https://drive.google.com/file/d/source-fixture/view',
                    'https://docs.google.com/document/d/source-fixture/edit',
                    'https://download.googleusercontent.com/private-fixture.pdf'):
            data = sample(); data['sources']['a']['official_url'] = url
            with self.subTest(url=url), self.assertRaises(ContentError):
                normalize_bundle(data, 701, 'qa-knowledge')

    def test_unknown_optional_metadata_is_explicit_null_not_approval(self):
        data = sample()
        data['sources']['a'].update(native_revision=None, modified_at=None, printed_year=None, origin_url=None)
        source = normalize_bundle(data, 701, 'qa-knowledge')['sources']['a']
        for field in ('native_revision', 'modified_at', 'printed_year', 'origin_url'):
            self.assertIsNone(source[field])
        self.assertNotIn('review_status', source)
        self.assertNotIn('current_validity', source)

    def test_stored_legacy_sources_get_declarative_delivery_without_mutation(self):
        from services.institutional_assistant_content import digest
        data = normalize_bundle(sample(), 701, 'qa-knowledge')
        for source in [data['sources']['a']] + [n['sources'][0] for n in data['nodes'].values()]:
            source.pop('delivery', None)
        before = digest(data)
        self.assertEqual(overview(data)['sources'][0]['delivery']['format'], 'pdf')
        self.assertEqual(materialize_node(data['nodes']['start'])['sources'][0]['delivery']['format'], 'pdf')
        self.assertEqual(digest(data), before)

    def test_canonical_text_and_pages_without_private_path(self):
        result=normalize_bundle(sample(),701,'qa-knowledge')
        self.assertEqual(result['nodes']['requirements']['text'],'Respuesta institucional de prueba.')
        self.assertEqual(result['nodes']['requirements']['sources'][0]['pages'],[2])
        self.assertNotIn('PRIVATE',str(result))
    def test_same_answer_for_selection_and_navigation(self):
        result=normalize_bundle(sample(),701,'qa-knowledge')
        selected=select_nodes(result,'¿Qué hace falta?','start',lambda *args:{'node_ids':['requirements']})
        self.assertEqual(selected,[result['nodes']['requirements']])
    def test_selector_receives_persisted_action_labels_without_interpreting_them_in_python(self):
        import json
        data = sample(); data['nodes']['start']['actions'][0]['label'] = '♿ Requisitos y orientación'
        bundle = normalize_bundle(data, 701, 'qa-knowledge')
        def selector(instructions, request):
            payload = json.loads(request)
            self.assertEqual(payload['question'], '♿')
            start = next(node for node in payload['knowledge'] if node['id'] == 'start')
            self.assertEqual(start['actions'], bundle['nodes']['start']['actions'])
            self.assertNotIn('sources', start)
            self.assertNotIn('sha256', request)
            return {'node_ids': ['requirements']}
        selected = select_nodes(bundle, '♿', 'start', selector)
        self.assertEqual(selected, [bundle['nodes']['requirements']])
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
