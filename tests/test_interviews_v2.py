import json
import hashlib
from datetime import datetime, timedelta, timezone
import os
import time
import unittest
import uuid
from unittest.mock import patch

from app import create_app, db
from config import TestingConfig, _env_strict_opt_in
from models import (
    AuditEvent,
    ChannelSessionIdentityBinding,
    ChatSessionContext,
    TenantProfile,
    User,
    WhatsAppInboundTurn,
    WhatsAppOutboundAttempt,
)
from models_interviews import (
    AssessmentCase,
    AssessmentProgram,
    AssessmentProgramVersion,
    InterviewConsentChallenge,
    InterviewConsentPresentation,
    InterviewEvidence,
    InterviewSession,
)
from services.interview_access_policy import (
    INTERVIEW_CASES_CREATE,
    INTERVIEW_CASES_READ,
)
from utils.auth_helpers import generar_token


class InterviewTestingConfig(TestingConfig):
    ENABLE_ASSESSMENT_INTERVIEWS_V1 = True


class TestInterviewsV2(unittest.TestCase):
    CONSENT_TEXT = (
        "Autorizo el tratamiento de mis respuestas para esta entrevista institucional."
    )
    CONSENT_TEXT_SHA256 = hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest()
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(InterviewTestingConfig)

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self.suffix = str(time.time_ns())

        self.owner, self.tenant, self.owner_headers = self._make_tenant("primary")
        self.other_owner, self.other_tenant, self.other_headers = self._make_tenant(
            "foreign"
        )

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.ctx.pop()

    def _make_tenant(self, label):
        owner = User(
            name=f"{label} owner",
            email=f"interview-{label}-{self.suffix}@chatboc.test",
            password_hash="hash",
            rol="admin",
            es_empleado=False,
            entity_token=f"entity-{label}-{self.suffix}",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=f"interview-{label}-{self.suffix}",
            nombre=f"Interview {label}",
            tipo="pyme",
            pyme_id=owner.id,
            plan="full",
            vertical="educacion",
            subvertical="colegio_privado",
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        owner.tenant_slug = tenant.slug
        db.session.commit()
        token = generar_token(
            owner.id,
            owner.rol,
            "pyme",
            municipio_id=None,
            pyme_id=owner.id,
            extra_claims={"tenant_id": tenant.id, "tenant_slug": tenant.slug},
        )
        return owner, tenant, {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": tenant.slug,
        }

    def _employee(self, capabilities):
        user = User(
            name="Scoped employee",
            email=f"interview-employee-{time.time_ns()}@chatboc.test",
            password_hash="hash",
            rol="empleado",
            es_empleado=True,
            empresa_id=self.owner.id,
            tenant_id=self.tenant.id,
            tenant_slug=self.tenant.slug,
            accesibilidad={
                "employee_scope": {"capabilities": list(capabilities)}
            },
        )
        db.session.add(user)
        db.session.commit()
        token = generar_token(
            user.id,
            user.rol,
            "pyme",
            municipio_id=None,
            pyme_id=self.owner.id,
            extra_claims={
                "tenant_id": self.tenant.id,
                "tenant_slug": self.tenant.slug,
            },
        )
        return user, {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": self.tenant.slug,
        }

    @staticmethod
    def _with_idempotency(headers, key):
        return {**headers, "Idempotency-Key": key}

    @staticmethod
    def _payload_digest(payload):
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _subject_binding(self, *, tenant=None):
        tenant = tenant or self.tenant
        chat_session_id = str(uuid.uuid4())
        identity_hmac = hashlib.sha256(
            f"subject:{tenant.id}:{chat_session_id}".encode("utf-8")
        ).hexdigest()
        db.session.add(
            ChatSessionContext(
                chat_session_id=chat_session_id,
                tenant_id=tenant.id,
                context_data={},
            )
        )
        db.session.flush()
        binding = ChannelSessionIdentityBinding(
            tenant_id=tenant.id,
            channel="whatsapp",
            provider="twilio",
            identity_version="v1",
            identity_hmac=identity_hmac,
            chat_session_id=chat_session_id,
            status=ChannelSessionIdentityBinding.STATUS_ACTIVE,
            continuity_status=ChannelSessionIdentityBinding.CONTINUITY_NEW,
            generation=1,
        )
        db.session.add(binding)
        db.session.flush()
        return binding

    def _participant_attestation(
        self,
        session_id,
        source="whatsapp",
        text_sha256=None,
        *,
        register_presentation=True,
        provider_status="delivered",
        outbound_mutator=None,
        presentation_status_at=None,
    ):
        if source != "whatsapp":
            raise AssertionError("test helper only creates signed WhatsApp receipts")
        session = db.session.get(InterviewSession, session_id)
        binding = db.session.get(
            ChannelSessionIdentityBinding, session.subject_identity_binding_id
        )
        self.assertIsNotNone(binding)
        issued = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/consent-challenges",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(issued.status_code, 201, issued.get_json())
        challenge = issued.get_json()["consent_challenge"]
        action_id = challenge["action_id"]
        prefix, nonce, action_text_sha256, action = action_id.split(":", 3)
        self.assertEqual(prefix, "interview_consent_v3")
        self.assertEqual(action, "grant_consent")
        self.assertEqual(action_text_sha256, challenge["consent_text_sha256"])

        presentation_at = presentation_status_at or datetime.now(timezone.utc)
        source_sid = f"SMConsentSource{time.time_ns()}"
        source_turn = WhatsAppInboundTurn(
            tenant_id=self.tenant.id,
            provider="twilio",
            provider_message_sid=source_sid,
            stream_key=f"test-consent:{self.tenant.id}:{binding.id}",
            chat_session_id=binding.chat_session_id,
            session_identity_binding_id=binding.id,
            session_identity_version=binding.identity_version,
            session_identity_hmac=binding.identity_hmac,
            message_kind="text",
            payload_digest=hashlib.sha256(source_sid.encode("utf-8")).hexdigest(),
            payload_json={"scrubbed": True},
            status=WhatsAppInboundTurn.STATUS_COMPLETED,
            received_at=presentation_at - timedelta(seconds=1),
            completed_at=presentation_at - timedelta(seconds=1),
            result_json={"contract_version": "whatsapp.worker_result.v1"},
        )
        db.session.add(source_turn)
        db.session.flush()
        outbound_contract = challenge["presentation_outbound_contract"]
        outbound_payload = {
            "content_sid": "HXInterviewConsentExactV1",
            "content_variables": outbound_contract["content_variables"],
            "_chatboc_policy_metadata": outbound_contract["policy_metadata"],
        }
        if outbound_mutator is not None:
            outbound_mutator(outbound_payload)
        attempt_sid = f"SMConsentPresentation{time.time_ns()}"
        attempt = WhatsAppOutboundAttempt(
            tenant_id=self.tenant.id,
            inbound_turn_id=source_turn.id,
            provider="twilio",
            stream_key=source_turn.stream_key,
            sequence_no=1,
            message_kind="interactive",
            idempotency_key=f"consent-presentation:{time.time_ns()}",
            payload_digest=self._payload_digest(outbound_payload),
            payload_json=outbound_payload,
            status=WhatsAppOutboundAttempt.STATUS_ACCEPTED,
            provider_message_sid=attempt_sid,
            provider_status=provider_status,
            attempt_count=1,
            max_attempts=5,
            accepted_at=presentation_at,
            completed_at=presentation_at,
        )
        db.session.add(attempt)
        db.session.commit()
        if register_presentation:
            registered = self.client.post(
                f"/api/v2/interviews/sessions/{session_id}/consent-challenges/"
                f"{challenge['id']}/presentations",
                headers=self._with_idempotency(
                    self.owner_headers, f"presentation:{time.time_ns()}"
                ),
                json={"outbound_attempt_id": attempt.attempt_id},
            )
            self.assertEqual(registered.status_code, 201, registered.get_json())

        provider_message_sid = f"SMConsentEvent{time.time_ns()}"
        evidence_ref = f"whatsapp:{provider_message_sid}"
        payload_digest = hashlib.sha256(
            f"signed-provider-payload:{provider_message_sid}".encode("utf-8")
        ).hexdigest()
        received_at = datetime.now(timezone.utc)
        turn = WhatsAppInboundTurn(
            tenant_id=self.tenant.id,
            provider="twilio",
            provider_message_sid=provider_message_sid,
            stream_key=f"test-consent:{self.tenant.id}:{provider_message_sid}",
            chat_session_id=binding.chat_session_id,
            session_identity_binding_id=binding.id,
            session_identity_version=binding.identity_version,
            session_identity_hmac=binding.identity_hmac,
            message_kind="interactive",
            payload_digest=payload_digest,
            payload_json={"scrubbed": True},
            status=WhatsAppInboundTurn.STATUS_COMPLETED,
            received_at=received_at,
            completed_at=received_at,
            result_json={
                "contract_version": "whatsapp.worker_result.v1",
                "interview_consent_receipt": {
                    "contract_version": "interview.consent_provider_receipt.v3",
                    "challenge_nonce_sha256": hashlib.sha256(
                        nonce.encode("ascii")
                    ).hexdigest(),
                    "consent_text_sha256": action_text_sha256,
                    "action": "grant_consent",
                    "granted": True,
                },
            },
        )
        db.session.add(turn)
        db.session.flush()
        return {
            "kind": "participant_event",
            "provider": "twilio",
            "evidence_ref": evidence_ref,
            "evidence_sha256": payload_digest,
            "captured_at": received_at.isoformat(),
        }

    def _register_presentation(self, session_id, challenge_id, attempt_id, *, key=None):
        return self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/consent-challenges/"
            f"{challenge_id}/presentations",
            headers=self._with_idempotency(
                self.owner_headers, key or f"presentation:{time.time_ns()}"
            ),
            json={"outbound_attempt_id": attempt_id},
        )

    def _create_program(self, *, headers=None, key=None, name="Ingreso 2027"):
        response = self.client.post(
            "/api/v2/interviews/programs",
            headers=self._with_idempotency(
                headers or self.owner_headers,
                key or f"program:{time.time_ns()}",
            ),
            json={
                "name": name,
                "description": "Entrevista institucional",
                "program_type": "school_admission",
                "definition": {
                    "sections": [
                        {
                            "id": "family_context",
                            "questions": ["Motivaciones y expectativas"],
                        }
                    ]
                },
                "consent_policy_version": "school-consent-2026.1",
                "consent_text": self.CONSENT_TEXT,
            },
        )
        self.assertIn(response.status_code, {200, 201}, response.get_json())
        return response

    def _publish_program(self, program_id, *, headers=None, key=None):
        response = self.client.post(
            f"/api/v2/interviews/programs/{program_id}/versions/1/publish",
            headers=self._with_idempotency(
                headers or self.owner_headers,
                key or f"publish:{time.time_ns()}",
            ),
            json={},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def _published_program(self):
        created = self._create_program()
        program_id = created.get_json()["program"]["id"]
        self._publish_program(program_id)
        return program_id

    def _create_case(
        self,
        program_id,
        *,
        headers=None,
        key=None,
        source_channel="whatsapp",
        binding=None,
    ):
        if source_channel == "whatsapp":
            binding = binding or self._subject_binding()
        payload = {
            "program_id": program_id,
            "subject_type": "student_applicant",
            "subject_ref": f"student:applicant{time.time_ns()}",
            "source_channel": source_channel,
        }
        if binding is not None:
            payload["subject_identity_binding_id"] = binding.id
        response = self.client.post(
            "/api/v2/interviews/cases",
            headers=self._with_idempotency(
                headers or self.owner_headers,
                key or f"case:{time.time_ns()}",
            ),
            json=payload,
        )
        self.assertIn(response.status_code, {200, 201}, response.get_json())
        return response

    def _create_session(self, case_id, *, key=None):
        response = self.client.post(
            f"/api/v2/interviews/cases/{case_id}/sessions",
            headers=self._with_idempotency(
                self.owner_headers,
                key or f"session:{time.time_ns()}",
            ),
            json={"channel": "whatsapp"},
        )
        self.assertIn(response.status_code, {200, 201}, response.get_json())
        return response

    def _start_session(self, session_id, *, key=None):
        response = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers,
                key or f"start:{time.time_ns()}",
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": self._participant_attestation(session_id),
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def _active_session(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        self._start_session(session_id)
        return case_id, session_id

    def test_feature_is_closed_by_default_contract_and_anonymous_is_rejected(self):
        anonymous = self.client.post(
            "/api/v2/interviews/programs",
            headers={"X-Tenant-Slug": self.tenant.slug},
            json={},
        )
        self.assertEqual(anonymous.status_code, 401)

        self.app.config["ENABLE_ASSESSMENT_INTERVIEWS_V1"] = False
        try:
            disabled = self.client.post(
                "/api/v2/interviews/programs",
                headers=self._with_idempotency(
                    self.owner_headers, "program:disabled01"
                ),
                json={},
            )
        finally:
            self.app.config["ENABLE_ASSESSMENT_INTERVIEWS_V1"] = True
        self.assertEqual(disabled.status_code, 403)
        self.assertEqual(
            disabled.get_json()["reason_code"],
            "assessment_interviews_feature_disabled",
        )

    def test_feature_config_requires_strict_explicit_opt_in(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENABLE_ASSESSMENT_INTERVIEWS_V1", None)
            self.assertFalse(_env_strict_opt_in("ENABLE_ASSESSMENT_INTERVIEWS_V1"))
        with patch.dict(
            os.environ, {"ENABLE_ASSESSMENT_INTERVIEWS_V1": "true"}
        ):
            self.assertTrue(_env_strict_opt_in("ENABLE_ASSESSMENT_INTERVIEWS_V1"))
        with patch.dict(
            os.environ, {"ENABLE_ASSESSMENT_INTERVIEWS_V1": "definitely"}
        ):
            self.assertFalse(_env_strict_opt_in("ENABLE_ASSESSMENT_INTERVIEWS_V1"))

    def test_published_version_is_immutable_and_case_pins_hash_not_definition(self):
        created = self._create_program()
        draft = created.get_json()["program"]["version"]
        self.assertIn("definition", draft)
        self.assertEqual(draft["consent_text"], self.CONSENT_TEXT)
        self.assertEqual(draft["consent_text_sha256"], self.CONSENT_TEXT_SHA256)
        self.assertEqual(draft["consent_text_format"], "plain_text")
        self.assertEqual(
            draft["consent_text_normalization"], "unicode_nfc_lf_trim_v1"
        )
        self.assertTrue(draft["consent_snapshot_complete"])
        program_id = created.get_json()["program"]["id"]
        published = self._publish_program(program_id).get_json()["program"]["version"]
        self.assertNotIn("definition", published)
        self.assertTrue(published["immutable"])

        case_payload = self._create_case(program_id).get_json()["case"]
        self.assertNotIn("decision", case_payload)
        self.assertEqual(
            case_payload["program_version"]["definition_hash"],
            published["definition_hash"],
        )
        self.assertTrue(case_payload["program_version"]["immutable"])

        version = db.session.get(AssessmentProgramVersion, published["id"])
        original_hash = version.definition_hash
        version.definition_json = {"sections": [{"id": "mutated"}]}
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        persisted = db.session.get(AssessmentProgramVersion, published["id"])
        self.assertEqual(persisted.definition_hash, original_hash)
        self.assertNotEqual(persisted.definition_json, {"sections": [{"id": "mutated"}]})

    def test_consent_text_is_normalized_hashed_and_pinned_by_the_server(self):
        decomposed_text = "  Autorizo el tratamiento de informacio\u0301n.\r\n"
        normalized_text = "Autorizo el tratamiento de información."
        expected_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        created = self.client.post(
            "/api/v2/interviews/programs",
            headers=self._with_idempotency(
                self.owner_headers, "program:consent-snapshot-001"
            ),
            json={
                "name": "Consentimiento verificable",
                "program_type": "school_admission",
                "definition": {"sections": [{"id": "intro"}]},
                "consent_policy_version": "school-consent-2026.3",
                "consent_text": decomposed_text,
            },
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        version_payload = created.get_json()["program"]["version"]
        self.assertEqual(version_payload["consent_text"], normalized_text)
        self.assertEqual(version_payload["consent_text_sha256"], expected_hash)

        mismatch = self.client.post(
            "/api/v2/interviews/programs",
            headers=self._with_idempotency(
                self.owner_headers, "program:consent-mismatch-001"
            ),
            json={
                "name": "Hash ajeno",
                "program_type": "school_admission",
                "definition": {"sections": [{"id": "intro"}]},
                "consent_policy_version": "school-consent-2026.3",
                "consent_text": normalized_text,
                "consent_text_sha256": "f" * 64,
            },
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(
            mismatch.get_json()["reason_code"],
            "interview_consent_text_hash_mismatch",
        )

        program_id = created.get_json()["program"]["id"]
        self._publish_program(program_id, key="publish:consent-snapshot-001")
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]

        missing_hash = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers, "start:consent-hash-missing"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.3",
                    "source": "whatsapp",
                }
            },
        )
        self.assertEqual(missing_hash.status_code, 422)
        self.assertEqual(
            missing_hash.get_json()["reason_code"],
            "interview_consent_text_hash_required",
        )

        wrong_hash = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers, "start:consent-hash-wrong"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.3",
                    "text_sha256": "0" * 64,
                    "source": "whatsapp",
                    "attestation": self._participant_attestation(
                        session_id, text_sha256="0" * 64
                    ),
                }
            },
        )
        self.assertEqual(wrong_hash.status_code, 409)
        self.assertEqual(
            wrong_hash.get_json()["reason_code"],
            "interview_consent_text_mismatch",
        )

        started = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers, "start:consent-hash-exact"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.3",
                    "text_sha256": expected_hash,
                    "source": "whatsapp",
                    "attestation": self._participant_attestation(
                        session_id, text_sha256=expected_hash
                    ),
                }
            },
        )
        self.assertEqual(started.status_code, 200, started.get_json())
        consent = started.get_json()["session"]["consent"]
        self.assertEqual(consent["text_sha256"], expected_hash)
        self.assertTrue(consent["verified_against_program_version"])
        self.assertTrue(consent["participant_evidence_bound"])
        self.assertTrue(consent["participant_evidence_verified"])
        self.assertEqual(
            consent["proof_status"],
            "expected_channel_delivery_and_action_verified",
        )
        self.assertTrue(consent["consent_text_delivery_evidence_verified"])
        self.assertFalse(consent["civil_identity_verified"])
        stored_session = db.session.get(InterviewSession, session_id)
        self.assertEqual(stored_session.consent_text_sha256, expected_hash)

    def test_publish_recomputes_snapshot_hash_and_consent_requires_durable_attestation(self):
        created = self._create_program(key="program:tamper-consent-hash")
        program_id = created.get_json()["program"]["id"]
        version_id = created.get_json()["program"]["version"]["id"]
        version = db.session.get(AssessmentProgramVersion, version_id)
        version.consent_text_sha256 = "0" * 64
        db.session.commit()

        corrupted = self.client.post(
            f"/api/v2/interviews/programs/{program_id}/versions/1/publish",
            headers=self._with_idempotency(
                self.owner_headers, "publish:tampered-consent-hash"
            ),
            json={},
        )
        self.assertEqual(corrupted.status_code, 409)
        self.assertEqual(
            corrupted.get_json()["reason_code"],
            "interview_consent_snapshot_incomplete",
        )

        clean_program_id = self._published_program()
        case_id = self._create_case(clean_program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        no_attestation = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers, "start:no-attestation"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                }
            },
        )
        self.assertEqual(no_attestation.status_code, 422)
        self.assertEqual(
            no_attestation.get_json()["reason_code"],
            "interview_consent_attestation_required",
        )

        fake_operator = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers, "start:operator-over-whatsapp"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": {
                        "kind": "operator_attestation",
                        "attested": True,
                    },
                }
            },
        )
        self.assertEqual(fake_operator.status_code, 422)
        self.assertEqual(
            fake_operator.get_json()["reason_code"],
            "interview_consent_attestation_invalid",
        )

    def test_participant_consent_evidence_cannot_be_reused_between_sessions(self):
        program_id = self._published_program()
        first_case = self._create_case(program_id).get_json()["case"]["id"]
        second_case = self._create_case(program_id).get_json()["case"]["id"]
        first_session = self._create_session(first_case).get_json()["session"]["id"]
        second_session = self._create_session(second_case).get_json()["session"]["id"]
        attestation = self._participant_attestation(first_session)

        def start(session_id, key):
            return self.client.post(
                f"/api/v2/interviews/sessions/{session_id}/start",
                headers=self._with_idempotency(self.owner_headers, key),
                json={
                    "consent": {
                        "granted": True,
                        "policy_version": "school-consent-2026.1",
                        "text_sha256": self.CONSENT_TEXT_SHA256,
                        "source": "whatsapp",
                        "attestation": attestation,
                    }
                },
            )

        first = start(first_session, "start:evidence-unique-first")
        self.assertEqual(first.status_code, 200, first.get_json())
        reused = start(second_session, "start:evidence-unique-second")
        self.assertEqual(reused.status_code, 409)
        self.assertEqual(
            reused.get_json()["reason_code"],
            "interview_consent_challenge_scope_mismatch",
        )

    def test_consent_challenge_is_opaque_tenant_scoped_and_never_persists_nonce(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        attestation = self._participant_attestation(session_id)
        challenge = InterviewConsentChallenge.query.filter_by(
            tenant_id=self.tenant.id, interview_session_id=session_id
        ).one()
        turn = WhatsAppInboundTurn.query.filter_by(
            provider_message_sid=attestation["evidence_ref"].split(":", 1)[1]
        ).one()

        self.assertEqual(challenge.tenant_id, self.tenant.id)
        self.assertEqual(challenge.interview_session_id, session_id)
        self.assertEqual(
            challenge.expected_identity_binding_id,
            turn.session_identity_binding_id,
        )
        self.assertEqual(len(challenge.nonce_sha256), 64)
        persisted = json.dumps(
            {
                "challenge": {
                    key: value
                    for key, value in vars(challenge).items()
                    if not key.startswith("_")
                },
                "audit": [event.details for event in AuditEvent.query.all()],
            },
            default=str,
        )
        self.assertNotIn("interview_consent_v3:", persisted)

        replacement = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/consent-challenges",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(replacement.status_code, 201, replacement.get_json())
        replacement_row = db.session.get(
            InterviewConsentChallenge,
            replacement.get_json()["consent_challenge"]["id"],
        )
        db.session.refresh(challenge)
        self.assertLessEqual(challenge.expires_at, replacement_row.issued_at)

        foreign = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/consent-challenges",
            headers=self.other_headers,
            json={},
        )
        self.assertEqual(foreign.status_code, 404)

    def test_consent_challenge_rejects_different_participant_identity(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        attestation = self._participant_attestation(session_id)
        turn = WhatsAppInboundTurn.query.filter_by(
            provider_message_sid=attestation["evidence_ref"].split(":", 1)[1]
        ).one()

        other_chat_session_id = str(uuid.uuid4())
        other_hmac = hashlib.sha256(
            f"other-participant:{other_chat_session_id}".encode("utf-8")
        ).hexdigest()
        db.session.add(
            ChatSessionContext(
                chat_session_id=other_chat_session_id,
                tenant_id=self.tenant.id,
                context_data={},
            )
        )
        db.session.flush()
        other_binding = ChannelSessionIdentityBinding(
            tenant_id=self.tenant.id,
            channel="whatsapp",
            provider="twilio",
            identity_version="v1",
            identity_hmac=other_hmac,
            chat_session_id=other_chat_session_id,
            status=ChannelSessionIdentityBinding.STATUS_ACTIVE,
            continuity_status=ChannelSessionIdentityBinding.CONTINUITY_NEW,
            generation=1,
        )
        db.session.add(other_binding)
        db.session.flush()
        turn.session_identity_binding_id = other_binding.id
        turn.session_identity_hmac = other_binding.identity_hmac
        turn.session_identity_version = other_binding.identity_version
        turn.chat_session_id = other_binding.chat_session_id
        db.session.commit()

        response = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers, "start:wrong-participant-identity"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": attestation,
                }
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["reason_code"],
            "interview_consent_participant_identity_mismatch",
        )

    def test_whatsapp_case_pins_subject_identity_before_conductor_challenge(self):
        program_id = self._published_program()
        missing = self.client.post(
            "/api/v2/interviews/cases",
            headers=self._with_idempotency(
                self.owner_headers, "case:missing-subject-binding"
            ),
            json={
                "program_id": program_id,
                "subject_type": "student_applicant",
                "subject_ref": "student:missingBindingAbc1",
                "source_channel": "whatsapp",
            },
        )
        self.assertEqual(missing.status_code, 422, missing.get_json())
        self.assertEqual(
            missing.get_json()["reason_code"],
            "interview_subject_identity_binding_required",
        )

        foreign_binding = self._subject_binding(tenant=self.other_tenant)
        foreign = self.client.post(
            "/api/v2/interviews/cases",
            headers=self._with_idempotency(
                self.owner_headers, "case:foreign-subject-binding"
            ),
            json={
                "program_id": program_id,
                "subject_type": "student_applicant",
                "subject_ref": "student:foreignBindingAbc1",
                "source_channel": "whatsapp",
                "subject_identity_binding_id": foreign_binding.id,
            },
        )
        self.assertEqual(foreign.status_code, 409, foreign.get_json())

        pinned_binding = self._subject_binding()
        case_body = self._create_case(
            program_id, binding=pinned_binding
        ).get_json()["case"]
        self.assertEqual(
            case_body["subject_channel_identity"]["binding_id"], pinned_binding.id
        )
        self.assertFalse(
            case_body["subject_channel_identity"]["civil_identity_verified"]
        )
        session_body = self._create_session(case_body["id"]).get_json()["session"]
        self.assertEqual(
            session_body["subject_channel_identity"]["binding_id"], pinned_binding.id
        )
        wrong_binding = self._subject_binding()
        choose_again = self.client.post(
            f"/api/v2/interviews/sessions/{session_body['id']}/consent-challenges",
            headers=self.owner_headers,
            json={"session_identity_binding_id": wrong_binding.id},
        )
        self.assertEqual(choose_again.status_code, 422, choose_again.get_json())
        self.assertEqual(
            choose_again.get_json()["reason_code"], "interview_unknown_fields"
        )
        issued = self.client.post(
            f"/api/v2/interviews/sessions/{session_body['id']}/consent-challenges",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(issued.status_code, 201, issued.get_json())
        self.assertEqual(
            issued.get_json()["consent_challenge"]["expected_identity_binding_id"],
            pinned_binding.id,
        )

    def test_whatsapp_start_requires_prior_durable_consent_presentation(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        attestation = self._participant_attestation(
            session_id, register_presentation=False
        )
        response = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers, "start:no-presentation-ledger"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": attestation,
                }
            },
        )
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "interview_consent_presentation_required",
        )
        stored = db.session.get(InterviewSession, session_id)
        self.assertEqual(stored.status, "scheduled")
        self.assertFalse(stored.consent_granted)

    def test_only_delivered_or_read_presentation_can_verify_channel_action(self):
        program_id = self._published_program()
        for provider_status in ("accepted", "sent"):
            case_id = self._create_case(program_id).get_json()["case"]["id"]
            session_id = self._create_session(case_id).get_json()["session"]["id"]
            self._participant_attestation(
                session_id,
                register_presentation=False,
                provider_status=provider_status,
            )
            challenge = InterviewConsentChallenge.query.filter_by(
                interview_session_id=session_id
            ).one()
            attempt = WhatsAppOutboundAttempt.query.filter_by(
                inbound_turn_id=WhatsAppInboundTurn.query.filter_by(
                    session_identity_binding_id=(
                        db.session.get(InterviewSession, session_id).subject_identity_binding_id
                    ),
                    message_kind="text",
                )
                .order_by(WhatsAppInboundTurn.id.desc())
                .first()
                .id
            ).one()
            rejected = self._register_presentation(
                session_id, challenge.id, attempt.attempt_id
            )
            self.assertEqual(rejected.status_code, 409, rejected.get_json())
            self.assertEqual(
                rejected.get_json()["reason_code"],
                "interview_consent_presentation_not_delivered",
            )

        for provider_status in ("delivered", "read"):
            case_id = self._create_case(program_id).get_json()["case"]["id"]
            session_id = self._create_session(case_id).get_json()["session"]["id"]
            started = self._start_session_with_status(session_id, provider_status)
            consent = started.get_json()["session"]["consent"]
            self.assertEqual(
                consent["proof_status"],
                "expected_channel_delivery_and_action_verified",
            )
            self.assertFalse(consent["civil_identity_verified"])
            presentation = InterviewConsentPresentation.query.filter_by(
                interview_session_id=session_id
            ).one()
            self.assertEqual(presentation.outbound_provider_status, provider_status)

    def _start_session_with_status(self, session_id, provider_status):
        attestation = self._participant_attestation(
            session_id, provider_status=provider_status
        )
        response = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(
                self.owner_headers, f"start:{provider_status}:{time.time_ns()}"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": attestation,
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def test_consent_presentation_rejects_wrong_text_action_and_identity(self):
        program_id = self._published_program()
        mutations = (
            lambda payload: payload["content_variables"].__setitem__(
                "1", "Texto de consentimiento diferente"
            ),
            lambda payload: payload["content_variables"].__setitem__("2", "deny"),
            lambda payload: payload["_chatboc_policy_metadata"][
                "interview_consent_presentation"
            ].__setitem__("consent_text_sha256", "f" * 64),
        )
        for index, mutation in enumerate(mutations):
            case_id = self._create_case(program_id).get_json()["case"]["id"]
            session_id = self._create_session(case_id).get_json()["session"]["id"]
            self._participant_attestation(
                session_id,
                register_presentation=False,
                outbound_mutator=mutation,
            )
            challenge = InterviewConsentChallenge.query.filter_by(
                interview_session_id=session_id
            ).one()
            attempt = WhatsAppOutboundAttempt.query.order_by(
                WhatsAppOutboundAttempt.id.desc()
            ).first()
            response = self._register_presentation(
                session_id,
                challenge.id,
                attempt.attempt_id,
                key=f"presentation:bad-payload-{index}",
            )
            self.assertEqual(response.status_code, 409, response.get_json())
            self.assertIn(
                response.get_json()["reason_code"],
                {
                    "interview_consent_presentation_payload_mismatch",
                    "interview_consent_presentation_action_mismatch",
                },
            )

        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        self._participant_attestation(session_id, register_presentation=False)
        challenge = InterviewConsentChallenge.query.filter_by(
            interview_session_id=session_id
        ).one()
        attempt = WhatsAppOutboundAttempt.query.order_by(
            WhatsAppOutboundAttempt.id.desc()
        ).first()
        source_turn = db.session.get(WhatsAppInboundTurn, attempt.inbound_turn_id)
        other_binding = self._subject_binding()
        source_turn.session_identity_binding_id = other_binding.id
        source_turn.session_identity_version = other_binding.identity_version
        source_turn.session_identity_hmac = other_binding.identity_hmac
        source_turn.chat_session_id = other_binding.chat_session_id
        db.session.commit()
        wrong_identity = self._register_presentation(
            session_id,
            challenge.id,
            attempt.attempt_id,
            key="presentation:wrong-identity",
        )
        self.assertEqual(wrong_identity.status_code, 409, wrong_identity.get_json())
        self.assertEqual(
            wrong_identity.get_json()["reason_code"],
            "interview_consent_presentation_identity_mismatch",
        )

    def test_presentation_ordering_replay_reuse_and_cross_tenant_fail_closed(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        attestation = self._participant_attestation(session_id)
        presentation = InterviewConsentPresentation.query.filter_by(
            interview_session_id=session_id
        ).one()
        replay = self._register_presentation(
            session_id,
            presentation.consent_challenge_id,
            presentation.outbound_attempt_id,
            key=presentation.registration_idempotency_key,
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["idempotency_replayed"])

        replacement = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/consent-challenges",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(replacement.status_code, 201, replacement.get_json())
        replacement_id = replacement.get_json()["consent_challenge"]["id"]
        reused = self._register_presentation(
            session_id,
            replacement_id,
            presentation.outbound_attempt_id,
            key="presentation:reuse-old-attempt",
        )
        self.assertEqual(reused.status_code, 409, reused.get_json())
        cross_tenant = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/consent-challenges/"
            f"{replacement_id}/presentations",
            headers=self._with_idempotency(
                self.other_headers, "presentation:cross-tenant"
            ),
            json={"outbound_attempt_id": presentation.outbound_attempt_id},
        )
        self.assertEqual(cross_tenant.status_code, 404, cross_tenant.get_json())

        ordered_case = self._create_case(program_id).get_json()["case"]["id"]
        ordered_session = self._create_session(ordered_case).get_json()["session"]["id"]
        ordered_attestation = self._participant_attestation(
            ordered_session, register_presentation=False
        )
        ordered_challenge = InterviewConsentChallenge.query.filter_by(
            interview_session_id=ordered_session
        ).one()
        click_turn = WhatsAppInboundTurn.query.filter_by(
            provider_message_sid=ordered_attestation["evidence_ref"].split(":", 1)[1]
        ).one()
        ordered_attempt = WhatsAppOutboundAttempt.query.order_by(
            WhatsAppOutboundAttempt.id.desc()
        ).first()
        click_turn.received_at = ordered_challenge.issued_at + timedelta(microseconds=1)
        click_turn.completed_at = click_turn.received_at
        db.session.commit()
        registered_after_click = self._register_presentation(
            ordered_session,
            ordered_challenge.id,
            ordered_attempt.attempt_id,
            key="presentation:registered-after-click",
        )
        self.assertEqual(
            registered_after_click.status_code, 201, registered_after_click.get_json()
        )
        start = self.client.post(
            f"/api/v2/interviews/sessions/{ordered_session}/start",
            headers=self._with_idempotency(
                self.owner_headers, "start:presentation-after-click"
            ),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": ordered_attestation,
                }
            },
        )
        self.assertEqual(start.status_code, 409, start.get_json())
        self.assertEqual(
            start.get_json()["reason_code"],
            "interview_consent_presentation_order_invalid",
        )

    def test_consent_click_receipt_binds_exact_text_hash_and_grant_action(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        attestation = self._participant_attestation(session_id)
        turn = WhatsAppInboundTurn.query.filter_by(
            provider_message_sid=attestation["evidence_ref"].split(":", 1)[1]
        ).one()

        def start(key):
            return self.client.post(
                f"/api/v2/interviews/sessions/{session_id}/start",
                headers=self._with_idempotency(self.owner_headers, key),
                json={
                    "consent": {
                        "granted": True,
                        "policy_version": "school-consent-2026.1",
                        "text_sha256": self.CONSENT_TEXT_SHA256,
                        "source": "whatsapp",
                        "attestation": attestation,
                    }
                },
            )

        result_json = dict(turn.result_json)
        receipt = dict(result_json["interview_consent_receipt"])
        receipt["action"] = "deny"
        result_json["interview_consent_receipt"] = receipt
        turn.result_json = result_json
        db.session.commit()
        wrong_action = start("start:wrong-receipt-action")
        self.assertEqual(wrong_action.status_code, 409, wrong_action.get_json())
        self.assertEqual(
            wrong_action.get_json()["reason_code"],
            "interview_consent_provider_receipt_mismatch",
        )

        result_json = dict(turn.result_json)
        receipt = dict(result_json["interview_consent_receipt"])
        receipt["action"] = "grant_consent"
        receipt["consent_text_sha256"] = "f" * 64
        result_json["interview_consent_receipt"] = receipt
        turn.result_json = result_json
        db.session.commit()
        wrong_text = start("start:wrong-receipt-text")
        self.assertEqual(wrong_text.status_code, 409, wrong_text.get_json())
        self.assertEqual(
            wrong_text.get_json()["reason_code"],
            "interview_consent_challenge_scope_mismatch",
        )

    def test_expired_challenge_and_consumed_challenge_fail_closed(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        attestation = self._participant_attestation(session_id)
        turn = WhatsAppInboundTurn.query.filter_by(
            provider_message_sid=attestation["evidence_ref"].split(":", 1)[1]
        ).one()
        challenge = InterviewConsentChallenge.query.filter_by(
            tenant_id=self.tenant.id, interview_session_id=session_id
        ).one()
        challenge.expires_at = turn.received_at - timedelta(seconds=1)
        db.session.commit()

        expired = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(self.owner_headers, "start:expired-challenge"),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": attestation,
                }
            },
        )
        self.assertEqual(expired.status_code, 409)
        self.assertEqual(
            expired.get_json()["reason_code"],
            "interview_consent_challenge_expired",
        )

        challenge.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.session.commit()
        started = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(self.owner_headers, "start:consume-challenge"),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": attestation,
                }
            },
        )
        self.assertEqual(started.status_code, 200, started.get_json())
        db.session.refresh(challenge)
        self.assertIsNotNone(challenge.consumed_at)
        self.assertEqual(challenge.consumed_turn_id, turn.id)
        self.assertEqual(
            challenge.consumed_provider_message_sid,
            turn.provider_message_sid,
        )
        replay = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(self.owner_headers, "start:replay-challenge"),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": attestation,
                }
            },
        )
        self.assertEqual(replay.status_code, 409)

    def test_next_versions_are_monotonic_idempotent_and_explicit_or_copied(self):
        program_id = self._published_program()
        copy_key = "version:copy-v2-001"
        copy_payload = {"copy_from_version_number": 1}
        copied = self.client.post(
            f"/api/v2/interviews/programs/{program_id}/versions",
            headers=self._with_idempotency(self.owner_headers, copy_key),
            json=copy_payload,
        )
        self.assertEqual(copied.status_code, 201, copied.get_json())
        version_two = copied.get_json()["program"]["version"]
        self.assertEqual(version_two["version_number"], 2)
        self.assertEqual(version_two["status"], "draft")
        self.assertIn("definition", version_two)

        replay = self.client.post(
            f"/api/v2/interviews/programs/{program_id}/versions",
            headers=self._with_idempotency(self.owner_headers, copy_key),
            json=copy_payload,
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["idempotency_replayed"])

        conflict = self.client.post(
            f"/api/v2/interviews/programs/{program_id}/versions",
            headers=self._with_idempotency(self.owner_headers, copy_key),
            json={
                "definition": {"sections": [{"id": "changed"}]},
                "consent_policy_version": "school-consent-2026.2",
                "consent_text": "Autorizo la version 2026.2 del tratamiento.",
            },
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.get_json()["reason_code"], "interview_idempotency_conflict"
        )

        published_v2 = self.client.post(
            f"/api/v2/interviews/programs/{program_id}/versions/2/publish",
            headers=self._with_idempotency(
                self.owner_headers, "publish:version-two-001"
            ),
            json={},
        )
        self.assertEqual(published_v2.status_code, 200, published_v2.get_json())
        self.assertEqual(
            published_v2.get_json()["program"]["published_version_number"], 2
        )
        self.assertNotIn(
            "definition", published_v2.get_json()["program"]["version"]
        )

        pinned_case = self._create_case(program_id).get_json()["case"]
        self.assertEqual(pinned_case["program_version"]["version_number"], 2)

        explicit = self.client.post(
            f"/api/v2/interviews/programs/{program_id}/versions",
            headers=self._with_idempotency(
                self.owner_headers, "version:explicit-v3-001"
            ),
            json={
                "definition": {"sections": [{"id": "new_questions"}]},
                "consent_policy_version": "school-consent-2026.2",
                "consent_text": "Autorizo la version 2026.2 del tratamiento.",
            },
        )
        self.assertEqual(explicit.status_code, 201, explicit.get_json())
        version_three = explicit.get_json()["program"]["version"]
        self.assertEqual(version_three["version_number"], 3)
        self.assertNotEqual(
            version_three["definition_hash"],
            published_v2.get_json()["program"]["version"]["definition_hash"],
        )

    def test_case_idempotency_replay_and_conflict(self):
        program_id = self._published_program()
        key = "case:stable-replay-001"
        binding = self._subject_binding()
        payload = {
            "program_id": program_id,
            "subject_type": "student_applicant",
            "subject_ref": "student:opaqueApplicantA1",
            "source_channel": "whatsapp",
            "subject_identity_binding_id": binding.id,
        }
        first = self.client.post(
            "/api/v2/interviews/cases",
            headers=self._with_idempotency(self.owner_headers, key),
            json=payload,
        )
        replay = self.client.post(
            "/api/v2/interviews/cases",
            headers=self._with_idempotency(self.owner_headers, key),
            json=payload,
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["idempotency_replayed"])
        self.assertEqual(first.get_json()["case"]["id"], replay.get_json()["case"]["id"])

        conflict_payload = dict(payload)
        conflict_payload["subject_ref"] = "student:opaqueApplicantB2"
        conflict = self.client.post(
            "/api/v2/interviews/cases",
            headers=self._with_idempotency(self.owner_headers, key),
            json=conflict_payload,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.get_json()["reason_code"], "interview_idempotency_conflict"
        )

    def test_explicit_versioned_consent_and_human_review_only_completion(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]

        missing = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(self.owner_headers, "start:missing-001"),
            json={"consent": {"granted": False}},
        )
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(missing.get_json()["reason_code"], "interview_consent_required")

        invalid_source = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(self.owner_headers, "start:source-001"),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "school-consent-2026.1",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "api",
                }
            },
        )
        self.assertEqual(invalid_source.status_code, 422)
        self.assertEqual(
            invalid_source.get_json()["reason_code"],
            "interview_consent_source_invalid",
        )

        mismatch = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/start",
            headers=self._with_idempotency(self.owner_headers, "start:mismatch-001"),
            json={
                "consent": {
                    "granted": True,
                    "policy_version": "old-policy",
                    "text_sha256": self.CONSENT_TEXT_SHA256,
                    "source": "whatsapp",
                    "attestation": self._participant_attestation(session_id),
                }
            },
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(
            mismatch.get_json()["reason_code"], "interview_consent_version_mismatch"
        )

        started = self._start_session(session_id, key="start:accepted-001")
        self.assertTrue(started.get_json()["session"]["consent"]["granted"])

        forbidden = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/complete",
            headers=self._with_idempotency(self.owner_headers, "complete:forbidden01"),
            json={"outcome": "admitted"},
        )
        self.assertEqual(forbidden.status_code, 422)
        self.assertEqual(
            forbidden.get_json()["reason_code"], "interview_decision_not_supported"
        )
        self.assertEqual(db.session.get(InterviewSession, session_id).status, "active")

        complete_key = "complete:human-review-001"
        completed = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/complete",
            headers=self._with_idempotency(self.owner_headers, complete_key),
            json={},
        )
        self.assertEqual(completed.status_code, 200)
        case = db.session.get(AssessmentCase, case_id)
        self.assertEqual(case.status, "awaiting_human_review")
        completed_body = completed.get_json()
        self.assertNotIn("decision", json.dumps(completed_body).lower())

        replay = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/complete",
            headers=self._with_idempotency(self.owner_headers, complete_key),
            json={},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["idempotency_replayed"])

        invalid_transition = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/complete",
            headers=self._with_idempotency(
                self.owner_headers, "complete:second-attempt"
            ),
            json={},
        )
        self.assertEqual(invalid_transition.status_code, 409)

    def test_capabilities_are_separate_and_tenant_scoped(self):
        program_id = self._published_program()
        _employee, create_headers = self._employee([INTERVIEW_CASES_CREATE])

        denied_program = self.client.post(
            "/api/v2/interviews/programs",
            headers=self._with_idempotency(create_headers, "program:employee01"),
            json={},
        )
        self.assertEqual(denied_program.status_code, 403)
        self.assertIn(
            "interviews.programs.manage",
            denied_program.get_json()["missing_capabilities"],
        )

        case_response = self._create_case(
            program_id,
            headers=create_headers,
            key="case:employee-create-001",
        )
        case_id = case_response.get_json()["case"]["id"]
        denied_read = self.client.get(
            f"/api/v2/interviews/cases/{case_id}",
            headers=create_headers,
        )
        self.assertEqual(denied_read.status_code, 403)
        self.assertIn(
            INTERVIEW_CASES_READ,
            denied_read.get_json()["missing_capabilities"],
        )

        _reader, reader_headers = self._employee([INTERVIEW_CASES_READ])
        allowed_read = self.client.get(
            f"/api/v2/interviews/cases/{case_id}",
            headers=reader_headers,
        )
        self.assertEqual(allowed_read.status_code, 200)

    def test_cross_tenant_case_is_not_disclosed(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]

        own_context_lookup = self.client.get(
            f"/api/v2/interviews/cases/{case_id}",
            headers=self.other_headers,
        )
        self.assertEqual(own_context_lookup.status_code, 404)
        self.assertEqual(
            own_context_lookup.get_json()["reason_code"], "assessment_case_not_found"
        )

        foreign_context_headers = {
            **self.other_headers,
            "X-Tenant-Slug": self.tenant.slug,
        }
        foreign_context_lookup = self.client.get(
            f"/api/v2/interviews/cases/{case_id}",
            headers=foreign_context_headers,
        )
        self.assertEqual(foreign_context_lookup.status_code, 403)

    def test_evidence_requires_active_session_opaque_refs_and_safe_provenance(self):
        program_id = self._published_program()
        case_id = self._create_case(program_id).get_json()["case"]["id"]
        session_id = self._create_session(case_id).get_json()["session"]["id"]
        evidence_payload = {
            "evidence_type": "audio",
            "source_channel": "whatsapp",
            "storage_ref": "storage:audioObjectAbc123",
            "content_sha256": "b" * 64,
            "provenance": {
                "provider": "meta",
                "source_channel": "whatsapp",
                "source_message_ref": "whatsapp:wamidAbc12345",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "mime_type": "audio/ogg",
                "transformation": "none",
            },
            "size_bytes": 4096,
        }

        inactive = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/evidence",
            headers=self._with_idempotency(self.owner_headers, "evidence:inactive1"),
            json=evidence_payload,
        )
        self.assertEqual(inactive.status_code, 409)
        self._start_session(session_id)

        raw_payload = dict(evidence_payload)
        raw_payload["transcript"] = "raw applicant answer"
        raw = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/evidence",
            headers=self._with_idempotency(self.owner_headers, "evidence:raw-data1"),
            json=raw_payload,
        )
        self.assertEqual(raw.status_code, 422)
        self.assertEqual(
            raw.get_json()["reason_code"],
            "interview_evidence_raw_content_forbidden",
        )

        credential_url = dict(evidence_payload)
        credential_url["storage_ref"] = "https://user:secret@example.com/object"
        unsafe_ref = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/evidence",
            headers=self._with_idempotency(self.owner_headers, "evidence:unsafe-ref"),
            json=credential_url,
        )
        self.assertEqual(unsafe_ref.status_code, 422)

        pii_provenance = dict(evidence_payload)
        pii_provenance["provenance"] = {
            **evidence_payload["provenance"],
            "applicant_email": "person@example.com",
        }
        unsafe_provenance = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/evidence",
            headers=self._with_idempotency(self.owner_headers, "evidence:unsafe-prov"),
            json=pii_provenance,
        )
        self.assertEqual(unsafe_provenance.status_code, 422)
        self.assertEqual(
            unsafe_provenance.get_json()["reason_code"],
            "interview_evidence_raw_content_forbidden",
        )

        future_provenance = dict(evidence_payload)
        future_provenance["provenance"] = {
            **evidence_payload["provenance"],
            "captured_at": (
                datetime.now(timezone.utc) + timedelta(hours=2)
            ).isoformat(),
        }
        future = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/evidence",
            headers=self._with_idempotency(self.owner_headers, "evidence:future-001"),
            json=future_provenance,
        )
        self.assertEqual(future.status_code, 422)
        self.assertEqual(
            future.get_json()["reason_code"],
            "interview_evidence_provenance_invalid",
        )

        key = "evidence:stable-replay-001"
        created = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/evidence",
            headers=self._with_idempotency(self.owner_headers, key),
            json=evidence_payload,
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        evidence_id = created.get_json()["evidence"]["id"]
        stored = db.session.get(InterviewEvidence, evidence_id)
        self.assertEqual(stored.content_sha256, "b" * 64)
        self.assertEqual(stored.provenance_json["provider"], "meta")

        audit = AuditEvent.query.filter_by(
            tenant_id=self.tenant.id,
            event_type="interview.evidence.created",
            resource_id=str(evidence_id),
        ).one()
        audit_json = json.dumps(audit.details, sort_keys=True)
        self.assertIn("b" * 64, audit_json)
        self.assertNotIn(evidence_payload["storage_ref"], audit_json)
        self.assertNotIn(
            evidence_payload["provenance"]["source_message_ref"], audit_json
        )
        self.assertNotIn("raw applicant answer", audit_json)

        replay = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/evidence",
            headers=self._with_idempotency(self.owner_headers, key),
            json=evidence_payload,
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["idempotency_replayed"])

        conflict_payload = dict(evidence_payload)
        conflict_payload["storage_ref"] = "storage:audioObjectDef456"
        conflict = self.client.post(
            f"/api/v2/interviews/sessions/{session_id}/evidence",
            headers=self._with_idempotency(self.owner_headers, key),
            json=conflict_payload,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.get_json()["reason_code"], "interview_idempotency_conflict"
        )

    def test_subject_reference_rejects_pii_like_and_object_values(self):
        program_id = self._published_program()
        for index, bad_ref in enumerate(
            [
                "https://example.com/student/1",
                "32877851",
                {"dni": "32877851"},
            ]
        ):
            response = self.client.post(
                "/api/v2/interviews/cases",
                headers=self._with_idempotency(
                    self.owner_headers, f"case:bad-ref-{index:02d}"
                ),
                json={
                    "program_id": program_id,
                    "subject_type": "student_applicant",
                    "subject_ref": bad_ref,
                    "source_channel": "web",
                },
            )
            self.assertEqual(response.status_code, 422)

    def test_audit_failure_rolls_back_the_domain_mutation(self):
        program_id = self._published_program()
        case_count = AssessmentCase.query.filter_by(tenant_id=self.tenant.id).count()
        audit_count = AuditEvent.query.filter_by(tenant_id=self.tenant.id).count()
        key = "case:audit-failure-001"
        with patch(
            "services.interview_service._audit",
            side_effect=RuntimeError("audit unavailable"),
        ):
            response = self.client.post(
                "/api/v2/interviews/cases",
                headers=self._with_idempotency(self.owner_headers, key),
                json={
                    "program_id": program_id,
                    "subject_type": "student_applicant",
                    "subject_ref": "student:auditRollbackAbc1",
                    "source_channel": "web",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["reason_code"], "interview_audit_commit_failed"
        )
        self.assertEqual(
            AssessmentCase.query.filter_by(tenant_id=self.tenant.id).count(),
            case_count,
        )
        self.assertEqual(
            AssessmentCase.query.filter_by(
                tenant_id=self.tenant.id, idempotency_key=key
            ).count(),
            0,
        )
        self.assertEqual(
            AuditEvent.query.filter_by(tenant_id=self.tenant.id).count(), audit_count
        )

    def test_unpublished_program_cannot_create_case(self):
        program_id = self._create_program().get_json()["program"]["id"]
        response = self.client.post(
            "/api/v2/interviews/cases",
            headers=self._with_idempotency(
                self.owner_headers, "case:unpublished-001"
            ),
            json={
                "program_id": program_id,
                "subject_type": "student_applicant",
                "subject_ref": "student:unpublishedAbc123",
                "source_channel": "web",
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["reason_code"], "assessment_program_not_published"
        )


if __name__ == "__main__":
    unittest.main()
