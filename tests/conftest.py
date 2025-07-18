import sys
import os
import pytest



from app import create_app, db as _db

@pytest.fixture(scope='session')
def app():
    """Create a new app instance for each test session."""
    app = create_app('testing')
    with app.app_context():
        yield app

@pytest.fixture(scope='function')
def db(app):
    """Create a new database for each function."""
    _db.app = app
    _db.create_all()
    yield _db
    _db.session.remove()
    _db.drop_all()

@pytest.fixture(scope='function')
def client(app, db):
    """Create a new test client for each function."""
    with app.test_client() as client:
        yield client
