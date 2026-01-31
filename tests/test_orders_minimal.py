from flask import Blueprint, jsonify
from models import db, Order, User, TenantProfile, OrderItem
import pytest
from app import create_app, db
import json

def test_order_creation_persistence():
    # Setup - In-memory DB or temporary file would be better, but we rely on app config
    app = create_app()
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'

    with app.app_context():
        db.create_all()

        # Create dependencies
        u = User(name="Test User", email="test@test.com", password_hash="hash")
        db.session.add(u)
        db.session.commit()

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
