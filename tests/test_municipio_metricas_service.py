import unittest
from app import create_app, db
from config import TestConfig
from models import MunicipioTicket
from services.municipio_metricas_service import MunicipioMetricasService


class MunicipioMetricasServiceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        # Create users and a municipality "user" to satisfy foreign key constraints
        from models import User
        muni_user = User(id=1, name="Test Muni", email="muni@test.com", password_hash="a", tipo_chat='municipio')
        user1 = User(id=2, name="Test User 1", email="u1@test.com", password_hash="a")
        user2 = User(id=3, name="Test User 2", email="u2@test.com", password_hash="a")
        db.session.add_all([muni_user, user1, user2])
        db.session.commit()


        # Create sample tickets
        t1 = MunicipioTicket(municipio_id=1, estado='nuevo', user_id=2)
        t2 = MunicipioTicket(municipio_id=1, estado='cerrado', user_id=1)
        t3 = MunicipioTicket(municipio_id=1, estado='cerrado', user_id=2)
        db.session.add_all([t1, t2, t3])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_metrics_calculation(self):
        service = MunicipioMetricasService(municipio_id=1)
        self.assertEqual(service.get_total_tickets(), 3)
        self.assertEqual(service.get_open_tickets(), 1)
        self.assertEqual(service.get_closed_tickets(), 2)
        self.assertEqual(service.get_unique_citizens(), 2)
        self.assertEqual(service.get_resolution_rate(), 0.67)


if __name__ == '__main__':
    unittest.main()
