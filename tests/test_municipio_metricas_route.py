import unittest
from app import create_app, db
from config import TestConfig
from models import User, MunicipioTicket


class MunicipioMetricasRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        user = User(name='Admin', email='admin@municipio.com', municipio_id=1)
        user.set_password('secret')
        db.session.add(user)
        db.session.commit()
        self.user = user

        t1 = MunicipioTicket(municipio_id=1, estado='nuevo', user_id=1)
        t2 = MunicipioTicket(municipio_id=1, estado='cerrado', user_id=1)
        t3 = MunicipioTicket(municipio_id=1, estado='cerrado', user_id=2)
        db.session.add_all([t1, t2, t3])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _login(self):
        return self.client.post(
            '/auth/login',
            json={'email': 'admin@municipio.com', 'password': 'secret'}
        )

    def test_summary_endpoint(self):
        resp = self._login()
        self.assertEqual(resp.status_code, 200)
        token = resp.json['token']

        resp = self.client.get(
            '/api/municipal/metricas/summary',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json['total_tickets'], 3)
        self.assertEqual(resp.json['open_tickets'], 1)
        self.assertEqual(resp.json['closed_tickets'], 2)
        self.assertEqual(resp.json['unique_citizens'], 2)
        self.assertEqual(resp.json['resolution_rate'], 0.67)


if __name__ == '__main__':
    unittest.main()
