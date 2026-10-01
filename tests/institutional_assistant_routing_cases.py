"""Integration cases mixed into the disposable full-application HTTP suite."""
from unittest.mock import patch
from types import SimpleNamespace

class ExistingResponderCases:
    def prepare_published_owner(self):
        from database import db
        from models import TenantProfile,User
        client=self.login();state=self.seed(client);state=self.put(client,'publish',state['revision']).get_json()
        tenant=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
        owner=db.session.get(User,tenant.municipio_id or tenant.pyme_id)
        return tenant,owner,state

    def test_text_whatsapp_menu_codes_follow_current_node_without_model(self):
        from services.institutional_assistant import maybe_handle_institutional_question
        from utils.whatsapp import _build_text_fallback_body
        from models import AuditEvent
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            session=SimpleNamespace(tenant_id=tenant.id,context_data={})
            audit_before=AuditEvent.query.count()
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=AssertionError('explicit menu codes must not call the model')):
                menu=maybe_handle_institutional_question('menu',owner,session)
                rendered=_build_text_fallback_body(menu['message_body'],botones=menu['botones'])
                self.assertIn('1. Requisitos',rendered)
                self.assertEqual(menu['botones'][0]['reply_code'],'1')
                response=maybe_handle_institutional_question(' 1 ',owner,session)
                self.assertIn('Respuesta institucional de prueba.',response['message_body'])
                self.assertEqual(session.context_data['institutional_knowledge']['node_id'],'requirements')
                self.assertEqual(response['botones'][0]['reply_code'],'9')
                returned=maybe_handle_institutional_question('9',owner,session)
                self.assertIn('Elegí una consulta.',returned['message_body'])
                self.assertEqual(session.context_data['institutional_knowledge'],{'revision':state['revision'],'node_id':'start','reply_node_id':'start','reply_choices':{'1':'requirements'}})
            self.assertEqual(AuditEvent.query.count(),audit_before)

    def test_text_whatsapp_codes_reject_stale_unknown_and_foreign_context(self):
        from services.institutional_assistant import maybe_handle_institutional_question
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            valid={'institutional_knowledge':{'revision':state['revision'],'node_id':'start','reply_node_id':'start','reply_choices':{'1':'requirements'}}}
            session=SimpleNamespace(tenant_id=tenant.id,context_data=valid)
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=AssertionError('rejected code must not call the model')):
                self.assertEqual(maybe_handle_institutional_question('99',owner,session)['fuente'],'institutional_knowledge_unknown_choice')
                self.assertEqual(session.context_data,valid)
                stale={'institutional_knowledge':{'revision':'0'*64,'node_id':'start'}}
                session.context_data=stale
                self.assertEqual(maybe_handle_institutional_question('1',owner,session)['fuente'],'institutional_knowledge_stale')
                self.assertEqual(session.context_data,stale)
                session.context_data={}
                self.assertEqual(maybe_handle_institutional_question('1',owner,session)['fuente'],'institutional_knowledge_stale')
                session.tenant_id=self.accounts['acceptance-b']['tenant_id']
                session.context_data=valid
                self.assertIsNone(maybe_handle_institutional_question('1',owner,session))

    def test_number_reply_does_not_hijack_an_active_operational_flow(self):
        from services.institutional_assistant import maybe_handle_institutional_question
        from services.constants import CONTEXTO_MUNICIPIO
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            session=SimpleNamespace(tenant_id=tenant.id,context_data={
                'institutional_knowledge':{'revision':state['revision'],'node_id':'start'},
                CONTEXTO_MUNICIPIO:{'stage':'location'}})
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=AssertionError('active operational flow retains control')):
                self.assertIsNone(maybe_handle_institutional_question('1',owner,session))

    def test_multiple_selected_nodes_do_not_advertise_ambiguous_reply_codes(self):
        from services.institutional_assistant import maybe_handle_institutional_question
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            session=SimpleNamespace(tenant_id=tenant.id,context_data={})
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',return_value={'node_ids':['start','requirements']}) as selector:
                response=maybe_handle_institutional_question('consulta con dos temas',owner,session)
            selector.assert_called_once()
            self.assertTrue(response['botones'])
            self.assertTrue(all('reply_code' not in choice for choice in response['botones']))
            previous=session.context_data.copy()
            self.assertIsNone(previous['institutional_knowledge']['reply_node_id'])
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=AssertionError('an ambiguous reply must not call the model')):
                for code in ('1','9'):
                    rejected=maybe_handle_institutional_question(code,owner,session)
                    self.assertEqual(rejected['fuente'],'institutional_knowledge_stale')
                    self.assertEqual(session.context_data,previous)

    def test_legacy_context_without_displayed_numeric_menu_is_not_inferred(self):
        from services.institutional_assistant import maybe_handle_institutional_question
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            previous={'institutional_knowledge':{'revision':state['revision'],'node_id':'start'}}
            session=SimpleNamespace(tenant_id=tenant.id,context_data=previous)
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=AssertionError('a number requires a displayed menu')):
                self.assertEqual(maybe_handle_institutional_question('1',owner,session)['fuente'],'institutional_knowledge_stale')
                self.assertEqual(session.context_data,previous)

    def test_real_whatsapp_formatter_and_inbound_resolver_keep_the_shown_code(self):
        from services.institutional_assistant import maybe_handle_institutional_question
        from services.response_formatter import build_interactive_response
        from routes.whatsapp_webhook import _resolve_whatsapp_menu_selection
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            session=SimpleNamespace(tenant_id=tenant.id,context_data={})
            def format_response(response):
                formatted=build_interactive_response(options=response['botones'],body_text=response['message_body'],
                    channel='whatsapp',message_type=response['message_type'],original_bot_response=response)
                session.context_data={**session.context_data,**formatted['contexto_actualizado']}
                return formatted
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=AssertionError('the displayed numeric reply must not use the model')):
                menu=format_response(maybe_handle_institutional_question('menu',owner,session))
                self.assertIn('*1*. Requisitos',menu['text']['body'])
                self.assertEqual(_resolve_whatsapp_menu_selection('1',session.context_data),(None,None))
                requirements=format_response(maybe_handle_institutional_question('1',owner,session))
                self.assertIn('*9*. Volver',requirements['text']['body'])
                self.assertNotIn('*1*. Volver',requirements['text']['body'])
                self.assertEqual(_resolve_whatsapp_menu_selection('9',session.context_data),(None,None))
                self.assertEqual(maybe_handle_institutional_question('1',owner,session)['fuente'],'institutional_knowledge_unknown_choice')
                self.assertIn('Elegí una consulta.',maybe_handle_institutional_question('9',owner,session)['message_body'])

    def test_hidden_duplicate_and_long_numeric_codes_cannot_select_a_target(self):
        from services.institutional_assistant import maybe_handle_institutional_question
        from tests.test_institutional_assistant_content import sample
        from models import TenantProfile,User
        with self.app.app_context():
            client=self.login()
            data=sample(self.accounts['acceptance-a']['tenant_id'],'acceptance-a')
            data['nodes']['start']['actions'].extend([
                {'code':'2','label':'Requisitos','target':'requirements'},
                {'code':'1234','label':'Código extenso','target':'requirements'}])
            state=self.put(client,'import',None,data).get_json()
            state=self.put(client,'publish',state['revision']).get_json()
            db=self.app.extensions['sqlalchemy'].session
            tenant=db.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
            owner=db.get(User,tenant.municipio_id or tenant.pyme_id)
            session=SimpleNamespace(tenant_id=tenant.id,context_data={})
            with patch('services.llm_utils.llamar_llm_para_json_estructurado',side_effect=AssertionError('hidden numeric codes must not use the model')):
                menu=maybe_handle_institutional_question('menu',owner,session)
                self.assertEqual(session.context_data['institutional_knowledge']['reply_choices'],{'1':'requirements'})
                self.assertEqual([b.get('reply_code') for b in menu['botones']],['1',None])
                for code in ('2','1234'):
                    self.assertEqual(maybe_handle_institutional_question(code,owner,session)['fuente'],'institutional_knowledge_unknown_choice')

    def test_channel_cites_text_snapshot_and_image_without_native_page_claim(self):
        from services.institutional_assistant import maybe_handle_institutional_question
        from tests.test_institutional_assistant_content import sample
        from models import TenantProfile,User
        with self.app.app_context():
            client=self.login()
            bundle=sample(self.accounts['acceptance-a']['tenant_id'],'acceptance-a')
            bundle['sources']['a'].update(format='text',page_count=1,review_status='conflict',current_validity='not_verified',
                                         origin_url='https://drive.google.com/file/d/private-origin/view')
            for refs in bundle['node_evidence'].values():
                for entry in refs:
                    entry.pop('pages',None);entry['page']=1
            state=self.put(client,'import',None,bundle).get_json()
            state=self.put(client,'publish',state['revision']).get_json()
            tenant=self.app.extensions['sqlalchemy'].session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
            owner=self.app.extensions['sqlalchemy'].session.get(User,tenant.municipio_id or tenant.pyme_id)
            response=maybe_handle_institutional_question('knowledge:'+state['revision'][:16]+':requirements',owner)
            self.assertIn('Documento de prueba · texto extraído · fuentes por conciliar',response['message_body'])
            self.assertNotIn('private-origin',str(response))
            self.assertEqual(response['knowledge_sources'][0]['pagination'],'logical_snapshot')
            bundle['sources']['a'].update(format='jpeg',mime_type='image/jpeg',printed_year=2025,review_status='needs_review')
            state=self.put(client,'import',state['revision'],bundle).get_json()
            state=self.put(client,'publish',state['revision']).get_json()
            response=maybe_handle_institutional_question('knowledge:'+state['revision'][:16]+':requirements',owner)
            self.assertIn('Documento de prueba · imagen · edición 2025 · revisión pendiente',response['message_body'])
            self.assertNotIn('private-origin',str(response))

    def test_catalogue_handler_keeps_priority_over_knowledge_selection(self):
        from services.logic import responder_chatboc
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            expected={'message_body':'Catalogue handler response','fuente':'catalogue'}
            with patch('services.logic.maybe_handle_catalog_share',return_value=expected) as catalogue,patch('services.llm_utils.llamar_llm_para_json_estructurado') as selector:
                result=responder_chatboc('compartir catalogo',owner_user=owner,tipo_chat='municipio')
            self.assertEqual(result,expected);catalogue.assert_called_once();selector.assert_not_called()

    def test_unmatched_question_reaches_the_existing_municipal_handler(self):
        from services.logic import responder_chatboc
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            expected={'message_body':'Operational handler response','fuente':'operational'}
            with patch('services.logic.maybe_handle_catalog_share',return_value=None),patch('services.llm_utils.llamar_llm_para_json_estructurado',return_value={'node_ids':[]}),patch('services.municipio_responder.responder_municipio',return_value=expected) as operational:
                result=responder_chatboc('iniciar un reclamo',owner_user=owner,tipo_chat='municipio')
            operational.assert_called_once();self.assertEqual(result['fuente'],'operational')

    def test_operational_context_and_explicit_action_do_not_call_knowledge_model(self):
        from services.constants import CONTEXTO_MUNICIPIO
        from services.institutional_assistant import maybe_handle_institutional_question
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            context=SimpleNamespace(tenant_id=tenant.id,context_data={CONTEXTO_MUNICIPIO:{'stage':'location'}})
            with patch('services.llm_utils.llamar_llm_para_json_estructurado') as selector:
                self.assertIsNone(maybe_handle_institutional_question('texto del reclamo',owner,context))
                self.assertIsNone(maybe_handle_institutional_question({'action':'iniciar_reclamo'},owner))
                selector.assert_not_called()

    def test_knowledge_answer_preserves_the_existing_audio_response_path(self):
        from services.logic import responder_chatboc
        with self.app.app_context():
            tenant,owner,state=self.prepare_published_owner()
            with patch('services.logic.maybe_handle_catalog_share',return_value=None),patch('services.google_text_to_speech.generate_audio_url',return_value='https://example.test/audio') as voice:
                result=responder_chatboc('knowledge:'+state['revision'][:16]+':requirements',owner_user=owner,tipo_chat='municipio',channel='voice')
            voice.assert_called_once();self.assertEqual(result['audio_url'],'https://example.test/audio')
            self.assertEqual(result['fuente'],'institutional_knowledge')
