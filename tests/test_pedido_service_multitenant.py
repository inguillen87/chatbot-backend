import json
import os
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import IntegrityError

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MarketOrder, Order, PedidoConversacional, PymePedido, TenantProfile, User
from services.pedido_service import PedidoService
from services.order_idempotency import order_payload_hash


class PedidoServiceMultitenantConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class PedidoServiceMultitenantTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(PedidoServiceMultitenantConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.owner_1 = self._create_user("owner-1@test.com")
        self.owner_2 = self._create_user("owner-2@test.com")
        self.tenant_1 = TenantProfile(
            slug="orders-tenant-1",
            nombre="Orders Tenant 1",
            tipo="pyme",
            pyme_id=self.owner_1.id,
        )
        self.tenant_2 = TenantProfile(
            slug="orders-tenant-2",
            nombre="Orders Tenant 2",
            tipo="pyme",
            pyme_id=self.owner_2.id,
        )
        db.session.add_all([self.tenant_1, self.tenant_2])
        db.session.flush()
        self.owner_1.tenant_id = self.tenant_1.id
        self.owner_2.tenant_id = self.tenant_2.id
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @staticmethod
    def _create_user(email: str) -> User:
        user = User(name="Cliente Test", email=email, rol="admin", password_hash="hash")
        db.session.add(user)
        db.session.flush()
        return user

    @staticmethod
    def _payload(*, owner: User, tenant: TenantProfile, key: str, currency: str = "USD") -> dict:
        return {
            "pyme_id": owner.id,
            "tenant_id": tenant.id,
            "asunto": "Pedido aislado",
            "detalles": json.dumps(
                [
                    {
                        "nombre": "Producto",
                        "cantidad": 1,
                        "precio_unitario": 10,
                        "subtotal": 10,
                        "currency_id": currency,
                    }
                ]
            ),
            "monto_total": 10,
            "moneda": currency,
            "rubro": "general",
            "idempotency_key": key,
        }

    def test_idempotency_key_is_reused_only_within_tenant(self):
        service = PedidoService()
        with (
            patch("services.pedido_service.generar_pdf_nota_pedido", return_value=None),
            patch("services.pedido_service.notification_dispatcher.dispatch_order_created"),
            patch.object(service, "sync_order_model_from_pyme", return_value=None),
            patch.object(service, "sync_market_order_from_pyme", return_value=None),
        ):
            first = service.crear_nuevo_pedido(
                self._payload(owner=self.owner_1, tenant=self.tenant_1, key="checkout-1")
            )
            repeated = service.crear_nuevo_pedido(
                self._payload(owner=self.owner_1, tenant=self.tenant_1, key="checkout-1")
            )
            second_tenant = service.crear_nuevo_pedido(
                self._payload(owner=self.owner_2, tenant=self.tenant_2, key="checkout-1")
            )

        self.assertIsNotNone(first)
        self.assertEqual(repeated.id, first.id)
        self.assertEqual(len(first.idempotency_payload_hash), 64)
        self.assertIsNotNone(second_tenant)
        self.assertNotEqual(second_tenant.id, first.id)
        self.assertEqual(
            PymePedido.query.filter_by(idempotency_key="checkout-1").count(),
            2,
        )

    def test_same_key_with_changed_payload_fails_closed(self):
        service = PedidoService()
        original = self._payload(
            owner=self.owner_1,
            tenant=self.tenant_1,
            key="checkout-bound-payload",
        )
        changed = dict(original)
        changed["monto_total"] = 999

        with (
            patch("services.pedido_service.generar_pdf_nota_pedido", return_value=None),
            patch("services.pedido_service.notification_dispatcher.dispatch_order_created"),
            patch.object(service, "sync_order_model_from_pyme", return_value=None),
            patch.object(service, "sync_market_order_from_pyme", return_value=None),
        ):
            first = service.crear_nuevo_pedido(original)
            conflict = service.crear_nuevo_pedido(changed)

        self.assertIsNotNone(first)
        self.assertIsNone(conflict)
        self.assertEqual(first.monto_total, 10)
        self.assertEqual(
            PymePedido.query.filter_by(
                tenant_id=self.tenant_1.id,
                idempotency_key="checkout-bound-payload",
            ).count(),
            1,
        )

    def test_matching_legacy_order_without_hash_can_replay_but_changed_payload_cannot(self):
        payload = self._payload(
            owner=self.owner_1,
            tenant=self.tenant_1,
            key="legacy-checkout",
        )
        legacy = PymePedido(
            pyme_id=self.owner_1.id,
            tenant_id=self.tenant_1.id,
            asunto=payload["asunto"],
            detalles=payload["detalles"],
            monto_total=payload["monto_total"],
            moneda=payload["moneda"],
            idempotency_key=payload["idempotency_key"],
        )
        db.session.add(legacy)
        db.session.commit()

        service = PedidoService()
        replay = service.crear_nuevo_pedido(dict(payload))
        changed = dict(payload)
        changed["direccion"] = "Otra direccion 999"
        conflict = service.crear_nuevo_pedido(changed)

        self.assertEqual(replay.id, legacy.id)
        self.assertIsNone(conflict)
        self.assertIsNone(legacy.idempotency_payload_hash)

    def test_unique_race_recovery_compares_payload_before_replay(self):
        service = PedidoService()
        payload = self._payload(
            owner=self.owner_1,
            tenant=self.tenant_1,
            key="race-checkout",
        )
        existing = PymePedido(
            pyme_id=self.owner_1.id,
            tenant_id=self.tenant_1.id,
            asunto=payload["asunto"],
            detalles=payload["detalles"],
            monto_total=payload["monto_total"],
            moneda=payload["moneda"],
            idempotency_key=payload["idempotency_key"],
            idempotency_payload_hash=order_payload_hash(payload),
        )
        db.session.add(existing)
        db.session.commit()

        query = MagicMock()
        query.filter_by.return_value.first.side_effect = [None, existing]
        integrity_error = IntegrityError("insert", {}, RuntimeError("duplicate"))
        with (
            patch.object(PymePedido, "query", query),
            patch.object(db.session, "commit", side_effect=integrity_error),
        ):
            recovered = service.crear_nuevo_pedido(dict(payload))

        self.assertEqual(recovered.id, existing.id)

    def test_order_creation_log_does_not_emit_customer_narrative(self):
        service = PedidoService()
        payload = self._payload(
            owner=self.owner_1,
            tenant=self.tenant_1,
            key="privacy-log-checkout",
        )
        payload.update(
            {
                "nombre_cliente": "Persona Narrativa Secreta",
                "email_cliente": "persona.narrativa@example.test",
                "direccion": "Calle Privada 123",
            }
        )
        with (
            patch("services.pedido_service.generar_pdf_nota_pedido", return_value=None),
            patch("services.pedido_service.notification_dispatcher.dispatch_order_created"),
            patch.object(service, "sync_order_model_from_pyme", return_value=None),
            patch.object(service, "sync_market_order_from_pyme", return_value=None),
            self.assertLogs("services.pedido_service", level="INFO") as captured,
        ):
            created = service.crear_nuevo_pedido(payload)

        self.assertIsNotNone(created)
        rendered = "\n".join(captured.output)
        self.assertIn("order_created", rendered)
        self.assertNotIn("Persona Narrativa Secreta", rendered)
        self.assertNotIn("persona.narrativa@example.test", rendered)
        self.assertNotIn("Calle Privada 123", rendered)

    def test_order_context_survives_reload_and_drives_crm_projections(self):
        service = PedidoService()
        payload = self._payload(
            owner=self.owner_1,
            tenant=self.tenant_1,
            key="persisted-order-context",
        )
        payload.update({"rubro": "Almacén", "channel": "whatsapp"})

        with (
            patch("services.pedido_service.generar_pdf_nota_pedido", return_value=None),
            patch("services.pedido_service.notification_dispatcher.dispatch_order_created"),
        ):
            created = service.crear_nuevo_pedido(payload)

        self.assertIsNotNone(created)
        created_id = created.id
        order_number = created.nro_pedido
        tenant_id = self.tenant_1.id
        db.session.expunge_all()

        reloaded = db.session.get(PymePedido, created_id)
        self.assertEqual(reloaded.rubro, "Almacén")
        self.assertEqual(reloaded.channel, "whatsapp")
        self.assertEqual(reloaded.to_dict()["rubro"], "Almacén")
        self.assertEqual(reloaded.to_dict()["channel"], "whatsapp")

        canonical = db.session.get(Order, order_number)
        market = MarketOrder.legacy_safe_query().filter_by(
            tenant_id=tenant_id,
            external_provider="pyme_pedido",
            external_order_id=order_number,
        ).one()
        self.assertEqual(canonical.channel, "whatsapp")
        self.assertEqual(market.channel, "whatsapp")

    def test_order_context_length_limits_fail_before_database_write(self):
        service = PedidoService()
        invalid_rubro = self._payload(
            owner=self.owner_1,
            tenant=self.tenant_1,
            key="invalid-rubro-context",
        )
        invalid_rubro["rubro"] = "r" * 101
        invalid_channel = self._payload(
            owner=self.owner_1,
            tenant=self.tenant_1,
            key="invalid-channel-context",
        )
        invalid_channel["channel"] = "c" * 51

        self.assertIsNone(service.crear_nuevo_pedido(invalid_rubro))
        self.assertIsNone(service.crear_nuevo_pedido(invalid_channel))
        self.assertEqual(PymePedido.query.count(), 0)

    def test_same_tenant_duplicate_idempotency_key_is_rejected_by_database(self):
        first = PymePedido(
            pyme_id=self.owner_1.id,
            tenant_id=self.tenant_1.id,
            asunto="Primero",
            detalles="[]",
            idempotency_key="tenant-duplicate",
        )
        db.session.add(first)
        db.session.commit()

        duplicate = PymePedido(
            pyme_id=self.owner_1.id,
            tenant_id=self.tenant_1.id,
            asunto="Duplicado",
            detalles="[]",
            idempotency_key="tenant-duplicate",
        )
        db.session.add(duplicate)

        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_market_and_canonical_sync_prefer_explicit_tenant_and_currency(self):
        pedido = PymePedido(
            pyme_id=self.owner_2.id,
            tenant_id=self.tenant_1.id,
            asunto="Pedido USD",
            detalles=json.dumps(
                [
                    {
                        "nombre": "Producto USD",
                        "cantidad": 2,
                        "precio_unitario": 15,
                        "subtotal": 30,
                        "currency_id": "USD",
                    }
                ]
            ),
            monto_total=30,
            moneda="USD",
        )
        db.session.add(pedido)
        db.session.commit()

        service = PedidoService()
        market_order = service.sync_market_order_from_pyme(pedido, channel="marketplace")
        canonical_order = service.sync_order_model_from_pyme(pedido, channel="marketplace")

        self.assertIsNotNone(market_order)
        self.assertEqual(market_order.tenant_id, self.tenant_1.id)
        self.assertEqual(market_order.currency, "USD")
        self.assertEqual(market_order.items[0].currency, "USD")
        self.assertEqual(MarketOrder.query.filter_by(tenant_id=self.tenant_2.id).count(), 0)
        self.assertIsNotNone(canonical_order)
        self.assertEqual(canonical_order.tenant_id, self.tenant_1.id)
        self.assertEqual(canonical_order.currency, "USD")
        self.assertEqual(Order.query.filter_by(tenant_id=self.tenant_2.id).count(), 0)

    def test_conversational_materialization_does_not_reuse_other_tenant_key(self):
        conversational = PedidoConversacional(
            tenant_id=self.tenant_2.id,
            user_id=self.owner_2.id,
            estado="confirmed",
            tipo="compra",
            origen="marketplace",
            monto_monetario=25,
            items=[
                {
                    "title": "Producto EUR",
                    "quantity": 1,
                    "unit_price": 25,
                    "currency_id": "EUR",
                }
            ],
            metadata_payload={"currency": "EUR"},
        )
        db.session.add(conversational)
        db.session.flush()
        collision_key = f"conv_order_{conversational.id}"
        foreign_order = PymePedido(
            pyme_id=self.owner_1.id,
            tenant_id=self.tenant_1.id,
            asunto="Colision externa",
            detalles="[]",
            idempotency_key=collision_key,
        )
        db.session.add(foreign_order)
        db.session.commit()

        service = PedidoService()
        with (
            patch("services.pedido_service.generar_pdf_nota_pedido", return_value=None),
            patch("services.pedido_service.notification_dispatcher.dispatch_order_created"),
            patch.object(service, "sync_order_model_from_pyme", return_value=None),
            patch.object(service, "sync_market_order_from_pyme", return_value=None),
        ):
            materialized = service.create_from_conversational(conversational)

        self.assertIsNotNone(materialized)
        self.assertNotEqual(materialized.id, foreign_order.id)
        self.assertEqual(materialized.tenant_id, self.tenant_2.id)
        self.assertEqual(materialized.moneda, "EUR")
        self.assertEqual(
            PymePedido.query.filter_by(idempotency_key=collision_key).count(),
            2,
        )


if __name__ == "__main__":
    unittest.main()
