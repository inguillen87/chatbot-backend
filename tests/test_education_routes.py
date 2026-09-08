import json
import unittest
import time
from unittest.mock import patch

from app import create_app, db
from config import TestingConfig
from models import AuditEvent, PymeTicket, TenantProfile, User
from models_education import (
    AcademicLevel,
    Campus,
    FamilyVerificationAttempt,
    Guardian,
    School,
    Shift,
    Student,
    StudentGuardianRelation,
    CourseSection,
)
from services.education_case_service import create_school_case_alias_for_ticket
from services.education_access_policy import EDUCATION_GUARDIANS_READ
from utils.auth_helpers import generar_token


class TestEducationRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestingConfig)

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()

        suffix = int(time.time() * 1000)
        self.owner = User(email=f"edu-owner-{suffix}@chatboc.ar", password_hash="hash", es_empleado=False, name="Edu Owner")
        db.session.add(self.owner)
        db.session.commit()

        self.tenant = TenantProfile(
            slug=f"edu-tenant-{suffix}",
            nombre="Escuela Demo",
            tipo="pyme",
            pyme_id=self.owner.id,
            plan="full",
            vertical="educacion",
            subvertical="colegio_privado",
            capabilities_json={"education": {"enabled": True}},
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.school = School(tenant_id=self.tenant.id, name="Colegio Demo", school_type="private")
        db.session.add(self.school)
        db.session.flush()

        self.campus = Campus(school_id=self.school.id, name="Sede Centro", address="Calle Falsa 123", is_main=True)
        db.session.add(self.campus)
        db.session.flush()

        self.level = AcademicLevel(school_id=self.school.id, code="primaria", name="Primaria")
        self.shift = Shift(school_id=self.school.id, code="manana", name="Mañana")
        db.session.add_all([self.level, self.shift])
        db.session.flush()

        self.section = CourseSection(
            campus_id=self.campus.id,
            academic_year=2026,
            level_id=self.level.id,
            grade="5",
            division="A",
            shift_id=self.shift.id,
        )
        db.session.add(self.section)
        db.session.flush()

        self.student = Student(
            school_id=self.school.id,
            campus_id=self.campus.id,
            section_id=self.section.id,
            first_name="Ana",
            last_name="Pérez",
            document_number="40111222",
            status="active",
        )
        self.guardian = Guardian(
            tenant_id=self.tenant.id,
            school_id=self.school.id,
            first_name="Laura",
            last_name="Pérez",
            phone_number="+5492615000001",
            email="laura@example.com",
            document_number="25111222",
            verification_status="pending",
        )
        db.session.add_all([self.student, self.guardian])
        db.session.flush()

        self.relation = StudentGuardianRelation(
            student_id=self.student.id,
            guardian_id=self.guardian.id,
            relationship_type="madre",
            can_receive_sensitive_updates=True,
            status="active",
        )
        db.session.add(self.relation)
        db.session.commit()

        self.token = generar_token(self.owner.id, "admin", "pyme", municipio_id=None, pyme_id=self.owner.id)
        self.auth_header = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.ctx.pop()

    def test_list_campuses_and_sections(self):
        school_resp = self.client.get(
            f"/api/v1/education/schools/{self.school.id}",
            headers=self.auth_header,
        )
        self.assertEqual(school_resp.status_code, 200)
        school_data = school_resp.get_json()
        self.assertEqual(school_data["id"], self.school.id)
        self.assertEqual(school_data["counts"]["campuses"], 1)

        nested_campuses_resp = self.client.get(
            f"/api/v1/education/schools/{self.school.id}/campuses",
            headers=self.auth_header,
        )
        self.assertEqual(nested_campuses_resp.status_code, 200)
        self.assertEqual(nested_campuses_resp.get_json()[0]["name"], "Sede Centro")

        nested_sections_resp = self.client.get(
            f"/api/v1/education/schools/{self.school.id}/sections",
            headers=self.auth_header,
        )
        self.assertEqual(nested_sections_resp.status_code, 200)
        self.assertEqual(nested_sections_resp.get_json()[0]["school_id"], self.school.id)

        campuses_resp = self.client.get(
            f"/api/v1/education/campuses?school_id={self.school.id}",
            headers=self.auth_header,
        )
        self.assertEqual(campuses_resp.status_code, 200)
        campuses = campuses_resp.get_json()
        self.assertEqual(len(campuses), 1)
        self.assertEqual(campuses[0]["name"], "Sede Centro")

        sections_resp = self.client.get(
            f"/api/v1/education/sections?campus_id={self.campus.id}",
            headers=self.auth_header,
        )
        self.assertEqual(sections_resp.status_code, 200)
        sections = sections_resp.get_json()
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["grade"], "5")

    def test_tenant_capabilities_and_taxonomy_endpoints(self):
        caps_resp = self.client.get(
            "/api/v1/education/tenant/capabilities",
            headers=self.auth_header,
        )
        self.assertEqual(caps_resp.status_code, 200)
        caps_data = caps_resp.get_json()
        self.assertTrue(caps_data["education_enabled"])
        self.assertTrue((caps_data.get("education_profile") or {}).get("is_education"))
        self.assertEqual((caps_data.get("whatsapp_playbook") or {}).get("contract_version"), "education.whatsapp_playbook.v1")

        update_resp = self.client.put(
            "/api/v1/education/tenant/capabilities",
            headers=self.auth_header,
            json={
                "education_enabled": True,
                "subvertical": "colegio_privado",
                "modules": ["inbox_escolar", "family_context"],
            },
        )
        self.assertEqual(update_resp.status_code, 200)
        update_data = update_resp.get_json()
        self.assertTrue(update_data["education_enabled"])
        self.assertEqual(update_data["vertical"], "educacion")

        taxonomy_resp = self.client.get(
            "/api/v1/education/cases/taxonomy",
            headers=self.auth_header,
        )
        self.assertEqual(taxonomy_resp.status_code, 200)
        taxonomy_data = taxonomy_resp.get_json()
        keys = {item["key"] for item in taxonomy_data["taxonomy"]}
        self.assertIn("documentacion", keys)
        self.assertIn("secretaria", keys)

        admin_menu_resp = self.client.get(
            "/api/v1/education/admin/menu",
            headers=self.auth_header,
        )
        self.assertEqual(admin_menu_resp.status_code, 200)
        admin_menu = admin_menu_resp.get_json()
        self.assertEqual(admin_menu.get("contract_version"), "education.admin_menu.v1")
        section_ids = {item.get("id") for item in admin_menu.get("panel_sections") or []}
        self.assertIn("whatsapp_school", section_ids)
        overview = next(item for item in admin_menu.get("panel_sections") or [] if item.get("id") == "education_overview")
        self.assertEqual(overview.get("endpoint"), "/api/v1/education/operations/summary")

        playbook_resp = self.client.get(
            "/api/v1/education/whatsapp/playbook",
            headers=self.auth_header,
        )
        self.assertEqual(playbook_resp.status_code, 200)
        playbook = playbook_resp.get_json()
        intents = {item.get("intent") for item in playbook.get("quick_menu") or []}
        self.assertIn("justificar_inasistencia", intents)

    def test_create_school_campus_and_section_endpoints(self):
        school_resp = self.client.post(
            "/api/v1/education/schools",
            headers=self.auth_header,
            json={"name": "Colegio Norte", "school_type": "private"},
        )
        self.assertEqual(school_resp.status_code, 201)
        school_id = school_resp.get_json()["id"]

        campus_resp = self.client.post(
            "/api/v1/education/campuses",
            headers=self.auth_header,
            json={"school_id": school_id, "name": "Sede Norte"},
        )
        self.assertEqual(campus_resp.status_code, 201)
        campus_id = campus_resp.get_json()["id"]

        level_resp = self.client.post(
            "/api/v1/education/levels",
            headers=self.auth_header,
            json={"school_id": school_id, "code": "secundaria", "name": "Secundaria"},
        )
        self.assertEqual(level_resp.status_code, 201)
        level_id = level_resp.get_json()["id"]

        shift_resp = self.client.post(
            "/api/v1/education/shifts",
            headers=self.auth_header,
            json={"school_id": school_id, "code": "tarde", "name": "Tarde"},
        )
        self.assertEqual(shift_resp.status_code, 201)
        shift_id = shift_resp.get_json()["id"]

        levels_list_resp = self.client.get(
            f"/api/v1/education/levels?school_id={school_id}",
            headers=self.auth_header,
        )
        self.assertEqual(levels_list_resp.status_code, 200)
        self.assertEqual(len(levels_list_resp.get_json()), 1)

        shifts_list_resp = self.client.get(
            f"/api/v1/education/shifts?school_id={school_id}",
            headers=self.auth_header,
        )
        self.assertEqual(shifts_list_resp.status_code, 200)
        self.assertEqual(len(shifts_list_resp.get_json()), 1)

        section_resp = self.client.post(
            "/api/v1/education/sections",
            headers=self.auth_header,
            json={
                "campus_id": campus_id,
                "academic_year": 2026,
                "level_id": level_id,
                "grade": "2",
                "division": "B",
                "shift_id": shift_id,
            },
        )
        self.assertEqual(section_resp.status_code, 201)

    def test_guardian_lookup_verify_and_family_context(self):
        lookup_resp = self.client.post(
            "/api/v1/education/guardians/lookup",
            headers=self.auth_header,
            json={"tenant_id": self.tenant.id, "phone_number": self.guardian.phone_number},
        )
        self.assertEqual(lookup_resp.status_code, 200)
        self.assertEqual(lookup_resp.get_json()["verification_status"], "pending")
        self.assertEqual(lookup_resp.get_json()["lookup_scope"], "operator")

        legacy_lookup_resp = self.client.post(
            "/api/v1/education/guardian/lookup",
            headers=self.auth_header,
            json={"tenant_id": self.tenant.id, "phone_number": self.guardian.phone_number},
        )
        self.assertEqual(legacy_lookup_resp.status_code, 200)

        evidence_ref = "family-record-2026-001"
        verify_resp = self.client.post(
            "/api/v1/education/guardians/verify",
            headers=self.auth_header,
            json={
                "tenant_id": self.tenant.id,
                "guardian_id": self.guardian.id,
                "verification_method": "institutional_record",
                "evidence_ref": evidence_ref,
            },
        )
        self.assertEqual(verify_resp.status_code, 200)
        self.assertEqual(verify_resp.get_json()["verification_status"], "verified")
        self.assertFalse(verify_resp.get_json()["verification"]["phone_ownership_verified"])

        attempts = FamilyVerificationAttempt.query.filter_by(tenant_id=self.tenant.id).all()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "verified")
        self.assertNotIn(evidence_ref, attempts[0].verification_value)
        self.assertNotIn(evidence_ref, json.dumps(self.guardian.verification_context))
        self.assertFalse(self.guardian.verification_context["phone_ownership_verified"])

        link_resp = self.client.post(
            "/api/v1/education/guardians/link-student",
            headers=self.auth_header,
            json={
                "tenant_id": self.tenant.id,
                "guardian_id": self.guardian.id,
                "student_id": self.student.id,
                "relationship_type": "madre",
                "can_receive_sensitive_updates": True,
            },
        )
        self.assertIn(link_resp.status_code, {200, 201})
        link_payload = link_resp.get_json()
        self.assertEqual(link_payload["student"]["id"], self.student.id)
        self.assertTrue(link_payload["relationship"]["can_receive_sensitive_updates"])

        audit_details = json.dumps(
            [
                event.details
                for event in AuditEvent.query.filter(
                    AuditEvent.tenant_id == self.tenant.id,
                    AuditEvent.event_type.like("education.guardian.%"),
                ).all()
            ],
            sort_keys=True,
        )
        self.assertNotIn(self.guardian.phone_number, audit_details)
        self.assertNotIn(self.guardian.document_number, audit_details)
        self.assertNotIn(evidence_ref, audit_details)

        family_resp = self.client.get(
            f"/api/v1/education/family/context?guardian_id={self.guardian.id}",
            headers=self.auth_header,
        )
        self.assertEqual(family_resp.status_code, 200)
        data = family_resp.get_json()
        self.assertEqual(len(data["students"]), 1)
        self.assertTrue(data["students"][0]["can_receive_sensitive_updates"])

        self.guardian.user_id = self.owner.id
        db.session.add(self.guardian)
        db.session.commit()

        me_family_resp = self.client.get(
            "/api/v1/education/me/family-context",
            headers=self.auth_header,
        )
        self.assertEqual(me_family_resp.status_code, 200)
        self.assertEqual(me_family_resp.get_json()["guardian"]["id"], self.guardian.id)

    def test_verify_guardian_rejects_unknown_tenant(self):
        lookup_response = self.client.post(
            "/api/v1/education/guardians/lookup",
            headers=self.auth_header,
            json={"tenant_id": 999999, "phone_number": self.guardian.phone_number},
        )
        response = self.client.post(
            "/api/v1/education/guardian/verify",
            headers=self.auth_header,
            json={
                "tenant_id": 999999,
                "guardian_id": self.guardian.id,
                "verification_method": "institutional_record",
                "evidence_ref": "family-record-2026-foreign",
            },
        )
        self.assertEqual(lookup_response.status_code, 403)
        self.assertEqual(
            lookup_response.get_json()["reason_code"],
            "education_cross_tenant_denied",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason_code"], "education_cross_tenant_denied")
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant.id,
                event_type="education.guardian.lookup",
            ).count(),
            0,
        )

    def test_guardian_operations_reject_anonymous_requests_without_mutation(self):
        lookup_response = self.client.post(
            "/api/v1/education/guardians/lookup",
            json={"tenant_id": self.tenant.id, "phone_number": self.guardian.phone_number},
        )
        verify_response = self.client.post(
            "/api/v1/education/guardians/verify",
            json={
                "tenant_id": self.tenant.id,
                "guardian_id": self.guardian.id,
                "verification_method": "institutional_record",
                "evidence_ref": "anonymous-proof-attempt",
            },
        )
        link_response = self.client.post(
            "/api/v1/education/guardians/link-student",
            json={
                "tenant_id": self.tenant.id,
                "guardian_id": self.guardian.id,
                "student_id": self.student.id,
                "can_receive_sensitive_updates": False,
            },
        )

        self.assertEqual(lookup_response.status_code, 401)
        self.assertEqual(verify_response.status_code, 401)
        self.assertEqual(link_response.status_code, 401)
        db.session.refresh(self.guardian)
        db.session.refresh(self.relation)
        self.assertEqual(self.guardian.verification_status, "pending")
        self.assertTrue(self.relation.can_receive_sensitive_updates)
        self.assertEqual(
            FamilyVerificationAttempt.query.filter_by(tenant_id=self.tenant.id).count(),
            0,
        )

    def test_guardian_phone_only_verification_fails_closed(self):
        response = self.client.post(
            "/api/v1/education/guardians/verify",
            headers=self.auth_header,
            json={"tenant_id": self.tenant.id, "phone_number": self.guardian.phone_number},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason_code"], "education_guardian_proof_required")
        db.session.refresh(self.guardian)
        self.assertEqual(self.guardian.verification_status, "pending")
        self.assertEqual(
            FamilyVerificationAttempt.query.filter_by(tenant_id=self.tenant.id).count(),
            0,
        )

    def test_guardian_link_requires_verified_profile(self):
        response = self.client.post(
            "/api/v1/education/guardians/link-student",
            headers=self.auth_header,
            json={
                "tenant_id": self.tenant.id,
                "guardian_id": self.guardian.id,
                "student_id": self.student.id,
                "can_receive_sensitive_updates": False,
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["error"]["message"],
            "Guardian must be verified before linking students",
        )
        db.session.refresh(self.relation)
        self.assertTrue(self.relation.can_receive_sensitive_updates)

    def test_guardian_verification_rolls_back_when_audit_commit_fails(self):
        with patch.object(
            db.session,
            "commit",
            side_effect=RuntimeError("simulated durable audit failure"),
        ):
            response = self.client.post(
                "/api/v1/education/guardians/verify",
                headers=self.auth_header,
                json={
                    "tenant_id": self.tenant.id,
                    "guardian_id": self.guardian.id,
                    "verification_method": "institutional_record",
                    "evidence_ref": "audit-failure-proof",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["reason_code"],
            "education_guardian_audit_failed",
        )
        db.session.expire_all()
        guardian = db.session.get(Guardian, self.guardian.id)
        self.assertEqual(guardian.verification_status, "pending")
        self.assertEqual(
            FamilyVerificationAttempt.query.filter_by(tenant_id=self.tenant.id).count(),
            0,
        )
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant.id,
                event_type="education.guardian.verified",
            ).count(),
            0,
        )

    def test_guardian_link_rolls_back_when_audit_commit_fails(self):
        self.guardian.verification_status = "verified"
        self.guardian.verification_context = {
            "source": "institutional_attestation",
            "phone_ownership_verified": False,
        }
        db.session.add(self.guardian)
        db.session.commit()

        with patch.object(
            db.session,
            "commit",
            side_effect=RuntimeError("simulated durable audit failure"),
        ):
            response = self.client.post(
                "/api/v1/education/guardians/link-student",
                headers=self.auth_header,
                json={
                    "tenant_id": self.tenant.id,
                    "guardian_id": self.guardian.id,
                    "student_id": self.student.id,
                    "can_receive_sensitive_updates": False,
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json()["reason_code"],
            "education_guardian_audit_failed",
        )
        db.session.expire_all()
        relation = db.session.get(StudentGuardianRelation, self.relation.id)
        guardian = db.session.get(Guardian, self.guardian.id)
        self.assertTrue(relation.can_receive_sensitive_updates)
        self.assertEqual(guardian.verification_status, "verified")
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant.id,
                event_type="education.guardian.student_linked",
            ).count(),
            0,
        )

    def test_guardian_employee_capabilities_are_operation_specific(self):
        suffix = int(time.time() * 1000000)
        employee = User(
            email=f"edu-employee-{suffix}@chatboc.ar",
            password_hash="hash",
            name="Education Reader",
            rol="empleado",
            es_empleado=True,
            tenant_id=self.tenant.id,
            accesibilidad={},
        )
        db.session.add(employee)
        db.session.commit()
        token = generar_token(
            employee.id,
            "empleado",
            "pyme",
            municipio_id=None,
            pyme_id=self.owner.id,
        )
        headers = {"Authorization": f"Bearer {token}"}

        denied_lookup_response = self.client.post(
            "/api/v1/education/guardians/lookup",
            headers=headers,
            json={"tenant_id": self.tenant.id, "guardian_id": self.guardian.id},
        )
        self.assertEqual(denied_lookup_response.status_code, 403)
        self.assertIn(
            EDUCATION_GUARDIANS_READ,
            denied_lookup_response.get_json()["missing_capabilities"],
        )

        employee.accesibilidad = {
            "employee_scope": {"permissions": [EDUCATION_GUARDIANS_READ]}
        }
        db.session.add(employee)
        db.session.commit()

        lookup_response = self.client.post(
            "/api/v1/education/guardians/lookup",
            headers=headers,
            json={"tenant_id": self.tenant.id, "guardian_id": self.guardian.id},
        )
        verify_response = self.client.post(
            "/api/v1/education/guardians/verify",
            headers=headers,
            json={
                "tenant_id": self.tenant.id,
                "guardian_id": self.guardian.id,
                "verification_method": "institutional_record",
                "evidence_ref": "employee-proof-attempt",
            },
        )
        link_response = self.client.post(
            "/api/v1/education/guardians/link-student",
            headers=headers,
            json={
                "tenant_id": self.tenant.id,
                "guardian_id": self.guardian.id,
                "student_id": self.student.id,
            },
        )

        self.assertEqual(lookup_response.status_code, 200)
        self.assertEqual(lookup_response.get_json()["lookup_scope"], "operator")
        self.assertEqual(verify_response.status_code, 403)
        self.assertIn(
            "education.guardians.verify",
            verify_response.get_json()["missing_capabilities"],
        )
        self.assertEqual(link_response.status_code, 403)
        self.assertIn(
            "education.guardians.link",
            link_response.get_json()["missing_capabilities"],
        )

    def test_guardian_family_lookup_is_self_scoped_and_non_enumerable(self):
        suffix = int(time.time() * 1000000)
        family = User(
            email=f"edu-family-{suffix}@chatboc.ar",
            password_hash="hash",
            name="Guardian Family",
            rol="usuario",
            es_empleado=False,
            tenant_id=self.tenant.id,
        )
        unlinked_family = User(
            email=f"edu-family-unlinked-{suffix}@chatboc.ar",
            password_hash="hash",
            name="Unlinked Family",
            rol="usuario",
            es_empleado=False,
            tenant_id=self.tenant.id,
        )
        db.session.add_all([family, unlinked_family])
        db.session.flush()
        self.guardian.user_id = family.id
        db.session.add(self.guardian)
        db.session.commit()

        family_token = generar_token(
            family.id, "usuario", "pyme", municipio_id=None, pyme_id=self.owner.id
        )
        family_response = self.client.post(
            "/api/v1/education/guardians/lookup",
            headers={"Authorization": f"Bearer {family_token}"},
            json={
                "tenant_id": self.tenant.id,
                "phone_number": "+5499999999999",
                "document_number": "does-not-match",
            },
        )
        self.assertEqual(family_response.status_code, 200)
        self.assertEqual(family_response.get_json()["id"], self.guardian.id)
        self.assertEqual(family_response.get_json()["lookup_scope"], "self")

        unlinked_token = generar_token(
            unlinked_family.id,
            "usuario",
            "pyme",
            municipio_id=None,
            pyme_id=self.owner.id,
        )
        unlinked_response = self.client.post(
            "/api/v1/education/guardians/lookup",
            headers={"Authorization": f"Bearer {unlinked_token}"},
            json={
                "tenant_id": self.tenant.id,
                "phone_number": self.guardian.phone_number,
            },
        )
        self.assertEqual(unlinked_response.status_code, 404)
        self.assertEqual(
            unlinked_response.get_json()["reason_code"],
            "education_guardian_profile_unavailable",
        )

    def test_family_context_blocks_same_and_cross_tenant_idor_and_identity_fallback(self):
        suffix = int(time.time() * 1000000)
        family = User(
            email=f"edu-context-family-{suffix}@chatboc.ar",
            password_hash="hash",
            name="Linked Family",
            rol="usuario",
            tenant_id=self.tenant.id,
        )
        # This email intentionally matches the guardian record. It must not
        # become identity proof without an explicit Guardian.user_id link.
        unlinked = User(
            email=self.guardian.email,
            password_hash="hash",
            name="Unlinked Matching Email",
            rol="usuario",
            tenant_id=self.tenant.id,
        )
        same_tenant_guardian = Guardian(
            tenant_id=self.tenant.id,
            school_id=self.school.id,
            first_name="Other",
            last_name="Guardian",
            phone_number="+5492615999991",
            email=f"other-guardian-{suffix}@example.com",
            verification_status="verified",
        )
        foreign_owner = User(
            email=f"edu-context-owner-foreign-{suffix}@chatboc.ar",
            password_hash="hash",
            name="Foreign School Owner",
            rol="admin",
        )
        db.session.add_all([family, unlinked, same_tenant_guardian, foreign_owner])
        db.session.flush()
        foreign_tenant = TenantProfile(
            slug=f"edu-context-foreign-{suffix}",
            nombre="Foreign School",
            tipo="pyme",
            pyme_id=foreign_owner.id,
            plan="full",
            vertical="educacion",
            capabilities_json={"education": {"enabled": True}},
        )
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign_school = School(
            tenant_id=foreign_tenant.id,
            name="Foreign School",
            school_type="private",
        )
        db.session.add(foreign_school)
        db.session.flush()
        foreign_guardian = Guardian(
            tenant_id=foreign_tenant.id,
            school_id=foreign_school.id,
            first_name="Foreign",
            last_name="Guardian",
            phone_number="+5492615999992",
            verification_status="verified",
        )
        db.session.add(foreign_guardian)
        self.guardian.user_id = family.id
        db.session.add(self.guardian)
        db.session.commit()

        family_token = generar_token(
            family.id, "usuario", "pyme", municipio_id=None, pyme_id=self.owner.id
        )
        family_headers = {"Authorization": f"Bearer {family_token}"}
        same_tenant_response = self.client.get(
            f"/api/v1/education/family/context?guardian_id={same_tenant_guardian.id}",
            headers=family_headers,
        )
        cross_tenant_response = self.client.get(
            f"/api/v1/education/family/context?guardian_id={foreign_guardian.id}",
            headers=family_headers,
        )

        self.assertEqual(same_tenant_response.status_code, 200)
        self.assertEqual(same_tenant_response.get_json()["access_scope"], "self")
        self.assertEqual(same_tenant_response.get_json()["guardian"]["id"], self.guardian.id)
        self.assertEqual(cross_tenant_response.status_code, 200)
        self.assertEqual(cross_tenant_response.get_json()["guardian"]["id"], self.guardian.id)

        unlinked_token = generar_token(
            unlinked.id, "usuario", "pyme", municipio_id=None, pyme_id=self.owner.id
        )
        unlinked_headers = {"Authorization": f"Bearer {unlinked_token}"}
        unlinked_by_id = self.client.get(
            f"/api/v1/education/me/family-context?guardian_id={self.guardian.id}",
            headers=unlinked_headers,
        )
        unlinked_by_email = self.client.get(
            "/api/v1/education/me/family-context",
            headers=unlinked_headers,
        )
        self.assertEqual(unlinked_by_id.status_code, 404)
        self.assertEqual(unlinked_by_email.status_code, 404)
        self.assertEqual(
            unlinked_by_email.get_json()["reason_code"],
            "education_guardian_profile_unavailable",
        )

        foreign_operator_response = self.client.get(
            f"/api/v1/education/family/context?guardian_id={foreign_guardian.id}",
            headers=self.auth_header,
        )
        self.assertEqual(foreign_operator_response.status_code, 404)
        self.assertEqual(
            foreign_operator_response.get_json()["reason_code"],
            "education_guardian_profile_unavailable",
        )
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant.id,
                event_type="education.guardian.family_context_read",
                resource_id=str(foreign_guardian.id),
            ).count(),
            0,
        )

    def test_free_plan_allows_education_operations_as_self_service_module(self):
        self.tenant.plan = "free"
        db.session.add(self.tenant)
        db.session.commit()

        create_school_resp = self.client.post(
            "/api/v1/education/schools",
            headers=self.auth_header,
            json={"name": "Colegio Free", "school_type": "private"},
        )
        self.assertEqual(create_school_resp.status_code, 201, create_school_resp.get_json())
        create_payload = create_school_resp.get_json()
        self.assertEqual(create_payload["name"], "Colegio Free")

        heatmap_resp = self.client.get(
            "/api/v1/education/operations/heatmap",
            headers=self.auth_header,
        )
        self.assertEqual(heatmap_resp.status_code, 200, heatmap_resp.get_json())
        heatmap_payload = heatmap_resp.get_json()
        self.assertTrue(heatmap_payload["access"]["features"]["heatmaps"]["enabled"])

        verify_resp = self.client.post(
            "/api/v1/education/guardians/verify",
            headers=self.auth_header,
            json={
                "tenant_id": self.tenant.id,
                "guardian_id": self.guardian.id,
                "verification_method": "institutional_record",
                "evidence_ref": "free-plan-family-record",
            },
        )
        self.assertEqual(verify_resp.status_code, 200, verify_resp.get_json())
        self.assertEqual(verify_resp.get_json()["verification_status"], "verified")

    def test_create_and_list_school_cases_aliases_to_tickets(self):
        create_resp = self.client.post(
            "/api/v1/education/cases",
            headers=self.auth_header,
            json={
                "school_id": self.school.id,
                "campus_id": self.campus.id,
                "section_id": self.section.id,
                "student_id": self.student.id,
                "guardian_id": self.guardian.id,
                "case_type": "documentacion",
                "asunto": "Constancia de alumno regular",
                "pregunta": "Necesito la constancia para presentar en obra social.",
                "sensitivity_level": "family",
                "channel": "widget",
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        payload = create_resp.get_json()
        self.assertEqual(payload["case_type"], "documentacion")
        self.assertEqual(payload["ticket_type"], "pyme")

        list_resp = self.client.get(
            f"/api/v1/education/cases?school_id={self.school.id}",
            headers=self.auth_header,
        )
        self.assertEqual(list_resp.status_code, 200)
        cases = list_resp.get_json()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["taxonomy_label"], "Documentación")

        case_id = payload["school_case_id"]

        filtered_resp = self.client.get(
            "/api/v1/education/cases?case_type=documentacion&channel=widget&sensitivity_level=family&status=nuevo&unassigned=1&envelope=1",
            headers=self.auth_header,
        )
        self.assertEqual(filtered_resp.status_code, 200)
        filtered = filtered_resp.get_json()
        self.assertEqual(filtered.get("contract_version"), "education.cases.list.v1")
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["filters"]["case_type"], "documentacion")
        self.assertTrue(filtered["filters"]["unassigned"])

        detail_resp = self.client.get(
            f"/api/v1/education/cases/{case_id}",
            headers=self.auth_header,
        )
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.get_json()["school_case_id"], case_id)

        reply_resp = self.client.post(
            f"/api/v1/education/cases/{case_id}/reply",
            headers=self.auth_header,
            json={"comentario": "La constancia queda en preparación."},
        )
        self.assertEqual(reply_resp.status_code, 201)
        self.assertTrue(reply_resp.get_json()["ok"])

        assign_resp = self.client.post(
            f"/api/v1/education/cases/{case_id}/assign",
            headers=self.auth_header,
            json={"assignee_id": self.owner.id, "expected_assignee_id": None},
        )
        self.assertEqual(assign_resp.status_code, 200)
        self.assertEqual(assign_resp.get_json()["ticket"]["asignado_a_id"], self.owner.id)

        escalate_resp = self.client.post(
            f"/api/v1/education/cases/{case_id}/escalate",
            headers=self.auth_header,
            json={"sensitivity_level": "critical", "reason": "Requiere dirección"},
        )
        self.assertEqual(escalate_resp.status_code, 200)
        self.assertEqual(escalate_resp.get_json()["sensitivity_level"], "critical")

    def test_create_school_case_rejects_foreign_guardian(self):
        other_owner = User(
            email=f"foreign-owner-{int(time.time()*1000)}@chatboc.ar",
            password_hash="hash",
            es_empleado=False,
            name="Foreign Owner",
        )
        db.session.add(other_owner)
        db.session.commit()
        other_tenant = TenantProfile(
            slug=f"foreign-tenant-{int(time.time()*1000)}",
            nombre="Foreign Tenant",
            tipo="pyme",
            pyme_id=other_owner.id,
        )
        db.session.add(other_tenant)
        db.session.commit()

        foreign_guardian = Guardian(
            tenant_id=other_tenant.id,
            first_name="Otro",
            last_name="Tutor",
            phone_number="+5492615000099",
            verification_status="pending",
        )
        db.session.add(foreign_guardian)
        db.session.commit()

        response = self.client.post(
            "/api/v1/education/cases",
            headers=self.auth_header,
            json={
                "school_id": self.school.id,
                "case_type": "documentacion",
                "asunto": "Constancia",
                "pregunta": "Necesito constancia.",
                "guardian_id": foreign_guardian.id,
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["message"], "Guardian not found for tenant")

    def test_whatsapp_ticket_alias_resolves_guardian_and_summary(self):
        ticket = PymeTicket(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            nro_ticket=int(time.time() * 1000) % 1000000000,
            pregunta="Adjunto certificado medico por inasistencia.",
            asunto="Colegio - Justificar inasistencia",
            categoria="inasistencia",
            anon_id=self.guardian.phone_number,
            estado="nuevo",
            latitud=-34.601,
            longitud=-58.381,
        )
        db.session.add(ticket)
        db.session.commit()

        alias = create_school_case_alias_for_ticket(
            tenant_profile=self.tenant,
            ticket_type="pyme",
            ticket_id=ticket.id,
            case_type="inasistencia",
            channel="whatsapp",
            phone=self.guardian.phone_number,
        )
        self.assertIsNotNone(alias)
        self.assertEqual(alias.school_id, self.school.id)
        self.assertEqual(alias.campus_id, self.campus.id)
        self.assertEqual(alias.student_id, self.student.id)
        self.assertEqual(alias.guardian_id, self.guardian.id)

        same_alias = create_school_case_alias_for_ticket(
            tenant_profile=self.tenant,
            ticket_type="pyme",
            ticket_id=ticket.id,
            case_type="documentacion",
            channel="whatsapp",
            phone=self.guardian.phone_number,
        )
        self.assertEqual(same_alias.id, alias.id)

        summary_resp = self.client.get(
            "/api/v1/education/operations/summary",
            headers=self.auth_header,
        )
        self.assertEqual(summary_resp.status_code, 200)
        summary = summary_resp.get_json()
        self.assertEqual(summary.get("contract_version"), "education.operations_summary.v1")
        self.assertEqual(summary["summary"]["total_cases"], 1)
        self.assertEqual(summary["summary"]["open_cases"], 1)
        self.assertEqual(summary["summary"]["waiting_assignment"], 1)
        self.assertEqual(summary["summary"]["cases_with_location"], 1)
        self.assertEqual(summary["breakdown"]["by_type"]["inasistencia"], 1)
        self.assertEqual(summary["breakdown"]["by_channel"]["whatsapp"], 1)
        action_ids = {item["id"] for item in summary.get("next_best_actions") or []}
        self.assertIn("assign_open_cases", action_ids)
        self.assertIn("inspect_school_case_heatmap", action_ids)

        heatmap_resp = self.client.get(
            "/api/v1/education/operations/heatmap?channel=whatsapp&case_type=inasistencia",
            headers=self.auth_header,
        )
        self.assertEqual(heatmap_resp.status_code, 200)
        heatmap = heatmap_resp.get_json()
        self.assertEqual(heatmap.get("contract_version"), "education.operations_heatmap.v1")
        self.assertEqual(heatmap["render_contract"]["state"], "ready")
        self.assertEqual(heatmap["summary"]["points"], 1)
        self.assertEqual(heatmap["points"][0]["school_case_id"], alias.id)


if __name__ == "__main__":
    unittest.main()
