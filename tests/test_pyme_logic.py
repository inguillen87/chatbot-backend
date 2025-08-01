import unittest
from unittest.mock import patch
from app import create_app, db
from models import User, PymeTicket, TicketComentario, Rubro
from types import SimpleNamespace
from services import pymes

class PymeLogicTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.from_object('config.TestConfig')
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.setup_database()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def setup_database(self):
        self.rubro = Rubro(nombre='vinoteca', clave='vinoteca')
        self.user = User(name='Bodega Ejemplo', email='a@b.com', password_hash='password', rubro=self.rubro, plan='full', limite_preguntas=100)
        db.session.add(self.rubro)
        db.session.add(self.user)
        db.session.commit()


if __name__ == '__main__':
    unittest.main()
