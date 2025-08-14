import unittest
from unittest.mock import patch
from app import create_app, db
from models import CatalogoItem, User, QA, Rubro
from config import Config

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

from routes.catalogo import catalogo_bp

class CatalogoEndpointsTests(unittest.TestCase):
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

    def test_resumen_catalogo_counts_by_category(self):
        with self.app.app_context():
            # Create a user with a valid rubro and password
            rubro = Rubro(nombre="pymes", clave="pymes")
            db.session.add(rubro)
            db.session.commit()
            user = User(name='testuser', email='test@example.com', rubro_id=rubro.id)
            user.set_password('password123')
            db.session.add(user)
            db.session.commit()

            # Create catalog items associated with the user
            item1 = CatalogoItem(categoria='vino', user_id=user.id, nombre='item1')
            item2 = CatalogoItem(categoria='vino', user_id=user.id, nombre='item2')
            item3 = CatalogoItem(categoria='cerveza', user_id=user.id, nombre='item3')
            db.session.add_all([item1, item2, item3])
            db.session.commit()

            # Login to get a token
            login_resp = self.client.post('/login', json={
                'email': 'test@example.com',
                'password': 'password123'
            })
            self.assertEqual(login_resp.status_code, 200)
            token = login_resp.get_json()['token']
            headers = {'Authorization': f'Bearer {token}'}

            # Make the request with the token
            response = self.client.get('/catalogo/resumen_catalogo', headers=headers)
            self.assertEqual(response.status_code, 200)

            # Assertions
            response_json = response.get_json()
            self.assertEqual(response_json['total'], 3)
            # Sort for predictable comparison
            categorias = sorted(response_json['categorias'], key=lambda x: x['nombre'])
            self.assertEqual(categorias[0], {'nombre': 'cerveza', 'cantidad': 1})
            self.assertEqual(categorias[1], {'nombre': 'vino', 'cantidad': 2})

if __name__ == '__main__':
    unittest.main()
