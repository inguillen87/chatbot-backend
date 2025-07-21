import sys
import os
import unittest
from unittest.mock import patch
from app import create_app, db
from models import User

class AuthRoutesTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_login_no_json(self):
        """
        Tests that the login route returns a 400 error if the request is not JSON.
        """
        response = self.client.post('/login', data="not a json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "La solicitud debe ser de tipo JSON."})

    def test_login_missing_credentials(self):
        """
        Tests that the login route returns a 400 error if the email or password are not provided.
        """
        response = self.client.post('/login', json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "Email y contraseña requeridos."})

    def test_login_invalid_credentials(self):
        """
        Tests that the login route returns a 401 error if the credentials are invalid.
        """
        response = self.client.post('/login', json={"email": "a@b.com", "password": "c"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Email o contraseña incorrectos."})

    def test_login_successful(self):
        """
        Tests that the login route returns a 200 status code and a token if the credentials are valid.
        """
        with self.app.app_context():
            user = User(email="a@b.com", name="Test User", token="test-token")
            user.set_password("c")
            db.session.add(user)
            db.session.commit()

        response = self.client.post('/login', json={"email": "a@b.com", "password": "c"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.get_json())

if __name__ == '__main__':
    unittest.main()
