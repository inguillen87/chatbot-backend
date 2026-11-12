import secrets
import unittest
from unittest.mock import patch

from flask import has_app_context
from sqlalchemy.exc import SQLAlchemyError

from app import create_app
from config import TestingConfig
from database import db
from models import MunicipioTicket, PymeTicket, TenantProfile, TicketSatisfaccion, User


class TicketCsatConfig(TestingConfig):
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SESSION_TYPE = "null"
    RATELIMIT_STORAGE_URI = "memory://"
    CUTOVER_WRITER_FENCE_ENABLED = False
    OUTBOUND_NOTIFICATIONS_ENABLED = False


class TicketCsatHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TicketCsatConfig)
        password = secrets.token_urlsafe(24)
        accounts = {}
        cls.ticket_ids = {}
        with cls.app.app_context():
            assert db.engine.url.database == ":memory:"
            db.create_all()
            for account, tipo in (("owner", "municipio"), ("foreign", "municipio"), ("pyme", "pyme")):
                slug = f"csat-http-{account}"
                admin = User(name="Responsable sintético", email=f"admin-{slug}@example.invalid", rol="admin")
                admin.set_password(password)
                db.session.add(admin)
                db.session.flush()
                tenant = TenantProfile(
                    slug=slug, nombre="Organización sintética", tipo=tipo,
                    municipio_id=admin.id if tipo == "municipio" else None,
                    pyme_id=admin.id if tipo == "pyme" else None, plan="full", configuracion={},
                )
                db.session.add(tenant)
                db.session.flush()
                admin.tenant_id, admin.tenant_slug = tenant.id, slug
                email = f"user-{slug}@example.invalid"
                user = User(
                    name="Usuario sintético", email=email, rol="usuario", tipo_chat=tipo,
                    tenant_id=tenant.id, tenant_slug=slug, municipio_id=tenant.municipio_id,
                )
                user.set_password(password)
                db.session.add(user)
                db.session.flush()
                accounts[account] = (slug, email)
                if account == "owner":
                    accounts["owner_admin"] = (slug, admin.email)
                    for purpose in ("write", "invalid", "foreign", "failure", "zero", "seeded", "retry", "aggregate"):
                        ticket = MunicipioTicket(
                            tenant_id=tenant.id, municipio_id=admin.id, user_id=user.id,
                            pregunta="Caso sintético", categoria="arbolado", estado="cerrado", canal_ingreso="web",
                        )
                        db.session.add(ticket)
                        db.session.flush()
                        cls.ticket_ids[purpose] = ticket.id
                    db.session.add(TicketSatisfaccion(ticket_id=cls.ticket_ids["seeded"], tipo=tipo, puntuacion=4))
                elif account == "pyme":
                    ticket = PymeTicket(tenant_id=tenant.id, user_id=user.id, pregunta="Pedido sintético", nro_ticket=1)
                    db.session.add(ticket)
                    db.session.flush()
                    cls.ticket_ids["pyme"] = ticket.id
            db.session.commit()

        cls.clients = {}
        cls.headers = {}
        for account, (slug, email) in accounts.items():
            client = cls.app.test_client()
            login = client.post("/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200, f"Login de prueba devolvió {login.status_code}"
            cls.clients[account] = client
            cls.headers[account] = {
                "Authorization": "Bearer " + login.get_json()["token"],
                "X-Tenant": slug,
                "X-Tenant-Slug": slug,
            }

    def endpoint(self, purpose, tipo="municipio"):
        return f"/api/tickets/{tipo}/{self.ticket_ids[purpose]}/encuesta"

    def rows(self, purpose, tipo="municipio"):
        self.assertFalse(has_app_context())
        with self.app.app_context():
            rows = TicketSatisfaccion.query.filter_by(tipo=tipo, ticket_id=self.ticket_ids[purpose]).all()
            return [(row.id, row.puntuacion, row.comentario) for row in rows]

    def test_ack_confirms_committed_id_and_readback(self):
        url = self.endpoint("write")
        with patch.object(db.session, "commit", wraps=db.session.commit) as commit:
            response = self.clients["owner"].post(
                url, headers=self.headers["owner"], json={"puntuacion": 5, "comentario": "Comentario sintético"},
            )
            commit.assert_called_once_with()
        self.assertEqual(response.status_code, 200)
        ack = response.get_json()
        self.assertTrue(ack["success"])
        self.assertIsInstance(ack["encuesta_id"], int)
        self.assertEqual(self.rows("write"), [(ack["encuesta_id"], 5, "Comentario sintético")])
        readback = self.clients["owner"].get(url, headers=self.headers["owner"])
        self.assertEqual(readback.status_code, 200)
        self.assertEqual(readback.get_json()["puntuacion"], 5)
        self.assertEqual(readback.get_json()["comentario"], "Comentario sintético")

    def test_failed_commit_rolls_back_flushed_insert(self):
        url = self.endpoint("failure")

        def fail_commit():
            row = TicketSatisfaccion.query.filter_by(tipo="municipio", ticket_id=self.ticket_ids["failure"]).one()
            self.assertIsInstance(row.id, int)
            raise SQLAlchemyError("Fallo sintético")

        with (
            patch.object(db.session, "commit", side_effect=fail_commit) as commit,
            patch.object(db.session, "rollback", wraps=db.session.rollback) as rollback,
        ):
            response = self.clients["owner"].post(url, headers=self.headers["owner"], json={"puntuacion": 4})
            commit.assert_called_once_with()
            rollback.assert_called_once_with()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("success", response.get_json())
        self.assertEqual(self.rows("failure"), [])
        readback = self.clients["owner"].get(url, headers=self.headers["owner"])
        self.assertEqual(readback.status_code, 200)
        self.assertEqual(readback.get_json(), {})

    def test_identical_retry_returns_original_id_without_erasing_distinct_ratings(self):
        url = self.endpoint("retry")
        client, headers = self.clients["owner"], self.headers["owner"]
        first = client.post(url, headers=headers, json={"puntuacion": 7, "comentario": "Muestra"})
        duplicate = client.post(url, headers=headers, json={"puntuacion": "7", "comentario": "Muestra"})
        changed = client.post(url, headers=headers, json={"puntuacion": 8, "comentario": "Muestra"})
        self.assertEqual([response.status_code for response in (first, duplicate, changed)], [200, 200, 200])
        first_id = first.get_json()["encuesta_id"]
        self.assertEqual(duplicate.get_json()["encuesta_id"], first_id)
        changed_id = changed.get_json()["encuesta_id"]
        self.assertNotEqual(changed_id, first_id)
        self.assertEqual(sorted(self.rows("retry")), [(first_id, 7, "Muestra"), (changed_id, 8, "Muestra")])
        self.assertEqual(client.get(url, headers=headers).get_json()["puntuacion"], 7)

    def test_foreign_read_does_not_distinguish_empty_from_rated_ticket(self):
        client, headers = self.clients["foreign"], self.headers["foreign"]
        empty = client.get(self.endpoint("foreign"), headers=headers)
        rated = client.get(self.endpoint("seeded"), headers=headers)
        self.assertEqual((empty.status_code, rated.status_code), (403, 403))
        self.assertEqual(empty.get_json(), rated.get_json())

    def test_persisted_rating_counts_once_in_the_authorized_overview(self):
        overview_url = "/api/v2/analytics/overview"
        admin, admin_headers = self.clients["owner_admin"], self.headers["owner_admin"]
        resident, resident_headers = self.clients["owner"], self.headers["owner"]

        def overview():
            response = admin.get(overview_url, headers=admin_headers)
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["tenant_slug"], "csat-http-owner")
            self.assertIsNone(payload["csat_score"])
            self.assertIsNone(payload["nps_score"])
            self.assertEqual(payload["ticket_satisfaction"]["state"], "observed")
            return payload["ticket_satisfaction"]["rated_ticket_count"]

        before = overview()
        url = self.endpoint("aggregate")
        first = resident.post(url, headers=resident_headers, json={"puntuacion": 0})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(overview(), before + 1)
        duplicate = resident.post(url, headers=resident_headers, json={"puntuacion": "0"})
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.get_json(), first.get_json())
        self.assertEqual(overview(), before + 1)
        self.assertEqual(resident.get(overview_url, headers=resident_headers).status_code, 403)

    def test_nps_bounds_and_legacy_numeric_string(self):
        for account, purpose, tipo, payload, expected in (
            ("owner", "zero", "municipio", 0, 0),
            ("pyme", "pyme", "pyme", "10", 10),
        ):
            with self.subTest(tipo=tipo, expected=expected):
                url = self.endpoint(purpose, tipo)
                response = self.clients[account].post(url, headers=self.headers[account], json={"puntuacion": payload})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.rows(purpose, tipo), [(response.get_json()["encuesta_id"], expected, None)])
                readback = self.clients[account].get(url, headers=self.headers[account])
                self.assertEqual(readback.status_code, 200)
                self.assertEqual(readback.get_json()["puntuacion"], expected)

    def test_rejects_invalid_ratings_without_persistence(self):
        url = self.endpoint("invalid")
        for payload in ({"puntuacion": -1}, {"puntuacion": 11}, {"puntuacion": True},
                        {"puntuacion": 3.5}, {"puntuacion": "sin puntaje"},
                        {"puntuacion": 5, "comentario": {"texto": "incorrecto"}}, {}, [5]):
            with self.subTest(payload=payload):
                response = self.clients["owner"].post(url, headers=self.headers["owner"], json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("success", response.get_json())
        self.assertEqual(self.rows("invalid"), [])

    def test_preserves_existing_ratings_and_foreign_scope(self):
        readback = self.clients["owner"].get(self.endpoint("seeded"), headers=self.headers["owner"])
        self.assertEqual(readback.status_code, 200)
        self.assertEqual(readback.get_json()["puntuacion"], 4)
        denied = self.clients["foreign"].post(
            self.endpoint("foreign"), headers=self.headers["foreign"], json={"puntuacion": 5},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.rows("foreign"), [])
