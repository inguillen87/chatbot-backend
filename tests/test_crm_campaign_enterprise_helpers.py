import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from models_memory import Contact, InteractionEvent
from routes.crm.routes import (
    _campaign_sends_last_days,
    _parse_scheduled_for,
    _save_tenant_templates,
    _tenant_templates,
)


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class CRMCampaignEnterpriseHelpersTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.owner = User(
            name="Owner",
            email="owner@example.com",
            password_hash="hash",
            rol="admin",
            tipo_chat="pyme",
        )
        db.session.add(self.owner)
        db.session.commit()

        self.tenant = TenantProfile(
            slug="owner-tenant",
            nombre="Owner Tenant",
            tipo="pyme",
            pyme_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.commit()

        self.contact = Contact(
            id="contact-1",
            tenant_id=self.tenant.id,
            name="Cliente",
            phone="+549111111111",
            email="cliente@example.com",
            preferences={},
        )
        db.session.add(self.contact)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_parse_schedule_datetime_with_timezone(self):
        parsed = _parse_scheduled_for("2026-02-13T10:00:00", "America/Argentina/Buenos_Aires")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_parse_schedule_datetime_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            _parse_scheduled_for("not-a-date", "UTC")

    def test_template_roundtrip(self):
        payload = [{"slug": "winback", "name": "Winback", "message": "Hola", "version": 1}]
        _save_tenant_templates(self.tenant, payload)
        db.session.commit()
        loaded = _tenant_templates(self.tenant)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["slug"], "winback")

    def test_campaign_send_count_last_week(self):
        old_evt = InteractionEvent(
            tenant_id=self.tenant.id,
            contact_id=self.contact.id,
            channel="whatsapp",
            direction="outbound",
            content="old",
            metadata_payload={"event_type": "campaign_send"},
            created_at=datetime.now(timezone.utc) - timedelta(days=15),
        )
        new_evt = InteractionEvent(
            tenant_id=self.tenant.id,
            contact_id=self.contact.id,
            channel="whatsapp",
            direction="outbound",
            content="new",
            metadata_payload={"event_type": "campaign_send"},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.session.add(old_evt)
        db.session.add(new_evt)
        db.session.commit()

        count = _campaign_sends_last_days(self.tenant.id, self.contact.id, days=7)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
