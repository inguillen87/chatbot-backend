import pytest
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
