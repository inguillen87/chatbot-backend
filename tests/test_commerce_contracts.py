from app import create_app, db
from config import TestConfig
from models import CatalogoItem, MarketCart, MarketCartItem, TenantProfile, User
from routes.market import _cart_summary
from services.commerce_contracts import build_contact_key, normalize_sales_channel, resolve_order_contact_payload


def test_commerce_contracts_normalize_channel_and_contact_key():
    assert normalize_sales_channel("whatsapp_business") == "whatsapp"
    assert normalize_sales_channel("widget_chat") == "widget"
    assert normalize_sales_channel("telefono") == "phone"
    assert build_contact_key(phone="+54 9 11 5555 4444") == "phone:5491155554444"
    assert build_contact_key(email="VENTAS@chatboc.ar") == "email:ventas@chatboc.ar"


def test_resolve_order_contact_payload_prefers_user_identity():
    user = type("UserStub", (), {"id": 44, "name": "Ana", "email": "ana@test.com", "telefono": "+5491122233344", "anon_id": None})()
    payload = resolve_order_contact_payload(
        user=user,
        payload={"contacto": {"telefono": "+5491188877766"}},
        session_id="session-1",
        channel="widget_chat",
    )
    assert payload["channel"] == "widget"
    assert payload["contact_key"] == "user:44"
    assert payload["phone"] == "+5491188877766"


def test_market_cart_summary_includes_promotions_and_contact_contract():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()

        owner = User(email="owner-commerce@test.com", name="Owner Commerce", rol="admin", tipo_chat="pyme")
        owner.set_password("pass")
        db.session.add(owner)
        db.session.commit()

        tenant = TenantProfile(slug="tenant-commerce", nombre="Tenant Commerce", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        product = CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Caja de vino",
            categoria="Bebidas",
            precio="1000",
            precio_monetario=1000,
            moneda="ARS",
            modalidad="venta",
            promocion_info="10% OFF",
        )
        db.session.add(product)
        db.session.commit()

        cart = MarketCart(
            tenant_id=tenant.id,
            user_id=owner.id,
            session_id="session-commerce",
            contact_name="Ana Buyer",
            contact_phone="+5491122233344",
            contact_email="buyer@test.com",
            contact_key="user:1",
            channel="whatsapp",
        )
        db.session.add(cart)
        db.session.commit()
        db.session.add(
            MarketCartItem(
                cart_id=cart.id,
                product_id=product.id,
                quantity=2,
                price_text="1000",
                price_monetary=1000,
                currency="ARS",
                modalidad="venta",
                name_snapshot=product.nombre,
            )
        )
        db.session.commit()

        with app.test_request_context("/api/market/tenant-commerce/cart"):
            summary = _cart_summary(cart, owner, event="refresh")
        assert summary["channel"] == "whatsapp"
        assert summary["contact_key"] == "user:1"
        assert summary["contacto"]["email"] == "buyer@test.com"
        assert summary["checkout_preview"]["state"] == "ready"
        assert summary["continuity"]["resume_key"] == "user:1"
        assert isinstance(summary["suggested_actions"], list)
        if "promotions" in summary:
            assert summary["promotions"]["total_con_descuento"] <= summary["total_estimado"]
