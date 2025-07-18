import unittest
from unittest.mock import patch
from app import create_app, db
from models import QA, CatalogoItem, User
from types import SimpleNamespace

class CatalogoEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.from_object('config.TestConfig')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_faq_texto_returns_clean_texts(self):
        with self.app.app_context():
            user = User(name='testuser', email='test@example.com', password_hash='password', rubro_id=1)
            db.session.add(user)
            db.session.commit()
            faq1 = QA(question='Q1', answer='A1', rubro_id=1)
            faq2 = QA(question='Q2', answer='A2', rubro_id=1)
            db.session.add_all([faq1, faq2])
            db.session.commit()

            with patch('routes.catalogo.limpiar_texto_base', lambda t: t.lower()):
                with self.client as client:
                    with client.session_transaction() as sess:
                        sess['user_id'] = user.id
                    response = client.get('/faq_texto')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json, ['q1 a1', 'q2 a2'])

    def test_textos_perfil_returns_clean_texts(self):
        with self.app.app_context():
            user = User(name='testuser', email='test@example.com', password_hash='password')
            db.session.add(user)
            db.session.commit()
            item1 = CatalogoItem(texto='Uno', user_id=user.id, nombre='item1')
            item2 = CatalogoItem(texto='Dos', user_id=user.id, nombre='item2')
            db.session.add_all([item1, item2])
            db.session.commit()

            with patch('routes.catalogo.limpiar_texto_base', lambda t: t.lower()):
                with self.client as client:
                    with client.session_transaction() as sess:
                        sess['user_id'] = user.id
                    response = client.get('/textos_perfil')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json, ['uno', 'dos'])

    def test_resumen_catalogo_counts_by_category(self):
        with self.app.app_context():
            user = User(name='testuser', email='test@example.com', password_hash='password')
            db.session.add(user)
            db.session.commit()
            item1 = CatalogoItem(categoria='vino', user_id=user.id, nombre='item1')
            item2 = CatalogoItem(categoria='vino', user_id=user.id, nombre='item2')
            item3 = CatalogoItem(categoria='cerveza', user_id=user.id, nombre='item3')
            db.session.add_all([item1, item2, item3])
            db.session.commit()

            with self.client as client:
                with client.session_transaction() as sess:
                    sess['user_id'] = user.id
                response = client.get('/resumen_catalogo')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['total'], 3)
                self.assertIn({'nombre': 'vino', 'cantidad': 2}, response.json['categorias'])
                self.assertIn({'nombre': 'cerveza', 'cantidad': 1}, response.json['categorias'])

if __name__ == '__main__':
    unittest.main()
