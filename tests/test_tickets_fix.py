import pytest
from app import create_app
from extensions import db
from models import User, Rubro

@pytest.fixture
def client():
    print("Setting up client...")
    app = create_app()
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    with app.test_client() as client:
        with app.app_context():
            print("Creating database...")
            db.create_all()
            print("Database created.")
            yield client
            print("Dropping database...")
            db.drop_all()
            print("Database dropped.")
    print("Client setup complete.")

def test_get_tickets_requires_authentication(client):
    print("Starting test_get_tickets_requires_authentication...")
    try:
        # Create a test user with a valid rubro
        with client.application.app_context():
            rubro = Rubro(nombre="pymes", clave="pymes")
            db.session.add(rubro)
            db.session.commit()

            test_user = User(email="test@example.com", name="Test", nombre_empresa="Test", rubro_id=rubro.id, tipo_chat="pyme", rol="admin")
            test_user.set_password("testpass")
            db.session.add(test_user)
            db.session.commit()

        print("Sending unauthenticated GET request to /tickets...")
        response = client.get('/tickets')
        print(f"Response status code: {response.status_code}")
        assert response.status_code == 401

        print("Logging in to obtain token...")
        login_resp = client.post('/auth/login', json={
            "email": "test@example.com",
            "password": "testpass"
        })
        token = login_resp.get_json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        print("Sending authenticated GET request to /tickets...")
        response = client.get('/tickets', headers=headers)
        print(f"Response status code: {response.status_code}")
        print(f"Response data: {response.data}")
        assert response.status_code == 200
        assert b'tickets' in response.data
        print("Test passed.")
    except Exception as e:
        print(f"An error occurred: {e}")
        pytest.fail(str(e))
    finally:
        print("Test finished.")
