import sys
import os
from app import db
from models import User


def test_login_no_json(client):
    """
    Tests that the login route returns a 400 error if the request is not JSON.
    """
    response = client.post('/login', data="not a json")
    assert response.status_code == 400
    assert response.get_json() == {"error": "La solicitud debe ser de tipo JSON."}

def test_login_missing_credentials(client):
    """
    Tests that the login route returns a 400 error if the email or password are not provided.
    """
    response = client.post('/login', json={})
    assert response.status_code == 400
    assert response.get_json() == {"error": "Email y contraseña requeridos."}

def test_login_invalid_credentials(client):
    """
    Tests that the login route returns a 401 error if the credentials are invalid.
    """
    response = client.post('/login', json={"email": "a@b.com", "password": "c"})
    assert response.status_code == 401
    assert response.get_json() == {"error": "Email o contraseña incorrectos."}

def test_login_successful(client):
    """
    Tests that the login route returns a 200 status code and a token if the credentials are valid.
    """
    user = User(email="a@b.com", name="Test User", token="test-token")
    user.set_password("c")
    db.session.add(user)
    db.session.commit()

    response = client.post('/login', json={"email": "a@b.com", "password": "c"})
    assert response.status_code == 200
    assert "token" in response.get_json()

def test_register_creates_admin_user(client):
    """
    Tests that a user created through the /register endpoint is assigned the 'admin' role.
    """
    from models import Rubro
    # Ensure a Rubro exists
    if not Rubro.query.first():
        rubro = Rubro(clave='default', nombre='Default')
        db.session.add(rubro)
        db.session.commit()

    rubro = Rubro.query.first()

    registration_data = {
        "name": "Test Admin",
        "email": "admin@test.com",
        "password": "adminpassword",
        "nombre_empresa": "Test Company",
        "rubro": rubro.id,
        "tipo_chat": "pyme",
        "acepto_terminos": True
    }

    response = client.post('/auth/register', json=registration_data)

    assert response.status_code == 201
    json_data = response.get_json()
    assert json_data['rol'] == 'admin'

    # Verify the user in the database
    user = User.query.filter_by(email="admin@test.com").first()
    assert user is not None
    assert user.rol == 'admin'

def test_get_profile_endpoints(client):
    """
    Tests that the /perfil and /me endpoints return user data for an authenticated user.
    """
    # First, create a user and a rubro to associate with
    from models import Rubro
    if not Rubro.query.first():
        rubro = Rubro(clave='testing', nombre='Testing')
        db.session.add(rubro)
        db.session.commit()
    rubro = Rubro.query.first()

    user = User(
        email="profile@test.com",
        name="Profile User",
        token="profile-test-token",
        rubro_id=rubro.id
    )
    user.set_password("password")
    db.session.add(user)
    db.session.commit()

    headers = {
        'Authorization': f'Bearer {user.token}'
    }

    # Test /perfil
    response_perfil = client.get('/perfil', headers=headers)
    assert response_perfil.status_code == 200
    assert response_perfil.get_json()['email'] == user.email

    # Test /me
    response_me = client.get('/me', headers=headers)
    assert response_me.status_code == 200
    assert response_me.get_json()['email'] == user.email

    # Test without token
    response_no_token = client.get('/perfil')
    assert response_no_token.status_code == 401


if __name__ == '__main__':
    import unittest
    unittest.main()
