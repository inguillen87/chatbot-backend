from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import jwt

from app import db
from models import CatalogoItem, ChatSessionContext, PymePedido, Rubro, TenantProfile, User
from services.actions.pyme_order_actions import AgregarItemCarritoAction, ConsultarProductoAction
from services.pymes import CONTEXTO_PYME, responder_pyme


def _auth_headers(app, user: User) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_whatsapp_message_catalog_cart_order_tracking_backoffice_lifecycle(
    client,
    app,
    monkeypatch,
):
    rubro = Rubro(clave="ferreteria-commerce-e2e", nombre="Ferreteria")
    db.session.add(rubro)
    db.session.flush()

    owner = User(
        name="Ferreteria Commerce",
        email="commerce-owner@test.com",
        rol="admin",
        tipo_chat="pyme",
        rubro_id=rubro.id,
    )
    owner.set_password("pw")
    customer = User(
        name="Cliente WhatsApp",
        email="commerce-customer@test.com",
        rol="usuario",
        tipo_chat="pyme",
        telefono="+5492615550101",
    )
    customer.set_password("pw")
    db.session.add_all([owner, customer])
    db.session.flush()

    tenant = TenantProfile(
        slug="ferreteria-commerce-e2e",
        nombre="Ferreteria Commerce",
        tipo="pyme",
        pyme_id=owner.id,
        plan="pro",
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    customer.tenant_id = tenant.id

    catalog_item = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Taladro percutor 18V",
        sku="TAL-18V",
        descripcion="Taladro con bateria",
        precio="$ 125000,55",
        precio_monetario=Decimal("125000.55"),
        cantidad="5",
        unidad="unidad",
        modalidad="venta",
        disponible=True,
    )
    session = ChatSessionContext(
        chat_session_id="whatsapp-commerce-e2e",
        user_id=owner.id,
        tenant_id=tenant.id,
        anon_id=customer.telefono,
        context_data={
            CONTEXTO_PYME: {},
            "mensajes_previos_llm_formato": [],
            "canal_origen": "whatsapp",
        },
    )
    db.session.add_all([catalog_item, session])
    db.session.commit()

    catalog_hit = SimpleNamespace(
        payload={
            "db_id": catalog_item.id,
            "nombre": catalog_item.nombre,
            "descripcion_corta": catalog_item.descripcion,
            "precio_str": "125000.55",
            "sku": catalog_item.sku,
            "cantidad": catalog_item.cantidad,
        }
    )
    llm_turns = iter(
        [
            {
                "accion_backend": "consultar_producto_pyme",
                "datos_estructura": {
                    "target": "pyme",
                    "consulta_producto": "taladro 18V",
                },
                "message_body": "Buscando el producto.",
            },
            {
                "accion_backend": "agregar_item_carrito",
                "datos_estructura": {
                    "target": "pyme",
                    "producto_sku": catalog_item.sku,
                    "cantidad_producto_mencionado": 2,
                },
                "message_body": "Agregando al carrito.",
            },
            {
                "accion_backend": "finalizar_pedido_pyme",
                "datos_estructura": {
                    "target": "pyme",
                    "nombre_usuario_detectado": customer.name,
                    "telefono_detectado": customer.telefono,
                    "email_detectado": customer.email,
                    "ubicacion": "San Martin 123, Mendoza",
                },
                "message_body": "Confirmando el pedido.",
            },
        ]
    )

    def fake_llm(*_args, **_kwargs):
        return next(llm_turns), {"provider": "offline-test"}

    monkeypatch.setattr("services.pymes.llamar_llm_con_fallback", fake_llm)
    monkeypatch.setattr(
        "services.actions.pyme_order_actions.buscar_catalogo_qdrant",
        lambda **_kwargs: [catalog_hit],
    )
    monkeypatch.setattr("services.pymes.sugerir_productos_relacionados", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "services.pedido_service.generar_pdf_nota_pedido",
        lambda *_args, **_kwargs: b"%PDF-controlled-test",
    )
    monkeypatch.setattr(
        "services.pedido_service.notification_dispatcher.dispatch_order_created",
        lambda *_args, **_kwargs: None,
    )

    common = {
        "owner_user": owner,
        "rubro_obj": rubro,
        "viewer_user": customer,
        "chat_db_context": session,
        "anon_id": customer.telefono,
        "channel": "whatsapp",
        "chat_session_uuid": session.chat_session_id,
        "tenant_profile": tenant,
        "tenant_id": tenant.id,
        "idempotency_key": "whatsapp:commerce-e2e:checkout-0001",
    }

    catalog_response = responder_pyme(
        pregunta_original="Cuanto cuesta el taladro 18V?",
        **common,
    )
    assert catalog_response["fuente"] == "consultar_producto_pyme"
    assert catalog_response["data"]["productos_encontrados"][0]["sku"] == catalog_item.sku
    assert "Taladro percutor 18V" in catalog_response["message_body"]
    assert "125,000.55" in catalog_response["message_body"]

    cart_response = responder_pyme(
        pregunta_original="Agrega dos al carrito",
        **common,
    )
    cart = cart_response["data"]["cart_summary"]
    assert cart["items_detalle"][0]["catalogo_item_id"] == catalog_item.id
    assert cart["items_detalle"][0]["nombre_producto"] == catalog_item.nombre
    assert cart["items_detalle"][0]["cantidad"] == 2
    assert Decimal(str(cart["total_final_con_descuento"])) == Decimal("250001.10")

    order_response = responder_pyme(
        pregunta_original="Finaliza el pedido",
        **common,
    )
    assert order_response["fuente"] == "pyme_pedido_confirmado"
    assert order_response["data"]["nota_pedido_pdf_generado"] is True
    assert "quedó generada" in order_response["message_body"]
    assert "el envío se confirma por separado" in order_response["message_body"]
    assert order_response["message_body"].count("nota de pedido en PDF") == 1
    assert "Te enviamos" not in order_response["message_body"]
    assert order_response["data"]["tracking_url"].endswith(
        f"/tracking/order/{order_response['data']['nro_pedido']}"
    )

    pedido = db.session.get(PymePedido, order_response["data"]["pedido_id"])
    assert pedido is not None
    assert pedido.tenant_id == tenant.id
    assert pedido.pyme_id == owner.id
    assert pedido.channel == "whatsapp"
    assert Decimal(str(pedido.monto_total)) == Decimal("250001.10")
    assert pedido.idempotency_key == "whatsapp:commerce-e2e:checkout-0001"
    assert len(pedido.idempotency_payload_hash or "") == 64
    assert session.context_data["carritos_pymes"][str(owner.id)] == []
    db.session.commit()
    db.session.expire(session, ["context_data"])
    assert session.context_data["carritos_pymes"][str(owner.id)] == []

    backoffice_response = client.get(
        "/api/v2/backoffice/orders/summary",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(app, owner),
    )
    assert backoffice_response.status_code == 200, backoffice_response.get_json()
    backoffice = backoffice_response.get_json()
    assert backoffice["contract_version"] == "backoffice.orders_summary.v1"
    assert backoffice["summary"]["total"] == 1
    assert backoffice["active_orders"][0]["number"] == pedido.nro_pedido

    tracking_response = client.get(
        "/api/public/tracking/experience",
        query_string={"kind": "order", "code": pedido.nro_pedido},
        headers={"X-Request-Id": "req-commerce-tracking-e2e"},
    )
    assert tracking_response.status_code == 200, tracking_response.get_json()
    tracking = tracking_response.get_json()
    assert tracking["contract_version"] == "tracking.experience.v1"
    assert tracking["kind"] == "order"
    assert tracking["resource"]["code"] == pedido.nro_pedido
    assert tracking["privacy"]["pii_redacted"] is True


def test_cart_rejects_catalog_hit_from_another_tenant(client, monkeypatch):
    rubro = Rubro(clave="commerce-tenant-scope", nombre="Commerce scope")
    db.session.add(rubro)
    db.session.flush()
    owner = User(
        name="Tenant A owner",
        email="tenant-a-commerce@test.com",
        rol="admin",
        tipo_chat="pyme",
        rubro_id=rubro.id,
    )
    foreign_owner = User(
        name="Tenant B owner",
        email="tenant-b-commerce@test.com",
        rol="admin",
        tipo_chat="pyme",
        rubro_id=rubro.id,
    )
    owner.set_password("pw")
    foreign_owner.set_password("pw")
    db.session.add_all([owner, foreign_owner])
    db.session.flush()
    tenant = TenantProfile(
        slug="commerce-tenant-a",
        nombre="Commerce tenant A",
        tipo="pyme",
        pyme_id=owner.id,
    )
    foreign_tenant = TenantProfile(
        slug="commerce-tenant-b",
        nombre="Commerce tenant B",
        tipo="pyme",
        pyme_id=foreign_owner.id,
    )
    db.session.add_all([tenant, foreign_tenant])
    db.session.flush()
    foreign_item = CatalogoItem(
        user_id=foreign_owner.id,
        tenant_id=foreign_tenant.id,
        nombre="Producto privado tenant B",
        sku="PRIVATE-B-01",
        precio="$ 9999,99",
        modalidad="venta",
        disponible=True,
    )
    db.session.add(foreign_item)
    db.session.commit()

    monkeypatch.setattr(
        "services.actions.pyme_order_actions.buscar_catalogo_qdrant",
        lambda **_kwargs: [
            SimpleNamespace(
                payload={
                    "db_id": foreign_item.id,
                    "nombre": foreign_item.nombre,
                    "sku": foreign_item.sku,
                    "precio_str": "9999.99",
                }
            )
        ],
    )
    context_data = {"carritos_pymes": {}}
    result = AgregarItemCarritoAction(
        {
            "user_id": owner.id,
            "tenant_id": tenant.id,
            "tenant_profile": tenant,
            "chat_db_context_data": context_data,
            "cliente_id": None,
            "pregunta_actual_usuario": "Agrega PRIVATE-B-01",
        }
    ).execute(
        {
            "producto_sku": foreign_item.sku,
            "cantidad_producto_mencionado": 1,
        }
    )

    assert result["success"] is False
    assert foreign_item.nombre not in result["message_to_user"]
    assert context_data["carritos_pymes"].get(str(owner.id), []) == []


def test_cart_rejects_vector_price_without_canonical_catalog_row(client, monkeypatch):
    rubro = Rubro(clave="commerce-stale-vector", nombre="Commerce stale vector")
    owner = User(
        name="Canonical catalog owner",
        email="canonical-catalog-owner@test.com",
        rol="admin",
        tipo_chat="pyme",
    )
    owner.set_password("pw")
    db.session.add_all([rubro, owner])
    db.session.flush()
    owner.rubro_id = rubro.id
    tenant = TenantProfile(
        slug="canonical-commerce",
        nombre="Canonical commerce",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.commit()

    monkeypatch.setattr(
        "services.actions.pyme_order_actions.buscar_catalogo_qdrant",
        lambda **_kwargs: [
            SimpleNamespace(
                payload={
                    "nombre": "Producto vectorial inexistente",
                    "precio_str": "1.00",
                }
            )
        ],
    )
    context_data = {"carritos_pymes": {}}
    result = AgregarItemCarritoAction(
        {
            "user_id": owner.id,
            "tenant_id": tenant.id,
            "tenant_profile": tenant,
            "chat_db_context_data": context_data,
            "cliente_id": None,
            "pregunta_actual_usuario": "Agrega producto inexistente",
        }
    ).execute(
        {
            "nombre_producto_mencionado": "Producto vectorial inexistente",
            "cantidad_producto_mencionado": 1,
        }
    )

    assert result["success"] is False
    assert context_data["carritos_pymes"].get(str(owner.id), []) == []

    lookup = ConsultarProductoAction(
        {
            "user_id": owner.id,
            "tenant_id": tenant.id,
            "tenant_profile": tenant,
            "user_obj": owner,
        }
    ).execute({"consulta_producto": "Producto vectorial inexistente"})
    assert lookup["success"] is True
    assert lookup["data"]["productos_encontrados"] == []
    assert "$0.01" not in lookup["message_to_user"]
    assert "(Precio:" not in lookup["message_to_user"]


def test_cart_rehydrates_legacy_vector_hit_by_scoped_sku(client, monkeypatch):
    rubro = Rubro(clave="commerce-vector-sku", nombre="Commerce vector SKU")
    owner = User(
        name="Scoped SKU owner",
        email="scoped-sku-owner@test.com",
        rol="admin",
        tipo_chat="pyme",
    )
    owner.set_password("pw")
    db.session.add_all([rubro, owner])
    db.session.flush()
    owner.rubro_id = rubro.id
    tenant = TenantProfile(
        slug="scoped-sku-commerce",
        nombre="Scoped SKU commerce",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    canonical = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Producto SQL canónico",
        sku="SQL-ONLY-01",
        precio="$ 73,25",
        modalidad="venta",
        disponible=True,
    )
    db.session.add(canonical)
    db.session.commit()

    monkeypatch.setattr(
        "services.actions.pyme_order_actions.buscar_catalogo_qdrant",
        lambda **_kwargs: [
            SimpleNamespace(
                payload={
                    "sku": canonical.sku,
                    "nombre": "Nombre vectorial contaminado",
                    "precio_str": "0.01",
                }
            )
        ],
    )
    context_data = {"carritos_pymes": {}}
    result = AgregarItemCarritoAction(
        {
            "user_id": owner.id,
            "tenant_id": tenant.id,
            "tenant_profile": tenant,
            "chat_db_context_data": context_data,
            "cliente_id": None,
            "pregunta_actual_usuario": "Agrega SQL-ONLY-01",
        }
    ).execute(
        {
            "producto_sku": canonical.sku,
            "cantidad_producto_mencionado": 1,
        }
    )

    assert result["success"] is True
    item = result["data"]["cart_summary"]["items_detalle"][0]
    assert item["catalogo_item_id"] == canonical.id
    assert item["nombre_producto"] == canonical.nombre
    assert Decimal(str(item["precio_unitario_original"])) == Decimal("73.25")
