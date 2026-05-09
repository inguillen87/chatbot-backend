import os
import sys

# Keep eventlet out of the pytest bootstrap. The app disables eventlet in
# TESTING mode, and patching os/file APIs here breaks Flask-Session filesystem
# writes on Windows before endpoint logic can run.
os.environ.setdefault("EVENTLET_NO_GREENDNS", "YES")
os.environ.setdefault("TESTING", "1")

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

    # Reuse seeded data when available to avoid primary-key collisions.
    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(clave="municipio", nombre="Municipalidad")
        db.session.add(rubro)
        db.session.flush()

    # Create or reuse an owner user (admin)
    owner_user = User.query.filter_by(email="admin@test.com").first()
    if not owner_user:
        owner_user = User(
            name="Admin User",
            email="admin@test.com",
            rol="admin",
            municipio_id=1,
            rubro_id=rubro.id,
            tipo_chat="municipio",
        )
        owner_user.set_password("admin")
        db.session.add(owner_user)

    # Create or reuse a viewer user (citizen)
    viewer_user = User.query.filter_by(email="viewer@test.com").first()
    if not viewer_user:
        viewer_user = User(name="Test Viewer", email="viewer@test.com", rol="usuario")
        viewer_user.set_password("viewer")
        db.session.add(viewer_user)

    db.session.commit()

    yield db

@pytest.fixture
def owner_user(init_database):
    from models import User
    return User.query.filter_by(email="admin@test.com").first()

@pytest.fixture
def viewer_user(init_database):
    from models import User
    return User.query.filter_by(email="viewer@test.com").first()
