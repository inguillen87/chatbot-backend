"""Offline question-boundary contracts; not recording/provider/delivery proof.

Imports production audio/content modules; external adapters are synthetic.
No Flask app, database or application handler bootstrap is needed here.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import socket
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import audio_transcription_service as stt
from services import institutional_assistant_content as content
from services import tdf_meta_audio as audio

NOW = 1791721200
BYTES = b'OggS\x00' + b'\x00' * 21 + b'\x01\x13' + b'OpusHead' + b'\x00' * 11


class Context:
    def __init__(self, value): self.value = value
    def __enter__(self): return self.value
    def __exit__(self, *args): return False


class TestTdfAudioQuestionLimit(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(patch.object(socket, 'create_connection', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(patch.object(subprocess, 'run', side_effect=AssertionError('Subprocess forbidden')))
        sender = SimpleNamespace(tenant_id=46, app_id='1719510329487224', waba_id='2192778428137676',
            phone_number_id='1137550632773388', environment='sandbox')
        self.binding = SimpleNamespace(sender=sender,
            credential=SimpleNamespace(revision=1, access_token='synthetic-not-an-account-token'))
        self.event = SimpleNamespace(content_type='audio', sender=sender, timestamp=NOW,
            media_id='555555', media_mime_type='audio/ogg; codecs=opus', media_sha256=None)
        self.metadata = {'id': self.event.media_id, 'mime_type': 'audio/ogg', 'file_size': len(BYTES),
            'sha256': hashlib.sha256(BYTES).hexdigest(),
            'url': 'https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=synthetic'}
        self.bundle = {'nodes': {'start': {'id': 'start', 'title': 'Canonical',
            'text': 'Texto canónico.', 'actions': [], 'sources': [], 'links': []}}}
        self.provider_calls, self.selector_calls = [], []

    def private_stt(self, text):
        response = SimpleNamespace(iter_bytes=lambda **kwargs: iter([json.dumps({'text': text}).encode()]))
        def create(**kwargs):
            self.provider_calls.append(kwargs['model'])
            self.assertEqual(kwargs['file'][1].read(), BYTES)
            return Context(response)
        client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(
            with_streaming_response=SimpleNamespace(create=create))))
        self.stack.enter_context(patch.object(stt, '_openai_api_key', return_value='synthetic-api-key'))
        self.stack.enter_context(patch.object(stt, '_runtime_setting', side_effect=lambda name, default=None: default))
        self.stack.enter_context(patch.object(stt, 'llm_provider_network_allowed', return_value=True))
        self.stack.enter_context(patch.object(stt, 'OpenAI', side_effect=lambda **kwargs: Context(client)))
        # Timeout is a value object. The transport itself is wholly synthetic.
        import httpx
        self.stack.enter_context(patch.object(stt, 'httpx', SimpleNamespace(
            Timeout=httpx.Timeout, Client=lambda **kwargs: Context(SimpleNamespace()))))

    def transcribe(self, transcriber=None):
        return audio.transcribe_meta_voice_note(self.event, cfg={'GRAPH_VERSION': 'v25.0'},
            binding_loader=lambda: self.binding, now=lambda: NOW,
            metadata_request=lambda *args, **kwargs: deepcopy(self.metadata),
            metadata_parser=lambda value: value, download=lambda *args, **kwargs: BYTES,
            transcriber=transcriber)

    def select(self, question):
        def selector(instructions, payload):
            self.selector_calls.append(json.loads(payload)['question'])
            return {'node_ids': ['start']}
        return content.select_nodes(self.bundle, question, 'start', selector)

    def denied(self, length):
        question = 'q' * length
        self.private_stt(question)
        try:
            transcript = self.transcribe()
            self.select(transcript)
        except audio.TdfAudioError as error:
            self.assertEqual(str(error), 'tdf_audio_question_too_long')
            self.assertNotIn(question, str(error))
            message = audio.audio_error_message(str(error))
            self.assertIn('dividirla', message)
            self.assertIn('pregunta más corta', message)
            self.assertNotIn(question, message)
        else:
            self.fail('Long question must not reach the institutional selector')
        self.assertEqual(self.provider_calls, ['gpt-4o-transcribe'])
        self.assertEqual(self.selector_calls, [])

    def test_1800_passes_full_transcript_to_selector_without_truncation(self):
        question = 'q' * 1800
        self.assertEqual(content.MAX_QUESTION_CHARS, 1800)
        self.private_stt(question)
        transcript = self.transcribe()
        self.assertEqual(transcript, question)
        self.assertEqual(self.select(transcript), [self.bundle['nodes']['start']])
        self.assertEqual(self.selector_calls, [question])
        self.assertEqual(len(self.provider_calls), 1)

    def test_1801_is_specific_and_does_not_select(self): self.denied(1801)
    def test_2000_is_specific_and_does_not_select(self): self.denied(2000)
    def test_2001_is_specific_and_does_not_select(self): self.denied(2001)

    def test_injected_transcriber_cannot_bypass_the_institutional_limit(self):
        for length in (1801, 2000, 2001):
            with self.subTest(length=length):
                calls = []
                def transcriber(data, mime, **kwargs):
                    calls.append(kwargs)
                    return 'q' * length
                with self.assertRaisesRegex(audio.TdfAudioError, '^tdf_audio_question_too_long$'):
                    self.transcribe(transcriber)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]['max_text_chars'], content.MAX_QUESTION_CHARS)
        self.assertEqual(self.selector_calls, [])

    def test_whitespace_matches_the_selector_question_contract(self):
        question = 'q' * 1800
        self.private_stt(' \n' + question + '\t ')
        self.assertEqual(self.transcribe(), question)
        self.assertEqual(self.select(question), [self.bundle['nodes']['start']])

    def test_private_default_stays_2000_for_other_callers(self):
        question = 'q' * 2000
        self.private_stt(question)
        self.assertEqual(stt.transcribe_private_audio_bytes(BYTES, 'audio/ogg',
            max_bytes=2*1024*1024, timeout_seconds=20), question)

    def test_invalid_private_limits_do_not_start_transcription(self):
        self.private_stt('MENU')
        for value in (0, -1, 2001, True, '1800'):
            with self.subTest(value=value), self.assertRaisesRegex(stt.PrivateAudioTranscriptionError, '^private_audio_invalid$'):
                stt.transcribe_private_audio_bytes(BYTES, 'audio/ogg', max_bytes=2*1024*1024,
                    timeout_seconds=20, max_text_chars=value)
        self.assertEqual(self.provider_calls, [])

    def test_unintelligible_private_audio_keeps_a_fixed_unavailable_error(self):
        self.private_stt('')
        with self.assertRaisesRegex(audio.TdfAudioError, '^tdf_audio_unavailable$'):
            self.transcribe()
        self.assertNotIn('dividirla', audio.audio_error_message('tdf_audio_unavailable'))

    def test_other_provider_failure_never_becomes_a_length_error_or_discloses_text(self):
        def transcriber(*args, **kwargs):
            raise stt.PrivateAudioTranscriptionError('synthetic-private-question-or-provider-details')
        with self.assertRaisesRegex(audio.TdfAudioError, '^tdf_audio_unavailable$') as raised:
            self.transcribe(transcriber)
        self.assertNotIn('synthetic-private', str(raised.exception))
        self.assertEqual(self.selector_calls, [])


if __name__ == '__main__':
    unittest.main()
