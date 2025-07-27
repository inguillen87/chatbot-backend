import pytest
from flask import url_for, render_template_string
from app import create_app, db
from models import User
from unittest.mock import patch
import json

@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@patch('routes.auth.token_requerido')
def test_get_user_locations(mock_token_requerido, client):
    # Mock current_user
    mock_user = User(id=1, is_admin=True, municipio_id=1, rubro_id=None)
    mock_token_requerido.return_value = lambda f: lambda *args, **kwargs: f(mock_user, *args, **kwargs)

    # Create test users
    user1 = User(id=2, name='Test User 1', municipio_id=1, latitud=10.0, longitud=20.0)
    user2 = User(id=3, name='Test User 2', municipio_id=1, latitud=30.0, longitud=40.0)
    db.session.add_all([mock_user, user1, user2])
    db.session.commit()

    response = client.get(url_for('estadisticas.api/locations'))
    assert response.status_code == 200
    data = json.loads(response.data)
    assert len(data) == 2
    assert {'lat': 10.0, 'lng': 20.0} in data
    assert {'lat': 30.0, 'lng': 40.0} in data

@patch('routes.auth.token_requerido')
def test_mapa_calor_route(mock_token_requerido, client):
    # Mock current_user
    mock_user = User(id=1, is_admin=True, municipio_id=1, rubro_id=None)
    mock_token_requerido.return_value = lambda f: lambda *args, **kwargs: f(mock_user, *args, **kwargs)

    response = client.get(url_for('estadisticas.mapa_calor'))
    assert response.status_code == 200
    assert 'Estadísticas y Mapas de Calor' in response.get_data(as_text=True)
