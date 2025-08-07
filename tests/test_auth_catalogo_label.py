import unittest
from types import SimpleNamespace
from unittest.mock import patch
import sys
import os

# Añadir el directorio raíz al path para importar módulos de la app
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app import create_app, db
from models import User, Rubro
from routes.legacy_auth import get_current_user
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class CatalogoLabelTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _base_user(self):
        user = User(
            id=1,
            email='a@a',
            name='A',
            token='tok',
            rubro=None,
            nombre_empresa='E',
            rol='admin',
            empresa_id=None,
            telefono=None,
            direccion=None,
            ciudad=None,
            provincia=None,
            pais=None,
            latitud=None,
            longitud=None,
            link_web=None,
            plan='free',
            preguntas_usadas=0,
            horario="{}",
            logo_url='',
            ticket_categorias=''
        )
        user.set_password('password')
        return user

    def test_pyme_label(self):
        user = self._base_user()
        user.token = 'test-token-pyme'
        db.session.add(user)
        db.session.commit()
        with self.app.test_request_context():
            with patch('utils.plan_limits.limite_para_usuario', lambda u: 5):
                with patch('models.User.query') as mock_query:
                    mock_query.filter_by.return_value.first.return_value = user
                    resp = self.client.get('/perfil', headers={'Authorization': f'Bearer {user.token}'})
                    data = resp.get_json()
                    self.assertEqual(data['catalogo_label'], 'Cargar Catálogo de Productos')
                    self.assertEqual(data['tipo_chat'], 'pyme')

    def test_municipio_label(self):
        user = self._base_user()
        user.token = 'test-token-muni'
        user.rubro = Rubro(nombre='municipio', clave='municipio')
        db.session.add(user)
        db.session.commit()
        with self.app.test_request_context():
            with patch('utils.plan_limits.limite_para_usuario', lambda u: 5):
                with patch('models.User.query') as mock_query:
                    mock_query.filter_by.return_value.first.return_value = user
                    resp = self.client.get('/perfil', headers={'Authorization': f'Bearer {user.token}'})
                    data = resp.get_json()
                    self.assertEqual(data['catalogo_label'], 'Cargar Catálogo de Trámites')
                    self.assertEqual(data['tipo_chat'], 'municipio')

if __name__ == '__main__':
    unittest.main()
