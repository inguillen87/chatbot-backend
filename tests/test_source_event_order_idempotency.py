import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import (
    DomainEffectOutbox,
    MarketOrder,
    Order,
    PymePedido,
    TenantProfile,
    User,
)
from services.actions.pyme_order_actions import CrearPedidoAction
from services.logic import responder_chatboc
from services.pyme_multimodal import PymeSessionState, persist_order
from services.pymes import CONTEXTO_PYME, _tenant_profile_for_pyme_owner
from services.constants import CONTEXTO_MUNICIPIO
from services.source_event_context import bind_source_event_context


EVENT_IDENTITY = {
    "source_event_id": "SM-source-event-1",
    "durable_turn_id": 41,
    "idempotency_key": "whatsapp:7:41",
}


class SourceEventContextUnitTest(unittest.TestCase):
    def test_binding_replaces_current_turn_and_clears_stale_identity(self):
        context_data = {
            CONTEXTO_PYME: {
                "source_event_id": "old-event",
                "durable_turn_id": 1,
                "idempotency_key": "old-key",
                "cart": {"items": []},
            }
        }

        normalized, changed = bind_source_event_context(
            context_data,
            CONTEXTO_PYME,
            EVENT_IDENTITY,
        )

        self.assertTrue(changed)
        self.assertEqual(normalized, EVENT_IDENTITY)
        self.assertEqual(
            {key: context_data[CONTEXTO_PYME][key] for key in EVENT_IDENTITY},
            EVENT_IDENTITY,
        )
        self.assertIn("cart", context_data[CONTEXTO_PYME])

        _, changed = bind_source_event_context(context_data, CONTEXTO_PYME, {})
        self.assertTrue(changed)
        for key in EVENT_IDENTITY:
            self.assertNotIn(key, context_data[CONTEXTO_PYME])
        self.assertIn("cart", context_data[CONTEXTO_PYME])


class SourceEventLogicPropagationTest(unittest.TestCase):
    def setUp(self):
        class LogicTestConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
            ENABLE_RUNTIME_SCHEMA_SYNC = False
            ENABLE_RUNTIME_TENANT_INIT = False

        self.app = create_app(LogicTestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_logic_binds_identity_to_pyme_context_and_forwards_it(self):
        chat_context = SimpleNamespace(context_data={})
        rubro = SimpleNamespace(nombre="pyme")
        with patch("services.pymes.responder_pyme", return_value={"message_body": "ok"}) as responder:
            responder_chatboc(
                "quiero comprar",
                rubro_obj=rubro,
                tipo_chat="pyme",
                chat_db_context=chat_context,
                **EVENT_IDENTITY,
            )

        self.assertEqual(
            {key: chat_context.context_data[CONTEXTO_PYME][key] for key in EVENT_IDENTITY},
            EVENT_IDENTITY,
        )
        forwarded = responder.call_args.kwargs
        for key, value in EVENT_IDENTITY.items():
            self.assertEqual(forwarded[key], value)

    def test_logic_binds_identity_to_municipio_context_and_forwards_it(self):
        chat_context = SimpleNamespace(context_data={})
        rubro = SimpleNamespace(nombre="municipio")
        with patch(
            "services.municipio_responder.responder_municipio",
            return_value={"message_body": "ok"},
        ) as responder:
            responder_chatboc(
                "hola",
                rubro_obj=rubro,
                tipo_chat="municipio",
                chat_db_context=chat_context,
                **EVENT_IDENTITY,
            )

        self.assertEqual(
            {
                key: chat_context.context_data[CONTEXTO_MUNICIPIO][key]
                for key in EVENT_IDENTITY
            },
            EVENT_IDENTITY,
        )
        forwarded = responder.call_args.kwargs
        for key, value in EVENT_IDENTITY.items():
            self.assertEqual(forwarded[key], value)


class PymeOrderIdempotencyTest(unittest.TestCase):
    def setUp(self):
        class OrderTestConfig(Config):
            TESTING = True
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
            ENABLE_RUNTIME_SCHEMA_SYNC = False
            ENABLE_RUNTIME_TENANT_INIT = False

        self.app = create_app(OrderTestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_action_passes_turn_idempotency_to_order_service(self):
        cart_summary = {
            "items_detalle": [
                {
                    "nombre_producto": "Producto",
                    "cantidad": 1,
                    "precio_unitario_original": 100,
                    "subtotal_con_descuento": 100,
                    "sku": "SKU-1",
                }
            ],
            "total_final_con_descuento": 100,
        }
        context = {
            CONTEXTO_PYME: {},
            "user_id": 10,
            "tenant_id": 7,
            "cliente_id": 20,
            "idempotency_key": EVENT_IDENTITY["idempotency_key"],
            "user_obj": SimpleNamespace(rubro=SimpleNamespace(nombre="Comercio")),
            "nombre_usuario_contexto": "Ada Lovelace",
            "email_usuario_contexto": "ada@example.com",
            "direccion_usuario_contexto": "San Martin 123",
            "channel": "whatsapp",
            "chat_db_context_data": {"carritos_pymes": {}},
        }
        created = SimpleNamespace(
            id=99,
            nro_pedido="PED-99",
            nota_pedido_pdf_generado=False,
        )

        with (
            patch(
                "services.actions.pyme_order_actions.get_cart_summary",
                return_value=cart_summary,
            ),
            patch(
                "services.actions.pyme_order_actions.servicio_pedidos.crear_nuevo_pedido",
                return_value=created,
            ) as create_order,
            patch("services.actions.pyme_order_actions.clear_pyme_cart"),
            patch(
                "services.actions.pyme_order_actions.build_order_confirmation_payload",
                return_value={"contract_version": "order.confirmation.v1"},
            ),
            patch("services.pymes.formatear_carrito_desde_summary", return_value="Resumen"),
        ):
            result = CrearPedidoAction(context).execute({})

        self.assertTrue(result["success"])
        payload = create_order.call_args.args[0]
        self.assertEqual(payload["tenant_id"], 7)
        self.assertEqual(payload["idempotency_key"], EVENT_IDENTITY["idempotency_key"])

    def test_tenant_resolution_honors_route_tenant_for_multi_tenant_owner(self):
        owner = User(
            name="Comercio Multi Tenant",
            email="owner-multi-tenant@example.com",
            rol="admin",
            password_hash="hash",
        )
        db.session.add(owner)
        db.session.flush()
        first = TenantProfile(
            slug="source-event-first",
            nombre="Source Event First",
            tipo="pyme",
            pyme_id=owner.id,
        )
        second = TenantProfile(
            slug="source-event-second",
            nombre="Source Event Second",
            tipo="pyme",
            pyme_id=owner.id,
        )
        db.session.add_all([first, second])
        db.session.commit()

        resolved = _tenant_profile_for_pyme_owner(owner, preferred_id=first.id)

        self.assertEqual(resolved.id, first.id)

    def test_deterministic_flow_replays_same_order_for_same_turn(self):
        owner = User(
            name="Comercio Test",
            email="owner-source-event@example.com",
            rol="admin",
            password_hash="hash",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="source-event-orders",
            nombre="Source Event Orders",
            tipo="pyme",
            pyme_id=owner.id,
        )
        db.session.add(tenant)
        db.session.commit()

        state = PymeSessionState({}, owner.id)
        state.cart.update(
            {
                "items": [
                    {
                        "title": "Producto",
                        "sku": "SKU-1",
                        "qty": 1,
                        "unitPrice": 100,
                        "currency": "ARS",
                    }
                ],
                "subtotal": 100,
                "total": 100,
                "currency": "ARS",
            }
        )
        context = {
            "tenant_id": tenant.id,
            "idempotency_key": EVENT_IDENTITY["idempotency_key"],
            "nombre_cliente": "Ada Lovelace",
        }

        first = persist_order(state, owner.id, None, context=context, request_id="req-1")
        replay = persist_order(state, owner.id, None, context=context, request_id="req-2")

        self.assertIsNotNone(first)
        self.assertEqual(replay.id, first.id)
        self.assertEqual(first.tenant_id, tenant.id)
        self.assertEqual(first.idempotency_key, EVENT_IDENTITY["idempotency_key"])
        self.assertEqual(len(first.idempotency_payload_hash), 64)
        self.assertEqual(
            PymePedido.query.filter_by(
                tenant_id=tenant.id,
                idempotency_key=EVENT_IDENTITY["idempotency_key"],
            ).count(),
            1,
        )
        self.assertEqual(Order.query.filter_by(tenant_id=tenant.id).count(), 0)
        self.assertEqual(
            MarketOrder.legacy_safe_count(tenant_id=tenant.id),
            0,
        )
        self.assertEqual(
            DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count(),
            0,
        )

        state.cart["items"][0]["qty"] = 2
        state.cart["total"] = 200
        conflict = persist_order(state, owner.id, None, context=context, request_id="req-3")
        self.assertIsNone(conflict)
        self.assertEqual(
            PymePedido.query.filter_by(
                tenant_id=tenant.id,
                idempotency_key=EVENT_IDENTITY["idempotency_key"],
            ).count(),
            1,
        )

    def test_multimodal_canary_commits_order_projections_and_outbox_once(self):
        owner = User(
            name="Comercio Canary",
            email="owner-multimodal-canary@example.com",
            rol="admin",
            password_hash="hash",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="source-event-orders-canary",
            nombre="Source Event Orders Canary",
            tipo="pyme",
            pyme_id=owner.id,
            is_active=True,
            send_dispatch_whatsapp=False,
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        db.session.commit()
        owner_id = owner.id
        tenant_id = tenant.id

        self.app.config.update(
            DOMAIN_EFFECT_OUTBOX_MODE="queue",
            DOMAIN_EFFECT_OUTBOX_SECRET="multimodal-canary-secret-" + ("x" * 32),
            DOMAIN_EFFECT_OUTBOX_TENANT_IDS=str(tenant_id),
            DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES=4096,
            DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS=8,
        )
        state = PymeSessionState({}, owner_id)
        state.cart.update(
            {
                "items": [
                    {
                        "title": "Producto IA",
                        "sku": "SKU-IA-1",
                        "qty": 2,
                        "unitPrice": 125,
                        "currency": "ARS",
                    }
                ],
                "subtotal": 250,
                "total": 250,
                "currency": "ARS",
            }
        )
        state.delivery.update(
            {
                "address": "San Martin 123",
                "lat": -34.60,
                "lng": -58.38,
            }
        )
        context = {
            "tenant_id": tenant_id,
            "idempotency_key": "whatsapp:multimodal:canary:1",
            "nombre_cliente": "Ada Lovelace",
            "email_cliente": "ada.canary@example.com",
            "telefono_cliente": "+5492615550101",
            "channel": "whatsapp",
        }

        with (
            patch(
                "services.pedido_service.generar_pdf_nota_pedido",
                return_value=None,
            ),
            patch(
                "services.pedido_service.notification_dispatcher.dispatch_order_created"
            ) as legacy_dispatcher,
            patch(
                "services.domain_effect_worker.enqueue_domain_effect_dispatch",
                return_value=False,
            ) as wakeup,
            patch.object(db.session, "commit", wraps=db.session.commit) as commit_spy,
        ):
            created = persist_order(
                state,
                owner_id,
                None,
                context=context,
                request_id="req-canary-create",
            )
            self.assertIsNotNone(created)
            pedido_id = created.id
            nro_pedido = created.nro_pedido

            # Drop the identity map to exercise the durable database contract,
            # then replay as a separate worker/process would.
            db.session.expunge_all()
            persisted = db.session.get(PymePedido, pedido_id)
            canonical = db.session.get(Order, nro_pedido)
            market = MarketOrder.legacy_safe_query().filter_by(
                tenant_id=tenant_id,
                external_provider="pyme_pedido",
                external_order_id=nro_pedido,
            ).one()
            effects = DomainEffectOutbox.query.filter_by(
                tenant_id=tenant_id,
                aggregate_type="pyme_order",
                aggregate_ref=str(pedido_id),
            ).all()

            self.assertIsNotNone(persisted)
            self.assertIsNotNone(canonical)
            self.assertEqual(len(canonical.items), 1)
            self.assertEqual(len(market.items), 1)
            self.assertEqual(len(effects), 4)
            self.assertTrue(
                all(row.status == DomainEffectOutbox.STATUS_PENDING for row in effects)
            )

            db.session.expunge_all()
            replay = persist_order(
                state,
                owner_id,
                None,
                context=context,
                request_id="req-canary-replay",
            )

        self.assertEqual(replay.id, pedido_id)
        self.assertEqual(commit_spy.call_count, 1)
        self.assertEqual(
            PymePedido.query.filter_by(tenant_id=tenant_id).count(),
            1,
        )
        self.assertEqual(Order.query.filter_by(tenant_id=tenant_id).count(), 1)
        self.assertEqual(MarketOrder.legacy_safe_count(tenant_id=tenant_id), 1)
        self.assertEqual(
            DomainEffectOutbox.query.filter_by(tenant_id=tenant_id).count(),
            4,
        )
        legacy_dispatcher.assert_not_called()
        wakeup.assert_called_once_with(tenant_id=tenant_id)


if __name__ == "__main__":
    unittest.main()
