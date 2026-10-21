from __future__ import annotations

import importlib.util
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
import jwt
import sqlalchemy as sa

from app import create_app, db
from config import Config
from models import (
    Conversation,
    ChatSessionContext,
    Message,
    MunicipioChatIdempotencyReceipt,
    MunicipioTicket,
    TenantProfile,
    TicketComentario,
    User,
)
from models_memory import Contact, InteractionEvent
from routes.v2.tenants import create_demo_session_token
from services.municipio_chat_idempotency import (
    IdempotencyRequestInProgress,
    IdempotencyResponseExpired,
    build_identity,
    canonical_request_hash,
    claim_or_replay,
    complete_receipt,
    execution_lock,
    expire_completed_response_snapshots,
)


class MunicipioChatIdempotencyConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    EMAIL_NOTIFICATIONS_ENABLED = False
    ENABLE_EMAIL_NOTIFICATIONS = False
    MUNICIPIO_CHAT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS = 5


class MunicipioChatIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self._temp_dir.name) / "municipio-chat-idempotency.sqlite3"

        class FileBackedMunicipioChatIdempotencyConfig(
            MunicipioChatIdempotencyConfig
        ):
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{database_path.as_posix()}"

        self.app = create_app(FileBackedMunicipioChatIdempotencyConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        self.admin, self.tenant = self._create_tenant("municipio-idem-a", "idem-a")

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self._temp_dir.cleanup()

    def _create_tenant(self, slug: str, token: str):
        admin = User(
            name=f"Admin {slug}",
            email=f"{slug}@test.com",
            password_hash="hash",
            rol="admin",
            tipo_chat="municipio",
            tenant_slug=slug,
            token=f"entity-token-{token}",
        )
        db.session.add(admin)
        db.session.flush()
        tenant = TenantProfile(
            slug=slug,
            nombre=f"Municipio {slug}",
            tipo="municipio",
            vertical="gobierno",
            plan="full",
            municipio_id=admin.id,
        )
        db.session.add(tenant)
        db.session.flush()
        admin.tenant_id = tenant.id
        db.session.commit()
        return admin, tenant

    def _demo_request(self, tenant: TenantProfile, *, key: str, question: str):
        demo_session_id = create_demo_session_token(
            tenant_slug=tenant.slug,
            sector="gobierno",
            rubro="gobierno",
        )
        url = (
            f"/api/ask/municipio?tenant_slug={tenant.slug}"
            f"&demo_session_id={demo_session_id}"
        )
        payload = {
            "pregunta": question,
            "demo_mode": True,
            "tenant_slug": tenant.slug,
            "location": {
                "lat": -34.6101,
                "lng": -58.4402,
                "address": "Escuela 12, San Martin 500",
            },
        }
        headers = {
            "Origin": "https://www.chatboc.ar",
            "X-Chat-Session-Id": "sid-municipio-idempotency",
            "X-Anon-Id": "anon-municipio-idempotency",
            "Idempotency-Key": key,
            "X-Request-Id": "municipio-idempotency-request",
        }
        return url, payload, headers

    def _header_demo_request(
        self,
        tenant: TenantProfile,
        *,
        key: str,
        question: str,
        demo_session_id: str,
    ):
        url = (
            f"/api/ask/municipio?tenant_slug={tenant.slug}"
            f"&entityToken={self.admin.token}"
        )
        payload = {
            "pregunta": question,
            "demo_mode": True,
            "tenant_slug": tenant.slug,
        }
        headers = {
            "Origin": "https://www.chatboc.ar",
            "X-Chat-Session-Id": "sid-header-demo-idempotency",
            "X-Anon-Id": "anon-header-demo-idempotency",
            "Idempotency-Key": key,
            "X-Demo-Session-Id": demo_session_id,
        }
        return url, payload, headers

    @staticmethod
    def _tamper_jwt(token: str) -> str:
        header, payload, signature = token.split(".")
        replacement = "A" if signature[0] != "A" else "B"
        return ".".join((header, payload, replacement + signature[1:]))

    @staticmethod
    def _effect_counts():
        return {
            "tickets": MunicipioTicket.query.count(),
            "comments": TicketComentario.query.count(),
            "conversations": Conversation.query.count(),
            "messages": Message.query.count(),
            "contacts": Contact.query.count(),
            "interactions": InteractionEvent.query.count(),
        }

    def test_duplicate_replays_exact_response_without_second_domain_or_crm_effect(self):
        raw_key = "municipio-claim-idempotency-0001"
        url, payload, headers = self._demo_request(
            self.tenant,
            key=raw_key,
            question="Hay un bache peligroso frente a la escuela",
        )

        first = self.client.post(url, json=payload, headers=headers)
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(first.headers.get("X-Idempotency-Status"), "accepted")
        self.assertEqual(first.headers.get("Idempotency-Replayed"), "false")
        exposed_headers = first.headers.get("Access-Control-Expose-Headers") or ""
        self.assertIn("X-Chat-Idempotency-Contract", exposed_headers)
        self.assertIn("X-Idempotency-Status", exposed_headers)
        self.assertIn("Idempotency-Replayed", exposed_headers)
        first_payload = first.get_json()
        counts_after_first = self._effect_counts()
        self.assertEqual(counts_after_first["tickets"], 1)
        self.assertGreaterEqual(counts_after_first["comments"], 1)
        self.assertEqual(counts_after_first["messages"], 2)

        replay = self.client.post(url, json=dict(reversed(list(payload.items()))), headers=headers)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json(), first_payload)
        self.assertEqual(replay.headers.get("X-Idempotency-Status"), "replayed")
        self.assertEqual(replay.headers.get("Idempotency-Replayed"), "true")
        self.assertEqual(self._effect_counts(), counts_after_first)

        receipt = MunicipioChatIdempotencyReceipt.query.one()
        self.assertEqual(receipt.status, receipt.STATUS_COMPLETED)
        self.assertEqual(receipt.response_json, first_payload)
        self.assertNotEqual(receipt.idempotency_key_hash, raw_key)
        self.assertEqual(len(receipt.idempotency_key_hash), 64)
        self.assertEqual(len(receipt.actor_scope_hash), 64)
        self.assertEqual(len(receipt.request_hash), 64)
        self.assertNotIn(raw_key, str(receipt.response_json))
        context = db.session.get(
            ChatSessionContext,
            headers["X-Chat-Session-Id"],
        )
        self.assertIsNotNone(context)
        self.assertEqual(context.tenant_id, self.tenant.id)
        ticket = MunicipioTicket.query.one()
        self.assertEqual(ticket.tenant_id, self.tenant.id)
        self.assertEqual(receipt.tenant_id, ticket.tenant_id)

    def test_same_key_with_changed_payload_returns_409_without_new_effects(self):
        url, payload, headers = self._demo_request(
            self.tenant,
            key="municipio-claim-conflict-0001",
            question="Hay un bache peligroso frente a la escuela",
        )
        first = self.client.post(url, json=payload, headers=headers)
        self.assertEqual(first.status_code, 200, first.get_json())
        counts_after_first = self._effect_counts()

        changed = dict(payload)
        changed["pregunta"] = "Hay una luminaria apagada frente a la plaza"
        conflict = self.client.post(url, json=changed, headers=headers)

        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(
            conflict.get_json()["reason_code"],
            "municipio_chat_idempotency_payload_conflict",
        )
        self.assertEqual(conflict.headers.get("Idempotency-Replayed"), "false")
        self.assertEqual(self._effect_counts(), counts_after_first)
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 1)

    def test_invalid_key_is_rejected_before_chat_execution(self):
        url, payload, headers = self._demo_request(
            self.tenant,
            key="short",
            question="Hay un bache peligroso frente a la escuela",
        )
        with patch("routes.chat._procesar_chat") as processor:
            response = self.client.post(url, json=payload, headers=headers)

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "invalid_idempotency_key")
        processor.assert_not_called()
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)

    def test_invalid_demo_header_is_rejected_before_replaying_completed_receipt(self):
        valid_token = create_demo_session_token(
            tenant_slug=self.tenant.slug,
            sector="gobierno",
            rubro="gobierno",
        )
        url, payload, headers = self._header_demo_request(
            self.tenant,
            key="municipio-demo-auth-replay-0001",
            question="Mensaje con respuesta sensible",
            demo_session_id=valid_token,
        )
        expired_token = jwt.encode(
            {
                "kind": "demo_session",
                "tenant_slug": self.tenant.slug,
                "sector": "gobierno",
                "rubro": "gobierno",
                "jti": "a" * 64,
                "iat": datetime.now(timezone.utc) - timedelta(hours=2),
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            self.app.config.get("DEMO_SESSION_SECRET")
            or self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        with patch(
            "routes.chat._procesar_chat",
            return_value=({"message_body": "respuesta privada", "consulta_pin": "123456"}, 200),
        ) as processor:
            first = self.client.post(url, json=payload, headers=headers)
            invalid = self.client.post(
                url,
                json=payload,
                headers={
                    **headers,
                    "X-Demo-Session-Id": self._tamper_jwt(valid_token),
                },
            )
            malformed = self.client.post(
                url,
                json=payload,
                headers={**headers, "X-Demo-Session-Id": "truncated-demo-token"},
            )
            expired = self.client.post(
                url,
                json=payload,
                headers={**headers, "X-Demo-Session-Id": expired_token},
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(first.headers.get("X-Idempotency-Status"), "accepted")
        self.assertEqual(invalid.status_code, 400, invalid.get_json())
        self.assertEqual(invalid.get_json()["reason_code"], "demo_session_expired")
        self.assertEqual(invalid.headers.get("X-Idempotency-Status"), "rejected")
        self.assertEqual(invalid.headers.get("Idempotency-Replayed"), "false")
        self.assertNotIn("respuesta privada", str(invalid.get_json()))
        self.assertNotIn("123456", str(invalid.get_json()))
        self.assertEqual(malformed.status_code, 400, malformed.get_json())
        self.assertEqual(malformed.get_json()["reason_code"], "demo_session_expired")
        self.assertNotIn("respuesta privada", str(malformed.get_json()))
        self.assertNotIn("123456", str(malformed.get_json()))
        self.assertEqual(expired.status_code, 400, expired.get_json())
        self.assertEqual(expired.get_json()["reason_code"], "demo_session_expired")
        self.assertNotIn("respuesta privada", str(expired.get_json()))
        self.assertNotIn("123456", str(expired.get_json()))
        self.assertEqual(processor.call_count, 1)

    def test_distinct_valid_demo_header_token_conflicts_instead_of_replaying(self):
        first_token = create_demo_session_token(
            tenant_slug=self.tenant.slug,
            sector="gobierno",
            rubro="gobierno",
        )
        second_token = create_demo_session_token(
            tenant_slug=self.tenant.slug,
            sector="gobierno",
            rubro="gobierno",
        )
        self.assertNotEqual(first_token, second_token)
        url, payload, headers = self._header_demo_request(
            self.tenant,
            key="municipio-demo-token-binding-0001",
            question="Mismo payload en otra sesion demo",
            demo_session_id=first_token,
        )

        with patch(
            "routes.chat._procesar_chat",
            return_value=({"message_body": "primera sesion"}, 200),
        ) as processor:
            first = self.client.post(url, json=payload, headers=headers)
            conflict = self.client.post(
                url,
                json=payload,
                headers={**headers, "X-Demo-Session-Id": second_token},
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(
            conflict.get_json()["reason_code"],
            "municipio_chat_idempotency_payload_conflict",
        )
        self.assertEqual(processor.call_count, 1)

    def test_same_key_and_session_are_isolated_across_tenants(self):
        _, tenant_b = self._create_tenant("municipio-idem-b", "idem-b")
        key = "municipio-cross-tenant-key-0001"
        url_a, payload_a, headers_a = self._demo_request(
            self.tenant,
            key=key,
            question="Hay un bache peligroso frente a la escuela",
        )
        url_b, payload_b, headers_b = self._demo_request(
            tenant_b,
            key=key,
            question="Hay un bache peligroso frente a la escuela",
        )

        calls = 0

        def fake_processor(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {"ok": True, "sequence": calls}, 200

        with patch("routes.chat._procesar_chat", side_effect=fake_processor):
            response_a = self.client.post(url_a, json=payload_a, headers=headers_a)
            response_b = self.client.post(url_b, json=payload_b, headers=headers_b)

        self.assertEqual(response_a.status_code, 200, response_a.get_json())
        self.assertEqual(response_b.status_code, 200, response_b.get_json())
        self.assertEqual(calls, 2)
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 2)
        self.assertEqual(
            {receipt.tenant_id for receipt in MunicipioChatIdempotencyReceipt.query.all()},
            {self.tenant.id, tenant_b.id},
        )

    def test_existing_session_from_other_tenant_is_rejected_before_execution(self):
        _, tenant_b = self._create_tenant("municipio-idem-b", "idem-b")
        foreign_context = ChatSessionContext(
            chat_session_id="sid-municipio-idempotency",
            tenant_id=self.tenant.id,
            anon_id="anon-municipio-idempotency",
            context_data={
                "last_bot_response": {
                    "message_body": "foreign-sensitive-marker",
                }
            },
        )
        db.session.add(foreign_context)
        db.session.commit()
        url, payload, headers = self._demo_request(
            tenant_b,
            key="municipio-cross-tenant-session-0001",
            question="Hay un bache peligroso frente a la escuela",
        )

        with patch("routes.chat._procesar_chat") as processor:
            response = self.client.post(url, json=payload, headers=headers)

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "municipio_chat_idempotency_scope_conflict",
        )
        self.assertNotIn("foreign-sensitive-marker", str(response.get_json()))
        processor.assert_not_called()
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)

    def test_existing_session_from_other_anonymous_actor_is_rejected_without_leak(self):
        db.session.add(
            ChatSessionContext(
                chat_session_id="sid-municipio-idempotency",
                tenant_id=self.tenant.id,
                anon_id="victim-anon-id",
                context_data={
                    "last_bot_response": {
                        "message_body": "victim-sensitive-marker",
                        "consulta_pin": "654321",
                    }
                },
            )
        )
        db.session.commit()
        url, payload, headers = self._demo_request(
            self.tenant,
            key="municipio-cross-anon-session-0001",
            question="Hay un bache peligroso frente a la escuela",
        )
        headers["X-Anon-Id"] = "attacker-anon-id"

        with patch("routes.chat._procesar_chat") as processor:
            response = self.client.post(url, json=payload, headers=headers)

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "municipio_chat_idempotency_scope_conflict",
        )
        self.assertNotIn("victim-sensitive-marker", str(response.get_json()))
        self.assertNotIn("654321", str(response.get_json()))
        processor.assert_not_called()
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 0)

    def test_existing_session_from_other_anon_is_rejected_without_idempotency_key(self):
        db.session.add(
            ChatSessionContext(
                chat_session_id="sid-municipio-idempotency",
                tenant_id=self.tenant.id,
                anon_id="victim-anon-id",
                context_data={
                    "last_user_message": "Hay un bache peligroso frente a la escuela",
                    "last_user_action_id": "",
                    "last_user_message_time": datetime.now(timezone.utc).isoformat(),
                    "last_bot_response": {
                        "message_body": "victim-sensitive-marker-no-key",
                        "consulta_pin": "112233",
                    },
                },
            )
        )
        db.session.commit()
        url, payload, headers = self._demo_request(
            self.tenant,
            key="unused-without-idempotency-header",
            question="Hay un bache peligroso frente a la escuela",
        )
        headers.pop("Idempotency-Key")
        headers["X-Anon-Id"] = "attacker-anon-id"

        response = self.client.post(url, json=payload, headers=headers)

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "municipio_chat_idempotency_scope_conflict",
        )
        self.assertNotIn("victim-sensitive-marker-no-key", str(response.get_json()))
        self.assertNotIn("112233", str(response.get_json()))
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)

    def test_authenticated_transition_requires_exact_anon_and_binds_user(self):
        from routes.chat import _assert_existing_municipio_session_tenant
        from services.municipio_chat_idempotency import IdempotencyScopeConflict

        context = ChatSessionContext(
            chat_session_id="sid-auth-transition",
            tenant_id=self.tenant.id,
            anon_id="anon-before-login",
            context_data={},
        )
        db.session.add(context)
        db.session.commit()

        with self.assertRaises(IdempotencyScopeConflict):
            _assert_existing_municipio_session_tenant(
                chat_session_id=context.chat_session_id,
                tenant_id=self.tenant.id,
                current_user=self.admin,
                anon_id="different-anon",
                for_update=True,
                bind_authenticated_transition=True,
            )
        db.session.rollback()

        transitioned = _assert_existing_municipio_session_tenant(
            chat_session_id=context.chat_session_id,
            tenant_id=self.tenant.id,
            current_user=self.admin,
            anon_id="anon-before-login",
            for_update=True,
            bind_authenticated_transition=True,
        )
        self.assertTrue(transitioned)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(
            db.session.get(ChatSessionContext, context.chat_session_id).user_id,
            self.admin.id,
        )

        other_user = User(
            name="Otro vecino",
            email="otro-vecino@test.com",
            password_hash="hash",
            rol="usuario",
            tipo_chat="municipio",
            tenant_id=self.tenant.id,
        )
        db.session.add(other_user)
        db.session.commit()
        with self.assertRaises(IdempotencyScopeConflict):
            _assert_existing_municipio_session_tenant(
                chat_session_id=context.chat_session_id,
                tenant_id=self.tenant.id,
                current_user=other_user,
                anon_id="anon-before-login",
                for_update=True,
                bind_authenticated_transition=True,
            )

    def test_racing_session_scope_conflict_discards_processing_receipt(self):
        _, tenant_b = self._create_tenant("municipio-idem-b", "idem-b")
        db.session.add(
            ChatSessionContext(
                chat_session_id="sid-municipio-idempotency",
                tenant_id=self.tenant.id,
                anon_id="anon-municipio-idempotency",
                context_data={},
            )
        )
        db.session.commit()
        url, payload, headers = self._demo_request(
            tenant_b,
            key="municipio-racing-session-scope-0001",
            question="Hay un bache peligroso frente a la escuela",
        )

        with patch(
            "routes.chat._assert_existing_municipio_session_tenant",
            return_value=None,
        ):
            response = self.client.post(url, json=payload, headers=headers)

        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "municipio_chat_idempotency_scope_conflict",
        )
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)

    def test_concurrent_same_key_executes_processor_once_and_replays_winner(self):
        key = "municipio-concurrent-key-0001"
        query = f"tenant_slug={self.tenant.slug}&entityToken={self.admin.token}"
        url = f"/api/ask/municipio?{query}"
        payload = {"pregunta": "Mensaje concurrente", "tenant_slug": self.tenant.slug}
        headers = {
            "X-Chat-Session-Id": "sid-concurrent-idempotency",
            "X-Anon-Id": "anon-concurrent-idempotency",
            "Idempotency-Key": key,
        }
        calls = 0
        calls_guard = threading.Lock()

        def fake_processor(*_args, **_kwargs):
            nonlocal calls
            with calls_guard:
                calls += 1
                sequence = calls
            time.sleep(0.15)
            return {"ok": True, "sequence": sequence}, 200

        def post_once():
            with self.app.test_client() as client:
                response = client.post(url, json=payload, headers=headers)
                return response.status_code, response.get_json(), response.headers.get(
                    "X-Idempotency-Status"
                )

        with patch("routes.chat._procesar_chat", side_effect=fake_processor):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: post_once(), range(2)))

        self.assertEqual(calls, 1)
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(
            {result[2] for result in results},
            {"accepted", "replayed"},
        )

    def test_crashed_processing_receipt_requires_reconciliation_without_retry_loop(self):
        url, payload, headers = self._demo_request(
            self.tenant,
            key="municipio-crash-reconciliation-0001",
            question="Mensaje interrumpido",
        )

        with patch(
            "routes.chat._procesar_chat",
            side_effect=RuntimeError("simulated hard crash after durable claim"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated hard crash"):
                self.client.post(url, json=payload, headers=headers)

        receipt = MunicipioChatIdempotencyReceipt.query.one()
        self.assertEqual(receipt.status, receipt.STATUS_PROCESSING)

        with patch("routes.chat._procesar_chat") as processor:
            retry = self.client.post(url, json=payload, headers=headers)

        self.assertEqual(retry.status_code, 503, retry.get_json())
        self.assertEqual(
            retry.get_json()["reason_code"],
            "municipio_chat_idempotency_reconciliation_required",
        )
        self.assertFalse(retry.get_json()["retryable"])
        self.assertIsNone(retry.headers.get("Retry-After"))
        self.assertEqual(retry.headers.get("X-Idempotency-Status"), "rejected")
        processor.assert_not_called()

    def test_active_execution_lock_timeout_remains_retryable_425(self):
        url, payload, headers = self._demo_request(
            self.tenant,
            key="municipio-active-lock-0001",
            question="Mensaje con lock activo",
        )

        with patch(
            "services.municipio_chat_idempotency.execution_lock",
            side_effect=IdempotencyRequestInProgress("active execution lock"),
        ), patch("routes.chat._procesar_chat") as processor:
            response = self.client.post(url, json=payload, headers=headers)

        self.assertEqual(response.status_code, 425, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "municipio_chat_idempotency_in_progress",
        )
        self.assertTrue(response.get_json()["retryable"])
        self.assertEqual(response.headers.get("Retry-After"), "1")
        self.assertEqual(response.headers.get("X-Idempotency-Status"), "rejected")
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 0)
        processor.assert_not_called()

    def test_distinct_invalid_json_bodies_do_not_share_empty_body_hash(self):
        url = (
            f"/api/ask/municipio?tenant_slug={self.tenant.slug}"
            f"&entityToken={self.admin.token}"
        )
        headers = {
            "Origin": "https://www.chatboc.ar",
            "X-Chat-Session-Id": "sid-invalid-json-idempotency",
            "X-Anon-Id": "anon-invalid-json-idempotency",
            "Idempotency-Key": "municipio-invalid-json-0001",
        }

        with patch(
            "routes.chat._procesar_chat",
            return_value=({"error": {"code": 400, "message": "JSON invalido"}}, 400),
        ) as processor:
            first = self.client.post(
                url,
                data=b'{"pregunta":',
                content_type="application/json",
                headers=headers,
            )
            conflict = self.client.post(
                url,
                data=b'{"pregunta":"otro"',
                content_type="application/json",
                headers=headers,
            )

        self.assertEqual(first.status_code, 400, first.get_json())
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(
            conflict.get_json()["reason_code"],
            "municipio_chat_idempotency_payload_conflict",
        )
        self.assertEqual(processor.call_count, 1)

    def test_same_tenant_key_is_isolated_by_chat_session(self):
        key = "municipio-cross-session-key-0001"
        query = f"tenant_slug={self.tenant.slug}&entityToken={self.admin.token}"
        url = f"/api/ask/municipio?{query}"
        payload = {"pregunta": "Mensaje aislado", "tenant_slug": self.tenant.slug}
        calls = 0

        def fake_processor(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {"ok": True, "sequence": calls}, 200

        with patch("routes.chat._procesar_chat", side_effect=fake_processor):
            first = self.client.post(
                url,
                json=payload,
                headers={
                    "X-Chat-Session-Id": "sid-idempotency-actor-a",
                    "X-Anon-Id": "anon-idempotency-actor",
                    "Idempotency-Key": key,
                },
            )
            second = self.client.post(
                url,
                json=payload,
                headers={
                    "X-Chat-Session-Id": "sid-idempotency-actor-b",
                    "X-Anon-Id": "anon-idempotency-actor",
                    "Idempotency-Key": key,
                },
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertEqual(calls, 2)
        self.assertEqual(MunicipioChatIdempotencyReceipt.query.count(), 2)
        self.assertEqual(
            len(
                {
                    receipt.actor_scope_hash
                    for receipt in MunicipioChatIdempotencyReceipt.query.all()
                }
            ),
            2,
        )

    def test_same_session_key_is_isolated_by_anonymous_visitor(self):
        key = "municipio-cross-anon-key-0001"
        query = f"tenant_slug={self.tenant.slug}&entityToken={self.admin.token}"
        url = f"/api/ask/municipio?{query}"
        payload = {"pregunta": "Mensaje aislado", "tenant_slug": self.tenant.slug}
        calls = 0

        def fake_processor(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {"ok": True, "sequence": calls}, 200

        common_headers = {
            "X-Chat-Session-Id": "sid-idempotency-anon-boundary",
            "Idempotency-Key": key,
        }
        with patch("routes.chat._procesar_chat", side_effect=fake_processor):
            first = self.client.post(
                url,
                json=payload,
                headers={**common_headers, "X-Anon-Id": "anon-idempotency-a"},
            )
            second = self.client.post(
                url,
                json=payload,
                headers={**common_headers, "X-Anon-Id": "anon-idempotency-b"},
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertEqual(calls, 2)
        receipts = MunicipioChatIdempotencyReceipt.query.all()
        self.assertEqual(len(receipts), 2)
        self.assertEqual(len({receipt.actor_scope_hash for receipt in receipts}), 2)

    def test_query_hash_preserves_duplicate_value_order(self):
        first = canonical_request_hash(
            endpoint="/api/ask/municipio",
            query_items=[
                ("tenant", "junin"),
                ("mode", "public"),
                ("mode", "private"),
            ],
            json_payload={"pregunta": "hola"},
        )
        distinct_keys_reordered = canonical_request_hash(
            endpoint="/api/ask/municipio",
            query_items=[
                ("mode", "public"),
                ("mode", "private"),
                ("tenant", "junin"),
            ],
            json_payload={"pregunta": "hola"},
        )
        duplicates_reversed = canonical_request_hash(
            endpoint="/api/ask/municipio",
            query_items=[
                ("tenant", "junin"),
                ("mode", "private"),
                ("mode", "public"),
            ],
            json_payload={"pregunta": "hola"},
        )

        self.assertEqual(first, distinct_keys_reordered)
        self.assertNotEqual(first, duplicates_reversed)

    def test_retention_scrubs_response_but_keeps_non_reexecuting_tombstone(self):
        identity = build_identity(
            tenant_id=self.tenant.id,
            actor_kind="session",
            actor_id="retention-session:retention-anon",
            idempotency_key="municipio-retention-key-0001",
            request_hash="a" * 64,
        )
        decision = claim_or_replay(identity)
        complete_receipt(
            decision.receipt_id,
            response_json={"message_body": "citizen response"},
            response_status=200,
            response_request_id="retention-request",
        )
        receipt = db.session.get(
            MunicipioChatIdempotencyReceipt,
            decision.receipt_id,
        )
        now = datetime.now(timezone.utc)
        receipt.completed_at = now - timedelta(days=31)
        db.session.commit()

        self.assertEqual(expire_completed_response_snapshots(now=now), 1)
        db.session.expire_all()
        receipt = db.session.get(
            MunicipioChatIdempotencyReceipt,
            decision.receipt_id,
        )
        self.assertEqual(receipt.status, receipt.STATUS_EXPIRED)
        self.assertIsNone(receipt.response_json)
        self.assertIsNone(receipt.response_status)
        self.assertIsNone(receipt.response_request_id)
        self.assertEqual(len(receipt.idempotency_key_hash), 64)
        with self.assertRaises(IdempotencyResponseExpired):
            claim_or_replay(identity)

    def test_postgres_lock_uses_transaction_scoped_advisory_lock(self):
        identity = build_identity(
            tenant_id=self.tenant.id,
            actor_kind="session",
            actor_id="postgres-lock-session",
            idempotency_key="municipio-postgres-lock-0001",
            request_hash="b" * 64,
        )

        class FakeResult:
            @staticmethod
            def scalar():
                return True

        class FakeTransaction:
            def __init__(self):
                self.is_active = True
                self.committed = False
                self.rolled_back = False

            def commit(self):
                self.committed = True
                self.is_active = False

            def rollback(self):
                self.rolled_back = True
                self.is_active = False

        class FakeConnection:
            def __init__(self):
                self.transaction = FakeTransaction()
                self.statements = []
                self.closed = False

            def begin(self):
                return self.transaction

            def execute(self, statement, _params):
                self.statements.append(str(statement))
                return FakeResult()

            def close(self):
                self.closed = True

        fake_bind = type("FakeBind", (), {
            "dialect": type("FakeDialect", (), {"name": "postgresql"})()
        })()
        connection = FakeConnection()

        with patch(
            "services.municipio_chat_idempotency.db.session.get_bind",
            return_value=fake_bind,
        ), patch(
            "services.municipio_chat_idempotency.db.engine.connect",
            return_value=connection,
        ):
            with execution_lock(identity):
                pass

        self.assertTrue(connection.transaction.committed)
        self.assertFalse(connection.transaction.rolled_back)
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.statements), 1)
        self.assertIn("pg_try_advisory_xact_lock", connection.statements[0])
        self.assertNotIn("pg_advisory_unlock", connection.statements[0])


def test_municipio_chat_idempotency_migration_supports_sqlite(tmp_path):
    root = Path(__file__).resolve().parents[1]
    migration_path = (
        root
        / "migrations"
        / "versions"
        / "20260825_add_municipio_chat_idempotency.py"
    )
    spec = importlib.util.spec_from_file_location(
        "municipio_chat_idempotency_migration",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "20260825_chat_idempotency_v1"
    assert migration.down_revision == "20260825_legacy_municipio_ticket_scope_repair_v1"

    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'chat-idem.sqlite3').as_posix()}")
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        inspector = sa.inspect(connection)
        assert "municipio_chat_idempotency_receipt" in inspector.get_table_names()
        unique_constraints = inspector.get_unique_constraints(
            "municipio_chat_idempotency_receipt"
        )
        assert any(
            constraint.get("name") == "uq_municipio_chat_idempotency_scope"
            for constraint in unique_constraints
        )
        assert "expired_at" in {
            column["name"]
            for column in inspector.get_columns(
                "municipio_chat_idempotency_receipt"
            )
        }
        with Operations.context(context):
            migration.downgrade()
        assert "municipio_chat_idempotency_receipt" not in sa.inspect(
            connection
        ).get_table_names()
