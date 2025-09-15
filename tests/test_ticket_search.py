import unittest
from app import create_app, db
from config import Config
from models import User, MunicipioTicket
from datetime import datetime
from routes.ticket import get_tickets_del_usuario_logic

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

class TicketSearchTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.admin = User(email='admin@test.com', name='Admin', rol='admin', municipio_id=5, tipo_chat='municipio')
        self.admin.set_password('password')
        self.neighbor = User(email='vecino@test.com', name='Juan Gomez')
        self.neighbor.set_password('password')
        db.session.add_all([self.admin, self.neighbor])
        db.session.commit()

        t1 = MunicipioTicket(id=1, nro_ticket='100', estado='nuevo', fecha=datetime.now(),
                             categoria='luminaria rota', municipio_id=5, user_id=self.neighbor.id,
                             nombre_vecino='Carlos Perez', dni_vecino='12345678')
        t2 = MunicipioTicket(id=2, nro_ticket='101', estado='resuelto', fecha=datetime.now(),
                             categoria='bache', municipio_id=5, user_id=self.neighbor.id,
                             nombre_vecino='Juan Gomez')
        db.session.add_all([t1, t2])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_search_by_name(self):
        with self.app.test_request_context('?q=Gomez'):
            response = get_tickets_del_usuario_logic(self.admin)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 2)

    def test_search_by_estado(self):
        with self.app.test_request_context('?q=resuelto'):
            response = get_tickets_del_usuario_logic(self.admin)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['estado'], 'resuelto')

    def test_search_by_nombre_vecino(self):
        with self.app.test_request_context('?q=Perez'):
            response = get_tickets_del_usuario_logic(self.admin)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['nombre_usuario'], 'Carlos Perez')

    def test_filter_luminaria_category(self):
        with self.app.test_request_context('?categoria=Luminarias'):
            response = get_tickets_del_usuario_logic(self.admin)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(len(data['tickets']), 1)
            self.assertEqual(data['tickets'][0]['categoria'], 'Luminarias')

    def test_normalize_category_helper(self):
        from utils.ticket_utils import normalize_category
        self.assertEqual(normalize_category('luminaria sin luz'), 'Luminarias')
        self.assertEqual(normalize_category('pozo en la calle'), 'Arreglo De Calle')
        self.assertEqual(normalize_category('poste sin luz'), 'Luminarias')
        self.assertEqual(normalize_category('arbol del vecino'), 'Arbol Caido')

    def test_dni_serialized(self):
        from routes.ticket import serialize_ticket_to_json
        ticket = MunicipioTicket.query.get(1)
        data = serialize_ticket_to_json(ticket, 'municipio')
        self.assertEqual(data['dni'], '12345678')

    def test_dynamic_keyword_cache(self):
        from services.herramientas_municipio import recargar_cache_keywords_para_tests
        from utils.ticket_utils import normalize_category

        nuevo = MunicipioTicket(id=3, nro_ticket='102', estado='nuevo', fecha=datetime.now(),
                                 categoria='luminaria', municipio_id=5, user_id=self.neighbor.id,
                                 nombre_vecino='Ana Lopez', detalles='alumbrado publico apagado')
        db.session.add(nuevo)
        db.session.commit()

        recargar_cache_keywords_para_tests()
        self.assertEqual(normalize_category('alumbrado'), 'Luminarias')

if __name__ == '__main__':
    unittest.main()
