import json
import os
import time
import unittest
import uuid
from unittest.mock import patch

from app import create_app, db
from config import _env_strict_opt_in
from models import AuditEvent, User
from models_interviews import InterviewAssignment, InterviewSession
from services.interview_access_policy import (
    INTERVIEW_ASSIGNMENTS_MANAGE,
    INTERVIEW_CASES_READ,
    INTERVIEW_SESSIONS_CONDUCT,
)
from tests import test_interview_inbox_v2 as _inbox_test_helpers
from utils.auth_helpers import generar_token


class InterviewAssignmentsTestingConfig(
    _inbox_test_helpers.InterviewInboxTestingConfig
):
    ENABLE_INTERVIEW_ASSIGNMENTS_V1 = True


class TestInterviewAssignmentsV2(unittest.TestCase):
    _make_tenant = _inbox_test_helpers.TestInterviewInboxV2._make_tenant
    _digest = staticmethod(_inbox_test_helpers.TestInterviewInboxV2._digest)
    _seed_runtime = _inbox_test_helpers.TestInterviewInboxV2._seed_runtime

    @classmethod
    def setUpClass(cls):
        cls.app = create_app(InterviewAssignmentsTestingConfig)

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self.suffix = str(time.time_ns())
        self.owner, self.tenant, self.owner_headers = self._make_tenant("assignment")
        self.other_owner, self.other_tenant, self.other_headers = self._make_tenant(
            "assignment-foreign"
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

    def _employee(self, *, capabilities, tenant=None, owner=None, name="Entrevistador"):
        tenant = tenant or self.tenant
        owner = owner or self.owner
        user = User(
            name=name,
            email=f"assignment-{uuid.uuid4().hex}@chatboc.test",
            password_hash="hash",
            rol="empleado",
            es_empleado=True,
            empresa_id=owner.id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
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
            pyme_id=owner.id,
            extra_claims={"tenant_id": tenant.id, "tenant_slug": tenant.slug},
        )
        return user, {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": tenant.slug,
        }

    @staticmethod
    def _assignment_headers(headers, key):
        return {**headers, "Idempotency-Key": key}

    def _assign(self, assignee_id, expected_version, key, *, headers=None, reason=None):
        return self.client.post(
            f"/api/v2/interviews/sessions/{self.session.id}/assignment",
            headers=self._assignment_headers(headers or self.owner_headers, key),
            json={
                "assignee_user_id": assignee_id,
                "reason_code": reason or "initial_assignment",
                "expected_assignment_version": expected_version,
            },
        )

    def test_assignment_gate_is_strict_and_every_response_is_no_store(self):
        with patch.dict(os.environ, {"ENABLE_INTERVIEW_ASSIGNMENTS_V1": "true"}):
            self.assertTrue(_env_strict_opt_in("ENABLE_INTERVIEW_ASSIGNMENTS_V1"))
        with patch.dict(
            os.environ,
            {"ENABLE_INTERVIEW_ASSIGNMENTS_V1": "definitely"},
        ):
            self.assertFalse(_env_strict_opt_in("ENABLE_INTERVIEW_ASSIGNMENTS_V1"))

        self.app.config["ENABLE_INTERVIEW_ASSIGNMENTS_V1"] = False
        response = self._assign(self.owner.id, 0, "assignment:disabled-001")
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "interview_assignments_feature_disabled",
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Pragma"], "no-cache")

        inbox = self.client.get("/api/v2/interviews/inbox", headers=self.owner_headers)
        self.assertEqual(inbox.status_code, 200)
        self.assertEqual(
            inbox.get_json()["inbox"]["contract_version"],
            "assessment.interviews.inbox.v1",
        )
        self.app.config["ENABLE_INTERVIEW_ASSIGNMENTS_V1"] = True

    def test_candidates_are_minimal_and_match_assignment_eligibility(self):
        conductor, _headers = self._employee(
            capabilities={INTERVIEW_SESSIONS_CONDUCT},
            name="Ana Conductora",
        )
        no_conduct, _ = self._employee(
            capabilities={INTERVIEW_CASES_READ},
            name="Sin permiso",
        )
        global_admin = User(
            name="Global no asignable",
            email=f"global-{self.suffix}@chatboc.test",
            password_hash="hash",
            rol="superadmin",
        )
        db.session.add(global_admin)
        slug_only = User(
            name="Membresia por slug",
            email=f"slug-only-{uuid.uuid4().hex}@chatboc.test",
            password_hash="hash",
            rol="empleado",
            es_empleado=True,
            tenant_slug=f"  {self.tenant.slug.upper()}  ",
            accesibilidad={
                "employee_scope": {
                    "capabilities": [INTERVIEW_SESSIONS_CONDUCT]
                }
            },
        )
        db.session.add(slug_only)
        db.session.commit()

        response = self.client.get(
            "/api/v2/interviews/assignment-candidates",
            headers=self.owner_headers,
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        roster = body["assignment_candidates"]
        self.assertEqual(
            roster["contract_version"],
            "assessment.interviews.assignment_candidates.v1",
        )
        ids = {item["user_id"] for item in roster["candidates"]}
        self.assertIn(self.owner.id, ids)
        self.assertIn(conductor.id, ids)
        self.assertIn(slug_only.id, ids)
        self.assertNotIn(no_conduct.id, ids)
        self.assertNotIn(global_admin.id, ids)
        self.assertNotIn(self.other_owner.id, ids)
        for item in roster["candidates"]:
            self.assertEqual(
                set(item),
                {"user_id", "display_name", "role_label", "can_conduct"},
            )
            self.assertTrue(item["display_name"])
            self.assertTrue(item["can_conduct"])

        _reader, reader_headers = self._employee(
            capabilities={INTERVIEW_CASES_READ},
        )
        denied = self.client.get(
            "/api/v2/interviews/assignment-candidates",
            headers=reader_headers,
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            denied.get_json()["reason_code"],
            "interview_assignment_capability_required",
        )

    def test_append_only_versions_replay_stale_guard_and_inbox_contract(self):
        conductor, _ = self._employee(
            capabilities={INTERVIEW_SESSIONS_CONDUCT},
            name="Lucia Entrevistadora",
        )

        baseline_key = "assignment:baseline-001"
        baseline = self._assign(self.owner.id, 0, baseline_key)
        self.assertEqual(baseline.status_code, 201, baseline.get_json())
        baseline_receipt = baseline.get_json()["assignment"]
        self.assertEqual(
            baseline.get_json()["contract_version"],
            "assessment.interviews.assignment.v1",
        )
        self.assertEqual(baseline_receipt["contract_version"], "interview.assignment.v1")
        self.assertEqual(baseline_receipt["version"], 1)
        self.assertEqual(baseline_receipt["previous_assignee_user_id"], self.owner.id)
        self.assertIsNone(baseline_receipt["supersedes_assignment_id"])

        second_key = "assignment:second-001"
        second = self._assign(
            conductor.id,
            1,
            second_key,
            reason="workload_balance",
        )
        self.assertEqual(second.status_code, 201, second.get_json())
        second_receipt = second.get_json()["assignment"]
        self.assertEqual(second_receipt["version"], 2)
        self.assertEqual(second_receipt["previous_assignee_user_id"], self.owner.id)
        self.assertEqual(
            second_receipt["supersedes_assignment_id"],
            baseline_receipt["id"],
        )
        self.assertEqual(
            db.session.get(InterviewSession, self.session.id).interviewer_user_id,
            conductor.id,
        )

        replay = self._assign(self.owner.id, 0, baseline_key)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["idempotency_replayed"])
        self.assertEqual(replay.get_json()["assignment"], baseline_receipt)
        self.assertEqual(
            db.session.get(InterviewSession, self.session.id).interviewer_user_id,
            conductor.id,
        )

        conflicting_reuse = self._assign(
            conductor.id,
            0,
            baseline_key,
            reason="availability",
        )
        self.assertEqual(conflicting_reuse.status_code, 409)
        self.assertEqual(
            conflicting_reuse.get_json()["reason_code"],
            "interview_idempotency_conflict",
        )
        self.assertEqual(
            db.session.get(InterviewSession, self.session.id).interviewer_user_id,
            conductor.id,
        )

        stale = self._assign(
            self.owner.id,
            1,
            "assignment:stale-001",
            reason="continuity",
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(
            stale.get_json()["reason_code"],
            "interview_assignment_version_conflict",
        )
        self.assertEqual(stale.get_json()["current_assignment_version"], 2)

        unchanged = self._assign(
            conductor.id,
            2,
            "assignment:unchanged-001",
            reason="continuity",
        )
        self.assertEqual(unchanged.status_code, 409)
        self.assertEqual(
            unchanged.get_json()["reason_code"],
            "interview_assignment_unchanged",
        )
        rows = InterviewAssignment.query.filter_by(
            tenant_id=self.tenant.id,
            interview_session_id=self.session.id,
        ).order_by(InterviewAssignment.version.asc()).all()
        self.assertEqual([item.version for item in rows], [1, 2])

        changed_audits = AuditEvent.query.filter_by(
            tenant_id=self.tenant.id,
            event_type="interview.assignment.changed",
        ).all()
        baseline_audits = AuditEvent.query.filter_by(
            tenant_id=self.tenant.id,
            event_type="interview.assignment.baselined",
        ).all()
        self.assertEqual(len(changed_audits), 1)
        self.assertEqual(len(baseline_audits), 1)
        self.assertEqual(
            baseline_audits[0].details["change_kind"],
            "baseline_materialized",
        )
        self.assertEqual(
            changed_audits[0].details["change_kind"],
            "assignee_changed",
        )
        serialized_audit = json.dumps(
            [event.details for event in baseline_audits + changed_audits]
        )
        self.assertNotIn(conductor.email, serialized_audit)
        self.assertNotIn(conductor.name, serialized_audit)
        self.assertIn('"free_text_persisted": false', serialized_audit)

        inbox_response = self.client.get(
            "/api/v2/interviews/inbox",
            headers=self.owner_headers,
        )
        self.assertEqual(inbox_response.status_code, 200, inbox_response.get_json())
        inbox = inbox_response.get_json()["inbox"]
        self.assertEqual(inbox["contract_version"], "assessment.interviews.inbox.v2")
        self.assertTrue(inbox["capabilities"]["can_assign"])
        self.assertTrue(inbox["governance"]["assignment_workflow_persisted"])
        item = next(value for value in inbox["items"] if value["id"] == self.session.id)
        self.assertEqual(item["assignment"]["version"], 2)
        self.assertEqual(item["assignment"]["assigned_user_label"], conductor.name)
        self.assertEqual(item["actions"]["assign"]["method"], "POST")
        self.assertEqual(
            item["actions"]["assign"]["candidates"],
            {
                "method": "GET",
                "endpoint": "/api/v2/interviews/assignment-candidates",
            },
        )
        self.assertEqual(
            item["audit"]["last_event"]["event_type"],
            "interview.assignment.changed",
        )

    def test_tenant_capability_assignee_and_audit_fail_closed(self):
        conductor, _ = self._employee(
            capabilities={INTERVIEW_SESSIONS_CONDUCT},
        )
        no_conduct, _ = self._employee(capabilities={INTERVIEW_CASES_READ})
        manager, manager_headers = self._employee(
            capabilities={INTERVIEW_ASSIGNMENTS_MANAGE},
            name="Gestor",
        )
        _ = manager

        no_capability = self._assign(
            conductor.id,
            0,
            "assignment:no-manager-capability",
            headers=self._employee(capabilities={INTERVIEW_CASES_READ})[1],
        )
        self.assertEqual(no_capability.status_code, 403)

        for index, assignee_id in enumerate(
            (no_conduct.id, self.other_owner.id, 99999999)
        ):
            denied = self._assign(
                assignee_id,
                0,
                f"assignment:bad-assignee-{index}",
                headers=manager_headers,
            )
            self.assertEqual(denied.status_code, 422, denied.get_json())
            self.assertEqual(
                denied.get_json()["reason_code"],
                "interview_assignment_assignee_ineligible",
            )

        foreign = self.client.post(
            f"/api/v2/interviews/sessions/{self.foreign_session.id}/assignment",
            headers=self._assignment_headers(
                manager_headers,
                "assignment:foreign-session",
            ),
            json={
                "assignee_user_id": conductor.id,
                "reason_code": "initial_assignment",
                "expected_assignment_version": 0,
            },
        )
        self.assertEqual(foreign.status_code, 404, foreign.get_json())

        original_interviewer = self.session.interviewer_user_id
        with patch(
            "services.interview_service._audit",
            side_effect=RuntimeError("audit unavailable"),
        ):
            failed = self._assign(
                conductor.id,
                0,
                "assignment:audit-rollback",
                headers=manager_headers,
            )
        self.assertEqual(failed.status_code, 503, failed.get_json())
        self.assertEqual(
            InterviewAssignment.query.filter_by(
                tenant_id=self.tenant.id,
                interview_session_id=self.session.id,
            ).count(),
            0,
        )
        self.assertEqual(
            db.session.get(InterviewSession, self.session.id).interviewer_user_id,
            original_interviewer,
        )

    def test_first_managed_write_that_changes_legacy_owner_is_a_change(self):
        conductor, _ = self._employee(
            capabilities={INTERVIEW_SESSIONS_CONDUCT},
            name="Primera reasignacion",
        )

        response = self._assign(
            conductor.id,
            0,
            "assignment:first-managed-change",
            reason="availability",
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        receipt = response.get_json()["assignment"]
        self.assertEqual(receipt["version"], 1)
        self.assertEqual(receipt["previous_assignee_user_id"], self.owner.id)
        changed = AuditEvent.query.filter_by(
            tenant_id=self.tenant.id,
            event_type="interview.assignment.changed",
        ).one()
        self.assertEqual(changed.details["change_kind"], "assignee_changed")
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant.id,
                event_type="interview.assignment.baselined",
            ).count(),
            0,
        )

    def test_completed_and_void_sessions_cannot_be_reassigned(self):
        conductor, _ = self._employee(
            capabilities={INTERVIEW_SESSIONS_CONDUCT},
        )

        for status in ("completed", "void"):
            self.session.status = status
            db.session.commit()
            denied = self._assign(
                conductor.id,
                0,
                f"assignment:closed-{status}",
            )
            self.assertEqual(denied.status_code, 409, denied.get_json())
            self.assertEqual(
                denied.get_json()["reason_code"],
                "interview_assignment_session_closed",
            )

        self.assertEqual(
            InterviewAssignment.query.filter_by(
                tenant_id=self.tenant.id,
                interview_session_id=self.session.id,
            ).count(),
            0,
        )
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant.id,
                event_type="interview.assignment.changed",
            ).count(),
            0,
        )

        inbox = self.client.get(
            "/api/v2/interviews/inbox",
            headers=self.owner_headers,
        ).get_json()["inbox"]
        item = next(value for value in inbox["items"] if value["id"] == self.session.id)
        self.assertTrue(inbox["capabilities"]["can_assign"])
        self.assertEqual(
            item["actions"]["assign"],
            {
                "action_id": "assign",
                "label": "Asignar responsable",
                "enabled": False,
                "method": None,
                "endpoint": None,
                "disabled_reason_code": "interview_assignment_session_closed",
            },
        )


if __name__ == "__main__":
    unittest.main()
