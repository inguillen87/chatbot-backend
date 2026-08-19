import os
import unittest
from datetime import datetime, timezone
import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, User


class CuadrillasPwaTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SECRET_KEY = "test_secret_key"


class CuadrillasPwaTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(CuadrillasPwaTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.operator = User(
            name="Operario Cuadrilla 1",
            email="operario@junin.com",
            role="empleado",
            tipo_chat="municipio",
        )
        self.operator.set_password("123456")
        db.session.add(self.operator)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="junin",
            nombre="Municipalidad de Junín",
            tipo="municipio",
            municipio_id=self.operator.id,
        )
        db.session.add(self.tenant)
        db.session.flush()

        self.operator.tenant_id = self.tenant.id
        self.operator.tenant_slug = "junin"
        self.operator.municipio_id = self.tenant.id

        # Seed 2 tickets
        self.ticket1 = MunicipioTicket(
            nro_ticket="REC-9001",
            municipio_id=self.operator.id,
            tenant_id=self.tenant.id,
            categoria="Alumbrado",
            distrito="Centro",
            estado="nuevo",
            direccion="Av. Mitre 450",
            latitud=-33.1412,
            longitud=-68.4839,
            fecha=datetime.now(timezone.utc),
        )
        self.ticket2 = MunicipioTicket(
            nro_ticket="REC-9002",
            municipio_id=self.operator.id,
            tenant_id=self.tenant.id,
            categoria="Bacheo",
            distrito="Los Barriales",
            estado="nuevo",
            direccion="Ruta 60 Km 12",
            latitud=-33.1250,
            longitud=-68.5120,
            fecha=datetime.now(timezone.utc),
        )
        db.session.add_all([self.ticket1, self.ticket2])
        db.session.commit()

        self.token = jwt.encode(
            {"user_id": self.operator.id, "email": self.operator.email, "role": "empleado"},
            self.app.config["SECRET_KEY"],
            algorithm="HS256"
        )
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_cuadrilla_tareas_sorted_by_gps_proximity(self):
        # Current operator at Av. Mitre (-33.1412, -68.4839)
        res = self.client.get("/api/cuadrillas/tareas?lat=-33.1412&lng=-68.4839", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["total_tareas"], 2)
        self.assertIn("tareas", data)
        # Closer ticket (distance 0.0 km) should be first
        self.assertEqual(data["tareas"][0]["nro_ticket"], "REC-9001")
        self.assertEqual(data["tareas"][0]["distancia_km"], 0.0)

    def test_cuadrilla_iniciar_y_completar_tarea(self):
        # 1. Iniciar
        res_init = self.client.post(f"/api/cuadrillas/tareas/{self.ticket1.id}/iniciar", headers=self.headers)
        self.assertEqual(res_init.status_code, 200)
        self.assertEqual(res_init.get_json()["estado"], "en_proceso")

        # 2. Completar con foto y comentario
        payload = {
            "foto_resolucion_url": "https://res.cloudinary.com/chatboc/image/upload/v1234/reparacion.jpg",
            "comentario": "Luminaria LED 150W reemplazada y operativa."
        }
        res_done = self.client.post(
            f"/api/cuadrillas/tareas/{self.ticket1.id}/completar",
            json=payload,
            headers=self.headers
        )
        self.assertEqual(res_done.status_code, 200)
        self.assertEqual(res_done.get_json()["estado"], "cerrado")

        # Verify DB update
        ticket_db = db.session.get(MunicipioTicket, self.ticket1.id)
        self.assertEqual(ticket_db.estado, "cerrado")
