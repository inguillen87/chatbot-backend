import json

from app import create_app, db
from config import TestConfig
from models import MarketOrder, MarketOrderItem, PedidoConversacional, PymePedido, TenantProfile, User
from services.commerce_contracts import build_customer_profile
from services.commerce_unified import serialize_unified_order


def test_build_customer_profile_normalizes_phone_channels_and_identity():
    user = type(
        "UserStub",
        (),
        {"id": 77, "name": "  Ana Buyer ", "email": "ANA@Buyer.com ", "telefono": None, "anon_id": "whatsapp:+5491166677788"},
    )()

    profile = build_customer_profile(user=user, payload={"channel": "telefono"}, session_id="session-77")

    assert profile["channel"] == "phone"
    assert profile["channel_group"] == "conversational"
    assert profile["phone"] == "+5491166677788"
    assert profile["contact_key"] == "user:77"


def test_serialize_unified_order_supports_multiple_models():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        owner = User(email="owner-unified@test.com", name="Owner Unified", rol="admin", tipo_chat="pyme")
        owner.set_password("pass")
        db.session.add(owner)
        db.session.commit()

        tenant = TenantProfile(slug="tenant-unified", nombre="Tenant Unified", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        market = MarketOrder(
            tenant_id=tenant.id,
            user_id=owner.id,
            status="pending",
            contact_name="Buyer",
            contact_phone="+5491122233344",
            contact_email="buyer@test.com",
            contact_key="user:1",
            channel="voice",
            total_monetary=5500,
            currency="ARS",
        )
        market.items.append(MarketOrderItem(quantity=2, name_snapshot="Yerba", price_monetary=2750, currency="ARS"))
        db.session.add(market)

        conv = PedidoConversacional(
            tenant_id=tenant.id,
            user_id=owner.id,
            estado="pendiente_pago",
            monto_monetario=1200,
            origen="whatsapp",
            items=[{"title": "Café", "quantity": 1, "unit_price": 1200, "currency_id": "ARS"}],
            metadata_payload={"contact_key": "email:test@test.com", "contacto": {"nombre": "Ana", "email": "test@test.com"}},
        )
        db.session.add(conv)

        legacy = PymePedido(
            pyme_id=owner.id,
            tenant_id=tenant.id,
            asunto="Pedido",
            detalles=json.dumps([{"nombre": "Pan", "cantidad": 3, "precio_unitario": 1000}], ensure_ascii=False),
            monto_total=3000,
            nombre_cliente="Buyer Legacy",
            email_cliente="legacy@test.com",
            telefono_cliente="+5491100000000",
            user_id=owner.id,
        )
        db.session.add(legacy)
        db.session.commit()

        serialized_market = serialize_unified_order(market)
        serialized_conv = serialize_unified_order(conv)
        serialized_legacy = serialize_unified_order(legacy)

        assert serialized_market["channel"] == "phone"
        assert serialized_market["commercial_stage"] == "awaiting_confirmation"
        assert serialized_market["items"][0]["title"] == "Yerba"
        assert serialized_conv["commercial_stage"] == "awaiting_payment"
        assert serialized_conv["contact"]["contact_key"] == "email:test@test.com"
        assert serialized_legacy["source_model"] == "PymePedido"
        assert serialized_legacy["items"][0]["title"] == "Pan"


def test_market_order_query_is_legacy_safe_for_deferred_columns():
    app = create_app(TestConfig)
    with app.app_context():
        statement = str(MarketOrder.query.statement)
        assert "contact_key" not in statement
        assert "session_id" not in statement


def test_market_order_legacy_safe_query_omits_deferred_columns_in_filtered_queries():
    app = create_app(TestConfig)
    with app.app_context():
        statement = str(MarketOrder.legacy_safe_query().filter_by(tenant_id=1, user_id=2).statement)
        assert "contact_key" not in statement
        assert "session_id" not in statement
