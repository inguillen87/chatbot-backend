import os
import unittest
from datetime import datetime, timezone
import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, User


class GovExecutiveAnalyticsConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SECRET_KEY = "test_secret_key"


class GovExecutiveAnalyticsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(GovExecutiveAnalyticsConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(
            name="Intendente Junin",
            email="intendente@junin.com",
            role="admin",
            tipo_chat="municipio",
        )
        self.admin.set_password("123456")
        db.session.add(self.admin)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="junin",
            nombre="Municipalidad de Junín",
            tipo="municipio",
            municipio_id=self.admin.id,
        )
        db.session.add(self.tenant)
        db.session.flush()

        self.admin.tenant_id = self.tenant.id
        self.admin.tenant_slug = "junin"
        self.admin.municipio_id = self.tenant.id

        # Seed realistic claims
        categories = ["Luminarias", "Luminarias", "Bacheo", "Poda y Arbolado", "Higiene Urbana"]
        for i, cat in enumerate(categories):
            ticket = MunicipioTicket(
                nro_ticket=f"REC-{1000 + i}",
                municipio_id=self.admin.id,
                tenant_id=self.tenant.id,
                categoria=cat,
                distrito="Centro" if i < 3 else "Los Barriales",
                estado="resuelto" if i % 2 == 0 else "nuevo",
                direccion=f"Calle San Martin {100 + i * 50}",
                latitud=-33.14 + (i * 0.001),
                longitud=-68.48 + (i * 0.001),
                fecha=datetime.now(timezone.utc),
            )
            db.session.add(ticket)

        db.session.commit()

        # Generate auth token
        self.token = jwt.encode(
            {"user_id": self.admin.id, "email": self.admin.email, "role": "admin"},
            self.app.config["SECRET_KEY"],
            algorithm="HS256"
        )
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_traffic_light_endpoint_returns_secretarias(self):
        res = self.client.get("/gov/analytics/traffic-light", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("resumen_general", data)
        self.assertIn("ranking_secretarias", data)
        self.assertEqual(data["resumen_general"]["total_reclamos"], 5)
        self.assertGreater(len(data["ranking_secretarias"]), 0)

    def test_crisis_sentinel_endpoint_detects_anomalies(self):
        res = self.client.get("/gov/analytics/crisis-sentinel", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("estado_centinela", data)
        self.assertIn("alertas_activas", data)

    def test_executive_summary_consolidated_endpoint(self):
        res = self.client.get("/api/gov/analytics/executive-summary", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("scorecards", data)
        self.assertIn("semaforo_secretarias", data)
        self.assertIn("centinela_crisis", data)
