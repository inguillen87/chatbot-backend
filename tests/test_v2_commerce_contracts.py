import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MarketOrder, OrderEvent, PedidoConversacional, PointsTransaction, TenantProfile, User


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

        class DummyMercadoPagoResponse:
            ok = True

            @staticmethod
            def json():
                return {"id": "pref_123", "init_point": "https://pay.test/pref_123"}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            captured["auth"] = headers.get("Authorization") if headers else None
            captured["timeout"] = timeout
            return DummyMercadoPagoResponse()

        with patch("services.commerce_contracts.requests.post", fake_post):
            response = self.client.post(
                "/api/v2/payments/checkout-session",
                json={
                    "items": [{"title": "Plan mensual", "quantity": 1, "unit_price": 1500, "currency_id": "ARS"}],
                    "contact": {"email": "buyer@test.com"},
                    "external_reference": "order-123",
                },
                headers={**self._auth(self.owner), "X-Request-Id": "checkout-session-1"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "payments.checkout_session.v1")
        self.assertEqual(payload.get("request_id"), "checkout-session-1")
        self.assertEqual(payload.get("preference_id"), "pref_123")
        self.assertEqual(payload.get("init_point"), "https://pay.test/pref_123")
        self.assertEqual(captured["auth"], "Bearer mp-token")
        self.assertEqual(captured["payload"]["external_reference"], "order-123")
        self.assertEqual(captured["payload"]["metadata"]["tenant_slug"], self.tenant.slug)

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
