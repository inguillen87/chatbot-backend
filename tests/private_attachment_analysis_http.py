"""Real durable login -> producer -> worker -> bounded private SDK bytes.

Run in the isolated acceptance runtime: python -m tests.private_attachment_analysis_http
"""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == "__main__":
    prepare_process()

import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_private_attachment_storage_http as fixtures


class PrivateAttachmentAnalysisTests(unittest.TestCase):
    login = fixtures.PrivateAttachmentHTTPTests.login
    sdk_read = fixtures.PrivateAttachmentHTTPTests.sdk_read

    @classmethod
    def setUpClass(cls):
        fixtures.PrivateAttachmentHTTPTests.setUpClass.__func__(cls)
        from flask import current_app, g, jsonify
        from utils.auth_helpers import token_requerido

        @token_requerido
        def enqueue(current_user):
            from services.analisis_archivo_service import enqueue_file_content_analysis
            from services.attachment_analysis_access import AttachmentAnalysisAccessError
            if current_app.config.get("ANALYSIS_WIDGET_FIXTURE"):
                g.widget_session = True
            try:
                result = enqueue_file_content_analysis(current_app.config["PRIVATE_ATTACHMENT_TEST_ID"])
            except AttachmentAnalysisAccessError:
                return jsonify({"queued": False}), 403
            return jsonify({"queued": result is not False})

        cls.app.add_url_rule("/_acceptance/attachment-analysis/enqueue",
                             view_func=enqueue, methods=["POST"])

    @classmethod
    def tearDownClass(cls):
        fixtures.PrivateAttachmentHTTPTests.tearDownClass.__func__(cls)

    def setUp(self):
        fixtures.PrivateAttachmentHTTPTests.setUp(self)
        from models import ArchivoAdjunto, db
        self.app.config["ANALYSIS_WIDGET_FIXTURE"] = False
        with self.app.app_context():
            db.session.get(ArchivoAdjunto, self.attachment_id).mime = "audio/ogg"
            db.session.commit()

    def queue(self, client=None):
        from services.analisis_archivo_service import tarea_analizar_contenido_archivo
        client = client or self.login()
        with patch.object(tarea_analizar_contenido_archivo, "apply_async", return_value="fixture-queued") as broker:
            response = client.post("/_acceptance/attachment-analysis/enqueue")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json(), {"queued": True})
        attachment_id, grant = broker.call_args.kwargs["args"]
        self.assertNotIn(grant, broker.call_args.kwargs["argsrepr"])
        self.assertEqual(attachment_id, self.attachment_id)
        self.assertNotIn(grant, response.get_data(as_text=True))
        return client, grant

    def run_worker(self, grant, attachment_id=None):
        from flask import has_request_context
        from services.analisis_archivo_service import tarea_analizar_contenido_archivo
        with self.app.app_context():
            self.assertFalse(has_request_context())
            return tarea_analizar_contenido_archivo.run(
                self.attachment_id if attachment_id is None else attachment_id, grant)

    def analysis(self):
        from models import AnalisisArchivo
        with self.app.app_context():
            rows = AnalisisArchivo.query.filter_by(archivo_adjunto_id=self.attachment_id).all()
            return [{"state": row.estado_analisis, "text": row.texto_extraido,
                     "error": row.error_analisis} for row in rows]

    def assert_denied_before_io(self, grant, attachment_id=None):
        from services.private_attachment_storage import PrivateAttachmentStorage
        with patch.object(PrivateAttachmentStorage, "_create_client") as sdk, \
             patch("services.audio_transcription_service.transcribe_audio_bytes") as transcribe:
            self.assertEqual(self.run_worker(grant, attachment_id)["processed"], 0)
        sdk.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(self.analysis(), [])

    def retire(self, descriptor):
        from services.auth_session_lifecycle import retire_session
        with self.app.app_context():
            receipt = retire_session(descriptor["proof"], uuid4().hex)
            self.assertTrue(receipt["local_revoked"])

    def test_private_audio_worker_uses_verified_bytes_without_http_context(self):
        _, grant = self.queue()
        with self.sdk_read(mime="audio/ogg") as stream, \
             patch("services.audio_transcription_service.transcribe_audio_bytes", return_value="Texto de prueba") as transcribe:
            self.assertEqual(self.run_worker(grant), {"processed": 1})
        transcribe.assert_called_once_with(fixtures.DATA, "audio/ogg")
        self.assertEqual(stream.requested, [15 * 1024 * 1024 + 1])
        self.assertEqual(self.analysis(), [{"state": "completado", "text": "Texto de prueba", "error": None}])

    def test_document_worker_uses_same_authority_and_real_text_extractor(self):
        from models import ArchivoAdjunto, db
        with self.app.app_context():
            db.session.get(ArchivoAdjunto, self.attachment_id).mime = "text/plain"
            db.session.commit()
        _, grant = self.queue()
        with self.sdk_read(mime="text/plain"), \
             patch("services.document_processing_service.llamar_llm_para_json_estructurado", return_value={}):
            self.assertEqual(self.run_worker(grant), {"processed": 1})
        self.assertEqual(self.analysis()[0]["state"], "completado")
        self.assertIn("Disposable attachment fixture", self.analysis()[0]["text"])

    def test_missing_tampered_and_other_resource_grants_never_read_or_create_analysis(self):
        _, grant = self.queue()
        for candidate, attachment_id in ((None, None), (grant + "tampered", None),
                                         (grant, self.attachment_id + 10000)):
            with self.subTest(attachment_id=attachment_id, missing=candidate is None):
                self.assert_denied_before_io(candidate, attachment_id)

    def test_expired_grant_never_reads(self):
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=int(time.time()) - 3601):
            _, grant = self.queue()
        self.assert_denied_before_io(grant)

    def test_grant_cannot_be_borrowed_by_http_or_used_as_a_login_token(self):
        from services.attachment_analysis_access import BackgroundAttachmentAccess, AttachmentAnalysisAccessError
        _, grant = self.queue()
        with self.app.test_request_context():
            with self.assertRaises(AttachmentAnalysisAccessError):
                BackgroundAttachmentAccess(self.attachment_id, grant).require()
        response = self.app.test_client().get("/auth/me", headers={"Authorization": "Bearer " + grant})
        self.assertIn(response.status_code, (401, 403))

    def test_anonymous_foreign_tenant_and_widget_cannot_enqueue(self):
        from services.analisis_archivo_service import tarea_analizar_contenido_archivo
        clients = (self.app.test_client(), self.login("acceptance-b"))
        with patch.object(tarea_analizar_contenido_archivo, "apply_async") as broker:
            for client in clients:
                response = client.post("/_acceptance/attachment-analysis/enqueue")
                self.assertIn(response.status_code, (401, 403))
            self.app.config["ANALYSIS_WIDGET_FIXTURE"] = True
            response = self.login().post("/_acceptance/attachment-analysis/enqueue")
            self.assertEqual(response.status_code, 403)
        broker.assert_not_called()

    def test_retired_session_cannot_run_previously_queued_analysis(self):
        client, grant = self.queue()
        self.retire(client.get("/auth/me").get_json()["session_retirement"])
        self.assert_denied_before_io(grant)

    def test_changed_resource_cannot_reuse_old_grant(self):
        from models import ArchivoAdjunto, db
        _, grant = self.queue()
        with self.app.app_context():
            db.session.get(ArchivoAdjunto, self.attachment_id).url = fixtures.REFERENCE.replace("acceptance-a", "acceptance-b")
            db.session.commit()
        self.assert_denied_before_io(grant)

    def test_changed_membership_cannot_reuse_old_grant(self):
        from models import User, db
        _, grant = self.queue()
        with self.app.app_context():
            actor = db.session.get(User, self.accounts["acceptance-a"]["id"])
            actor.tenant_id = self.accounts["acceptance-b"]["tenant_id"]
            db.session.commit()
        self.assert_denied_before_io(grant)

    def test_disabled_tenant_cannot_reuse_old_grant(self):
        from models import TenantProfile, db
        _, grant = self.queue()
        with self.app.app_context():
            db.session.get(TenantProfile, self.accounts["acceptance-a"]["tenant_id"]).is_active = False
            db.session.commit()
        self.assert_denied_before_io(grant)

    def test_retirement_during_storage_releases_no_bytes_to_provider(self):
        client, grant = self.queue()
        descriptor = client.get("/auth/me").get_json()["session_retirement"]
        with self.sdk_read(mime="audio/ogg", on_read=lambda: self.retire(descriptor)), \
             patch("services.audio_transcription_service.transcribe_audio_bytes") as transcribe:
            self.assertEqual(self.run_worker(grant)["processed"], 0)
        transcribe.assert_not_called()
        self.assertEqual(self.analysis()[0]["state"], "error")
        self.assertIsNone(self.analysis()[0]["text"])

    def test_retirement_during_transcription_discards_result(self):
        client, grant = self.queue()
        descriptor = client.get("/auth/me").get_json()["session_retirement"]
        def transcribe(*args):
            self.retire(descriptor)
            return "Must not be persisted"
        with self.sdk_read(mime="audio/ogg"), \
             patch("services.audio_transcription_service.transcribe_audio_bytes", side_effect=transcribe):
            self.assertEqual(self.run_worker(grant)["processed"], 0)
        self.assertEqual(self.analysis()[0]["state"], "error")
        self.assertIsNone(self.analysis()[0]["text"])

    def test_retirement_during_document_extraction_never_reaches_provider(self):
        from models import ArchivoAdjunto, db
        from services.document_processing_service import document_processing_service
        with self.app.app_context():
            db.session.get(ArchivoAdjunto, self.attachment_id).mime = "text/plain"
            db.session.commit()
        client, grant = self.queue()
        descriptor = client.get("/auth/me").get_json()["session_retirement"]
        extract_text = document_processing_service._extract_from_text

        def extract_then_retire(*args, **kwargs):
            result = extract_text(*args, **kwargs)
            self.assertIn("Disposable attachment fixture", result[0])
            self.retire(descriptor)
            return result

        with self.sdk_read(mime="text/plain"), \
             patch.object(document_processing_service, "_extract_from_text", side_effect=extract_then_retire) as extract, \
             patch("services.document_processing_service.llamar_llm_para_json_estructurado", return_value={}) as provider:
            self.assertEqual(self.run_worker(grant)["processed"], 0)
        extract.assert_called_once()
        provider.assert_not_called()
        self.assertEqual(self.analysis()[0]["state"], "error")
        self.assertIsNone(self.analysis()[0]["text"])

    def test_interpretation_error_is_never_saved_as_completed(self):
        _, grant = self.queue()
        with self.sdk_read(mime="audio/ogg"), \
             patch("services.audio_transcription_service.transcribe_audio_bytes",
                   side_effect=RuntimeError("provider-private-detail")):
            self.assertEqual(self.run_worker(grant)["processed"], 0)
        result = self.analysis()[0]
        self.assertEqual(result["state"], "error")
        self.assertIsNone(result["text"])
        self.assertNotIn("provider-private-detail", result["error"])

    def test_wrong_storage_mime_never_reaches_provider_or_completed_state(self):
        _, grant = self.queue()
        with self.sdk_read(mime="application/pdf"), \
             patch("services.audio_transcription_service.transcribe_audio_bytes") as transcribe:
            self.assertEqual(self.run_worker(grant)["processed"], 0)
        transcribe.assert_not_called()
        self.assertEqual(self.analysis()[0]["state"], "error")

    def test_provider_returning_no_transcript_is_an_error(self):
        _, grant = self.queue()
        with self.sdk_read(mime="audio/ogg"), \
             patch("services.audio_transcription_service.transcribe_audio_bytes", return_value=None):
            self.assertEqual(self.run_worker(grant)["processed"], 0)
        self.assertEqual(self.analysis()[0]["state"], "error")
        self.assertIsNone(self.analysis()[0]["text"])

    def test_disabled_actor_cannot_reuse_old_grant(self):
        from models import User, db
        _, grant = self.queue()
        with self.app.app_context():
            db.session.get(User, self.accounts["acceptance-a"]["id"]).accesibilidad = {"auth": {"disabled": True}}
            db.session.commit()
        self.assert_denied_before_io(grant)


if __name__ == "__main__":
    unittest.main(verbosity=2)
