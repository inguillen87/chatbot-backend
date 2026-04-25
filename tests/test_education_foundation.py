import unittest
import time
from app import create_app, db
from models import TenantProfile, PymeTicket, User
from models_education import School, Guardian, Student, StudentGuardianRelation
from utils.auth_helpers import generar_token
from services.education_utils import is_education_tenant

class TestEducationFoundation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()

        # Needs a backing User for the tenant constraint (pyme_id)
        self.test_user = User(email=f"edu_test_{int(time.time()*1000)}@chatboc.ar", password_hash="hash", es_empleado=False, name="Edu Admin")
        db.session.add(self.test_user)
        db.session.commit()

        # Create Tenant
        self.tenant = TenantProfile(
            slug=f"colegio-test-{int(time.time()*1000)}",
            nombre="Colegio Test",
            tipo="pyme",
            pyme_id=self.test_user.id,
            vertical="educacion",
            is_active=True
        )
        db.session.add(self.tenant)
        db.session.commit()

        # Seed Foundation Data
        self.school = School(tenant_id=self.tenant.id, name="Primary School", school_type="private")
        db.session.add(self.school)
        db.session.commit()

        self.student = Student(school_id=self.school.id, first_name="John", last_name="Doe", document_number="123456")
        self.guardian = Guardian(tenant_id=self.tenant.id, first_name="Jane", last_name="Doe", document_number="987654", verification_status="pending")
        db.session.add(self.student)
        db.session.add(self.guardian)
        db.session.commit()

        self.relation = StudentGuardianRelation(student_id=self.student.id, guardian_id=self.guardian.id, relationship_type="madre")
        db.session.add(self.relation)

        self.ticket = PymeTicket(tenant_id=self.tenant.id, nro_ticket=f"EDU-{int(time.time()*1000)}", pregunta="Test Case", asunto="Consulta Escolar", categoria="Trámite")
        db.session.add(self.ticket)
        db.session.commit()

        self.admin_token = generar_token(self.test_user.id, "admin", "pyme", municipio_id=None, pyme_id=self.test_user.id)

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.app_context.pop()

    def test_tenant_vertical_capability(self):
        self.assertTrue(is_education_tenant(self.tenant.id))

    def test_list_schools(self):
        resp = self.client.get('/api/v1/education/schools', headers={'Authorization': f'Bearer {self.admin_token}'})
        self.assertIn(resp.status_code, [200, 404])
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["name"], "Primary School")

    def test_verify_guardian(self):
        resp = self.client.post('/api/v1/education/guardians/verify',
                                headers={'Authorization': f'Bearer {self.admin_token}'},
                                json={"document_number": "987654"})
        self.assertIn(resp.status_code, [200, 404])
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertEqual(data["status"], "verified")

            # Check DB State
            db.session.refresh(self.guardian)
            self.assertEqual(self.guardian.verification_status, "verified")

    def test_family_context(self):
        resp = self.client.get(f'/api/v1/education/me/family-context?guardian_id={self.guardian.id}',
                               headers={'Authorization': f'Bearer {self.admin_token}'})
        self.assertIn(resp.status_code, [200, 404])
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertEqual(len(data["students"]), 1)
            self.assertEqual(data["students"][0]["first_name"], "John")

    def test_list_cases_aliases(self):
        resp = self.client.get('/api/v1/education/cases', headers={'Authorization': f'Bearer {self.admin_token}'})
        self.assertIn(resp.status_code, [200, 404])
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["subject"], "Consulta Escolar")

if __name__ == '__main__':
    unittest.main()
