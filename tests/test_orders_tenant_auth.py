import os
import unittest
from datetime import datetime, timedelta

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import Order, PedidoConversacional, TenantProfile, User


class OrdersTenantAuthConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class OrdersTenantAuthTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(OrdersTenantAuthConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin_1 = self._create_user("admin1@test.com", "admin", "tenant-1")
        self.tenant_1 = TenantProfile(slug="tenant-1", nombre="Tenant 1", tipo="pyme", pyme_id=self.admin_1.id)
        db.session.add(self.tenant_1)
        db.session.commit()
        self.admin_1.tenant_id = self.tenant_1.id
        db.session.add(self.admin_1)

        self.admin_2 = self._create_user("admin2@test.com", "admin", "tenant-2")
        self.tenant_2 = TenantProfile(slug="tenant-2", nombre="Tenant 2", tipo="pyme", pyme_id=self.admin_2.id)
        db.session.add(self.tenant_2)
        db.session.commit()
        self.admin_2.tenant_id = self.tenant_2.id
        db.session.add(self.admin_2)

        self.employee = self._create_user("employee@test.com", "empleado", "tenant-1", tenant_id=self.tenant_1.id)
        self.customer = self._create_user("customer@test.com", "usuario", "tenant-1", tenant_id=self.tenant_1.id)

        self.order_1 = Order(tenant_id=self.tenant_1.id, buyer_name="Buyer 1", total=100, status="created")
        self.order_2 = Order(tenant_id=self.tenant_2.id, buyer_name="Buyer 2", total=200, status="created")
        self.assisted_order = PedidoConversacional(
            tenant_id=self.tenant_1.id,
            user_id=self.admin_1.id,
            estado="pendiente",
            tipo="nota_de_pedido",
            origen="marketplace",
            monto_monetario=0,
            items=[
                {
                    "items_detectados": [
                        {"nombre": "Clavos punta paris", "cantidad": 3, "precio_float": 0},
                    ],
                    "customer_message": "Recibimos tu nota y armamos un borrador.",
                }
            ],
            metadata_payload={
                "contract_version": "marketplace.assisted_request.v1",
                "mode": "order_note_upload",
                "request_kind": "quote_request",
                "request_kind_label": "nota de pedido",
                "contact": {"name": "Marcelo", "phone": "+5492613168608", "email": "marcelo@example.com"},
                "source": {"channel": "marketplace", "input_type": "text"},
                "match_summary": {"matched": 1, "unmatched": 0, "detected": 1, "needs_operator_review": False},
                "crm_order_draft": {
                    "contract_version": "marketplace.crm_order_draft.v1",
                    "reference": "pedido:test",
                    "summary": {"matched": 1, "unmatched": 0},
                    "lines": [{"status": "catalog_matched", "source_name": "Clavos punta paris"}],
                },
                "operator_intake_summary": {
                    "contract_version": "marketplace.operator_intake_summary.v1",
                    "target_module": "orders",
                    "recommended_next_step": "confirmar_y_responder",
                },
            },
        )
        db.session.add_all([self.order_1, self.order_2, self.assisted_order])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _create_user(self, email: str, rol: str, tenant_slug: str | None, tenant_id: int | None = None):
        user = User(name=email.split("@")[0], email=email, rol=rol, tenant_slug=tenant_slug, tenant_id=tenant_id)
        user.set_password("secret123")
        db.session.add(user)
        db.session.flush()
        return user

    def _auth_header(self, user: User):
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
        return {"Authorization": f"Bearer {token}"}

    def test_admin_orders_requires_operator_role(self):
        resp = self.client.get(
            "/api/admin/orders",
            headers={**self._auth_header(self.customer), "X-Tenant-Slug": "tenant-1"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual((resp.get_json() or {}).get("reason_code"), "insufficient_permissions")

    def test_tenant_admin_order_reads_reject_citizen_from_same_tenant(self):
        headers = {**self._auth_header(self.customer), "X-Tenant-Slug": "tenant-1"}
        urls = (
            "/api/admin/tenants/tenant-1/orders",
            f"/api/admin/tenants/tenant-1/orders/order:{self.order_1.id}",
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    (response.get_json() or {}).get("reason_code"),
                    "insufficient_permissions",
                )

    def test_tenant_admin_order_patch_rejects_citizen_from_same_tenant(self):
        response = self.client.patch(
            f"/api/admin/tenants/tenant-1/orders/order:{self.order_1.id}",
            json={"status": "confirmed"},
            headers={**self._auth_header(self.customer), "X-Tenant-Slug": "tenant-1"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            (response.get_json() or {}).get("reason_code"),
            "insufficient_permissions",
        )
        db.session.refresh(self.order_1)
        self.assertEqual(self.order_1.status, "created")

    def test_admin_orders_rejects_cross_tenant_admin(self):
        resp = self.client.get(
            "/api/admin/orders",
            headers={**self._auth_header(self.admin_1), "X-Tenant-Slug": "tenant-2"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_admin_orders_lists_only_authorized_tenant(self):
        resp = self.client.get(
            "/api/admin/orders",
            headers={**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json() or {}
        buyers = [(item.get("buyer") or {}).get("name") for item in payload.get("items") or []]
        self.assertIn("Buyer 1", buyers)
        self.assertNotIn("Buyer 2", buyers)

    def test_admin_orders_includes_assisted_marketplace_intakes(self):
        resp = self.client.get(
            "/api/admin/orders",
            headers={**self._auth_header(self.employee), "X-Tenant-Slug": "tenant-1"},
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json() or {}
        items = payload.get("items") or []
        assisted = next(
            item for item in items if item.get("id") == f"conversational:{self.assisted_order.id}"
        )
        self.assertEqual(assisted["source_model"], "PedidoConversacional")
        self.assertEqual(assisted["source_type"], "assisted_intake")
        self.assertEqual(assisted["buyer"]["name"], "Marcelo")
        self.assertEqual(assisted["buyer"]["phone"], "+5492613168608")
        self.assertEqual(assisted["assisted_request"]["contract_version"], "marketplace.assisted_request.v1")
        self.assertEqual(assisted["assisted_request"]["crm_order_draft"]["contract_version"], "marketplace.crm_order_draft.v1")
        self.assertEqual(assisted["crm_review_card"]["contract_version"], "marketplace.crm_review_card.v1")

    def test_update_order_rejects_cross_tenant_admin(self):
        resp = self.client.patch(
            f"/api/admin/orders/{self.order_2.id}",
            json={"status": "confirmed"},
            headers={**self._auth_header(self.admin_1), "X-Tenant-Slug": "tenant-2"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_public_ad_hoc_order_is_quote_priced_by_backend(self):
        resp = self.client.post(
            "/api/orders?tenant=tenant-1",
            json={
                "items": [{"title": "Pedido manuscrito sin catalogo", "quantity": 2, "unit_price": 999999}],
                "buyer": {"name": "Vecino"},
                "channel": "web_widget",
            },
        )
        self.assertEqual(resp.status_code, 201)
        order = Order.query.get((resp.get_json() or {}).get("order_id"))
        self.assertIsNotNone(order)
        self.assertEqual(float(order.total), 0.0)


if __name__ == "__main__":
    unittest.main()
