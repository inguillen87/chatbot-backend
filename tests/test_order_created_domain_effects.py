from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from models import (
    CatalogoItem,
    DomainEffectOutbox,
    MarketOrder,
    Order,
    PymePedido,
    TenantProfile,
    User,
    db,
)
from services.domain_effect_outbox import dispatch_domain_effects
from services.order_domain_effects import (
    CUSTOMER_EMAIL_HANDLER,
    CUSTOMER_WHATSAPP_HANDLER,
    DISPATCH_EMAIL_HANDLER,
    DISPATCH_WHATSAPP_HANDLER,
    ORDER_DOMAIN_EFFECT_REGISTRY,
    OWNER_EMAIL_HANDLER,
)
from services.pedido_service import PedidoService


SECRET = "order-outbox-test-secret-" + ("x" * 32)


@pytest.fixture
def order_effect_tenants(init_database):
    owner = User(
        name="Pyme Owner",
        email="pyme-owner@example.com",
        rol="admin",
        tipo_chat="pyme",
    )
    owner.set_password("test-password")
    other_owner = User(
        name="Other Owner",
        email="other-owner@example.com",
        rol="admin",
        tipo_chat="pyme",
    )
    other_owner.set_password("test-password")
    db.session.add_all([owner, other_owner])
    db.session.flush()

    tenant = TenantProfile(
        slug="order-outbox-pyme",
        nombre="Order Outbox Pyme",
        tipo="pyme",
        pyme_id=owner.id,
        is_active=True,
        dispatch_email="warehouse@example.com",
        dispatch_phone="+5492613111111,+5492613222222",
        send_buyer_email=True,
        send_dispatch_email=True,
        send_dispatch_whatsapp=True,
    )
    other_tenant = TenantProfile(
        slug="order-outbox-other-pyme",
        nombre="Other Order Outbox Pyme",
        tipo="pyme",
        pyme_id=other_owner.id,
        is_active=True,
    )
    db.session.add_all([tenant, other_tenant])
    db.session.flush()
    owner.tenant_id = tenant.id
    other_owner.tenant_id = other_tenant.id
    db.session.commit()
    return tenant, owner, other_tenant, other_owner


def _set_queue(app, monkeypatch, tenant_id: int) -> None:
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "queue")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", SECRET)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", str(tenant_id))
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES", 4096)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS", 8)


def _set_legacy(app, monkeypatch) -> None:
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", "")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", "")


def _payload(tenant: TenantProfile, owner: User, *, key: str) -> dict:
    return {
        "tenant_id": tenant.id,
        "pyme_id": owner.id,
        "asunto": "Pedido confidencial",
        "detalles": json.dumps(
            [
                {
                    "nombre": "Producto privado",
                    "cantidad": 2,
                    "precio_unitario": 15,
                    "subtotal": 30,
                }
            ]
        ),
        "monto_total": 30,
        "moneda": "ARS",
        "rubro": "Almacen",
        "nombre_cliente": "Cliente Confidencial",
        "email_cliente": "buyer-secret@example.com",
        "telefono_cliente": "+5492613333333",
        "direccion": "Calle Privada 123",
        "idempotency_key": key,
        "channel": "whatsapp",
    }


def _cart_input(owner: User) -> tuple[list[dict], dict]:
    item = CatalogoItem(
        user_id=owner.id,
        nombre="Producto carrito",
        precio="100 ARS",
        cantidad="10",
        unidad="unidad",
    )
    db.session.add(item)
    db.session.commit()
    return (
        [{"nombre": item.nombre, "cantidad": 2}],
        {
            "asunto": "Pedido carrito",
            "rubro": "Almacen",
            "nombre_cliente": "Cliente Carrito",
            "email_cliente": "cart-secret@example.com",
            "telefono_cliente": "+5492613444444",
            "direccion": "Calle Carrito 456",
        },
    )


def _create_canary_order(
    tenant: TenantProfile,
    owner: User,
    *,
    key: str,
):
    service = PedidoService()
    with patch(
        "services.pedido_service.generar_pdf_nota_pedido",
        return_value=None,
    ), patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher, patch.object(
        service,
        "sync_order_model_from_pyme",
        return_value=object(),
    ), patch.object(
        service,
        "sync_market_order_from_pyme",
        return_value=object(),
    ), patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch",
        return_value=False,
    ) as wakeup:
        order = service.crear_nuevo_pedido(_payload(tenant, owner, key=key))
    return order, legacy_dispatcher, wakeup


def test_canary_stages_one_row_per_external_recipient_without_dual_send(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    service = PedidoService()

    with patch(
        "services.pedido_service.generar_pdf_nota_pedido",
        return_value=None,
    ), patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher, patch.object(
        service,
        "sync_order_model_from_pyme",
        return_value=object(),
    ) as canonical_projection, patch.object(
        service,
        "sync_market_order_from_pyme",
        return_value=object(),
    ) as market_projection, patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch",
        return_value=False,
    ) as wakeup:
        order = service.crear_nuevo_pedido(
            _payload(tenant, owner, key="order-outbox-create-1")
        )

    assert order is not None
    rows = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).order_by(
        DomainEffectOutbox.id
    ).all()
    assert [row.handler_name for row in rows] == [
        CUSTOMER_EMAIL_HANDLER,
        OWNER_EMAIL_HANDLER,
        DISPATCH_EMAIL_HANDLER,
        CUSTOMER_WHATSAPP_HANDLER,
        DISPATCH_WHATSAPP_HANDLER,
        DISPATCH_WHATSAPP_HANDLER,
    ]
    assert all(row.aggregate_type == "pyme_order" for row in rows)
    assert all(row.aggregate_ref == str(order.id) for row in rows)
    assert all(row.status == DomainEffectOutbox.STATUS_PENDING for row in rows)
    assert all(set(row.payload_json) == {"owner_binding"} for row in rows)
    assert len({row.payload_json["owner_binding"] for row in rows}) == 1
    assert all(len(row.intent_hmac) == 64 for row in rows)
    dispatch_refs = [
        row.recipient_ref
        for row in rows
        if row.handler_name == DISPATCH_WHATSAPP_HANDLER
    ]
    assert len(dispatch_refs) == 2
    assert len(set(dispatch_refs)) == 2
    assert all(ref.startswith("recipient_hash:") and len(ref) == 79 for ref in dispatch_refs)

    serialized = json.dumps(
        [
            {
                "recipient_ref": row.recipient_ref,
                "effect_key": row.effect_key,
                "payload": row.payload_json,
            }
            for row in rows
        ],
        sort_keys=True,
    )
    for pii in (
        "Cliente Confidencial",
        "buyer-secret@example.com",
        "+5492613333333",
        "+5492613111111",
        "+5492613222222",
        "Calle Privada 123",
        "pyme-owner@example.com",
        "warehouse@example.com",
    ):
        assert pii not in serialized
    legacy_dispatcher.assert_not_called()
    wakeup.assert_called_once_with(tenant_id=tenant.id)
    # Internal CRM projections are not provider effects, but canary creation
    # stages them without their own commit in the same database unit.
    canonical_projection.assert_called_once_with(
        order,
        channel="whatsapp",
        commit=False,
    )
    market_projection.assert_called_once_with(
        order,
        channel="whatsapp",
        commit=False,
    )


def test_staging_failure_rolls_back_order_and_partial_outbox(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    service = PedidoService()

    with patch(
        "services.order_domain_effects.stage_domain_effect",
        side_effect=RuntimeError("staging unavailable"),
    ), patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher:
        order = service.crear_nuevo_pedido(
            _payload(tenant, owner, key="order-outbox-rollback")
        )

    assert order is None
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
    legacy_dispatcher.assert_not_called()


def test_canary_projection_failure_rolls_back_order_and_outbox(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    service = PedidoService()

    with patch.object(
        service,
        "sync_order_model_from_pyme",
        side_effect=RuntimeError("crm projection unavailable"),
    ), patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher, patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch"
    ) as wakeup:
        order = service.crear_nuevo_pedido(
            _payload(tenant, owner, key="order-outbox-projection-rollback")
        )

    assert order is None
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == 0
    assert Order.query.filter_by(tenant_id=tenant.id).count() == 0
    assert MarketOrder.legacy_safe_count(tenant_id=tenant.id) == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
    legacy_dispatcher.assert_not_called()
    wakeup.assert_not_called()


def test_canary_rejects_cross_tenant_owner_binding_before_commit(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, _, _, other_owner = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    service = PedidoService()

    with patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher:
        order = service.crear_nuevo_pedido(
            _payload(tenant, other_owner, key="order-outbox-cross-tenant")
        )

    assert order is None
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
    legacy_dispatcher.assert_not_called()


def test_invalid_dispatch_recipient_fails_closed_before_commit(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    tenant.dispatch_phone = "not-a-phone"
    db.session.commit()

    order = PedidoService().crear_nuevo_pedido(
        _payload(tenant, owner, key="order-outbox-bad-dispatch")
    )

    assert order is None
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0


def test_disabled_dispatch_whatsapp_ignores_invalid_legacy_phone(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    tenant.send_dispatch_whatsapp = False
    tenant.dispatch_phone = "legacy-invalid-phone"
    db.session.commit()

    order = PedidoService().crear_nuevo_pedido(
        _payload(tenant, owner, key="order-outbox-disabled-dispatch-whatsapp")
    )

    assert order is not None
    rows = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).all()
    assert rows
    assert not any(row.handler_name == DISPATCH_WHATSAPP_HANDLER for row in rows)
    assert not any(
        row.effect_type == "order.created.whatsapp.dispatch" for row in rows
    )


def test_canary_cart_creation_stages_before_commit_and_suppresses_direct_send(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    cart_items, customer = _cart_input(owner)
    service = PedidoService()

    with patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher, patch.object(
        service,
        "sync_order_model_from_pyme",
        return_value=None,
    ) as canonical_projection, patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch",
        return_value=False,
    ) as wakeup:
        order = service.crear_pedido_desde_carrito(
            owner.id,
            cart_items,
            customer,
        )

    assert order is not None
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == 1
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 6
    legacy_dispatcher.assert_not_called()
    wakeup.assert_called_once_with(tenant_id=tenant.id)
    canonical_projection.assert_called_once_with(order, channel="web_widget")


def test_canary_cart_staging_failure_rolls_back_order_and_outbox(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    cart_items, customer = _cart_input(owner)
    service = PedidoService()

    with patch(
        "services.order_domain_effects.stage_domain_effect",
        side_effect=RuntimeError("cart staging unavailable"),
    ), patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher:
        order = service.crear_pedido_desde_carrito(
            owner.id,
            cart_items,
            customer,
        )

    assert order is None
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
    legacy_dispatcher.assert_not_called()


def test_legacy_tenant_keeps_direct_dispatch_and_no_outbox(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_legacy(app, monkeypatch)
    service = PedidoService()

    with patch(
        "services.pedido_service.generar_pdf_nota_pedido",
        return_value=None,
    ), patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher, patch.object(
        service,
        "sync_order_model_from_pyme",
        return_value=None,
    ) as canonical_projection, patch.object(
        service,
        "sync_market_order_from_pyme",
        return_value=None,
    ) as market_projection:
        order = service.crear_nuevo_pedido(
            _payload(tenant, owner, key="order-legacy-direct")
        )

    assert order is not None
    legacy_dispatcher.assert_called_once_with(order, pdf_bytes=None)
    canonical_projection.assert_called_once_with(order, channel="whatsapp")
    market_projection.assert_called_once_with(order, channel="whatsapp")
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0


def test_idempotent_replay_neither_duplicates_rows_nor_direct_sends(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    service = PedidoService()
    payload = _payload(tenant, owner, key="order-outbox-replay")

    with patch(
        "services.pedido_service.generar_pdf_nota_pedido",
        return_value=None,
    ), patch(
        "services.pedido_service.notification_dispatcher.dispatch_order_created"
    ) as legacy_dispatcher, patch.object(
        service,
        "sync_order_model_from_pyme",
        return_value=object(),
    ), patch.object(
        service,
        "sync_market_order_from_pyme",
        return_value=object(),
    ), patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch",
        return_value=False,
    ) as wakeup:
        first = service.crear_nuevo_pedido(dict(payload))
        replay = service.crear_nuevo_pedido(dict(payload))

    assert first is not None
    assert replay.id == first.id
    assert PymePedido.query.filter_by(tenant_id=tenant.id).count() == 1
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 6
    legacy_dispatcher.assert_not_called()
    wakeup.assert_called_once_with(tenant_id=tenant.id)


def test_worker_skips_unconfigured_email_and_log_only_whatsapp_before_io(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    monkeypatch.setitem(app.config, "EMAIL_NOTIFICATIONS_ENABLED", False)
    order, _, _ = _create_canary_order(
        tenant,
        owner,
        key="order-outbox-preflight-skip",
    )
    assert order is not None

    summary = dispatch_domain_effects(
        registry=ORDER_DOMAIN_EFFECT_REGISTRY,
        intent_secret=SECRET,
        tenant_id=tenant.id,
        limit=20,
    )

    rows = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).all()
    assert summary.skipped == 6
    assert all(row.status == DomainEffectOutbox.STATUS_SKIPPED for row in rows)
    assert all(row.io_started_at is None for row in rows)
    for row in rows:
        if row.channel == "email":
            assert row.result_json == {"reason_code": "email_notifications_disabled"}
        else:
            assert row.result_json == {"reason_code": "whatsapp_transport_unavailable"}


def test_worker_uses_persisted_rubro_after_orm_session_restart(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    tenant_id = tenant.id
    _set_queue(app, monkeypatch, tenant_id)
    monkeypatch.setitem(app.config, "EMAIL_NOTIFICATIONS_ENABLED", False)
    order, _, _ = _create_canary_order(
        tenant,
        owner,
        key="order-outbox-persisted-rubro",
    )
    order_id = order.id
    db.session.remove()
    monkeypatch.setattr(
        "services.notifications.ORDER_WHATSAPP_TRANSPORT_IMPLEMENTED",
        True,
        raising=False,
    )

    with patch(
        "services.notifications.enviar_notificacion_whatsapp_con_plantilla",
        return_value=True,
    ) as whatsapp_sender:
        summary = dispatch_domain_effects(
            registry=ORDER_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant_id,
            limit=20,
        )

    reloaded = db.session.get(PymePedido, order_id)
    assert reloaded.rubro == "Almacen"
    assert reloaded.channel == "whatsapp"
    assert summary.succeeded == 3
    assert summary.skipped == 3
    assert any(
        call.args == (
            "+5492613333333",
            "Cliente Confidencial",
            str(reloaded.nro_pedido),
            "Almacen",
        )
        for call in whatsapp_sender.call_args_list
    )


def test_false_and_timeout_after_io_become_unknown_and_are_not_replayed(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, _ = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    order, _, _ = _create_canary_order(
        tenant,
        owner,
        key="order-outbox-ambiguous",
    )
    assert order is not None
    monkeypatch.setattr(
        "services.notifications.ORDER_WHATSAPP_TRANSPORT_IMPLEMENTED",
        True,
        raising=False,
    )

    with patch(
        "services.order_domain_effects._email_preflight_error",
        return_value=None,
    ), patch(
        "services.order_domain_effects._optional_pdf",
        return_value=None,
    ), patch(
        "services.email_service.enviar_email_pedido_cliente",
        return_value=False,
    ) as customer_email, patch(
        "services.email_service.enviar_email_pedido_despacho",
        side_effect=[True, TimeoutError("provider timeout")],
    ) as tenant_email, patch(
        "services.notifications.enviar_notificacion_whatsapp_con_plantilla",
        return_value=True,
    ) as whatsapp_sender:
        first_summary = dispatch_domain_effects(
            registry=ORDER_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=20,
        )

    rows = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).all()
    by_handler: dict[str, list[DomainEffectOutbox]] = {}
    for row in rows:
        by_handler.setdefault(row.handler_name, []).append(row)
    assert first_summary.unknown == 2
    assert first_summary.succeeded == 4
    assert by_handler[CUSTOMER_EMAIL_HANDLER][0].status == DomainEffectOutbox.STATUS_UNKNOWN
    assert by_handler[OWNER_EMAIL_HANDLER][0].status == DomainEffectOutbox.STATUS_SUCCEEDED
    assert by_handler[OWNER_EMAIL_HANDLER][0].result_json == {
        "delivery": "provider_accepted"
    }
    assert by_handler[DISPATCH_EMAIL_HANDLER][0].status == DomainEffectOutbox.STATUS_UNKNOWN
    assert all(
        row.status == DomainEffectOutbox.STATUS_SUCCEEDED
        for row in by_handler[CUSTOMER_WHATSAPP_HANDLER]
        + by_handler[DISPATCH_WHATSAPP_HANDLER]
    )
    assert all(
        row.result_json == {"delivery": "provider_accepted"}
        for row in by_handler[CUSTOMER_WHATSAPP_HANDLER]
        + by_handler[DISPATCH_WHATSAPP_HANDLER]
    )
    assert all(row.io_started_at is not None for row in rows)
    customer_email.assert_called_once()
    assert [call.args[1] for call in tenant_email.call_args_list] == [
        owner.email,
        tenant.dispatch_email,
    ]
    whatsapp_sender.assert_called()

    with patch("services.email_service.enviar_email_pedido_cliente") as no_customer_retry, patch(
        "services.email_service.enviar_email_pedido_despacho"
    ) as no_tenant_retry, patch(
        "services.notifications.enviar_notificacion_whatsapp_con_plantilla"
    ) as no_whatsapp_retry:
        replay_summary = dispatch_domain_effects(
            registry=ORDER_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=20,
        )
    assert replay_summary.processed == 0
    no_customer_retry.assert_not_called()
    no_tenant_retry.assert_not_called()
    no_whatsapp_retry.assert_not_called()


def test_worker_fails_closed_if_tenant_owner_changes_before_delivery(
    app,
    monkeypatch,
    order_effect_tenants,
):
    tenant, owner, _, other_owner = order_effect_tenants
    _set_queue(app, monkeypatch, tenant.id)
    order, _, _ = _create_canary_order(
        tenant,
        owner,
        key="order-outbox-owner-change",
    )
    assert order is not None
    other_owner.tenant_id = tenant.id
    tenant.pyme_id = other_owner.id
    db.session.commit()

    with patch("services.email_service.enviar_email_pedido_cliente") as customer_email, patch(
        "services.email_service.enviar_email_pedido_despacho"
    ) as tenant_email, patch(
        "services.notifications.enviar_notificacion_whatsapp_con_plantilla"
    ) as whatsapp_sender:
        summary = dispatch_domain_effects(
            registry=ORDER_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=20,
        )

    rows = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).all()
    assert summary.dead == 6
    assert all(row.status == DomainEffectOutbox.STATUS_DEAD for row in rows)
    assert all(row.io_started_at is None for row in rows)
    assert all(row.last_error_code == "order_tenant_binding_invalid" for row in rows)
    customer_email.assert_not_called()
    tenant_email.assert_not_called()
    whatsapp_sender.assert_not_called()
