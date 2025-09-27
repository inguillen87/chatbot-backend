import unittest

from app import create_app
from config import Config
from models import User, db
from services.plan_config import (
    MERCADOPAGO_PLAN_LOOKUP,
    apply_plan_to_user,
    get_plan_metadata,
    serialize_plan_catalog,
)


class _PlanTestConfig(Config):
    TESTING = True
    SESSION_TYPE = "filesystem"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


class PlanConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(_PlanTestConfig)
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

    def test_metadata_prices_and_limits(self):
        pro = get_plan_metadata("pro")
        full = get_plan_metadata("full")

        self.assertIsNotNone(pro)
        self.assertIsNotNone(full)
        self.assertEqual(pro.price_ars, 65_000)
        self.assertEqual(pro.message_limit, 200)
        self.assertEqual(full.price_ars, 95_000)
        self.assertIsNone(full.message_limit)

    def test_apply_plan_updates_user_limits(self):
        user = User(
            name="Tester",
            email="tester@example.com",
            password_hash="hash",
        )
        db.session.add(user)
        db.session.commit()

        apply_plan_to_user(user, "pro")
        self.assertEqual(user.plan, "pro")
        self.assertEqual(user.limite_preguntas, 200)

        apply_plan_to_user(user, "full")
        self.assertEqual(user.plan, "full")
        self.assertIsNone(user.limite_preguntas)

        # Mercado Pago IDs should keep mapping consistency
        for mp_id, key in MERCADOPAGO_PLAN_LOOKUP.items():
            self.assertIsNotNone(mp_id)
            self.assertIsNotNone(get_plan_metadata(key))

    def test_public_plan_endpoint_exposes_prices(self):
        response = self.client.get("/auth/plans")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("planes", payload)
        catalog = {plan["key"]: plan for plan in payload["planes"]}
        self.assertIn("pro", catalog)
        self.assertIn("full", catalog)
        self.assertEqual(catalog["pro"]["price_ars"], 65_000)
        self.assertEqual(catalog["full"]["price_ars"], 95_000)

        serialized = serialize_plan_catalog()
        self.assertTrue(any(plan["key"] == "pro" for plan in serialized))


if __name__ == "__main__":
    unittest.main()
