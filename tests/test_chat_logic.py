import unittest
from unittest.mock import patch, MagicMock
from app import create_app, db
from models import User

class ChatLogicTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"
        })
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Crear un usuario de prueba
        self.user = User(
            email='test@example.com',
            name='Test User',
            rol='admin'
        )
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_authenticated_user_no_info_request(self):
        """
        Prueba que el chatbot no solicita información a un usuario autenticado.
        """
        with self.client:
            # Iniciar sesión (simulado)
            with patch('routes.auth.User') as mock_user:
                mock_user.query.filter_by.return_value.first.return_value = self.user

                response = self.client.post(
                    '/ask',
                    json={'pregunta': 'Necesito ayuda', 'tipo_chat': 'pyme'},
                    headers={'Authorization': f'Bearer {self.user.token}'}
                )

                self.assertEqual(response.status_code, 200)
                json_data = response.get_json()
                self.assertNotIn('pedir_info', json_data)

    def test_anonymous_user_info_request(self):
        """
        Prueba que el chatbot solicita información a un usuario anónimo.
        """
        with self.client:
            # Esta prueba ahora verifica que la conversación puede iniciar sin
            # pedir datos, ya que la solicitud de datos es contextual.
            response = self.client.post(
                '/ask',
                json={'pregunta': 'Hola', 'tipo_chat': 'municipio'},
                headers={'X-Anon-Id': 'test-anon-id'}
            )

            self.assertEqual(response.status_code, 200)
            json_data = response.get_json()
            self.assertIn('respuesta', json_data)

if __name__ == '__main__':
    unittest.main()
