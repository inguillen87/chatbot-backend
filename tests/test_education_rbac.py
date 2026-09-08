import time
import unittest

from app import create_app, db
from config import TestingConfig
from models import AuditEvent, PymeTicket, TenantProfile, TicketComentario, User
from models_education import (
    AcademicLevel,
    Campus,
    CourseSection,
    FamilyVerificationAttempt,
    Guardian,
    School,
    SchoolCaseAlias,
    Shift,
    Student,
)
from services.education_access_policy import (
    EDUCATION_ANALYTICS_READ,
    EDUCATION_CASES_READ,
    EDUCATION_CASES_WRITE,
    EDUCATION_DIRECTORY_READ,
)
from services.education_case_service import create_school_case_alias_for_ticket
from utils.auth_helpers import generar_token


class TestEducationRbac(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestingConfig)

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.suffix = int(time.time() * 1_000_000)
        self.owner_a, self.tenant_a = self._create_tenant("a")
        self.owner_b, self.tenant_b = self._create_tenant("b")
        self.school_a, self.campus_a, self.level_a, self.shift_a = self._create_directory(
            self.tenant_a, "A"
        )
        self.school_b, self.campus_b, self.level_b, self.shift_b = self._create_directory(
            self.tenant_b, "B"
        )
        db.session.commit()
        self.owner_headers = self._headers(self.owner_a, claimed_role="admin")

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.ctx.pop()

    def _create_tenant(self, marker):
        owner = User(
            email=f"edu-rbac-owner-{marker}-{self.suffix}@chatboc.ar",
            password_hash="hash",
            name=f"Education owner {marker}",
            rol="admin",
            tipo_chat="pyme",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=f"edu-rbac-{marker}-{self.suffix}",
            nombre=f"Education tenant {marker}",
            tipo="pyme",
            pyme_id=owner.id,
            plan="full",
            vertical="educacion",
            is_active=True,
            capabilities_json={"education": {"enabled": True}},
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        owner.tenant_slug = tenant.slug
        db.session.add(owner)
        return owner, tenant

    def _create_directory(self, tenant, marker):
        school = School(
            tenant_id=tenant.id,
            name=f"School {marker} {self.suffix}",
            school_type="private",
        )
        db.session.add(school)
        db.session.flush()
        campus = Campus(school_id=school.id, name=f"Campus {marker}", is_main=True)
        level = AcademicLevel(school_id=school.id, code="primary", name="Primary")
        shift = Shift(school_id=school.id, code="morning", name="Morning")
        db.session.add_all([campus, level, shift])
        db.session.flush()
        return school, campus, level, shift

    def _headers(self, user, *, claimed_role=None):
        token = generar_token(
            user.id,
            claimed_role or user.rol,
            "pyme",
            municipio_id=None,
            pyme_id=self.owner_a.id,
        )
        return {"Authorization": f"Bearer {token}"}

    def _employee(self, tenant, owner, capabilities):
        employee = User(
            email=f"edu-rbac-employee-{tenant.id}-{len(capabilities)}-{time.time_ns()}@chatboc.ar",
            password_hash="hash",
            name="Education staff",
            rol="empleado",
            es_empleado=True,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            empresa_id=owner.id,
            accesibilidad={
                "employee_scope": {"permissions": list(capabilities)}
            },
        )
        db.session.add(employee)
        db.session.commit()
        return employee

    def _create_case(self):
        response = self.client.post(
            "/api/v1/education/cases",
            headers=self.owner_headers,
            json={
                "school_id": self.school_a.id,
                "case_type": "documentacion",
                "asunto": "Constancia",
                "pregunta": "Necesito una constancia.",
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    def test_persisted_ordinary_role_cannot_enter_admin_surfaces_or_spoof_reply(self):
        ordinary = User(
            email=f"edu-rbac-family-{self.suffix}@chatboc.ar",
            password_hash="hash",
            name="Family user",
            rol="usuario",
            tenant_id=self.tenant_a.id,
            tenant_slug=self.tenant_a.slug,
            accesibilidad={"employee_scope": {"permissions": ["*"]}},
        )
        lead = User(
            email=f"edu-rbac-lead-{self.suffix}@chatboc.ar",
            password_hash="hash",
            name="Prospective family",
            rol="lead",
            tenant_id=self.tenant_a.id,
            tenant_slug=self.tenant_a.slug,
        )
        db.session.add_all([ordinary, lead])
        db.session.flush()
        guardian_profile = Guardian(
            tenant_id=self.tenant_a.id,
            school_id=self.school_a.id,
            user_id=ordinary.id,
            first_name="Linked",
            last_name="Guardian",
            verification_status="verified",
        )
        db.session.add(guardian_profile)
        db.session.commit()
        # The JWT intentionally lies about the role. Authorization must use the
        # persisted user and still reject this family identity.
        headers = self._headers(ordinary, claimed_role="admin")

        for path in (
            "/api/v1/education/tenant/capabilities",
            "/api/v1/education/schools",
            "/api/v1/education/operations/summary",
            "/api/v1/education/cases",
        ):
            response = self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 403, (path, response.get_json()))
            self.assertEqual(
                response.get_json()["reason_code"], "education_admin_role_required"
            )

        response = self.client.post(
            "/api/v1/education/cases",
            headers=headers,
            json={
                "school_id": self.school_a.id,
                "case_type": "documentacion",
                "asunto": "Spoofed case",
                "pregunta": "Should not be created",
                "es_admin": True,
                "origen": "system",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PymeTicket.query.filter_by(tenant_id=self.tenant_a.id).count(), 0)

        lead_response = self.client.get(
            "/api/v1/education/admin/menu",
            headers=self._headers(lead, claimed_role="admin"),
        )
        self.assertEqual(lead_response.status_code, 403)
        self.assertEqual(
            lead_response.get_json()["reason_code"],
            "education_admin_role_required",
        )

    def test_employee_capabilities_are_explicit_and_surface_specific(self):
        employee = self._employee(
            self.tenant_a,
            self.owner_a,
            {EDUCATION_DIRECTORY_READ},
        )
        headers = self._headers(employee)

        directory = self.client.get("/api/v1/education/schools", headers=headers)
        analytics = self.client.get(
            "/api/v1/education/operations/summary", headers=headers
        )
        cases = self.client.get("/api/v1/education/cases", headers=headers)
        mutation = self.client.post(
            "/api/v1/education/schools",
            headers=headers,
            json={"name": "Unauthorized school", "school_type": "private"},
        )

        self.assertEqual(directory.status_code, 200)
        self.assertEqual(analytics.status_code, 403)
        self.assertIn(
            EDUCATION_ANALYTICS_READ,
            analytics.get_json()["missing_capabilities"],
        )
        self.assertEqual(cases.status_code, 403)
        self.assertIn(EDUCATION_CASES_READ, cases.get_json()["missing_capabilities"])
        self.assertEqual(mutation.status_code, 403)

    def test_citizen_metadata_cannot_grant_guardian_verification_or_linking(self):
        ordinary = User(
            email=f"edu-rbac-guardian-spoof-{self.suffix}@chatboc.ar",
            password_hash="hash",
            name="Family identity",
            rol="usuario",
            tenant_id=self.tenant_a.id,
            tenant_slug=self.tenant_a.slug,
            accesibilidad={"employee_scope": {"permissions": ["*"]}},
        )
        db.session.add(ordinary)
        db.session.flush()
        guardian = Guardian(
            tenant_id=self.tenant_a.id,
            school_id=self.school_a.id,
            user_id=ordinary.id,
            first_name="Family",
            last_name="Identity",
            verification_status="pending",
        )
        db.session.add(guardian)
        db.session.commit()
        headers = self._headers(ordinary, claimed_role="admin")

        verify = self.client.post(
            "/api/v1/education/guardians/verify",
            headers=headers,
            json={
                "guardian_id": guardian.id,
                "verification_method": "institutional_record",
                "evidence_ref": "attacker-controlled-proof",
                "es_admin": True,
            },
        )
        link = self.client.post(
            "/api/v1/education/guardians/link-student",
            headers=headers,
            json={
                "guardian_id": guardian.id,
                "student_id": 999999,
                "es_admin": True,
            },
        )

        self.assertEqual(verify.status_code, 403, verify.get_json())
        self.assertEqual(link.status_code, 403, link.get_json())
        self.assertEqual(
            verify.get_json()["reason_code"],
            "education_guardian_role_required",
        )
        self.assertEqual(
            link.get_json()["reason_code"],
            "education_guardian_role_required",
        )
        db.session.expire_all()
        self.assertEqual(db.session.get(Guardian, guardian.id).verification_status, "pending")
        self.assertEqual(
            FamilyVerificationAttempt.query.filter_by(guardian_id=guardian.id).count(),
            0,
        )

    def test_cross_tenant_and_unprivileged_staff_ids_fail_closed(self):
        foreign_staff = self._employee(
            self.tenant_b,
            self.owner_b,
            {EDUCATION_DIRECTORY_READ, EDUCATION_CASES_WRITE},
        )
        ordinary_staff = User(
            email=f"edu-rbac-ordinary-staff-{self.suffix}@chatboc.ar",
            password_hash="hash",
            name="Ordinary same tenant",
            rol="usuario",
            tenant_id=self.tenant_a.id,
        )
        db.session.add(ordinary_staff)
        db.session.commit()

        for staff_id in (foreign_staff.id, ordinary_staff.id):
            response = self.client.post(
                "/api/v1/education/sections",
                headers=self.owner_headers,
                json={
                    "campus_id": self.campus_a.id,
                    "level_id": self.level_a.id,
                    "shift_id": self.shift_a.id,
                    "grade": str(staff_id),
                    "division": "A",
                    "academic_year": 2026,
                    "homeroom_staff_id": staff_id,
                },
            )
            self.assertEqual(response.status_code, 400, response.get_json())
            self.assertEqual(
                response.get_json()["reason_code"],
                "education_homeroom_staff_invalid",
            )

        foreign_directory = self.client.get(
            f"/api/v1/education/schools/{self.school_b.id}",
            headers=self.owner_headers,
        )
        foreign_case = self.client.post(
            "/api/v1/education/cases",
            headers=self.owner_headers,
            json={
                "school_id": self.school_b.id,
                "case_type": "documentacion",
                "asunto": "Cross tenant",
                "pregunta": "Must fail",
            },
        )
        self.assertEqual(foreign_directory.status_code, 404)
        self.assertEqual(foreign_case.status_code, 404)

        case = self._create_case()
        assignment = self.client.post(
            f"/api/v1/education/cases/{case['school_case_id']}/assign",
            headers=self.owner_headers,
            json={"assignee_id": foreign_staff.id, "expected_assignee_id": None},
        )
        self.assertEqual(assignment.status_code, 400)
        self.assertEqual(
            assignment.get_json()["reason_code"], "education_assignee_invalid"
        )

    def test_legitimate_privileged_staff_and_server_owned_reply_metadata_remain(self):
        employee = self._employee(
            self.tenant_a,
            self.owner_a,
            {EDUCATION_DIRECTORY_READ, EDUCATION_CASES_WRITE},
        )
        section = self.client.post(
            "/api/v1/education/sections",
            headers=self.owner_headers,
            json={
                "campus_id": self.campus_a.id,
                "level_id": self.level_a.id,
                "shift_id": self.shift_a.id,
                "grade": "7",
                "division": "A",
                "academic_year": 2026,
                "homeroom_staff_id": employee.id,
            },
        )
        self.assertEqual(section.status_code, 201, section.get_json())
        section_model = db.session.get(CourseSection, section.get_json()["id"])
        self.assertEqual(section_model.homeroom_staff_id, employee.id)

        case = self._create_case()
        assignment = self.client.post(
            f"/api/v1/education/cases/{case['school_case_id']}/assign",
            headers=self.owner_headers,
            json={"assignee_id": employee.id, "expected_assignee_id": None},
        )
        self.assertEqual(assignment.status_code, 200, assignment.get_json())

        reply = self.client.post(
            f"/api/v1/education/cases/{case['school_case_id']}/reply",
            headers=self.owner_headers,
            json={
                "comentario": "Respuesta institucional",
                "es_admin": False,
                "origen": "whatsapp-user-controlled",
            },
        )
        self.assertEqual(reply.status_code, 201, reply.get_json())
        comment = TicketComentario.query.filter_by(
            pyme_ticket_id=case["ticket_id"],
            comentario="Respuesta institucional",
        ).one()
        self.assertTrue(comment.es_admin)
        self.assertEqual(comment.origen, "education")

    def test_cross_tenant_ticket_alias_is_rejected_omitted_and_durably_audited(self):
        foreign_ticket = PymeTicket(
            tenant_id=self.tenant_b.id,
            user_id=self.owner_b.id,
            nro_ticket=self.suffix % 1_000_000_000,
            pregunta="Foreign tenant ticket",
            asunto="Foreign",
            categoria="educacion:documentacion",
            estado="nuevo",
        )
        db.session.add(foreign_ticket)
        db.session.commit()

        created = create_school_case_alias_for_ticket(
            tenant_profile=self.tenant_a,
            ticket_type="pyme",
            ticket_id=foreign_ticket.id,
            case_type="documentacion",
            school_id=self.school_a.id,
        )
        self.assertIsNone(created)

        corrupt = SchoolCaseAlias(
            tenant_id=self.tenant_a.id,
            school_id=self.school_a.id,
            case_type="documentacion",
            sensitivity_level="internal",
            channel="api",
            ticket_type="pyme",
            ticket_id=foreign_ticket.id,
        )
        db.session.add(corrupt)
        db.session.commit()

        listing = self.client.get(
            "/api/v1/education/cases", headers=self.owner_headers
        )
        detail = self.client.get(
            f"/api/v1/education/cases/{corrupt.id}", headers=self.owner_headers
        )
        summary = self.client.get(
            "/api/v1/education/operations/summary", headers=self.owner_headers
        )

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json(), [])
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.get_json()["summary"]["total_cases"], 0)
        audit = AuditEvent.query.filter_by(
            tenant_id=self.tenant_a.id,
            event_type="education.case_alias.corrupt",
            resource_type="education_case_alias",
            resource_id=str(corrupt.id),
        ).all()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0].details["reason_code"], "ticket_tenant_mismatch")

    def test_alias_creation_rejects_each_foreign_directory_reference(self):
        section_b = CourseSection(
            campus_id=self.campus_b.id,
            academic_year=2026,
            level_id=self.level_b.id,
            grade="5",
            division="B",
            shift_id=self.shift_b.id,
        )
        db.session.add(section_b)
        db.session.flush()
        student_b = Student(
            school_id=self.school_b.id,
            campus_id=self.campus_b.id,
            section_id=section_b.id,
            first_name="Foreign",
            last_name="Student",
        )
        guardian_b = Guardian(
            tenant_id=self.tenant_b.id,
            school_id=self.school_b.id,
            first_name="Foreign",
            last_name="Guardian",
        )
        local_ticket = PymeTicket(
            tenant_id=self.tenant_a.id,
            user_id=self.owner_a.id,
            nro_ticket=(self.suffix + 1) % 1_000_000_000,
            pregunta="Local ticket",
            asunto="Local",
            categoria="educacion:documentacion",
            estado="nuevo",
        )
        db.session.add_all([student_b, guardian_b, local_ticket])
        db.session.commit()

        for foreign_reference in (
            {"school_id": self.school_b.id},
            {"campus_id": self.campus_b.id},
            {"section_id": section_b.id},
            {"student_id": student_b.id},
            {"guardian_id": guardian_b.id},
        ):
            alias = create_school_case_alias_for_ticket(
                tenant_profile=self.tenant_a,
                ticket_type="pyme",
                ticket_id=local_ticket.id,
                case_type="documentacion",
                **foreign_reference,
            )
            self.assertIsNone(alias, foreign_reference)
        self.assertEqual(
            SchoolCaseAlias.query.filter_by(
                ticket_type="pyme", ticket_id=local_ticket.id
            ).count(),
            0,
        )

    def test_corrupt_internal_directory_links_are_not_exposed_or_accepted(self):
        corrupt_section = CourseSection(
            campus_id=self.campus_a.id,
            academic_year=2026,
            level_id=self.level_b.id,
            grade="99",
            division="X",
            shift_id=self.shift_b.id,
        )
        corrupt_student = Student(
            school_id=self.school_a.id,
            campus_id=self.campus_b.id,
            first_name="Corrupt",
            last_name="Reference",
        )
        db.session.add_all([corrupt_section, corrupt_student])
        db.session.commit()

        sections = self.client.get(
            f"/api/v1/education/sections?campus_id={self.campus_a.id}",
            headers=self.owner_headers,
        )
        self.assertEqual(sections.status_code, 200)
        self.assertNotIn(
            corrupt_section.id,
            {item["id"] for item in sections.get_json()},
        )

        case = self.client.post(
            "/api/v1/education/cases",
            headers=self.owner_headers,
            json={
                "school_id": self.school_a.id,
                "student_id": corrupt_student.id,
                "case_type": "documentacion",
                "asunto": "Corrupt student",
                "pregunta": "Must fail closed",
            },
        )
        self.assertEqual(case.status_code, 404)
        self.assertEqual(case.get_json()["error"]["message"], "Student not found")

    def test_inactive_and_ambiguous_tenant_identity_never_falls_back(self):
        inactive_headers = self.owner_headers
        self.tenant_a.is_active = False
        db.session.add(self.tenant_a)
        db.session.commit()
        inactive = self.client.get(
            "/api/v1/education/schools", headers=inactive_headers
        )
        self.assertIn(inactive.status_code, {401, 403})

        self.tenant_a.is_active = True
        self.owner_a.tenant_id = None
        self.owner_a.tenant_slug = None
        ambiguous_tenant = TenantProfile(
            slug=f"edu-rbac-ambiguous-{self.suffix}",
            nombre="Ambiguous tenant",
            tipo="pyme",
            pyme_id=self.owner_a.id,
            plan="full",
            vertical="educacion",
            is_active=True,
        )
        db.session.add_all([self.tenant_a, self.owner_a, ambiguous_tenant])
        db.session.commit()
        ambiguous_headers = self._headers(self.owner_a, claimed_role="admin")
        ambiguous = self.client.get(
            "/api/v1/education/schools", headers=ambiguous_headers
        )
        self.assertIn(ambiguous.status_code, {400, 401})


if __name__ == "__main__":
    unittest.main()
