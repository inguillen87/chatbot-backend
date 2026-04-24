import unittest
from datetime import datetime

from app import create_app
from config import Config
from models import TenantProfile, User, MunicipioPost, db
from utils.auth_helpers import generar_token


class _TestConfig(Config):
    TESTING = True
    SESSION_TYPE = "filesystem"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


def _create_tenant_with_owner():
    owner = User(
        name="Admin",
        email="admin@example.com",
        password_hash="hash",
        rol="admin",
    )
    db.session.add(owner)
    db.session.flush()
    owner.token = generar_token(owner.id, owner.rol, None, None, None)

    tenant = TenantProfile(
        slug="demo",
        nombre="Demo Municipio",
        tipo="municipio",
        municipio=owner,
    )
    db.session.add(tenant)
    db.session.commit()
    return owner, tenant


class PublicAliasTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.owner, self.tenant = _create_tenant_with_owner()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_public_tenant_widget_alias_and_options(self):
        options = self.client.options(
            f"/public/tenants/{self.tenant.slug}/widget-config",
            query_string={"tenant": self.tenant.slug},
        )
        self.assertEqual(options.status_code, 200)

        resp = self.client.get(
            f"/public/tenants/{self.tenant.slug}/widget-config",
            query_string={"tenant": self.tenant.slug},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), dict)



    def test_public_catalog_default_slug_falls_back_to_active_tenant(self):
        resp = self.client.get(
            "/api/public/tenants/default/catalog",
            query_string={"tenant": "default", "tenant_slug": "default"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), list)

    def test_api_public_widget_config_alias(self):
        options = self.client.options(
            "/api/public/widget-config",
            query_string={"tenant": self.tenant.slug},
        )
        self.assertEqual(options.status_code, 200)

        resp = self.client.get(
            "/api/public/widget-config",
            query_string={"tenant": self.tenant.slug},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn("widget", payload)
        self.assertIn("tenant", payload)

    def test_public_news_and_events_aliases(self):
        post = MunicipioPost(
            municipio_id=self.owner.id,
            tipo_post="noticia",
            titulo="Titulo",
            descripcion="Desc",
            fecha_publicacion=datetime.utcnow(),
        )
        event = MunicipioPost(
            municipio_id=self.owner.id,
            tipo_post="evento",
            titulo="Evento",
            descripcion="Evento desc",
            fecha_publicacion=datetime.utcnow(),
        )
        db.session.add_all([post, event])
        db.session.commit()

        news_resp = self.client.get(
            "/public/news", query_string={"tenant": self.tenant.slug}
        )
        self.assertEqual(news_resp.status_code, 200)
        self.assertTrue(len(news_resp.get_json()) >= 1)

        events_resp = self.client.get(
            "/public/events", query_string={"tenant": self.tenant.slug}
        )
        self.assertEqual(events_resp.status_code, 200)
        self.assertTrue(len(events_resp.get_json()) >= 1)


if __name__ == "__main__":
    unittest.main()
