"""Hermetic Flask multipart regressions; synthetic bytes, no live provider/DB.

The blueprint, auth decorator, parser and STT byte adapter are real imports.
Persistence/identity lookups use fixtures; provider transport is blocked.
"""

import io
import os
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from flask import Flask, g
from models import ChatSessionContext, TenantProfile, User

from routes import chat as chat_routes
from services import audio_transcription_service as audio_service
from utils import auth_helpers as auth


class WebAudioMultipartTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "TESTING": "1", "OPENAI_ALLOW_NETWORK_IN_TESTS": "0",
            "COHERE_ALLOW_NETWORK_IN_TESTS": "0", "STT_PROVIDER_ORDER": "openai",
            "STT_CACHE_ENABLED": "false",
        }))
        # Guard even accidental SDK construction, DNS or socket connections.
        self.forbidden = []
        for target in ("socket.getaddrinfo", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.socket.sendto",
                       "services.audio_transcription_service.OpenAI",
                       "services.audio_transcription_service.requests.get"):
            self.forbidden.append(self.stack.enter_context(patch(target,
                side_effect=AssertionError("External I/O forbidden in multipart tests"))))
        self.addCleanup(lambda: self.assertTrue(all(not call.called for call in self.forbidden)))
        audio_service.clear_transcription_cache()
        self.addCleanup(audio_service.clear_transcription_cache)

        self.app = Flask("web-audio-handler-tests")
        self.app.config.update(TESTING=True, SECRET_KEY="synthetic-session-only",
            PROPAGATE_EXCEPTIONS=False, STT_MAX_AUDIO_BYTES=32)
        self.app.register_blueprint(chat_routes.chat_bp)
        self.client = self.app.test_client()
        self.owner = User(id=23, token="fixture-owner", tenant_id=11,
            tenant_slug="fixture-tenant", tipo_chat="municipio", rubro_id=None,
            rol="admin", preguntas_usadas=0)
        self.tenant = TenantProfile(id=11, slug="fixture-tenant", tipo="municipio",
            municipio_id=23, is_active=True, configuracion={}, plan="full")
        self.context = ChatSessionContext(chat_session_id="fixture-session",
            tenant_id=11, user_id=None, anon_id="fixture-anon", context_data={})
        query = SimpleNamespace(first=lambda: self.context, one_or_none=lambda: self.context)
        query.with_for_update = lambda: query
        query.filter_by = lambda **keys: query
        self.stack.enter_context(patch.object(chat_routes, "ChatSessionContext", SimpleNamespace(query=query)))
        self.stack.enter_context(patch.object(chat_routes, "ensure_chat_session_context_schema"))
        self.stack.enter_context(patch.object(auth, "_lookup_owner_for_static_token",
            side_effect=lambda token: self.owner if token == self.owner.token else None))
        self.stack.enter_context(patch.object(auth, "obtener_token", return_value=None))
        self.headers = {"X-Entity-Token": self.owner.token,
            "X-Chat-Session-Id": self.context.chat_session_id, "X-Anon-Id": self.context.anon_id}

        @self.app.before_request
        def synthetic_scope():
            g.tenant_profile = self.tenant

    def enable_chat_pipeline(self):
        """Replace identity/persistence I/O, preserving shared handler logic."""
        query = SimpleNamespace(scalar=lambda: None)
        query.filter = lambda *args: query
        session = SimpleNamespace(query=lambda *args: query, commit=lambda: None,
            rollback=lambda: None, get=lambda model, ident: self.owner if model is User else self.tenant)
        self.stack.enter_context(patch.object(chat_routes, "db", SimpleNamespace(session=session)))
        self.stack.enter_context(patch.object(chat_routes, "_resolve_regular_chat_tenant",
            return_value=self.tenant))
        self.stack.enter_context(patch.object(chat_routes, "_anonymous_message_count", return_value=0))
        self.stack.enter_context(patch.object(chat_routes, "_owner_for_tenant_profile", return_value=self.owner))
        self.responder = self.stack.enter_context(patch("services.logic.responder_chatboc",
            return_value=({"message_body": "Respuesta sintética del pipeline", "message_type": "text"}, 200)))
        return self.responder

    def fake_provider(self, result="Consulta sintética", error=None):
        """Exercise the real byte adapter with an explicit in-memory transport."""
        calls = []
        def create(**kwargs):
            calls.append({**kwargs, "bytes": kwargs["file"].getvalue(),
                          "filename": kwargs["file"].name})
            if error is not None:
                raise error
            return SimpleNamespace(text=result)
        client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
        self.stack.enter_context(patch.object(audio_service, "_get_openai_client", return_value=client))
        return calls

    def post_audio(self, path="/ask", *, content=b"synthetic-webm", filename="voice.webm",
                   mime="audio/webm", form=None, headers=None):
        data = dict(form or {})
        data["audio_file"] = (io.BytesIO(content), filename, mime)
        return self.client.post(path, data=data, headers=headers or self.headers)

    def test_real_adapter_unavailable_returns_safe_recoverable_error_without_named_temp(self):
        # Do not mock/invent SpeechToTextService.transcribe_audio_file. This is
        # also the baseline reproduction against the actual imported adapter.
        with tempfile.TemporaryDirectory(prefix="chatboc-web-audio-baseline-") as directory:
            with patch("tempfile.gettempdir", return_value=directory):
                response = self.post_audio()
            explicit_temp_files = sorted(path.name for path in Path(directory).iterdir())
            print("REAL_MULTIPART_RESULT", response.status_code,
                "NAMED_TEMP_COUNT", len(explicit_temp_files), flush=True)
            self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["fuente"], "audio_transcription_failed")
            self.assertEqual(explicit_temp_files, [])
            self.assertNotIn("source_is_audio", self.context.context_data)

    def test_success_reaches_shared_pipeline_for_all_routes_and_preserves_scope(self):
        responder = self.enable_chat_pipeline()
        calls = self.fake_provider(result="  Consulta sintética  ")
        with patch.object(chat_routes, "transcribe_audio_bytes", wraps=audio_service.transcribe_audio_bytes) as stt, \
                patch("werkzeug.datastructures.FileStorage.save", side_effect=AssertionError("No explicit temp file")) as save:
            for path, expected_type in (("/ask", "municipio"), ("/ask/pyme", "pyme"), ("/ask/municipio", "municipio")):
                with self.subTest(path=path):
                    self.owner.tipo_chat = expected_type
                    self.tenant.tipo = expected_type
                    response = self.post_audio(path, mime="audio/webm;codecs=opus",
                        filename="../../untrusted-client-name.webm")
                    self.assertEqual(response.status_code, 200, response.get_json())
                    stt.assert_called_with(b"synthetic-webm", "audio/webm")
                    args, kwargs = responder.call_args
                    self.assertEqual(args[0], "Consulta sintética")
                    self.assertIs(kwargs["chat_db_context"], self.context)
                    self.assertIs(kwargs["owner_user"], self.owner)
                    self.assertIsNone(kwargs["current_user"])
                    self.assertEqual(kwargs["chat_session_uuid"], self.context.chat_session_id)
                    self.assertEqual(kwargs["anon_id"], self.context.anon_id)
                    self.assertEqual(kwargs["tipo_chat"], expected_type)
                    self.assertEqual(response.headers["X-Anon-Id"], self.context.anon_id)
                    self.assertEqual(self.context.tenant_id, self.tenant.id)
                    self.assertTrue(self.context.context_data["source_is_audio"])
            save.assert_not_called()
        self.assertEqual(len(calls), 3)
        self.assertEqual({call["filename"] for call in calls}, {"audio.webm"})

    def test_invalid_uploads_reject_before_stt_and_chat(self):
        responder = self.enable_chat_pipeline()
        with patch.object(chat_routes, "transcribe_audio_bytes", wraps=audio_service.transcribe_audio_bytes) as stt:
            for content, filename, mime, status, reason in (
                (b"synthetic", "", "audio/webm", 400, "audio_file_empty"),
                (b"", "voice.webm", "audio/webm", 400, "audio_file_empty"),
                (b"synthetic", "voice.txt", "text/plain", 415, "audio_type_unsupported"),
                (b"x" * 33, "voice.webm", "audio/webm", 413, "audio_file_too_large"),
            ):
                with self.subTest(reason=reason, filename=filename):
                    response = self.post_audio(content=content, filename=filename, mime=mime)
                    self.assertEqual(response.status_code, status, response.get_json())
                    self.assertEqual(response.get_json()["reason_code"], reason)
                    self.assertTrue(response.get_json()["error"]["message"])
                    self.assertNotIn("source_is_audio", self.context.context_data)
            stt.assert_not_called()
            responder.assert_not_called()

    def test_provider_error_is_redacted_and_never_becomes_question(self):
        responder = self.enable_chat_pipeline()
        canary = "private-key+private-recording+private-transcript"
        calls = self.fake_provider(error=RuntimeError(canary))
        with self.assertLogs(audio_service.logger, level="WARNING") as logs:
            response = self.post_audio()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason_code"], "audio_transcription_failed")
        self.assertNotIn(canary, response.get_data(as_text=True))
        self.assertNotIn(canary, "\n".join(logs.output))
        self.assertEqual(len(calls), 1)
        responder.assert_not_called()
        self.assertNotIn("source_is_audio", self.context.context_data)

    def test_empty_or_unexpected_adapter_results_are_recoverable(self):
        responder = self.enable_chat_pipeline()
        for transcript in (None, "", "   ", 123):
            with self.subTest(result_type=type(transcript).__name__), patch.object(
                    chat_routes, "transcribe_audio_bytes", return_value=transcript):
                response = self.post_audio()
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["reason_code"], "audio_transcription_failed")
        responder.assert_not_called()

    def test_unexpected_adapter_exception_is_safe_and_creates_no_file(self):
        responder = self.enable_chat_pipeline()
        canary = "SDK exception containing recording and credential"
        with patch.object(chat_routes, "transcribe_audio_bytes", side_effect=RuntimeError(canary)), \
                patch("werkzeug.datastructures.FileStorage.save") as save, \
                self.assertLogs(self.app.logger, level="WARNING") as logs:
            response = self.post_audio(filename="credential-in-filename.webm")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(canary, response.get_data(as_text=True))
        self.assertNotIn(canary, "\n".join(logs.output))
        self.assertNotIn("credential-in-filename", "\n".join(logs.output))
        save.assert_not_called()
        responder.assert_not_called()

    def test_json_only_remains_on_existing_parser_and_never_transcribes(self):
        responder = self.enable_chat_pipeline()
        with patch.object(chat_routes, "transcribe_audio_bytes", wraps=audio_service.transcribe_audio_bytes) as stt:
            response = self.client.post("/ask", json={"pregunta": "Consulta escrita",
                "tipo_chat": "municipio", "contexto_previo": {"fixture": "retained"}}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(responder.call_args.args[0], "Consulta escrita")
        self.assertEqual(responder.call_args.kwargs["contexto_previo"], {"fixture": "retained"})
        stt.assert_not_called()
        self.assertNotIn("source_is_audio", self.context.context_data)

    def test_upload_read_is_bounded_even_when_client_length_is_absent(self):
        self.enable_chat_pipeline()
        reads = []
        class TrackedStream(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)
        stream = TrackedStream()
        with patch.object(self.app.request_class, "_get_file_stream", return_value=stream), \
                patch.object(chat_routes, "transcribe_audio_bytes", wraps=audio_service.transcribe_audio_bytes) as stt:
            response = self.post_audio(content=b"x" * 200)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(reads, [33])
        stt.assert_not_called()

    def test_each_advertised_audio_family_uses_real_adapter(self):
        self.enable_chat_pipeline()
        calls = self.fake_provider()
        for mime, extension in (("Audio/WebM; codecs=opus", "webm"),
                ("audio/ogg", "ogg"), ("audio/mpeg", "mp3"),
                ("audio/wav", "wav"), ("audio/mp4", "m4a")):
            with self.subTest(mime=mime):
                response = self.post_audio(mime=mime)
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(calls[-1]["filename"], "audio." + extension)

    def test_municipal_foreign_session_and_tenant_are_rejected_before_stt(self):
        responder = self.enable_chat_pipeline()
        with patch.object(chat_routes, "transcribe_audio_bytes", wraps=audio_service.transcribe_audio_bytes) as stt:
            headers = {**self.headers, "X-Anon-Id": "foreign-anon"}
            response = self.post_audio("/ask/municipio", headers=headers)
            self.assertEqual(response.status_code, 409, response.get_json())
            self.context.tenant_id = 12
            response = self.post_audio("/ask/municipio", headers={**self.headers,
                "Idempotency-Key": "fixture-audio-key"})
            self.assertEqual(response.status_code, 409, response.get_json())
            stt.assert_not_called()
            responder.assert_not_called()

    def test_invalid_demo_session_is_rejected_before_stt(self):
        self.enable_chat_pipeline()
        with patch.object(chat_routes, "transcribe_audio_bytes", wraps=audio_service.transcribe_audio_bytes) as stt:
            response = self.post_audio(headers={**self.headers, "X-Demo-Session-Id": "invalid-synthetic-demo"})
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertEqual(response.get_json()["reason_code"], "demo_session_expired")
            stt.assert_not_called()

    def test_municipal_idempotency_replays_without_second_transcription_and_hashes_audio(self):
        """Real wrapper/hash/scope, synthetic receipt storage (no durable DB proof)."""
        from services import municipio_chat_idempotency as idempotency
        responder = self.enable_chat_pipeline()
        calls = self.fake_provider()
        receipt = {}
        identities = []
        def claim(identity):
            identities.append(identity)
            if "identity" not in receipt:
                receipt["identity"] = identity
                return idempotency.IdempotencyDecision(receipt_id=41, replayed=False)
            if receipt["identity"].request_hash != identity.request_hash:
                raise idempotency.IdempotencyPayloadConflict("Synthetic changed payload")
            return idempotency.IdempotencyDecision(receipt_id=41, replayed=True)
        def complete(receipt_id, *, response_json, response_status, response_request_id, **kwargs):
            self.assertEqual(receipt_id, 41)
            receipt["snapshot"] = (response_json, response_status, response_request_id)
        with patch.object(idempotency, "execution_lock", side_effect=lambda identity: nullcontext()), \
                patch.object(idempotency, "maybe_expire_completed_response_snapshots"), \
                patch.object(idempotency, "claim_or_replay", side_effect=claim), \
                patch.object(idempotency, "complete_receipt", side_effect=complete), \
                patch.object(idempotency, "replay_snapshot", side_effect=lambda ident: receipt["snapshot"]), \
                patch.object(chat_routes, "transcribe_audio_bytes", wraps=audio_service.transcribe_audio_bytes) as stt:
            headers = {**self.headers, "Idempotency-Key": "fixture-audio-key"}
            first = self.post_audio("/ask/municipio", headers=headers)
            self.assertEqual(first.status_code, 200, first.get_json())
            self.assertEqual(first.headers["Idempotency-Replayed"], "false")
            self.assertEqual(responder.call_args.kwargs["tenant_id"], 11)
            self.assertTrue(responder.call_args.kwargs["idempotency_key"].startswith("chat:"))
            replay = self.post_audio("/ask/municipio", headers=headers)
            self.assertEqual(replay.status_code, 200, replay.get_json())
            self.assertEqual(replay.get_json(), first.get_json())
            self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
            changed = self.post_audio("/ask/municipio", content=b"changed-synthetic-webm", headers=headers)
            self.assertEqual(changed.status_code, 409, changed.get_json())
            self.assertEqual(changed.get_json()["reason_code"], "municipio_chat_idempotency_payload_conflict")
            self.assertEqual(stt.call_count, 1)
            self.assertEqual(responder.call_count, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(identities[0].request_hash, identities[1].request_hash)
        self.assertNotEqual(identities[0].request_hash, identities[2].request_hash)

    def test_legacy_null_context_can_receive_successful_audio(self):
        responder = self.enable_chat_pipeline()
        self.context.context_data = None
        self.fake_provider()
        response = self.post_audio()
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(responder.call_args.args[0], "Consulta sintética")
        self.assertTrue(self.context.context_data["source_is_audio"])

    def test_audio_at_exact_byte_limit_is_accepted_by_real_adapter(self):
        responder = self.enable_chat_pipeline()
        calls = self.fake_provider()
        response = self.post_audio(content=b"x" * 32)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["bytes"], b"x" * 32)
        responder.assert_called_once()


if __name__ == "__main__":
    unittest.main()
