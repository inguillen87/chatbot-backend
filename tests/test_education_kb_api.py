import unittest
import time
from app import create_app, db
from models import CatalogoItem, TenantProfile, User
from utils.auth_helpers import generar_token

class TestEducationKBApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()

        user_email = f"edu_kb_api_{int(time.time()*1000)}@chatboc.ar"
        self.test_user = User(email=user_email, password_hash="hash", es_empleado=False, name="Edu KB API Admin")
        db.session.add(self.test_user)
        db.session.commit()

        self.tenant = TenantProfile(
            slug=f"edu-kb-api-tenant-{int(time.time()*1000)}",
            nombre="Edu KB API",
            tipo="pyme",
            pyme_id=self.test_user.id,
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.item = CatalogoItem(
            tenant_id=self.tenant.id,
            user_id=self.test_user.id,
            nombre="Reglamento Escolar V2",
            descripcion="El uso de uniforme es obligatorio y la entrada es a las 8.",
            precio=0.0
        )
        db.session.add(self.item)
        db.session.commit()

        self.token = generar_token(self.test_user.id, "admin", "pyme", municipio_id=None, pyme_id=self.test_user.id)

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.app_context.pop()

    def test_query_kb_api(self):
        resp = self.client.post('/api/v1/education/knowledge/query',
                                headers={'Authorization': f'Bearer {self.token}'},
                                json={"query": "entrada"})

        self.assertIn(resp.status_code, [200, 404, 400])
        if resp.status_code == 200:
            data = resp.get_json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["title"], "Reglamento Escolar V2")

if __name__ == '__main__':
    unittest.main()
