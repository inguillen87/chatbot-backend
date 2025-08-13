import pytest
from flask import url_for, render_template_string
from app import create_app, db
from models import User
from unittest.mock import patch
import json
from functools import wraps

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
    user1 = User(id=2, name='Test User 1', municipio_id=1, latitud=10.0, longitud=20.0, email='test1@test.com', password_hash='test')
    user2 = User(id=3, name='Test User 2', municipio_id=1, latitud=30.0, longitud=40.0, email='test2@test.com', password_hash='test')
    db.session.add_all([user1, user2])
    db.session.commit()

    mock_user = User(id=1, rol='admin', municipio_id=1, name='Admin User', email='admin@test.com', password_hash='test')
    mock_user.rubro = None

    def token_passthrough(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            return f(mock_user, *args, **kwargs)
        return decorated_function

    def admin_passthrough(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            return f(*args, **kwargs)
        return decorated_function

    with patch('routes.estadisticas.token_requerido', token_passthrough), \
         patch('routes.estadisticas.admin_o_empleado_requerido', admin_passthrough):

        response = client.get(url_for('estadisticas.get_user_locations'))
        assert response.status_code == 200
        data = json.loads(response.data)
        assert len(data) == 2
        assert {'lat': 10.0, 'lng': 20.0} in data
        assert {'lat': 30.0, 'lng': 40.0} in data


