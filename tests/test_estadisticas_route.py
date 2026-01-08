import pytest
from flask import url_for, render_template_string
from app import create_app, db
from models import User
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

from models import Rubro
def test_get_user_locations(client):
    # Create test users without specifying IDs
    user1 = User(name='Test User 1', municipio_id=1, latitud=10.0, longitud=20.0, email='test1@test.com')
    user1.set_password("test")
    user2 = User(name='Test User 2', municipio_id=1, latitud=30.0, longitud=40.0, email='test2@test.com')
    user2.set_password("test")

    admin_rubro = db.session.get(Rubro, 1)
    if not admin_rubro:
        admin_rubro = Rubro(id=1, nombre="municipio", clave="municipio")
        db.session.add(admin_rubro)
        db.session.commit()

    # Create admin user without specifying ID
    admin_user = User(rol='admin', municipio_id=1, name='Admin User', email='admin@test.com', rubro_id=admin_rubro.id)
    admin_user.set_password("adminpass")

    db.session.add_all([user1, user2, admin_user])
    db.session.commit()

    # Fetch the admin user to get the database-assigned ID
    admin_user = User.query.filter_by(email='admin@test.com').first()

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


