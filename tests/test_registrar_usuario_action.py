import unittest
from unittest.mock import patch, MagicMock
import pytest
from services.actions.general_actions import RegistrarUsuarioActionHandler
from models import User, db
from app import create_app

@pytest.mark.legacy
class TestRegistrarUsuarioAction(unittest.TestCase):

    def setUp(self):
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.actions.general_actions.db.session.add')
    @patch('services.actions.general_actions.db.session.commit')
    def test_execute_success(self, mock_commit, mock_add):
        action = RegistrarUsuarioActionHandler({})
        datos = {
            "nombre": "Test User",
            "email": "test@example.com",
            "telefono": "1234567890",
            "password": "testpassword"
        }
        resultado = action.execute(datos)
        self.assertTrue(resultado['success'])
        self.assertIn('usuario_id', resultado['data'])
        mock_add.assert_called_once()
        mock_commit.assert_called_once()

    def test_execute_missing_data(self):
        action = RegistrarUsuarioActionHandler({})
        datos = {"nombre": "Test User"}
        resultado = action.execute(datos)
        self.assertFalse(resultado['success'])
        self.assertIn('message_to_user', resultado)

if __name__ == '__main__':
    unittest.main()
