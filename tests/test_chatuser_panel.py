from flask import Flask, jsonify
from app import create_app, db
from models import User
from config import Config
from routes.auth import chatuser_register_panel, chatuser_login_panel
import unittest
from unittest.mock import patch, MagicMock

class ChatUserPanelTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app(Config)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Mock owner user
        self.owner_user = User(
            id=1,
            name="Test Owner",
            email="owner@test.com",
            token="owner-token",
            nombre_empresa="TestCo",
            rubro_id=1,
            tipo_chat="pyme"
        )
        self.owner_user.set_password("ownerpass")
        db.session.add(self.owner_user)

        # Mock existing user
        self.existing_user = User(
            id=2,
            name="Existing User",
            email="existing@test.com",
            token="existing-user-token",
            empresa_id=1
        )
        self.existing_user.set_password("userpass")
        db.session.add(self.existing_user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_register_new_user(self):
        with patch('routes.auth.get_or_create_pyme_user_by_token', return_value=self.owner_user):
            response = self.client.post('/auth/chatuserregisterpanel', json={
                "name": "New User",
                "email": "new@test.com",
                "password": "newpassword",
                "empresa_token": "owner-token"
            })
            self.assertEqual(response.status_code, 201)
            data = response.get_json()
            self.assertIn("id", data)
            self.assertEqual(data["name"], "New User")

    def test_register_existing_user_same_entity(self):
        with patch('routes.auth.get_or_create_pyme_user_by_token', return_value=self.owner_user):
            response = self.client.post('/auth/chatuserregisterpanel', json={
                "name": "Existing User",
                "email": "existing@test.com",
                "password": "userpass",
                "empresa_token": "owner-token"
            })
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data["already_registered"])

    def test_register_existing_user_different_entity(self):
        other_owner = User(id=3, name="Other Owner", email="other@owner.com", token="other-owner-token")
        db.session.add(other_owner)
        db.session.commit()
        with patch('routes.auth.get_or_create_pyme_user_by_token', return_value=other_owner):
            response = self.client.post('/auth/chatuserregisterpanel', json={
                "name": "Existing User",
                "email": "existing@test.com",
                "password": "userpass",
                "empresa_token": "other-owner-token"
            })
            self.assertEqual(response.status_code, 409)

    def test_login_user(self):
        with patch('routes.auth.User.query') as mock_query:
            mock_query.filter_by.return_value.first.return_value = self.existing_user
            response = self.client.post('/auth/chatuserloginpanel', json={
                "email": "existing@test.com",
                "password": "userpass",
                "empresa_token": "owner-token"
            })
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(data["id"], self.existing_user.id)

if __name__ == '__main__':
    unittest.main()
