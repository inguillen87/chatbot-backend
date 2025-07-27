import pytest
from flask import url_for, render_template_string
from app import create_app, db
from models import User
from unittest.mock import patch
import json

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

@patch('routes.auth.token_requerido', lambda x: x)
def test_get_user_locations(client):
    # Create test users
    user1 = User(id=2, name='Test User 1', municipio_id=1, latitud=10.0, longitud=20.0, email='test1@test.com', password_hash='test')
    user2 = User(id=3, name='Test User 2', municipio_id=1, latitud=30.0, longitud=40.0, email='test2@test.com', password_hash='test')
    db.session.add_all([user1, user2])
    db.session.commit()

    with patch('routes.estadisticas.get_user_from_token') as mock_get_user:
        mock_get_user.return_value = User(id=1, rol='admin', municipio_id=1, rubro_id=None, name='Admin User', email='admin@test.com', password_hash='test')
        response = client.get(url_for('estadisticas.get_user_locations'))
        assert response.status_code == 200
        data = json.loads(response.data)
        assert len(data) == 2
        assert {'lat': 10.0, 'lng': 20.0} in data
        assert {'lat': 30.0, 'lng': 40.0} in data

@patch('routes.auth.token_requerido', lambda x: x)
def test_mapa_calor_route(client):
    with patch('routes.estadisticas.get_user_from_token') as mock_get_user:
        mock_get_user.return_value = User(id=1, rol='admin', municipio_id=1, rubro_id=None, name='Admin User', email='admin@test.com', password_hash='test')
        response = client.get(url_for('estadisticas.mapa_calor'))
        assert response.status_code == 200
        assert 'Estadísticas y Mapas de Calor' in response.get_data(as_text=True)
