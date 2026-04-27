import time
import unittest

from app import app as flask_app, db
from models import TenantProfile, User
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
from utils.auth_helpers import generar_token


class TestEducationRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = flask_app

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

    def test_guardian_lookup_verify_and_family_context(self):
        lookup_resp = self.client.post(
            "/api/v1/education/guardian/lookup",
            json={"tenant_id": self.tenant.id, "phone_number": self.guardian.phone_number},
        )
        self.assertEqual(lookup_resp.status_code, 200)
        self.assertEqual(lookup_resp.get_json()["verification_status"], "pending")

        verify_resp = self.client.post(
            "/api/v1/education/guardian/verify",
            json={"tenant_id": self.tenant.id, "phone_number": self.guardian.phone_number},
        )
        self.assertEqual(verify_resp.status_code, 200)
        self.assertEqual(verify_resp.get_json()["verification_status"], "verified")

        attempts = FamilyVerificationAttempt.query.filter_by(tenant_id=self.tenant.id).all()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "verified")

        family_resp = self.client.get(
            f"/api/v1/education/family/context?guardian_id={self.guardian.id}",
            headers=self.auth_header,
        )
        self.assertEqual(family_resp.status_code, 200)
        data = family_resp.get_json()
        self.assertEqual(len(data["students"]), 1)
        self.assertTrue(data["students"][0]["can_receive_sensitive_updates"])

    def test_verify_guardian_rejects_unknown_tenant(self):
        response = self.client.post(
            "/api/v1/education/guardian/verify",
            json={"tenant_id": 999999, "phone_number": self.guardian.phone_number},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["message"], "Tenant not found")

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


if __name__ == "__main__":
    unittest.main()
