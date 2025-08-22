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

def test_create_municipal_post_success(client):
    """
    Prueba la creación exitosa de un post municipal (noticia/evento).
    """
    with app.app_context():
        admin_user = User.query.filter_by(email="admin_muni@test.com").first()
        token = generar_token(admin_user.id, admin_user.rol, admin_user.tipo_chat, admin_user.municipio_id, admin_user.pyme_id)

    headers = {
        'Authorization': f'Bearer {token}'
    }

    post_data = {
        'titulo': 'Gran Evento de Primavera',
        'descripcion': 'Celebraremos la llegada de la primavera con música y comida.',
        'categoria': 'evento'
    }

    response = client.post('/municipal/posts', data=post_data, headers=headers)

    assert response.status_code == 201
    json_data = response.get_json()
    assert json_data['titulo'] == 'Gran Evento de Primavera'
    assert json_data['categoria'] == 'evento'
    assert json_data['estado'] == 'publicado'

    with app.app_context():
        post_in_db = db.session.get(MunicipioTicket, json_data['id'])
        assert post_in_db is not None
        assert post_in_db.asunto == 'Gran Evento de Primavera'
        assert post_in_db.municipio_id == 1

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
        # Falta descripción
    }

    response = client.post('/municipal/posts', data=post_data, headers=headers)

    assert response.status_code == 400
    json_data = response.get_json()
    assert "El título y la descripción son requeridos" in json_data['error']

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
