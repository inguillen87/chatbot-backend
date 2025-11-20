import os

import eventlet
eventlet.monkey_patch()
import pytest

# Evita que app.py cree una instancia global conectada a Postgres durante las pruebas.
os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import TestingConfig
@pytest.fixture(scope='session')
def app():
    """Create a new app instance for each test session."""
    app = create_app(TestingConfig)
    return app

@pytest.fixture(scope='function')
def client(app):
    """A test client for the app."""
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
            yield client
            db.session.remove()
            db.drop_all()

@pytest.fixture(scope='function')
def init_database(client):
    """Fixture to set up the database and create some initial users."""
    from models import User, Rubro

    # Create a test rubro
    rubro = Rubro(id=1, clave="municipio", nombre="Municipalidad")
    db.session.add(rubro)

    # Create an owner user (admin)
    owner_user = User(id=1, name="Admin User", email="admin@test.com", rol="admin", municipio_id=1, rubro_id=1, tipo_chat="municipio")
    owner_user.set_password("admin")
    db.session.add(owner_user)

    # Create a viewer user (citizen)
    viewer_user = User(id=2, name="Test Viewer", email="viewer@test.com", rol="usuario")
    viewer_user.set_password("viewer")
    db.session.add(viewer_user)

    db.session.commit()

    yield db

@pytest.fixture
def owner_user(init_database):
    from models import User
    return User.query.get(1)

@pytest.fixture
def viewer_user(init_database):
    from models import User
    return User.query.get(2)
