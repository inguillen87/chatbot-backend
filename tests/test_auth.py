import unittest
from app import create_app, db
from models import User
from config import Config

class AuthRoutesTests(unittest.TestCase):
    def setUp(self):
        """Set up the test client and initialize the database."""
        self.app = create_app(Config)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Crear un usuario de prueba
        user = User(email='test@test.com', name='Test User', token='test-token')
        user.set_password('testpassword')
        db.session.add(user)
        db.session.commit()

    def tearDown(self):
        """Tear down all initialized variables."""
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_login_successful(self):
        """Tests that the login route returns a 200 status code and a token if the credentials are valid."""
        response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'testpassword'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('token', response.get_json())

    def test_login_invalid_credentials(self):
        """Tests that the login route returns a 401 error if the credentials are invalid."""
        response = self.client.post('/auth/login', json={'email': 'test@test.com', 'password': 'wrongpassword'})
        self.assertEqual(response.status_code, 401)

    def test_login_missing_credentials(self):
        """Tests that the login route returns a 400 error if the email or password are not provided."""
        response = self.client.post('/auth/login', json={'email': 'test@test.com'})
        self.assertEqual(response.status_code, 400)

    def test_login_no_json(self):
        """Tests that the login route returns a 400 error if the request is not JSON."""
        response = self.client.post('/auth/login', data={'email': 'test@test.com', 'password': 'testpassword'})
        self.assertEqual(response.status_code, 400)

if __name__ == '__main__':
    unittest.main()
