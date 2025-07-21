import unittest
from app import create_app, db

if __name__ == '__main__':
    # Create a Flask app instance for the tests
    app = create_app()
    app.config.update({
        "TESTING": True,
    })

    # Discover and run tests
    with app.app_context():
        loader = unittest.TestLoader()
        suite = loader.discover('tests')
        runner = unittest.TextTestRunner()
        runner.run(suite)
