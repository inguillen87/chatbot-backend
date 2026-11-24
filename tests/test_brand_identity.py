"""Brand identity contract: strict validation and a superadmin-only publish path."""
import tempfile
import unittest
from unittest.mock import patch

import pytest

from services.brand_identity import BrandIdentityError, normalize

IDENTITY = {
    "palette": ["#FD8D00", "#F75318", "#0ABB03", "#1DBFFE", "#353535"],
    "gradient_stops": [{"color": "#FD8D00", "x": 0.18, "y": 0.18}, {"color": "#1dbffe", "x": 0.15, "y": 0.88}],
    "text_color": "#353535",
    "typography": {"family": "Passenger Sans", "fallback": "\"Archivo\", system-ui, sans-serif",
                   "stylesheet_url": "https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700&display=swap",
                   "letter_spacing": "-0.024em"},
    "logo": {"on_light": "https://api.chatboc.ar/static/municipios/demo/logo-oscuro.png",
             "mark": "https://api.chatboc.ar/static/municipios/demo/marca.png", "alt": "Organización de prueba"},
    "bubble_style": "globa",
    "avatar": {"style": "brand_bot"},
}


def test_normalize_keeps_the_contract_and_uppercases_colors():
    value = normalize(IDENTITY)
    assert value["gradient_stops"][1] == {"color": "#1DBFFE", "x": 0.15, "y": 0.88}
    assert value["typography"]["family"] == "Passenger Sans"
    assert value["avatar"] == {"style": "brand_bot"}
    assert normalize({"palette": ["#000000", "#ffffff"]}) == {"palette": ["#000000", "#FFFFFF"]}


@pytest.mark.parametrize("change", [
    {"palette": ["#FD8D00"]},
    {"palette": ["red", "#FFFFFF"]},
    {"gradient_stops": [{"color": "#FD8D00", "x": 1.5, "y": 0}, {"color": "#FD8D00", "x": 0, "y": 0}]},
    {"gradient_stops": [{"color": "#FD8D00", "x": True, "y": 0}, {"color": "#FD8D00", "x": 0, "y": 0}]},
    {"typography": {"family": "Passenger; } body { color: red"}},
    {"typography": {"family": "Archivo", "stylesheet_url": "https://evil.example/font.css"}},
    {"typography": {"family": "Archivo", "letter_spacing": "calc(1px)"}},
    {"logo": {"on_light": "http://insecure.example/logo.png"}},
    {"logo": {"on_light": "javascript:alert(1)"}},
    {"logo": {"alt": "<b>x</b>", "mark": "https://api.chatboc.ar/static/a.png"}},
    {"bubble_style": "neon"},
    {"avatar": {"style": "custom", "url": "https://x"}},
    {"unexpected": True},
])
def test_normalize_rejects_anything_outside_the_contract(change):
    with pytest.raises(BrandIdentityError):
        normalize({**IDENTITY, **change})


class BrandIdentityHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.profile_acceptance_runtime import create_disposable_app
        cls.directory = tempfile.TemporaryDirectory(prefix="chatboc-brand-identity-")
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove(); db.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        from database import db
        from models import AuditEvent, TenantProfile
        self.db, self.Tenant, self.Audit = db, TenantProfile, AuditEvent
        self.context = self.app.app_context(); self.context.push()
        tenant = TenantProfile.query.filter_by(slug="acceptance-a").one()
        tenant.configuracion = {}; tenant.plan = "full"; tenant.is_active = True
        db.session.commit()
        self.endpoint = "/api/admin/tenants/acceptance-a/brand-identity"
        operator = lambda actor: actor.email == self.accounts["second"]["email"]
        patcher = patch("routes.brand_identity.is_authorized_superadmin_user", side_effect=operator)
        patcher.start(); self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.session.rollback(); self.db.session.remove(); self.context.pop()

    def login(self, account):
        client = self.app.test_client()
        response = client.post("/auth/login", json={"email": self.accounts[account]["email"], "password": self.password})
        self.assertEqual(response.status_code, 200)
        return client

    def public_identity(self):
        response = self.app.test_client().get("/api/public/tenants/acceptance-a/widget-config")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        return response.json.get("brand_identity")

    def test_only_a_superadmin_publishes_and_removes_the_identity(self):
        owner = self.login("acceptance-a")
        self.assertEqual(owner.put(self.endpoint, json={"brand_identity": IDENTITY}).status_code, 403)
        self.assertEqual(owner.get(self.endpoint).status_code, 403)
        self.assertIsNone(self.public_identity())

        operator = self.login("second")
        bad = operator.put(self.endpoint, json={"brand_identity": {**IDENTITY, "bubble_style": "neon"}})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json["reason_code"], "brand_identity_bubble_style_invalid")
        self.assertEqual(operator.put(self.endpoint, json={"other": 1}).status_code, 400)

        saved = operator.put(self.endpoint, json={"brand_identity": IDENTITY})
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertIs(saved.json["published"], True)
        public = self.public_identity()
        self.assertEqual(public["contract_version"], "public.brand_identity.v1")
        self.assertEqual(public["palette"], IDENTITY["palette"])
        self.assertEqual(public["avatar"], {"style": "brand_bot"})

        tenant = self.Tenant.query.filter_by(slug="acceptance-a").one()
        tenant.plan = "basic"; self.db.session.commit()
        self.assertIsNone(self.public_identity())
        tenant.plan = "full"; self.db.session.commit()

        removed = operator.put(self.endpoint, json={"brand_identity": None})
        self.assertEqual(removed.status_code, 200)
        self.assertIsNone(removed.json["brand_identity"])
        self.assertIsNone(self.public_identity())
        events = self.db.session.query(self.Audit).filter(self.Audit.event_type.like("brand_identity.%")).count()
        self.assertEqual(events, 2)

    def test_a_corrupt_stored_identity_is_never_published(self):
        tenant = self.Tenant.query.filter_by(slug="acceptance-a").one()
        tenant.configuracion = {"brand_identity": {"palette": ["red"]}}
        self.db.session.commit()
        self.assertIsNone(self.public_identity())
