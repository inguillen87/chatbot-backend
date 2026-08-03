import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import services.google_auth as gauth

class GoogleLoginTests(unittest.TestCase):
    @patch.object(gauth, 'ALLOWED_CLIENT_IDS', ['test-client-id'])
    @patch.object(gauth.id_token, 'verify_oauth2_token')
    def test_crea_usuario_nuevo(self, mock_verify):
        mock_verify.return_value = {
            'aud': 'test-client-id',
            'email': 'new@example.com',
            'name': 'Nuevo Usuario'
        }

        dummy_user = SimpleNamespace(id=1, email='new@example.com', name='Nuevo', token='tok')
        query = MagicMock()
        query.first.return_value = None
        UserMock = MagicMock()
        UserMock.query.filter_by.return_value = query
        UserMock.return_value = dummy_user
        dummy_user.set_password = lambda *a, **k: None

        session = MagicMock()
        with patch.object(gauth, 'User', UserMock), patch.object(gauth, 'db', SimpleNamespace(session=session)):
            user = gauth.login_o_crear_usuario('idtoken', rol='admin', tipo_chat='municipio')

        self.assertEqual(user, dummy_user)
        session.add.assert_called_once_with(dummy_user)
        session.commit.assert_called_once()
        UserMock.assert_called_once()
        args, kwargs = UserMock.call_args
        self.assertEqual(kwargs.get('rol'), 'admin')
        self.assertEqual(kwargs.get('tipo_chat'), 'municipio')
        self.assertEqual(kwargs.get('name'), 'Nuevo Usuario')

    @patch.object(gauth, 'ALLOWED_CLIENT_IDS', ['test-client-id'])
    @patch.object(gauth.id_token, 'verify_oauth2_token')
    def test_usa_usuario_existente(self, mock_verify):
        mock_verify.return_value = {
            'aud': 'test-client-id',
            'email': 'exist@example.com',
            'name': 'Usuario Existente'
        }

        existing = SimpleNamespace(id=2, email='exist@example.com', name='Exist', token='tok2')
        query = MagicMock()
        query.first.return_value = existing
        UserMock = MagicMock()
        UserMock.query.filter_by.return_value = query

        session = MagicMock()
        with patch.object(gauth, 'User', UserMock), patch.object(gauth, 'db', SimpleNamespace(session=session)):
            user = gauth.login_o_crear_usuario('idtoken', rol='admin', tipo_chat='pyme')

        self.assertEqual(user, existing)
        session.add.assert_not_called()
        session.commit.assert_not_called()



if __name__ == '__main__':
    unittest.main()
