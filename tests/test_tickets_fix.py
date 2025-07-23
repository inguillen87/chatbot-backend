import pytest
from app import create_app
from extensions import db
from models import User

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

def test_get_tickets_unauthenticated(client):
    print("Starting test_get_tickets_unauthenticated...")
    try:
        # Ensure the test user exists
        with client.application.app_context():
            print("Checking for test user...")
            test_user = User.query.filter_by(email="test@example.com").first()
            if not test_user:
                print("Test user not found, creating...")
                test_user = User(email="test@example.com", nombre_empresa="Test", rubro_id=1, tipo_chat="pyme", rol="admin")
                db.session.add(test_user)
                db.session.commit()
                print("Test user created.")
            else:
                print("Test user found.")

        print("Sending GET request to /tickets...")
        response = client.get('/tickets')
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
