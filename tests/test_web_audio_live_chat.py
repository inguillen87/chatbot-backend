"""Multipart audio uses the existing signed human-chat route.

Actual Flask routes, auth decorator, STT byte adapter and room-token helpers;
identity, ticket persistence, provider and bot responses are isolated fixtures.
No live credentials/media/provider/production database are used.
"""
from datetime import datetime, timedelta, timezone
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_web_audio_multipart as multipart_fixture
from routes import chat as chat_routes
from services.live_chat_access import issue_ticket_room_token
from werkzeug.datastructures import MultiDict


class WebAudioLiveChatTests(unittest.TestCase):
    # Reuse only the fixture helpers, so unittest and pytest do not silently
    # collect the inherited multipart tests for a second time.
    setUp = multipart_fixture.WebAudioMultipartTests.setUp
    enable_chat_pipeline = multipart_fixture.WebAudioMultipartTests.enable_chat_pipeline
    fake_provider = multipart_fixture.WebAudioMultipartTests.fake_provider
    def post_audio(self, path='/ask', *, content=b'synthetic-webm', filename='voice.webm',
                   mime='audio/webm', form=None, headers=None):
        # The older fixture casts its form to dict, which intentionally loses
        # duplicate values. Preserve MultiDict so the real multipart parser
        # receives the duplicate field regression rather than a single value.
        data = MultiDict(form or {})
        data.add('audio_file', (io.BytesIO(content), filename, mime))
        return self.client.post(path, data=data, headers=headers or self.headers)

    def prepare_live(self, kind='municipio', *, ticket_state='en_vivo', missing_ticket=False):
        responder = self.enable_chat_pipeline()
        provider = self.fake_provider(result='Consulta sintética')
        self.owner.tipo_chat = kind
        self.tenant.tipo = kind
        self.app.config['SECRET_KEY'] = 'synthetic-local-room-key-32-characters'
        ticket = None if missing_ticket else SimpleNamespace(id=77, estado=ticket_state)
        query = SimpleNamespace(first=lambda: ticket, scalar=lambda: None)
        query.filter_by = lambda **kwargs: query
        query.with_for_update = lambda: query
        query.filter = lambda *args: query
        chat_routes.db.session.query = lambda *args: query
        self.stack.enter_context(patch.object(chat_routes, 'commit_with_retry'))
        comments = self.stack.enter_context(patch('services.ticket_service.servicio_tickets.crear_comentario',
            return_value=SimpleNamespace(id=88)))
        verify = self.stack.enter_context(patch.object(chat_routes, 'verify_ticket_room_token',
            wraps=chat_routes.verify_ticket_room_token))
        return responder, comments, verify, provider

    def token(self, kind='municipio', ticket_id=77, *, expired=False):
        with self.app.app_context():
            if expired:
                with patch('services.live_chat_access.datetime') as clock:
                    clock.now.return_value = datetime.now(timezone.utc) - timedelta(hours=2)
                    return issue_ticket_room_token(kind, ticket_id)
            return issue_ticket_room_token(kind, ticket_id)

    def form(self, kind='municipio', *, token=None):
        return {'ticket_id': '77', 'tipo_ticket': kind,
            'live_chat_access_token': token if token is not None else self.token(kind)}

    def assert_rejected_before_stt(self, response, responder, comments, provider, status):
        self.assertEqual(response.status_code, status, response.get_json())
        self.assertEqual(provider, [])
        responder.assert_not_called()
        comments.assert_not_called()
        self.assertNotIn('source_is_audio', self.context.context_data)

    def assert_active_audio(self, path, kind):
        responder, comments, verify, provider = self.prepare_live(kind)
        response = self.post_audio(path, form=self.form(kind))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json().get('status'), 'message_sent_to_live_chat')
        comments.assert_called_once_with(ticket_id=77, tipo_ticket=kind,
            comentario_data={'comentario': 'Consulta sintética', 'user_id': None,
                'anon_id': self.context.anon_id, 'es_admin': False, 'origen': 'widget'})
        self.assertEqual(verify.call_count, 2)  # Before STT and again before write.
        self.assertEqual(len(provider), 1)
        responder.assert_not_called()
        self.assertTrue(self.context.context_data['source_is_audio'])

    def test_generic_audio_is_sent_to_signed_human_room(self):
        self.assert_active_audio('/ask', 'municipio')

    def test_municipal_audio_is_sent_to_signed_human_room(self):
        self.assert_active_audio('/ask/municipio', 'municipio')

    def test_pyme_audio_is_sent_to_signed_human_room(self):
        self.assert_active_audio('/ask/pyme', 'pyme')

    def test_missing_room_token_is_rejected_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.post_audio('/ask/municipio', form={'ticket_id': '77', 'tipo_ticket': 'municipio'})
        self.assert_rejected_before_stt(response, responder, comments, provider, 403)
        verify.assert_called_once()

    def test_invalid_room_token_is_rejected_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.post_audio('/ask/municipio', form=self.form(token='synthetic-invalid-token'))
        self.assert_rejected_before_stt(response, responder, comments, provider, 403)
        verify.assert_called_once()

    def test_another_room_token_is_rejected_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.post_audio('/ask/municipio', form=self.form(token=self.token(ticket_id=78)))
        self.assert_rejected_before_stt(response, responder, comments, provider, 403)
        verify.assert_called_once()

    def test_other_ticket_type_token_is_rejected_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.post_audio('/ask/municipio', form=self.form(token=self.token('pyme')))
        self.assert_rejected_before_stt(response, responder, comments, provider, 403)
        verify.assert_called_once()

    def test_expired_room_token_is_rejected_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.post_audio('/ask/municipio', form=self.form(token=self.token(expired=True)))
        self.assert_rejected_before_stt(response, responder, comments, provider, 403)
        verify.assert_called_once()

    def test_duplicate_live_fields_are_rejected_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        base = self.form()
        for field in ('ticket_id', 'tipo_ticket', 'live_chat_access_token', 'access_token'):
            with self.subTest(field=field):
                form = MultiDict(base)
                value = self.token() if field == 'access_token' else base[field]
                form.add(field, value)
                if field == 'access_token':
                    form.add(field, value)
                response = self.post_audio('/ask/municipio', form=form)
                self.assert_rejected_before_stt(response, responder, comments, provider, 400)
        self.assertEqual(verify.call_count, 0)

    def test_partial_live_fields_are_rejected_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        for form in ({'ticket_id': '77'}, {'tipo_ticket': 'municipio'},
                {'live_chat_access_token': self.token()}, {'access_token': self.token()}):
            with self.subTest(fields=sorted(form)):
                response = self.post_audio('/ask/municipio', form=form)
                self.assert_rejected_before_stt(response, responder, comments, provider, 400)
        verify.assert_not_called()

    def test_bad_ticket_ids_and_types_are_rejected_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        base = self.form()
        variants = [('ticket_id', value) for value in ('', '0', '-77', '+77', '77.0',
            ' 77 ', '٧٧', '77x', '9' * 20, '9223372036854775808')]
        variants += [('tipo_ticket', value) for value in ('', 'Municipio', 'municipios',
            'municipio ', 'unknown')]
        for field, value in variants:
            with self.subTest(field=field, value=value):
                response = self.post_audio('/ask/municipio', form={**base, field: value})
                self.assert_rejected_before_stt(response, responder, comments, provider, 400)
        verify.assert_not_called()

    def test_conflicting_room_token_aliases_reject_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        form = {**self.form(), 'access_token': self.token(ticket_id=78)}
        response = self.post_audio('/ask/municipio', form=form)
        self.assert_rejected_before_stt(response, responder, comments, provider, 400)
        verify.assert_not_called()

    def test_oversized_room_token_rejects_before_stt(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.post_audio('/ask/municipio', form=self.form(token='x' * 4097))
        self.assert_rejected_before_stt(response, responder, comments, provider, 400)
        verify.assert_not_called()

    def test_equal_room_token_aliases_are_compatible(self):
        responder, comments, verify, provider = self.prepare_live()
        form = self.form()
        form['access_token'] = form['live_chat_access_token']
        response = self.post_audio('/ask/municipio', form=form)
        self.assertEqual(response.get_json().get('status'), 'message_sent_to_live_chat')
        comments.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(len(provider), 1)
        responder.assert_not_called()

    def test_access_token_alias_is_compatible(self):
        responder, comments, verify, provider = self.prepare_live()
        form = {'ticket_id': '77', 'tipo_ticket': 'municipio', 'access_token': self.token()}
        response = self.post_audio('/ask/municipio', form=form)
        self.assertEqual(response.get_json().get('status'), 'message_sent_to_live_chat')
        comments.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(len(provider), 1)
        responder.assert_not_called()

    def test_room_expiring_during_stt_is_checked_again_before_write(self):
        responder, comments, verify, provider = self.prepare_live()
        original_verify = chat_routes.verify_ticket_room_token._mock_wraps
        calls = []
        def check(token, *, expected_room):
            calls.append(expected_room)
            if len(calls) == 2:
                from services.live_chat_access import LiveChatAccessError
                raise LiveChatAccessError('expired_access_token')
            return original_verify(token, expected_room=expected_room)
        verify.side_effect = check
        response = self.post_audio('/ask/municipio', form=self.form())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(provider), 1)
        self.assertEqual(verify.call_count, 2)
        comments.assert_not_called()
        responder.assert_not_called()

    def test_plain_audio_preserves_existing_bot_defaults(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.post_audio('/ask/municipio', form={'contexto_previo': '{"untrusted":"ignored"}',
            'action_id': 'unsupported-action', 'nombre_usuario': 'untrusted-name'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(responder.call_args.args[0], 'Consulta sintética')
        self.assertIsNone(responder.call_args.kwargs['contexto_previo'])
        self.assertEqual(len(provider), 1)
        comments.assert_not_called()
        verify.assert_not_called()

    def test_closed_ticket_audio_preserves_existing_json_fallback(self):
        responder, comments, verify, provider = self.prepare_live(ticket_state='cerrado')
        form = self.form()
        audio = self.post_audio('/ask/municipio', form=form)
        self.assertEqual(audio.status_code, 200, audio.get_json())
        self.assertEqual(responder.call_count, 1)
        json_response = self.client.post('/ask/municipio', json={**form,
            'ticket_id': 77, 'tipo_chat': 'municipio', 'pregunta': 'Consulta sintética'}, headers=self.headers)
        self.assertEqual(json_response.status_code, 200, json_response.get_json())
        self.assertEqual(audio.get_json()['message_body'], json_response.get_json()['message_body'])
        self.assertEqual(responder.call_count, 2)
        self.assertEqual(verify.call_count, 3)
        self.assertEqual(len(provider), 1)
        comments.assert_not_called()

    def test_missing_ticket_audio_preserves_existing_json_fallback(self):
        responder, comments, verify, provider = self.prepare_live(missing_ticket=True)
        form = self.form()
        audio = self.post_audio('/ask/municipio', form=form)
        self.assertEqual(audio.status_code, 200, audio.get_json())
        json_response = self.client.post('/ask/municipio', json={**form,
            'ticket_id': 77, 'tipo_chat': 'municipio', 'pregunta': 'Consulta sintética'}, headers=self.headers)
        self.assertEqual(json_response.status_code, 200, json_response.get_json())
        self.assertEqual(audio.get_json()['message_body'], json_response.get_json()['message_body'])
        self.assertEqual(responder.call_count, 2)
        self.assertEqual(verify.call_count, 3)
        self.assertEqual(len(provider), 1)
        comments.assert_not_called()

    def test_json_valid_room_contract_is_unchanged(self):
        responder, comments, verify, provider = self.prepare_live()
        form = self.form()
        response = self.client.post('/ask/municipio', json={**form,
            'ticket_id': 77, 'tipo_chat': 'municipio', 'pregunta': 'Consulta sintética'}, headers=self.headers)
        self.assertEqual(response.get_json().get('status'), 'message_sent_to_live_chat')
        self.assertEqual(verify.call_count, 1)
        comments.assert_called_once()
        responder.assert_not_called()
        self.assertEqual(provider, [])

    def test_json_invalid_room_contract_is_unchanged(self):
        responder, comments, verify, provider = self.prepare_live()
        response = self.client.post('/ask/municipio', json={'ticket_id': 77, 'tipo_ticket': 'municipio',
            'tipo_chat': 'municipio', 'pregunta': 'Consulta sintética',
            'live_chat_access_token': 'synthetic-invalid-token'}, headers=self.headers)
        self.assert_rejected_before_stt(response, responder, comments, provider, 403)
        verify.assert_called_once()


if __name__ == '__main__':
    unittest.main()
