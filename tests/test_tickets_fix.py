import pytest
from app import create_app
from extensions import db
from models import User, Rubro, TenantProfile

def test_get_tickets_requires_authentication(client):
    # Create a test user with a valid rubro
    rubro = Rubro(nombre="pymes", clave="pymes")
    db.session.add(rubro)
    db.session.commit()

    test_user = User(email="test@example.com", name="Test", nombre_empresa="Test", rubro_id=rubro.id, tipo_chat="pyme", rol="admin")
    test_user.set_password("testpass")
    db.session.add(test_user)
    db.session.flush()
    db.session.add(
        TenantProfile(
            slug="tickets-auth-pyme",
            nombre="Tickets Auth Pyme",
            tipo="pyme",
            pyme_id=test_user.id,
            configuracion={},
        )
    )
    db.session.commit()

    response = client.get('/tickets')
    assert response.status_code == 401

    login_resp = client.post('/auth/login', json={
        "email": "test@example.com",
        "password": "testpass"
    })
    token = login_resp.get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get('/tickets', headers=headers)
    assert response.status_code == 200
    assert b'tickets' in response.data
