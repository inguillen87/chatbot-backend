from app import create_app, db
from config import Config
from models import Order, OrderItem, TenantProfile, User


class OrderPersistenceTestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SESSION_TYPE = "filesystem"
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


def test_order_creation_persistence():
    app = create_app(OrderPersistenceTestConfig)

    with app.app_context():
        db.create_all()

        # Create dependencies
        u = User.query.filter_by(email="test@test.com").first()
        if not u:
            u = User(name="Test User", email="test@test.com", password_hash="hash")
            db.session.add(u)
            db.session.commit()

        t = TenantProfile.query.filter_by(slug="test-tenant").first()
        if not t:
            t = TenantProfile(slug="test-tenant", nombre="Test Tenant", tipo="pyme", pyme_id=u.id)
            db.session.add(t)
            db.session.commit()

        # Test Order Create
        o = Order(
            tenant_id=t.id,
            buyer_name="Buyer",
            total=100,
            status="created"
        )
        db.session.add(o)

        oi = OrderItem(
            title="Item 1",
            quantity=1,
            unit_price=100,
            total_price=100
        )
        o.items.append(oi)
        db.session.commit()

        # Assertions
        saved_order = Order.query.first()
        assert saved_order is not None
        assert saved_order.buyer_name == "Buyer"
        assert len(saved_order.items) == 1
        assert saved_order.items[0].title == "Item 1"

        print("Order persistence test passed!")

if __name__ == "__main__":
    test_order_creation_persistence()
