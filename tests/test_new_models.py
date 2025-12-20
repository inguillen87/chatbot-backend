import unittest
from app import app
from models import db, IntegrationAccount, NotificationLog, PublicSurvey, TenantProfile, User

class NewModelsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Create user first
        self.user = User(name='Test User', email='test@example.com', password_hash='xxx')
        db.session.add(self.user)
        db.session.flush()

        # Create tenant with pyme_id
        self.tenant = TenantProfile(
            slug='test-tenant',
            nombre='Test Tenant',
            tipo='pyme',
            pyme_id=self.user.id
        )
        db.session.add(self.tenant)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_integration_account_creation(self):
        acc = IntegrationAccount(tenant_id=self.tenant.id, type='MercadoLibre', credentials={'token': '123'})
        db.session.add(acc)
        db.session.commit()
        retrieved = IntegrationAccount.query.first()
        self.assertEqual(retrieved.type, 'MercadoLibre')
        self.assertEqual(retrieved.tenant_id, self.tenant.id)

    def test_notification_log_creation(self):
        log = NotificationLog(tenant_id=self.tenant.id, channel='email', recipient='test@test.com')
        db.session.add(log)
        db.session.commit()
        retrieved = NotificationLog.query.first()
        self.assertEqual(retrieved.channel, 'email')
        self.assertEqual(retrieved.recipient, 'test@test.com')

    def test_public_survey_tenant_id(self):
        survey = PublicSurvey(slug='test-survey', titulo='Test Survey', tenant_id=self.tenant.id)
        db.session.add(survey)
        db.session.commit()
        retrieved = PublicSurvey.query.filter_by(slug='test-survey').first()
        self.assertEqual(retrieved.tenant_id, self.tenant.id)

if __name__ == '__main__':
    unittest.main()
