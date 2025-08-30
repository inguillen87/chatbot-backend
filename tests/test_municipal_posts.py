import pytest
from app import app, db
from models import User, MunicipioTicket
from utils.auth_helpers import generar_token

@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            # Crear usuario municipal admin para la prueba
            admin_user = User(
                name="Admin Muni",
                email="admin_muni@test.com",
                rol="admin",
                tipo_chat="municipio",
                municipio_id=1 # Asumimos un ID de municipio para la prueba
            )
            admin_user.set_password("adminpassword")
            db.session.add(admin_user)
            db.session.commit()
        yield client
        with app.app_context():
            db.drop_all()

import json
import os

def test_create_municipal_post_success(client):
    """
    Prueba la creación exitosa de un post municipal (noticia/evento) en el archivo JSON.
    """
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}'
    }

    post_data = {
        'titulo': 'Gran Evento de Primavera',
        'contenido': 'Celebraremos la llegada de la primavera con música y comida.',
        'tipo_post': 'evento'
    }

    # Ensure the file is clean before the test
    agenda_path = 'data/municipios/default/agenda_cultural.json'
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    response = client.post('/municipal/posts', data=post_data, headers=headers)

    assert response.status_code == 201
    json_data = response.get_json()
    assert json_data['titulo'] == 'Gran Evento de Primavera'

    # Verificar que el post se guardó en el archivo JSON
    assert os.path.exists(agenda_path)
    with open(agenda_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        agenda = data.get("eventos", [])
    assert any(p['titulo'] == 'Gran Evento de Primavera' for p in agenda)

    # Clean up the file after test
    if os.path.exists(agenda_path):
        os.remove(agenda_path)


def test_create_municipal_post_no_data(client):
    """
    Prueba que la creación de un post falle si faltan datos.
    """
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}'
    }

    post_data = {
        'titulo': 'Solo Título'
        # Falta contenido y tipo_post
    }

    response = client.post('/municipal/posts', data=post_data, headers=headers)

    assert response.status_code == 400
    json_data = response.get_json()
    assert "El título, el contenido y el tipo de post son requeridos" in json_data['error']

def test_create_municipal_post_unauthorized(client):
    """
    Prueba que un usuario no autorizado no pueda crear un post.
    """
    # Sin token
    response = client.post('/municipal/posts', data={'titulo': 't', 'descripcion': 'd'})
    assert response.status_code == 401

    # Con token de usuario no municipal
    with app.app_context():
        pyme_user = User(
            name="Usuario Pyme",
            email="pyme@test.com",
            rol="admin",
            tipo_chat="pyme",
            pyme_id=1
        )
        pyme_user.set_password("pymepass")
        db.session.add(pyme_user)
        db.session.commit()
        token_pyme = generar_token(pyme_user.id, pyme_user.rol, pyme_user.tipo_chat, pyme_user.municipio_id, pyme_user.pyme_id)

    headers_pyme = {
        'Authorization': f'Bearer {token_pyme}'
    }
    response_pyme = client.post('/municipal/posts', data={'titulo': 't', 'descripcion': 'd'}, headers=headers_pyme)
    assert response_pyme.status_code == 403
    assert "Se requiere un usuario municipal" in response_pyme.get_json()['error']


def test_bulk_create_municipal_posts(client):
    """Prueba la creación de múltiples eventos vía JSON."""
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    events = [
        {"title": "Entrega de Certificados", "day": "Sábado 30", "time": "11.30 hs.", "location": "Centro Universitario"},
        {"title": "Torneo de Fútbol", "day": "Domingo 31", "time": "10.00 hs.", "location": "Club Social"},
    ]

    agenda_path = 'data/municipios/default/agenda_cultural.json'
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    response = client.post('/municipal/posts/bulk', data=json.dumps({"events": events}), headers=headers)

    assert response.status_code == 201
    data_resp = response.get_json()
    assert len(data_resp['created']) == 2

    assert os.path.exists(agenda_path)
    with open(agenda_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        agenda = data.get('eventos', [])
    titles = [p['titulo'] for p in agenda]
    assert "Entrega de Certificados" in titles and "Torneo de Fútbol" in titles

    if os.path.exists(agenda_path):
        os.remove(agenda_path)

