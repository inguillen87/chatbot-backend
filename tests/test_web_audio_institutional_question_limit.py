"""Institutional multipart recovery with real Flask/logic/selector boundaries.

The audio bytes, actor, persistence, selector, provider and TTS are synthetic.
Fixture helpers are reused without inheriting or collecting historical cases.
"""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_web_audio_multipart as multipart_fixture
from tests import test_web_audio_live_chat as live_fixture
from tests.test_institutional_assistant_content import sample
from services import logic, institutional_assistant as knowledge
from services.institutional_assistant_content import ContentError, normalize_bundle
from services.tdf_meta_audio import audio_error_message

REAL_RESPONDER = logic.responder_chatboc


class WebAudioInstitutionalQuestionLimitTests(unittest.TestCase):
    setUp = multipart_fixture.WebAudioMultipartTests.setUp
    enable_chat_pipeline = multipart_fixture.WebAudioMultipartTests.enable_chat_pipeline
    fake_provider = multipart_fixture.WebAudioMultipartTests.fake_provider
    post_audio = live_fixture.WebAudioLiveChatTests.post_audio
    prepare_live = live_fixture.WebAudioLiveChatTests.prepare_live
    token = live_fixture.WebAudioLiveChatTests.token
    form = live_fixture.WebAudioLiveChatTests.form

    def institutional_pipeline(self, transcript):
        responder = self.enable_chat_pipeline()
        self.provider_calls = self.fake_provider(result=transcript)
        self.stack.enter_context(patch('routes.chat.commit_with_retry'))
        self.stack.enter_context(patch('routes.chat.socketio.emit'))
        self.stack.enter_context(patch('routes.chat._persist_core_conversation_messages'))
        bundle = normalize_bundle(sample(self.tenant.id, self.tenant.slug),
                                  self.tenant.id, self.tenant.slug)
        state = {'bundle': bundle, 'revision': 'b' * 64, 'visibility': 'public'}
        self.state_reader = self.stack.enter_context(patch.object(knowledge, 'read_state', return_value=state))
        self.stack.enter_context(patch.object(knowledge, 'db', SimpleNamespace(
            session=SimpleNamespace(get=lambda *args, **kwargs: self.tenant, expire_all=lambda: None))))
        self.selector = self.stack.enter_context(patch(
            'services.llm_utils.llamar_llm_para_json_estructurado',
            return_value={'node_ids': ['requirements']}))
        self.stack.enter_context(patch('services.logic.maybe_handle_catalog_share', return_value=None))
        self.stack.enter_context(patch('services.institutional_assistant_audio.audio_reading_capability',
                                      return_value=None))
        self.tts = self.stack.enter_context(patch(
            'services.google_text_to_speech.generate_audio_url', return_value=None))
        responder.side_effect = REAL_RESPONDER

    def assert_long_audio_recovery(self, length):
        transcript = 'q' * length
        self.institutional_pipeline(transcript)
        response = self.post_audio('/ask/municipio')
        body = response.get_json()
        self.assertEqual(response.status_code, 400, body)
        self.assertEqual(body['reason_code'], 'tdf_audio_question_too_long')
        self.assertEqual(body['fuente'], 'audio_transcription_failed')
        self.assertEqual(body['message_body'], audio_error_message('tdf_audio_question_too_long'))
        self.assertEqual(body['error']['code'], 400)
        self.assertEqual(body['error']['message'], body['message_body'])
        self.assertIn('dividirla', body['message_body'])
        self.assertIn('pregunta más corta', body['message_body'])
        self.assertNotIn(transcript, response.get_data(as_text=True))
        self.selector.assert_not_called()
        self.tts.assert_not_called()
        self.assertEqual(len(self.provider_calls), 1)  # STT has already occurred.
        self.assertNotIn('last_user_message', self.context.context_data)

    def test_1801_has_specific_recoverable_error_without_selector_or_tts(self):
        self.assert_long_audio_recovery(1801)

    def test_2001_has_specific_recoverable_error_without_selector_or_tts(self):
        self.assert_long_audio_recovery(2001)

    def assert_full_canonical_question(self, transcript, expected):
        self.institutional_pipeline(transcript)
        response = self.post_audio('/ask/municipio')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['fuente'], 'institutional_knowledge')
        payload = json.loads(self.selector.call_args.args[1])
        self.assertEqual(payload['question'], expected)
        self.selector.assert_called_once()

    def test_1800_is_passed_in_full_to_selector(self):
        self.assert_full_canonical_question('q' * 1800, 'q' * 1800)

    def test_outer_whitespace_preserves_the_same_1800_character_contract(self):
        self.assert_full_canonical_question(' \n' + 'q' * 1800 + '\t ', 'q' * 1800)

    def generic_responder(self):
        return self.stack.enter_context(patch('services.municipio_responder.responder_municipio',
            return_value={'message_body': 'Respuesta genérica sintética', 'message_type': 'text'}))

    def test_unpublished_corpus_preserves_generic_3000_character_audio(self):
        self.institutional_pipeline('q' * 3000)
        self.state_reader.side_effect = ContentError('knowledge_not_available', 404)
        generic = self.generic_responder()
        response = self.post_audio('/ask/municipio')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(generic.call_args.kwargs['pregunta_original'], 'q' * 3000)
        self.selector.assert_not_called()

    def test_active_operation_preserves_generic_1900_character_audio(self):
        self.institutional_pipeline('q' * 1900)
        self.context.context_data['active_ticket_id'] = 77
        generic = self.generic_responder()
        response = self.post_audio('/ask/municipio')
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(generic.call_args.kwargs['pregunta_original'], 'q' * 1900)
        self.selector.assert_not_called()

    def test_json_text_keeps_prior_contract_even_with_stale_audio_session_flag(self):
        self.institutional_pipeline('unused synthetic transcript')
        self.context.context_data['source_is_audio'] = True
        response = self.client.post('/ask/municipio', json={
            'pregunta': 'q' * 1801, 'tipo_chat': 'municipio'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['fuente'], 'institutional_knowledge_unavailable')
        self.assertNotIn('reason_code', response.get_json())
        self.assertEqual(self.provider_calls, [])
        self.selector.assert_not_called()

    def test_signed_human_room_preserves_full_1900_character_audio(self):
        responder, comments, verify, provider = self.prepare_live()
        self.stack.enter_context(patch('routes.chat.transcribe_audio_bytes', return_value='q' * 1900))
        response = self.post_audio('/ask/municipio', form=self.form())
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['status'], 'message_sent_to_live_chat')
        self.assertEqual(comments.call_args.kwargs['comentario_data']['comentario'], 'q' * 1900)
        self.assertEqual(verify.call_count, 2)
        responder.assert_not_called()

    def test_invalid_human_room_is_still_rejected_before_transcription(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.post_audio('/ask/municipio', form=self.form(token=self.token(ticket_id=78)))
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(provider, [])
        comments.assert_not_called()
        responder.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
