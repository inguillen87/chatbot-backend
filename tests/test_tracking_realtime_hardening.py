import os
import unittest

import jwt
from flask import Flask

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, TenantProfile, User
from services.live_chat_access import issue_ticket_room_token


class TrackingRealtimeHardeningConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"
    TRACKING_FAILURE_RATE_LIMIT = "2 per minute"
    TRACKING_SUBJECT_FAILURE_RATE_LIMIT = "3 per minute"


class TrackingFailureRateLimitTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TrackingRealtimeHardeningConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        owner = User(
            name="Municipio Rate Limit",
            email="tracking-rate-limit@test.com",
            rol="admin",
            tipo_chat="municipio",
        )
        owner.set_password("secret123")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="tracking-rate-limit",
            nombre="Municipio Rate Limit",
            tipo="municipio",
            municipio_id=owner.id,
            configuracion={},
        )
        db.session.add(tenant)
        db.session.flush()
        self.ticket = MunicipioTicket(
            tenant_id=tenant.id,
            municipio_id=owner.id,
            nro_ticket="900001",
            consulta_pin="654321",
            asunto="Seguimiento realtime",
            pregunta="Consulta protegida",
            estado="en_proceso",
        )
        db.session.add(self.ticket)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _poll(self, pin: str | None, *, remote_addr: str, code: str = "M-900001"):
        suffix = f"&pin={pin}" if pin is not None else ""
        return self.client.get(
            f"/api/public/tracking/experience?kind=claim&code={code}{suffix}",
            environ_base={"REMOTE_ADDR": remote_addr},
        )

    def test_successful_polling_does_not_consume_failure_quota(self):
        responses = [
            self._poll("654321", remote_addr="198.51.100.10")
            for _ in range(6)
        ]

        self.assertEqual([response.status_code for response in responses], [200] * 6)

    def test_repeated_404_attempts_return_429(self):
        first = self._poll("000000", remote_addr="198.51.100.11")
        second = self._poll(
            "111111",
            remote_addr="198.51.100.11",
            code="900001",
        )
        limited = self._poll("222222", remote_addr="198.51.100.11")

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.get_json()["reason_code"], "tracking_rate_limited")
        self.assertTrue(limited.get_json()["retryable"])
        self.assertTrue(limited.headers.get("Retry-After"))

    def test_rotating_ips_cannot_bypass_ticket_failure_quota(self):
        attempts = [
            self._poll(str(index).zfill(6), remote_addr=f"198.51.100.{20 + index}")
            for index in range(4)
        ]

        self.assertEqual([response.status_code for response in attempts[:3]], [404, 404, 404])
        self.assertEqual(attempts[3].status_code, 429)

    def test_400_validation_errors_do_not_consume_failure_quota(self):
        responses = [
            self._poll(None, remote_addr="198.51.100.12")
            for _ in range(4)
        ]
        accepted = self._poll("654321", remote_addr="198.51.100.12")

        self.assertEqual([response.status_code for response in responses], [400] * 4)
        self.assertEqual(accepted.status_code, 200)

    def test_repeated_403_message_attempts_return_429(self):
        endpoint = f"/api/public/tracking/claims/{self.ticket.id}/messages"
        request_kwargs = {
            "json": {"mensaje": "Intento", "pin": "000000"},
            "environ_base": {"REMOTE_ADDR": "198.51.100.13"},
        }

        first = self.client.post(endpoint, **request_kwargs)
        second = self.client.post(endpoint, **request_kwargs)
        limited = self.client.post(endpoint, **request_kwargs)

        self.assertEqual(first.status_code, 403)
        self.assertEqual(second.status_code, 403)
        self.assertEqual(limited.status_code, 429)


class LiveChatRoomTokenTtlTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "room-token-test-secret"

    @staticmethod
    def _ttl_seconds(token: str) -> int:
        payload = jwt.decode(
            token,
            options={"verify_signature": False, "verify_aud": False},
            algorithms=["HS256"],
        )
        return int(payload["exp"]) - int(payload["iat"])

    def test_default_room_token_ttl_is_short(self):
        with self.app.app_context():
            token = issue_ticket_room_token("municipio", 10)

        self.assertEqual(self._ttl_seconds(token), 900)

    def test_room_token_ttl_is_capped_at_one_hour(self):
        with self.app.app_context():
            token = issue_ticket_room_token("pyme", 20, ttl_seconds=7200)

        self.assertEqual(self._ttl_seconds(token), 3600)


if __name__ == "__main__":
    unittest.main()
