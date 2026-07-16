import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import jwt
import requests
from sqlalchemy import event
from sqlalchemy.orm import Session

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import CatalogoItem, MarketOrder, OrderEvent, PedidoConversacional, PointsTransaction, TenantProfile, User


class V2CommerceTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2CommerceContractsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2CommerceTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(
            name="Commerce Owner",
            email="commerce-owner@test.com",
            rol="admin",
            tenant_slug="commerce-tenant",
            saldo_puntos=1000,
        )
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="commerce-tenant",
            nombre="Commerce Tenant",
            tipo="pyme",
            plan="full",
            pyme_id=self.owner.id,
            configuracion={
                "mercadopago_access_token": "mp-token",
                "rewards_rules": {"compra": 25, "encuesta": 50},
                "rewards_redemptions": [
                    {"id": "discount_10", "label": "Descuento 10%", "points_cost": 800, "type": "discount"},
                    {"id": "priority_support", "label": "Atencion prioritaria", "points_cost": 300, "type": "service"},
                ],
            },
        )
        db.session.add(self.tenant)
        db.session.commit()
        self.owner.tenant_id = self.tenant.id
        db.session.add(self.owner)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self, user: User):
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": user.tenant_slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug}

    def test_payment_checkout_status_contract(self):
        response = self.client.get(
            "/api/v2/payments/checkout-status",
            headers={**self._auth(self.owner), "X-Request-Id": "pay-status-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "payments.checkout_status.v1")
        self.assertEqual(payload.get("request_id"), "pay-status-1")
        self.assertTrue(payload.get("payment_ready"))
        self.assertEqual(payload.get("gateway"), "mercadopago")
        self.assertEqual(payload.get("missing"), [])

    def test_payment_checkout_preview_contract(self):
        response = self.client.post(
            "/api/v2/payments/checkout-preview",
            json={
                "items": [{"id": 1, "title": "Plan mensual", "quantity": 2, "unit_price": 1500, "currency_id": "ARS"}],
                "contact": {"email": "buyer@test.com"},
                "idempotency_key": "preview-1",
            },
            headers=self._auth(self.owner),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "payments.checkout_preview.v1")
        self.assertEqual(payload["summary"]["total_monetary"], 3000)
        self.assertTrue(payload.get("payment_required"))
        self.assertTrue(payload.get("payment_ready"))
        self.assertTrue(payload.get("contact_ready"))
        self.assertEqual(payload["checkout_options"]["gateway"], "mercadopago")

    def test_payment_checkout_session_creates_mercadopago_preference(self):
        captured = {}
        committed_transactions = []

        def record_commit(_session):
            committed_transactions.append("committed")

        class DummyMercadoPagoResponse:
            ok = True

            @staticmethod
            def json():
                return {"id": "pref_123", "init_point": "https://pay.test/pref_123"}

        def fake_post(url, json=None, headers=None, timeout=None):
            self.assertTrue(committed_transactions, "La orden debe confirmarse antes del POST a Mercado Pago")
            captured["url"] = url
            captured["payload"] = json
            captured["auth"] = headers.get("Authorization") if headers else None
            captured["timeout"] = timeout
            pedido = db.session.get(PedidoConversacional, int(json["external_reference"]))
            self.assertIsNotNone(pedido)
            self.assertEqual(pedido.tenant_id, self.tenant.id)
            self.assertEqual(float(pedido.monto_monetario), 1500)
            self.assertEqual(pedido.estado, "pendiente_pago")
            market_order_id = json["metadata"]["market_order_id"]
            market_order = db.session.get(MarketOrder, market_order_id)
            self.assertIsNotNone(market_order)
            self.assertEqual(market_order.tenant_id, self.tenant.id)
            self.assertEqual(float(market_order.total_monetary), 1500)
            return DummyMercadoPagoResponse()

        event.listen(Session, "after_commit", record_commit)
        try:
            with patch("services.commerce_contracts.requests.post", fake_post):
                response = self.client.post(
                    "/api/v2/payments/checkout-session",
                    json={
                        "items": [
                            {
                                "title": "Plan mensual",
                                "quantity": 1,
                                "unit_price": 1500,
                                "currency_id": "ARS",
                            }
                        ],
                        "contact": {"email": "buyer@test.com"},
                        "external_reference": "order-123",
                    },
                    headers={**self._auth(self.owner), "X-Request-Id": "checkout-session-1"},
                )
        finally:
            event.remove(Session, "after_commit", record_commit)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "payments.checkout_session.v1")
        self.assertEqual(payload.get("request_id"), "checkout-session-1")
        self.assertEqual(payload.get("preference_id"), "pref_123")
        self.assertEqual(payload.get("init_point"), "https://pay.test/pref_123")
        self.assertEqual(captured["auth"], "Bearer mp-token")
        self.assertEqual(captured["payload"]["external_reference"], str(payload["pedido_id"]))
        self.assertEqual(captured["payload"]["metadata"]["pedido_id"], payload["pedido_id"])
        self.assertEqual(captured["payload"]["metadata"]["market_order_id"], payload["market_order_id"])
        self.assertEqual(captured["payload"]["metadata"]["tenant_slug"], self.tenant.slug)
        self.assertEqual(payload.get("client_external_reference"), "order-123")
        self.assertFalse(payload.get("duplicate"))

        pedido = db.session.get(PedidoConversacional, payload["pedido_id"])
        market_order = db.session.get(MarketOrder, payload["market_order_id"])
        self.assertEqual(pedido.mp_preference_id, "pref_123")
        self.assertEqual(market_order.external_url, "https://pay.test/pref_123")
        self.assertEqual(market_order.metadata_payload["checkout"]["payment"]["state"], "ready")

    def test_payment_checkout_session_reuses_idempotent_preference(self):
        calls = []

        class DummyMercadoPagoResponse:
            ok = True

            @staticmethod
            def json():
                return {"id": "pref_idem", "init_point": "https://pay.test/pref_idem"}

        def fake_post(*_args, **_kwargs):
            calls.append("post")
            return DummyMercadoPagoResponse()

        request_payload = {
            "items": [{"title": "Plan mensual", "quantity": 1, "unit_price": 1500, "currency_id": "ARS"}],
            "contact": {"email": "buyer@test.com"},
        }
        headers = {**self._auth(self.owner), "Idempotency-Key": "checkout-idem-1"}
        with patch("services.commerce_contracts.requests.post", fake_post):
            first = self.client.post("/api/v2/payments/checkout-session", json=request_payload, headers=headers)
            second = self.client.post("/api/v2/payments/checkout-session", json=request_payload, headers=headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_payload = first.get_json()
        second_payload = second.get_json()
        self.assertEqual(calls, ["post"])
        self.assertEqual(second_payload["pedido_id"], first_payload["pedido_id"])
        self.assertEqual(second_payload["market_order_id"], first_payload["market_order_id"])
        self.assertEqual(second_payload["preference_id"], "pref_idem")
        self.assertTrue(second_payload["duplicate"])
        self.assertEqual(PedidoConversacional.query.count(), 1)
        self.assertEqual(MarketOrder.query.count(), 1)

    def test_payment_checkout_session_rejects_idempotency_amount_conflict(self):
        calls = []

        class DummyMercadoPagoResponse:
            ok = True

            @staticmethod
            def json():
                return {"id": "pref_conflict", "init_point": "https://pay.test/pref_conflict"}

        def fake_post(*_args, **_kwargs):
            calls.append("post")
            return DummyMercadoPagoResponse()

        headers = {**self._auth(self.owner), "Idempotency-Key": "checkout-conflict-1"}
        with patch("services.commerce_contracts.requests.post", fake_post):
            first = self.client.post(
                "/api/v2/payments/checkout-session",
                json={"items": [{"title": "Plan", "quantity": 1, "unit_price": 1500, "currency_id": "ARS"}]},
                headers=headers,
            )
            conflict = self.client.post(
                "/api/v2/payments/checkout-session",
                json={"items": [{"title": "Plan", "quantity": 1, "unit_price": 1600, "currency_id": "ARS"}]},
                headers=headers,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["reason_code"], "idempotency_key_conflict")
        self.assertEqual(calls, ["post"])
        self.assertEqual(PedidoConversacional.query.count(), 1)
        self.assertEqual(MarketOrder.query.count(), 1)

    def test_payment_checkout_session_does_not_retry_uncertain_gateway_result(self):
        request_payload = {
            "items": [{"title": "Plan", "quantity": 1, "unit_price": 1500, "currency_id": "ARS"}],
        }
        headers = {**self._auth(self.owner), "Idempotency-Key": "checkout-timeout-1"}

        with patch(
            "services.commerce_contracts.requests.post",
            side_effect=requests.Timeout("gateway timeout"),
        ) as mp_post:
            first = self.client.post(
                "/api/v2/payments/checkout-session",
                json=request_payload,
                headers=headers,
            )
            replay = self.client.post(
                "/api/v2/payments/checkout-session",
                json=request_payload,
                headers=headers,
            )

        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.get_json()["reason_code"], "payment_gateway_unavailable")
        self.assertFalse(first.get_json()["retryable"])
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(
            replay.get_json()["reason_code"],
            "payment_preference_reconciliation_required",
        )
        self.assertEqual(mp_post.call_count, 1)
        self.assertEqual(PedidoConversacional.query.count(), 1)
        market_order = MarketOrder.query.one()
        self.assertEqual(
            market_order.metadata_payload["checkout"]["payment"]["state"],
            "creation_uncertain",
        )

    def test_payment_checkout_session_rejects_total_mismatch_before_persistence(self):
        with patch("services.commerce_contracts.requests.post") as mp_post:
            response = self.client.post(
                "/api/v2/payments/checkout-session",
                json={
                    "items": [{"title": "Plan", "quantity": 2, "unit_price": 1500, "currency_id": "ARS"}],
                    "total_monetary": 1500,
                    "idempotency_key": "checkout-total-1",
                },
                headers=self._auth(self.owner),
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["reason_code"], "checkout_total_mismatch")
        mp_post.assert_not_called()
        self.assertEqual(PedidoConversacional.query.count(), 0)
        self.assertEqual(MarketOrder.query.count(), 0)

    def test_payment_checkout_session_rejects_catalog_item_from_another_tenant(self):
        other_owner = User(name="Other Owner", email="other-owner@test.com", rol="admin")
        other_owner.set_password("secret123")
        db.session.add(other_owner)
        db.session.flush()
        other_tenant = TenantProfile(
            slug="other-commerce-tenant",
            nombre="Other Commerce Tenant",
            tipo="pyme",
            plan="full",
            pyme_id=other_owner.id,
        )
        db.session.add(other_tenant)
        db.session.flush()
        foreign_item = CatalogoItem(
            user_id=other_owner.id,
            tenant_id=other_tenant.id,
            nombre="Foreign plan",
            precio="1500",
            modalidad="venta",
        )
        db.session.add(foreign_item)
        db.session.commit()

        with patch("services.commerce_contracts.requests.post") as mp_post:
            response = self.client.post(
                "/api/v2/payments/checkout-session",
                json={
                    "items": [
                        {
                            "title": "Foreign plan",
                            "catalogo_item_id": foreign_item.id,
                            "quantity": 1,
                            "unit_price": 1500,
                            "currency_id": "ARS",
                        }
                    ],
                    "idempotency_key": "checkout-tenant-1",
                },
                headers=self._auth(self.owner),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["reason_code"], "checkout_catalog_tenant_mismatch")
        mp_post.assert_not_called()
        self.assertEqual(PedidoConversacional.query.count(), 0)
        self.assertEqual(MarketOrder.query.count(), 0)

    def test_payment_checkout_session_rejects_catalog_price_tampering(self):
        catalog_item = CatalogoItem(
            user_id=self.owner.id,
            tenant_id=self.tenant.id,
            nombre="Canonical plan",
            precio="ARS 1500",
            modalidad="venta",
        )
        db.session.add(catalog_item)
        db.session.commit()

        with patch("services.commerce_contracts.requests.post") as mp_post:
            response = self.client.post(
                "/api/v2/payments/checkout-session",
                json={
                    "items": [
                        {
                            "title": "Canonical plan",
                            "catalogo_item_id": catalog_item.id,
                            "quantity": 1,
                            "unit_price": 1,
                            "currency_id": "ARS",
                        }
                    ],
                    "idempotency_key": "checkout-price-1",
                },
                headers=self._auth(self.owner),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason_code"], "checkout_catalog_amount_mismatch")
        self.assertEqual(response.get_json()["expected_unit_price"], 1500)
        mp_post.assert_not_called()
        self.assertEqual(PedidoConversacional.query.count(), 0)
        self.assertEqual(MarketOrder.query.count(), 0)

    def test_payment_checkout_session_requires_full_plan(self):
        self.tenant.plan = "free"
        db.session.add(self.tenant)
        db.session.commit()

        response = self.client.post(
            "/api/v2/payments/checkout-session",
            json={
                "items": [{"title": "Plan mensual", "quantity": 1, "unit_price": 1500, "currency_id": "ARS"}],
                "contact": {"email": "buyer@test.com"},
                "external_reference": "order-locked",
            },
            headers=self._auth(self.owner),
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertEqual(payload.get("reason_code"), "plan_full_required")
        self.assertFalse(payload["integration_access"]["enabled"])
        self.assertEqual(payload["checkout_experience"]["blocking_reasons"][0]["id"], "plan_full_required")
        self.assertEqual(payload["checkout_experience"]["operator_next_actions"][0]["status"], "required")
        self.assertEqual(payload["frontend_contract"]["render_as"], "integration_locked")
        self.assertEqual(payload["frontend_contract"]["feature_id"], "mercadopago_checkout")
        self.assertTrue(payload["frontend_contract"]["hide_payment_credentials_form"])
        self.assertFalse(payload["feature"]["enabled"])
        self.assertIn("upgrade", payload)

    def test_payment_status_contract_by_preference_id(self):
        pedido = PedidoConversacional(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            estado="pagado",
            monto_monetario=1500,
            tipo="compra",
            mp_preference_id="pref_paid",
            mp_payment_id="pay_123",
            mp_status="approved",
            items=[{"title": "Plan mensual", "quantity": 1, "unit_price": 1500, "currency_id": "ARS"}],
        )
        db.session.add(pedido)
        db.session.flush()
        market_order = MarketOrder(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            status="confirmed",
            total_monetary=1500,
            currency="ARS",
            external_provider="pedido_conversacional",
            external_order_id=str(pedido.id),
        )
        db.session.add(market_order)
        db.session.flush()
        db.session.add(OrderEvent(market_order_id=market_order.id, type="payment.approved", payload={"payment_id": "pay_123"}))
        db.session.commit()

        response = self.client.get(
            "/api/v2/payments/status",
            query_string={"preference_id": "pref_paid"},
            headers={**self._auth(self.owner), "X-Request-Id": "payment-status-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "payments.status.v1")
        self.assertEqual(payload.get("request_id"), "payment-status-1")
        self.assertTrue(payload["payment"]["paid"])
        self.assertEqual(payload["payment"]["status"], "paid")
        self.assertEqual(payload["payment"]["mp_payment_id"], "pay_123")
        self.assertEqual(payload["order"]["pedido_id"], pedido.id)
        self.assertTrue(payload["timeline"])

    def test_checkout_preference_webhook_and_status_are_reconciled_without_external_api(self):
        captured = {}

        class DummyPreferenceResponse:
            ok = True

            @staticmethod
            def json():
                return {"id": "pref_e2e", "init_point": "https://pay.test/pref_e2e"}

        def fake_preference_post(_url, json=None, **_kwargs):
            captured["preference_payload"] = json
            return DummyPreferenceResponse()

        checkout_headers = {**self._auth(self.owner), "Idempotency-Key": "checkout-e2e-1"}
        with patch("services.commerce_contracts.requests.post", fake_preference_post):
            checkout_response = self.client.post(
                "/api/v2/payments/checkout-session",
                json={
                    "items": [
                        {
                            "title": "Plan mensual",
                            "quantity": 1,
                            "unit_price": 1500,
                            "currency_id": "ARS",
                        }
                    ],
                    "contact": {"email": "buyer@test.com"},
                },
                headers=checkout_headers,
            )

        self.assertEqual(checkout_response.status_code, 200)
        checkout_payload = checkout_response.get_json()
        external_reference = checkout_payload["external_reference"]

        class DummyPaymentResponse:
            status_code = 200
            ok = True

            @staticmethod
            def json():
                return {
                    "id": "pay_e2e",
                    "external_reference": external_reference,
                    "status": "approved",
                    "transaction_amount": 1500,
                    "currency_id": "ARS",
                    "metadata": {
                        "tenant_id": self.tenant.id,
                        "tenant_slug": self.tenant.slug,
                    },
                }

        with (
            patch("routes.mercadopago_webhook.requests.get", return_value=DummyPaymentResponse()),
            patch("routes.mercadopago_webhook.socketio.emit"),
            patch("services.notification_dispatcher.dispatch_order_update"),
            patch("services.pedido_service.servicio_pedidos.create_from_conversational"),
        ):
            webhook_response = self.client.post(
                f"/mercadopago_webhook?tenant_slug={self.tenant.slug}",
                json={"type": "payment", "data": {"id": "pay_e2e"}},
            )

        self.assertEqual(webhook_response.status_code, 200)
        self.assertEqual(webhook_response.get_json()["pedido_id"], checkout_payload["pedido_id"])

        status_response = self.client.get(
            "/api/v2/payments/status",
            query_string={"preference_id": "pref_e2e"},
            headers=self._auth(self.owner),
        )
        self.assertEqual(status_response.status_code, 200)
        status_payload = status_response.get_json()
        self.assertTrue(status_payload["payment"]["paid"])
        self.assertEqual(status_payload["payment"]["mp_payment_id"], "pay_e2e")
        self.assertEqual(status_payload["payment"]["external_reference"], external_reference)
        self.assertEqual(status_payload["order"]["pedido_id"], checkout_payload["pedido_id"])
        self.assertEqual(status_payload["order"]["market_order_id"], checkout_payload["market_order_id"])
        self.assertEqual(status_payload["order"]["market_status"], "paid")
        self.assertEqual(status_payload["order"]["idempotency_key"], "checkout-e2e-1")

        pedido = db.session.get(PedidoConversacional, checkout_payload["pedido_id"])
        market_order = db.session.get(MarketOrder, checkout_payload["market_order_id"])
        self.assertEqual(pedido.estado, "pagado")
        self.assertEqual(pedido.mp_status, "approved")
        self.assertEqual(market_order.status, "paid")
        self.assertTrue(
            market_order.events.filter_by(type="payment.approved").first()
        )

    def test_rewards_profile_contract(self):
        response = self.client.get("/api/v2/rewards/profile", headers=self._auth(self.owner))

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "rewards.profile.v1")
        self.assertEqual(payload["wallet"]["balance"], 1000)
        self.assertEqual(payload["rules"]["compra"], 25)
        self.assertTrue(any(item["id"] == "discount_10" for item in payload["available_redemptions"]))

    def test_rewards_redeem_is_idempotent(self):
        headers = {**self._auth(self.owner), "Idempotency-Key": "redeem-1"}
        first = self.client.post("/api/v2/rewards/redeem", json={"reward_id": "discount_10"}, headers=headers)
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json()
        self.assertEqual(first_payload.get("contract_version"), "rewards.redeem.v1")
        self.assertEqual(first_payload.get("balance"), 200)

        second = self.client.post("/api/v2/rewards/redeem", json={"reward_id": "discount_10"}, headers=headers)
        self.assertEqual(second.status_code, 200)
        second_payload = second.get_json()
        self.assertTrue(second_payload.get("duplicate"))
        self.assertEqual(second_payload.get("balance"), 200)

        refreshed = User.query.get(self.owner.id)
        self.assertEqual(refreshed.saldo_puntos, 200)
        self.assertEqual(PointsTransaction.query.filter_by(user_id=self.owner.id, tipo="reward_redeem").count(), 1)


if __name__ == "__main__":
    unittest.main()
