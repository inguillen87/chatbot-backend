import unittest

from app import create_app, db
from config import Config
from models import CatalogoItem, TenantProfile, User


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


class PublicCatalogAndCartTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.owner = User(
            name="Municipalidad de Junín",
            email="junin@example.com",
            password_hash="hash",
            tipo_chat="municipio",
            ciudad="Junín",
        )
        db.session.add(self.owner)
        db.session.commit()

        self.tenant = TenantProfile(
            slug="municipalidad-de-junin",
            nombre="Municipalidad de Junín",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_public_catalog_seeds_items(self):
        response = self.client.get(f"/api/pwa/public/catalog?tenant_id={self.tenant.id}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertGreaterEqual(len(data), 3)
        self.assertTrue(all("catalogo_item_id" in prod for prod in data))
        self.assertGreater(CatalogoItem.query.filter_by(user_id=self.owner.id).count(), 0)

    def test_public_cart_flow(self):
        catalog_resp = self.client.get(f"/api/pwa/public/catalog?tenant_id={self.tenant.id}")
        self.assertEqual(catalog_resp.status_code, 200)
        catalog = catalog_resp.get_json()
        first_item = next(prod for prod in catalog if "pt" in (prod.get("precio_texto", "").lower()))
        item_id = first_item["catalogo_item_id"]

        add_resp = self.client.post(
            f"/api/pwa/public/cart/add?tenant_id={self.tenant.id}",
            json={"catalogo_item_id": item_id, "cantidad": 2},
        )
        self.assertEqual(add_resp.status_code, 200)
        summary = add_resp.get_json()
        self.assertEqual(summary["items_count"], 2)
        self.assertEqual(summary["items"][0]["cantidad"], 2)
        self.assertEqual(summary["badge_count"], 2)
        self.assertGreater(summary["total_puntos_estimado"], 0)
        rewards = summary.get("recompensas_demo", {})
        self.assertGreater(rewards.get("balance_resumen", {}).get("saldo_disponible", 0), rewards.get("balance_resumen", {}).get("saldo_estimado_post_compra", 0))

        update_resp = self.client.post(
            f"/api/pwa/public/cart/update?tenant_id={self.tenant.id}",
            json={"catalogo_item_id": item_id, "cantidad": 1},
        )
        self.assertEqual(update_resp.status_code, 200)
        updated = update_resp.get_json()
        self.assertEqual(updated["items_count"], 1)

        remove_resp = self.client.post(
            f"/api/pwa/public/cart/remove?tenant_id={self.tenant.id}",
            json={"catalogo_item_id": item_id},
        )
        self.assertEqual(remove_resp.status_code, 200)
        emptied = remove_resp.get_json()
        self.assertEqual(emptied["items_count"], 0)

        clear_resp = self.client.post(f"/api/pwa/public/cart/clear?tenant_id={self.tenant.id}")
        self.assertEqual(clear_resp.status_code, 200)
        cleared = clear_resp.get_json()
        self.assertEqual(cleared["items_count"], 0)

    def test_catalog_prices_and_rewards_endpoint(self):
        response = self.client.get(f"/api/pwa/public/catalog?tenant_id={self.tenant.id}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(any(item.get("precio_texto") for item in data))
        self.assertTrue(all(item.get("imagen_url") for item in data))

        rewards_resp = self.client.get(f"/api/pwa/public/rewards?tenant_id={self.tenant.id}")
        self.assertEqual(rewards_resp.status_code, 200)
        rewards = rewards_resp.get_json()
        self.assertIn("saldo_demo_puntos", rewards)
        self.assertIn("balance_resumen", rewards)


if __name__ == "__main__":
    unittest.main()
