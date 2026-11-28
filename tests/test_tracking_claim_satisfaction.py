import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, TicketSatisfaccion, User


class SatisfactionConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    RATELIMIT_ENABLED = False


class ClaimSatisfactionTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SatisfactionConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        owner = User(name="Municipio", email="satisfaction@test.com", rol="admin", tipo_chat="municipio")
        owner.set_password("secret123")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="satisfaction", nombre="Municipio", tipo="municipio",
                               municipio_id=owner.id, configuracion={})
        db.session.add(tenant)
        db.session.flush()
        self.ticket = MunicipioTicket(tenant_id=tenant.id, municipio_id=owner.id, nro_ticket="900002",
                                      consulta_pin="654321", asunto="Luminaria", pregunta="Consulta",
                                      estado="resuelto")
        db.session.add(self.ticket)
        db.session.commit()
        self.url = f"/api/public/tracking/claims/{self.ticket.id}/satisfaction"

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def ratings(self):
        return [(row.puntuacion, row.comentario) for row in TicketSatisfaccion.query.order_by(TicketSatisfaccion.id)]

    def experience(self):
        return self.client.get("/api/public/tracking/experience?kind=claim&code=M-900002",
                               headers={"X-Tracking-Pin": "654321"}).get_json()

    def test_a_finished_claim_is_rated_once_with_its_pin(self):
        self.assertEqual(self.experience()["satisfaction"]["available"], True)
        first = self.client.post(self.url, json={"pin": "654321", "rating": 4})
        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(first.get_json()["satisfaction"],
                         {**first.get_json()["satisfaction"], "available": False, "submitted": True, "rating": 4})
        again = self.client.post(self.url, json={"pin": "654321", "rating": 1})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()["satisfaction"]["rating"], 4)
        self.assertEqual(self.ratings(), [(8, None)])
        state = self.experience()["satisfaction"]
        self.assertEqual((state["available"], state["submitted"], state["rating"]), (False, True, 4))

    def test_wrong_pin_open_claim_comments_and_bad_ratings_store_nothing(self):
        self.assertEqual(self.client.post(self.url, json={"pin": "000000", "rating": 5}).status_code, 404)
        self.assertEqual(self.client.post(self.url, json={"rating": 5}).status_code, 400)
        for body in ({"pin": "654321", "rating": 6}, {"pin": "654321", "rating": 0},
                     {"pin": "654321", "rating": True}, {"pin": "654321", "rating": "5"},
                     {"pin": "654321", "rating": 5, "comentario": "texto libre"}):
            self.assertEqual(self.client.post(self.url, json=body).status_code, 400, body)
        self.assertEqual(self.client.post("/api/public/tracking/claims/999999/satisfaction",
                                          json={"pin": "654321", "rating": 5}).status_code, 404)
        self.ticket.estado = "en_proceso"
        db.session.commit()
        self.assertEqual(self.client.post(self.url, json={"pin": "654321", "rating": 5}).status_code, 409)
        self.assertEqual(self.experience()["satisfaction"]["available"], False)
        self.assertEqual(self.ratings(), [])


if __name__ == "__main__":
    unittest.main()
