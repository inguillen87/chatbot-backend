import hashlib
import json
import time
import unittest
from datetime import datetime, timezone

from app import create_app, db
from config import TestingConfig
from models import AuditEvent, TenantProfile, User
from models_interviews import (
    AssessmentCase,
    AssessmentProgram,
    AssessmentProgramVersion,
    InterviewEvidence,
    InterviewSession,
)
from services.interview_access_policy import INTERVIEW_CASES_READ
from services.interview_service import (
    INTERVIEW_CONSENT_TEXT_FORMAT,
    INTERVIEW_CONSENT_TEXT_NORMALIZATION,
)
from utils.auth_helpers import generar_token


class InterviewInboxTestingConfig(TestingConfig):
    ENABLE_ASSESSMENT_INTERVIEWS_V1 = True


class TestInterviewInboxV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(InterviewInboxTestingConfig)

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
        self.session = self._seed_runtime(self.owner, self.tenant, active=True)
        self.foreign_session = self._seed_runtime(
            self.other_owner,
            self.other_tenant,
            active=False,
        )

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.ctx.pop()

    def _make_tenant(self, label):
        owner = User(
            name=f"{label} owner",
            email=f"inbox-{label}-{self.suffix}@chatboc.test",
            password_hash="hash",
            rol="admin",
            es_empleado=False,
            entity_token=f"inbox-{label}-{self.suffix}",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=f"inbox-{label}-{self.suffix}",
            nombre=f"Organizacion {label}",
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

    def _employee_headers(self, capabilities):
        employee = User(
            name="Operador entrevistas",
            email=f"inbox-employee-{time.time_ns()}@chatboc.test",
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
        db.session.add(employee)
        db.session.commit()
        token = generar_token(
            employee.id,
            employee.rol,
            "pyme",
            municipio_id=None,
            pyme_id=self.owner.id,
            extra_claims={
                "tenant_id": self.tenant.id,
                "tenant_slug": self.tenant.slug,
            },
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": self.tenant.slug,
        }

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _seed_runtime(self, owner, tenant, *, active):
        now = datetime.now(timezone.utc)
        definition = {
            "contract_version": "interview.definition.v1",
            "sections": [
                {
                    "id": "perfil",
                    "questions": [
                        {
                            "id": "motivacion",
                            "prompt": "Motivacion institucional",
                            "required": True,
                            "evidence_types": ["audio", "transcript"],
                        },
                        {
                            "id": "documentacion",
                            "prompt": "Documentacion de respaldo",
                            "required": True,
                            "evidence_types": ["image", "file"],
                        },
                    ],
                }
            ],
        }
        canonical = json.dumps(
            definition,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        consent_text = "Consentimiento institucional"
        consent_hash = self._digest(consent_text)
        program = AssessmentProgram(
            tenant_id=tenant.id,
            name="Ingreso institucional",
            description="Entrevista institucional",
            program_type="school_admission",
            status="active",
            published_version_number=1,
            created_by_user_id=owner.id,
            idempotency_key=f"program:{tenant.id}:{self.suffix}",
            request_hash=self._digest(f"program:{tenant.id}:{self.suffix}"),
        )
        db.session.add(program)
        db.session.flush()
        version = AssessmentProgramVersion(
            tenant_id=tenant.id,
            program_id=program.id,
            version_number=1,
            status="published",
            definition_json=definition,
            definition_hash=self._digest(canonical),
            consent_policy_version="consent.v1",
            consent_text=consent_text,
            consent_text_sha256=consent_hash,
            consent_text_format=INTERVIEW_CONSENT_TEXT_FORMAT,
            consent_text_normalization=INTERVIEW_CONSENT_TEXT_NORMALIZATION,
            created_by_user_id=owner.id,
            create_idempotency_key=f"version:create:{tenant.id}:{self.suffix}",
            create_request_hash=self._digest(f"version:create:{tenant.id}:{self.suffix}"),
            published_by_user_id=owner.id,
            published_at=now,
            publish_idempotency_key=f"version:publish:{tenant.id}:{self.suffix}",
            publish_request_hash=self._digest(f"version:publish:{tenant.id}:{self.suffix}"),
        )
        db.session.add(version)
        db.session.flush()
        case = AssessmentCase(
            tenant_id=tenant.id,
            program_id=program.id,
            program_version_id=version.id,
            subject_type="student_applicant",
            subject_ref=f"applicant:{tenant.id}:{self.suffix}",
            status="in_progress" if active else "scheduled",
            source_channel="web",
            created_by_user_id=owner.id,
            idempotency_key=f"case:{tenant.id}:{self.suffix}",
            request_hash=self._digest(f"case:{tenant.id}:{self.suffix}"),
        )
        db.session.add(case)
        db.session.flush()
        session = InterviewSession(
            tenant_id=tenant.id,
            assessment_case_id=case.id,
            program_version_id=version.id,
            status="active" if active else "scheduled",
            channel="web",
            interviewer_user_id=owner.id,
            scheduled_for=now,
            consent_granted=active,
            consent_policy_version="consent.v1" if active else None,
            consent_text_sha256=consent_hash if active else None,
            consent_recorded_at=now if active else None,
            consent_source="web" if active else None,
            consent_attestation_kind="operator_attestation" if active else None,
            consent_evidence_captured_at=now if active else None,
            consent_attested_by_user_id=owner.id if active else None,
            create_idempotency_key=f"session:{tenant.id}:{self.suffix}",
            create_request_hash=self._digest(f"session:{tenant.id}:{self.suffix}"),
            start_idempotency_key=(
                f"session:start:{tenant.id}:{self.suffix}" if active else None
            ),
            start_request_hash=(
                self._digest(f"session:start:{tenant.id}:{self.suffix}")
                if active
                else None
            ),
            started_at=now if active else None,
        )
        db.session.add(session)
        db.session.flush()
        db.session.add(
            AuditEvent(
                tenant_id=tenant.id,
                actor_user_id=owner.id,
                event_type="interview.session.created",
                resource_type="interview_session",
                resource_id=str(session.id),
                details={"raw_subject": "must-not-leak"},
                ip_address=None,
                created_at=now,
            )
        )
        if active:
            evidence = InterviewEvidence(
                tenant_id=tenant.id,
                interview_session_id=session.id,
                evidence_type="audio",
                source_channel="web",
                storage_ref="storage:private-audio-object",
                content_sha256=self._digest("audio-bytes"),
                provenance_json={
                    "provider": "internal",
                    "source_channel": "web",
                    "step_ref": "perfil.motivacion",
                },
                size_bytes=2048,
                created_by_user_id=owner.id,
                idempotency_key=f"evidence:{tenant.id}:{self.suffix}",
                request_hash=self._digest(f"evidence:{tenant.id}:{self.suffix}"),
                created_at=now,
            )
            db.session.add(evidence)
            db.session.flush()
            db.session.add_all(
                [
                    AuditEvent(
                        tenant_id=tenant.id,
                        actor_user_id=owner.id,
                        event_type="interview.session.started",
                        resource_type="interview_session",
                        resource_id=str(session.id),
                        details={"consent": True},
                        ip_address=None,
                        created_at=now,
                    ),
                    AuditEvent(
                        tenant_id=tenant.id,
                        actor_user_id=owner.id,
                        event_type="interview.evidence.created",
                        resource_type="interview_evidence",
                        resource_id=str(evidence.id),
                        details={"storage_ref": "must-not-leak"},
                        ip_address=None,
                        created_at=now,
                    ),
                ]
            )
        db.session.commit()
        return session

    def test_inbox_is_tenant_scoped_reconciled_and_redacted(self):
        response = self.client.get(
            "/api/v2/interviews/inbox",
            headers=self.owner_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        inbox = payload["inbox"]
        self.assertEqual(payload["contract_version"], "assessment.interviews.api.v1")
        self.assertEqual(inbox["contract_version"], "assessment.interviews.inbox.v1")
        self.assertEqual(inbox["tenant"]["id"], self.tenant.id)
        self.assertEqual(inbox["tenant"]["slug"], self.tenant.slug)
        self.assertEqual([item["id"] for item in inbox["items"]], [self.session.id])
        self.assertNotIn(self.foreign_session.id, [item["id"] for item in inbox["items"]])
        item = inbox["items"][0]
        self.assertEqual(item["progress"]["percent"], 50)
        self.assertEqual(item["progress"]["completed_required_steps"], 1)
        self.assertEqual(item["evidence"]["total"], 1)
        self.assertEqual(item["evidence"]["by_type"]["audio"], 1)
        self.assertEqual(item["evidence"]["content_hashes_present"], 1)
        self.assertFalse(item["evidence"]["references_exposed"])
        self.assertFalse(item["case"]["subject_reference_exposed"])
        self.assertNotIn("subject_ref", item["case"])
        self.assertNotIn("definition_hash", item["program"])
        self.assertEqual(item["audit"]["events_recorded"], 3)
        self.assertFalse(item["audit"]["sensitive_details_exposed"])
        self.assertEqual(inbox["summary"]["sessions"], 1)
        self.assertEqual(inbox["summary"]["evidence_records"], 1)
        self.assertEqual(inbox["summary"]["audit_events"], 3)
        self.assertTrue(item["actions"]["view_resume"]["enabled"])
        self.assertFalse(inbox["page"]["continuation_available"])
        self.assertIsNone(inbox["page"]["continuation_disabled_reason_code"])
        for action in ("assign", "review", "follow_up"):
            self.assertFalse(item["actions"][action]["enabled"])
            self.assertIsNone(item["actions"][action]["endpoint"])
            self.assertIsNone(item["actions"][action]["method"])
        serialized = json.dumps(payload)
        self.assertNotIn(f"applicant:{self.tenant.id}:{self.suffix}", serialized)
        self.assertNotIn("private-audio-object", serialized)
        self.assertNotIn("must-not-leak", serialized)

    def test_read_only_employee_cannot_open_resume_or_invent_review_actions(self):
        headers = self._employee_headers({INTERVIEW_CASES_READ})
        response = self.client.get("/api/v2/interviews/inbox", headers=headers)

        self.assertEqual(response.status_code, 200, response.get_json())
        inbox = response.get_json()["inbox"]
        self.assertTrue(inbox["capabilities"]["read_only"])
        self.assertFalse(inbox["capabilities"]["can_view_resume"])
        action = inbox["items"][0]["actions"]["view_resume"]
        self.assertFalse(action["enabled"])
        self.assertEqual(
            action["disabled_reason_code"],
            "interview_conduct_capability_required",
        )
        detail = self.client.get(
            f"/api/v2/interviews/sessions/{self.session.id}",
            headers=headers,
        )
        self.assertEqual(detail.status_code, 403, detail.get_json())

    def test_truncated_page_discloses_that_continuation_is_not_available(self):
        original_suffix = self.suffix
        try:
            self.suffix = f"{original_suffix}-extra"
            self._seed_runtime(self.owner, self.tenant, active=False)
        finally:
            self.suffix = original_suffix

        response = self.client.get(
            "/api/v2/interviews/inbox?limit=1",
            headers=self.owner_headers,
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        page = response.get_json()["inbox"]["page"]
        self.assertEqual(page["returned"], 1)
        self.assertTrue(page["has_more"])
        self.assertFalse(page["continuation_available"])
        self.assertEqual(
            page["continuation_disabled_reason_code"],
            "interview_inbox_cursor_not_implemented",
        )

    def test_inbox_rejects_missing_or_cross_tenant_scope(self):
        missing = self.client.get(
            "/api/v2/interviews/inbox",
            headers={"Authorization": self.owner_headers["Authorization"]},
        )
        self.assertEqual(missing.status_code, 400, missing.get_json())
        self.assertEqual(missing.get_json()["reason_code"], "interview_tenant_required")

        cross_headers = {
            **self.owner_headers,
            "X-Tenant-Slug": self.other_tenant.slug,
        }
        cross = self.client.get("/api/v2/interviews/inbox", headers=cross_headers)
        self.assertEqual(cross.status_code, 403, cross.get_json())
        self.assertEqual(cross.get_json()["reason_code"], "interview_tenant_forbidden")

    def test_inbox_filters_fail_closed_and_empty_state_is_truthful(self):
        bad_status = self.client.get(
            "/api/v2/interviews/inbox?status=approved",
            headers=self.owner_headers,
        )
        self.assertEqual(bad_status.status_code, 400, bad_status.get_json())
        self.assertEqual(
            bad_status.get_json()["reason_code"],
            "interview_inbox_status_invalid",
        )
        bad_limit = self.client.get(
            "/api/v2/interviews/inbox?limit=1000",
            headers=self.owner_headers,
        )
        self.assertEqual(bad_limit.status_code, 400, bad_limit.get_json())

        empty = self.client.get(
            "/api/v2/interviews/inbox?status=completed",
            headers=self.owner_headers,
        )
        self.assertEqual(empty.status_code, 200, empty.get_json())
        inbox = empty.get_json()["inbox"]
        self.assertEqual(inbox["items"], [])
        self.assertEqual(inbox["summary"]["sessions"], 0)
        self.assertEqual(inbox["page"]["returned"], 0)


if __name__ == "__main__":
    unittest.main()
