import unittest
from app import create_app, db
from models import CatalogoItem, TenantProfile, User
import time
from services.education_kb_service import education_kb_service

class TestEducationKBServices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()

        user_email = f"edu_kb_{int(time.time()*1000)}@chatboc.ar"
        self.test_user = User(email=user_email, password_hash="hash", es_empleado=False, name="Edu KB Admin")
        db.session.add(self.test_user)
        db.session.commit()

        self.tenant = TenantProfile(
            slug=f"edu-kb-tenant-{int(time.time()*1000)}",
            nombre="Edu KB",
            tipo="pyme",
            pyme_id=self.test_user.id,
        )
        db.session.add(self.tenant)
        db.session.commit()

        # Add mock KB item
        self.item = CatalogoItem(
            tenant_id=self.tenant.id,
            user_id=self.test_user.id,
            nombre="Reglamento Escolar",
            descripcion="El uso de uniforme es obligatorio.",
            precio=0.0
        )
        db.session.add(self.item)
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.app_context.pop()

    def test_query_knowledge_base_fallback(self):
        results = education_kb_service.query_knowledge_base(self.tenant.id, "uniforme")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Reglamento Escolar")
        self.assertIn("uniforme es obligatorio", results[0]["content_snippet"])
        self.assertEqual(results[0]["relevance_score"], 1.0)

if __name__ == '__main__':
    unittest.main()
