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
