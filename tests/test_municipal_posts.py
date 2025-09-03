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
import io
from services.config_loader import BASE_CONFIG_PATH

AGENDA_PATH = os.path.join(BASE_CONFIG_PATH, 'default', 'agenda_cultural.json')

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
    agenda_path = AGENDA_PATH
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


def test_get_municipal_posts(client):
    """Prueba la obtención de posts municipales existentes."""
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}'
    }
    agenda_path = AGENDA_PATH
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    # Crear un post con todos los campos para asegurarse de que se devuelven completos
    client.post(
        '/municipal/posts',
        data={
            'titulo': 'Evento',
            'subtitulo': 'Subtitulo',
            'contenido': 'Desc',
            'tipo_post': 'evento',
            'imagen_url': 'http://img.test/evento.jpg',
            'enlace': 'http://evento.test',
            'fecha_evento_inicio': '2025-01-01T10:00',
            'fecha_evento_fin': '2025-01-01T12:00',
            'ubicacion': 'Centro Cultural',
        },
        headers=headers,
    )

    response = client.get('/municipal/posts', headers=headers)
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
    post = next(p for p in data if p['titulo'] == 'Evento')
    assert post['subtitulo'] == 'Subtitulo'
    assert post['descripcion'] == 'Desc'
    assert post['tipo_post'] == 'evento'
    assert post['imagen_url'] == 'http://img.test/evento.jpg'
    assert post['enlace'] == 'http://evento.test'
    assert post['fecha_evento_inicio'] == '2025-01-01T10:00'
    assert post['fecha_evento_fin'] == '2025-01-01T12:00'
    assert post['ubicacion'] == 'Centro Cultural'
    assert 'fecha_publicacion' in post
    assert 'id' in post

    if os.path.exists(agenda_path):
        os.remove(agenda_path)


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
        {
            "title": "Entrega de Certificados",
            "day": "Sábado 30",
            "time": "11.30 hs.",
            "location": "Centro Universitario",
            "enlace": "https://certificados.example.com",
            "imagen_url": "http://img.test/certificados.jpg",
        },
        {
            "title": "Torneo de Fútbol",
            "day": "Domingo 31",
            "time": "10.00 hs.",
            "location": "Club Social",
            "enlace": "https://futbol.example.com",
            "imagen_url": "http://img.test/futbol.jpg",
        },
    ]

    agenda_path = AGENDA_PATH
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    response = client.post('/municipal/posts/bulk', data=json.dumps({"events": events}), headers=headers)

    assert response.status_code == 201
    data_resp = response.get_json()
    assert len(data_resp['created']) == 2
    assert data_resp['created'][0]['enlace'] == "https://certificados.example.com"
    assert data_resp['created'][0]['imagen_url'] == "http://img.test/certificados.jpg"

    assert os.path.exists(agenda_path)
    with open(agenda_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        agenda = data.get('eventos', [])
    titles = [p['titulo'] for p in agenda]
    assert "Entrega de Certificados" in titles and "Torneo de Fútbol" in titles

    if os.path.exists(agenda_path):
        os.remove(agenda_path)


def test_bulk_create_municipal_posts_spanish_keys(client):
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(
            admin_user.id,
            admin_user.rol,
            admin_user.tipo_chat,
            admin_user.municipio_id,
            admin_user.pyme_id,
        )

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    events = [
        {
            "titulo": "Feria del Libro",
            "dia": "Lunes 1",
            "hora": "10.00 hs.",
            "ubicacion": "Plaza Central",
        },
        {
            "titulo": "Festival de Música",
            "dia": "Martes 2",
            "ubicacion": "Teatro Municipal",
        },
    ]

    agenda_path = AGENDA_PATH
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    response = client.post(
        '/municipal/posts/bulk',
        data=json.dumps({"events": events}),
        headers=headers,
    )

    assert response.status_code == 201
    data_resp = response.get_json()
    assert len(data_resp['created']) == 2
    titles = [p['titulo'] for p in data_resp['created']]
    assert "Feria del Libro" in titles and "Festival de Música" in titles

    if os.path.exists(agenda_path):
        os.remove(agenda_path)


SAMPLE_TEXT = (
    "*Jueves 28*\n"
    "🕑9.30 hs.\n"
    "✅Entrega de reconocimientos a los cuatro primeros Presidentes del HCD en democracia.\n"
    "📍HCD\n\n"
    "*Viernes 29*\n"
    "🕑10.00 hs.\n"
    "✅Expo Educativa 2026\n"
    "📍Centro Universitario del Este\n\n"
    "🕑18.30 hs.\n"
    "✅Capacitación Internacional 'Taller de Juegos' (para docentes de jardines maternales)\n"
    "📍Casa del Bicentenario\n\n"
    "*Sábado 30*\n"
    "🕑11.30 hs.\n"
    "✅Entrega de Certificados del Curso de Lengua de Señas\n"
    "📍Centro Universitario del Este\n\n"
    "*Domingo 31*\n"
    "🕑9.00 a 16.00 hs.\n"
    "✅Encuentro Femenino de Vóley\n"
    "📍Polideportivo Posta El Retamo\n\n"
    "🕑10.00 hs.\n"
    "✅Torneo de Fútbol 'Desafío Libertadores'\n"
    "📍Club Social y Deportivo Los Barriales\n"
)


def test_bulk_create_from_text(client):
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}'
    }

    agenda_path = AGENDA_PATH
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    response = client.post('/municipal/posts/bulk', data={'text': SAMPLE_TEXT}, headers=headers)

    assert response.status_code == 201
    data_resp = response.get_json()
    assert len(data_resp['created']) == 6
    titles = [p['titulo'] for p in data_resp['created']]
    assert 'Expo Educativa 2026' in titles

    if os.path.exists(agenda_path):
        os.remove(agenda_path)


def test_bulk_create_from_raw_string_with_json_header(client):
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}'
    }

    agenda_path = AGENDA_PATH
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    response = client.post(
        '/municipal/posts/bulk',
        data=SAMPLE_TEXT,
        headers=headers,
        content_type='application/json'
    )

    assert response.status_code == 201
    data_resp = response.get_json()
    assert len(data_resp['created']) == 6

    if os.path.exists(agenda_path):
        os.remove(agenda_path)


def test_list_municipal_posts_limits_to_10(client):
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}'
    }

    agenda_path = AGENDA_PATH
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    events = [
        {"title": f"Evento {i}", "day": f"Dia {i}"}
        for i in range(15)
    ]

    response = client.post(
        '/municipal/posts/bulk',
        data=json.dumps({"events": events}),
        headers=headers
    )
    assert response.status_code == 201

    response = client.get('/municipal/posts', headers=headers)
    posts = response.get_json()
    assert len(posts) == 10
    assert posts[0]['titulo'] == 'Evento 14'
    assert posts[-1]['titulo'] == 'Evento 5'

    if os.path.exists(agenda_path):
        os.remove(agenda_path)


def test_bulk_create_from_file(client):
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}'
    }

    agenda_path = AGENDA_PATH
    if os.path.exists(agenda_path):
        os.remove(agenda_path)


def test_municipal_posts_limit(client):
    """Verifica que solo se almacenen los 50 posts más recientes y se devuelvan los últimos 10."""
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    agenda_path = AGENDA_PATH
    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    events = [{"title": f"Evento {i}"} for i in range(60)]
    response = client.post('/municipal/posts/bulk', data=json.dumps({"events": events}), headers=headers)
    assert response.status_code == 201

    with open(agenda_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    assert len(data['eventos']) == 50

    response = client.get('/municipal/posts', headers=headers)
    assert response.status_code == 200
    posts = response.get_json()
    assert len(posts) == 10
    assert posts[0]['titulo'] == 'Evento 59'
    assert posts[-1]['titulo'] == 'Evento 50'

    if os.path.exists(agenda_path):
        os.remove(agenda_path)

    file_data = io.BytesIO(SAMPLE_TEXT.encode('utf-8'))
    data = {'file': (file_data, 'agenda.txt')}
    response = client.post('/municipal/posts/bulk', data=data, headers=headers, content_type='multipart/form-data')

    assert response.status_code == 201
    data_resp = response.get_json()
    assert len(data_resp['created']) == 6
    titles = [p['titulo'] for p in data_resp['created']]
    assert 'Expo Educativa 2026' in titles

    if os.path.exists(agenda_path):
        os.remove(agenda_path)

