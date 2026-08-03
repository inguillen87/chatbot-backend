import unittest
from app import create_app, db
from config import TestConfig
from models import MunicipioTicket, TenantProfile, User
from services.municipio_metricas_service import MunicipioMetricasService


class MunicipioMetricasServiceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        owner = User(
            name='Municipio Métricas Owner',
            email='metricas-service-owner@example.com',
            rol='admin',
            tipo_chat='municipio',
        )
        owner.set_password('test')
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug='municipio-metricas-service',
            nombre='Municipio Métricas Service',
            tipo='municipio',
            municipio_id=owner.id,
            is_active=True,
        )
        db.session.add(tenant)
        db.session.flush()
        owner.municipio_id = owner.id
        owner.tenant_id = tenant.id
        owner.tenant_slug = tenant.slug

        # Create sample tickets in the authoritative tenant scope.
        t1 = MunicipioTicket(
            municipio_id=owner.id,
            tenant_id=tenant.id,
            estado='nuevo',
            user_id=1,
        )
        t2 = MunicipioTicket(
            municipio_id=owner.id,
            tenant_id=tenant.id,
            estado='cerrado',
            user_id=1,
        )
        t3 = MunicipioTicket(
            municipio_id=owner.id,
            tenant_id=tenant.id,
            estado='cerrado',
            user_id=2,
        )
        db.session.add_all([t1, t2, t3])
        db.session.commit()
        self.owner = owner
        self.tenant = tenant

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_metrics_calculation(self):
        service = MunicipioMetricasService(
            municipio_id=self.owner.id,
            tenant_id=self.tenant.id,
        )
        self.assertEqual(service.get_total_tickets(), 3)
        self.assertEqual(service.get_open_tickets(), 1)
        self.assertEqual(service.get_closed_tickets(), 2)
        self.assertEqual(service.get_unique_citizens(), 2)
        self.assertEqual(service.get_resolution_rate(), 0.67)


if __name__ == '__main__':
    unittest.main()
