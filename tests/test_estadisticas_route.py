import pytest
from flask import url_for, render_template_string
from app import create_app, db
from models import Rubro, TenantProfile, User
from unittest.mock import patch
import json
from functools import wraps
import jwt
from datetime import datetime, timedelta

from config import TestConfig

@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

def test_get_user_locations(client):
    # Create test users
    user1 = User(id=2, name='Test User 1', municipio_id=1, latitud=10.0, longitud=20.0, email='test1@test.com')
    user1.set_password("test")
    user2 = User(id=3, name='Test User 2', municipio_id=1, latitud=30.0, longitud=40.0, email='test2@test.com')
    user2.set_password("test")

    admin_rubro = Rubro(id=1, nombre="municipio", clave="municipio")
    db.session.add(admin_rubro)
    db.session.commit()

    admin_user = User(
        id=1,
        rol='admin',
        name='Admin User',
        email='admin@test.com',
        rubro_id=admin_rubro.id,
        tenant_slug='estadisticas-municipio',
    )
    admin_user.set_password("adminpass")

    db.session.add_all([user1, user2, admin_user])
    db.session.flush()
    admin_user.municipio_id = admin_user.id
    tenant = TenantProfile(
        slug='estadisticas-municipio',
        nombre='Municipio Estadisticas',
        tipo='municipio',
        municipio_id=admin_user.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    admin_user.tenant_id = tenant.id
    db.session.commit()

    jwt_payload = {'user_id': admin_user.id, 'exp': datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, client.application.config['SECRET_KEY'], algorithm="HS256")

    response = client.get(
        url_for('estadisticas.get_user_locations'),
        headers={'Authorization': f'Bearer {jwt_token}'}
    )

    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 2
    assert {'lat': 10.0, 'lng': 20.0} in data
    assert {'lat': 30.0, 'lng': 40.0} in data
