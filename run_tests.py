import unittest
import pytest
from app import create_app, db

if __name__ == '__main__':
    # Create a Flask app instance for the tests
    app = create_app()
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"
    })

    # Discover and run tests
    with app.app_context():
        db.create_all()
        pytest.main(['-v', 'tests'])
