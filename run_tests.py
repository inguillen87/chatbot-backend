import pytest
from app import create_app, db

def main():
    """
    Creates a Flask app instance for the tests, creates all the database tables,
    and runs the tests.
    """
    app = create_app()
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"
    })

    with app.app_context():
        db.create_all()
        result = pytest.main(['-v', 'tests'])

    # Exit with the same code as pytest
    exit(result)

if __name__ == '__main__':
    main()
