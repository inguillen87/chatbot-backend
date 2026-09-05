import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import services.google_auth as gauth

class GoogleLoginTests(unittest.TestCase):
    def test_google_auth_seam_is_patchable_without_loading_optional_sdk(self):
        token_info = {
            'aud': 'test-client-id',
            'email': 'patched@example.com',
            'email_verified': True,
        }

        with patch.object(
            gauth,
            '_load_google_auth_module',
            side_effect=ImportError('google-auth missing'),
        ) as mock_loader:
            with patch.object(
                gauth.id_token,
                'verify_oauth2_token',
                return_value=token_info,
            ) as mock_verify:
                request = gauth.google_requests.Request()
                result = gauth.id_token.verify_oauth2_token('idtoken', request)

        self.assertEqual(result, token_info)
        mock_verify.assert_called_once_with('idtoken', request)
        mock_loader.assert_not_called()

    def test_missing_google_auth_preserves_public_login_error(self):
        with patch.object(gauth, 'ALLOWED_CLIENT_IDS', ['test-client-id']):
            with patch.object(
                gauth,
                '_load_google_auth_module',
                side_effect=ImportError('google-auth missing'),
            ):
                with self.assertRaisesRegex(ValueError, 'Token de Google inválido'):
                    gauth.login_o_crear_usuario('idtoken')

    def test_missing_audience_configuration_fails_before_token_verification(self):
        with patch.object(gauth, 'ALLOWED_CLIENT_IDS', []), patch.object(
            gauth.id_token,
            'verify_oauth2_token',
        ) as mock_verify:
            with self.assertRaisesRegex(ValueError, 'Google OAuth no configurado'):
                gauth.login_o_crear_usuario('idtoken')
        mock_verify.assert_not_called()

    @patch.object(gauth, 'ALLOWED_CLIENT_IDS', ['test-client-id'])
    @patch.object(gauth.id_token, 'verify_oauth2_token')
    def test_rejects_unverified_google_email(self, mock_verify):
        mock_verify.return_value = {
            'aud': 'test-client-id',
            'email': 'unverified@example.com',
            'email_verified': False,
        }

        with self.assertRaisesRegex(ValueError, 'email de Google no verificado'):
            gauth.login_o_crear_usuario('idtoken')

    @patch.object(gauth, 'ALLOWED_CLIENT_IDS', ['test-client-id'])
    @patch.object(gauth.id_token, 'verify_oauth2_token')
    def test_rejects_token_for_another_google_client(self, mock_verify):
        mock_verify.return_value = {
            'aud': 'another-client-id',
            'email': 'verified@example.com',
            'email_verified': True,
        }

        with self.assertRaisesRegex(ValueError, 'audiencia no autorizada'):
            gauth.login_o_crear_usuario('idtoken')

    @patch.object(gauth, 'ALLOWED_CLIENT_IDS', ['test-client-id'])
    @patch.object(gauth.id_token, 'verify_oauth2_token')
    def test_crea_usuario_nuevo(self, mock_verify):
        mock_verify.return_value = {
            'aud': 'test-client-id',
            'email': 'new@example.com',
            'name': 'Nuevo Usuario',
            'email_verified': True,
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
        self.assertEqual(kwargs.get('rol'), 'usuario')
        self.assertEqual(kwargs.get('tipo_chat'), 'municipio')
        self.assertEqual(kwargs.get('name'), 'Nuevo Usuario')

    @patch.object(gauth, 'ALLOWED_CLIENT_IDS', ['test-client-id'])
    @patch.object(gauth.id_token, 'verify_oauth2_token')
    def test_usa_usuario_existente(self, mock_verify):
        mock_verify.return_value = {
            'aud': 'test-client-id',
            'email': 'exist@example.com',
            'name': 'Usuario Existente',
            'email_verified': True,
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
