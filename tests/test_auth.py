import sys
import os
from app import db
from models import User


def test_login_no_json(test_client):
    """
    Tests that the login route returns a 400 error if the request is not JSON.
    """
    response = test_client.post('/login', data="not a json")
    assert response.status_code == 400
    assert response.get_json() == {"error": "La solicitud debe ser de tipo JSON."}

def test_login_missing_credentials(test_client):
    """
    Tests that the login route returns a 400 error if the email or password are not provided.
    """
    response = test_client.post('/login', json={})
    assert response.status_code == 400
    assert response.get_json() == {"error": "Email y contraseña requeridos."}

def test_login_invalid_credentials(test_client):
    """
    Tests that the login route returns a 401 error if the credentials are invalid.
    """
    response = test_client.post('/login', json={"email": "a@b.com", "password": "c"})
    assert response.status_code == 401
    assert response.get_json() == {"error": "Email o contraseña incorrectos."}

def test_login_successful(test_client):
    """
    Tests that the login route returns a 200 status code and a token if the credentials are valid.
    """
    user = User(email="a@b.com", name="Test User", token="test-token")
    user.set_password("c")
    db.session.add(user)
    db.session.commit()

    response = test_client.post('/login', json={"email": "a@b.com", "password": "c"})
    assert response.status_code == 200
    assert "token" in response.get_json()

if __name__ == '__main__':
    unittest.main()
