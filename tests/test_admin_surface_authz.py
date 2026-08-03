import os
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import TestConfig
from models import CatalogoItem, Rubro, TenantProfile, User
from routes.auth import solo_admin_requerido
from utils.auth_helpers import admin_o_empleado_requerido, generar_token
from utils.parsers import admin_o_empleado_requerido as parser_admin_o_empleado_requerido


class AdminSurfaceAuthorizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestConfig)
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.client = self.app.test_client()
        self.rubro = Rubro(nombre="Servicios", clave=f"servicios-{uuid.uuid4().hex[:8]}")
        db.session.add(self.rubro)
        db.session.flush()

    def _user(self, *, email, role, empresa_id=None, tenant_id=None):
        user = User(
            name=email.split("@", 1)[0],
            email=email,
            rol=role,
            empresa_id=empresa_id,
            tenant_id=tenant_id,
            rubro_id=self.rubro.id,
        )
        user.set_password("secret")
        db.session.add(user)
        db.session.flush()
        return user

    def _tenant(self, owner, *, slug, active=True):
        tenant = TenantProfile(
            slug=slug,
            nombre=slug,
            tipo="pyme",
            pyme_id=owner.id,
            is_active=active,
        )
        db.session.add(tenant)
        db.session.flush()
        return tenant

    @staticmethod
    def _status(result):
        return result[1] if isinstance(result, tuple) else 200

    def test_canonical_decorators_fail_closed_for_roles_and_tenant_state(self):
        owner = self._user(email="owner@test.com", role="admin")
        tenant = self._tenant(owner, slug="owner")
        owner.tenant_id = tenant.id
        employee = self._user(
            email="employee@test.com",
            role="operator",
            empresa_id=owner.id,
            tenant_id=tenant.id,
        )
        lead = self._user(email="lead@test.com", role="lead")
        provisional = self._user(email="passkey@test.com", role="usuario")
        orphan_admin = self._user(email="orphan-admin@test.com", role="admin")

        inactive_owner = self._user(email="inactive@test.com", role="admin")
        inactive_tenant = self._tenant(inactive_owner, slug="inactive", active=False)
        inactive_owner.tenant_id = inactive_tenant.id

        ambiguous_owner = self._user(email="ambiguous@test.com", role="admin")
        ambiguous_one = self._tenant(ambiguous_owner, slug="ambiguous-one")
        self._tenant(ambiguous_owner, slug="ambiguous-two")
        ambiguous_owner.tenant_id = ambiguous_one.id

        superadmin = self._user(
            email="guillen.marce@gmail.com",
            role="super_admin",
        )
        db.session.commit()

        def endpoint(_user):
            return "ok"

        central = admin_o_empleado_requerido(endpoint)
        alias = parser_admin_o_empleado_requerido(endpoint)
        admin_only = solo_admin_requerido(endpoint)

        with self.app.test_request_context("/"):
            for decorator in (central, alias):
                self.assertEqual(self._status(decorator(owner)), 200)
                self.assertEqual(self._status(decorator(employee)), 200)
                self.assertEqual(self._status(decorator(superadmin)), 200)
                for denied in (lead, provisional, orphan_admin, inactive_owner, ambiguous_owner):
                    self.assertEqual(self._status(decorator(denied)), 403)

            self.assertEqual(self._status(admin_only(owner)), 200)
            self.assertEqual(self._status(admin_only(superadmin)), 200)
            self.assertEqual(self._status(admin_only(employee)), 403)
            self.assertEqual(self._status(admin_only(orphan_admin)), 403)

    def test_passkey_style_provisional_user_cannot_reach_admin_modules(self):
        provisional = self._user(email="provisional@test.com", role="usuario")
        db.session.commit()
        token = generar_token(provisional.id, provisional.rol, None, None, None)
        headers = {"Authorization": f"Bearer {token}"}

        templates = self.client.get("/api/ai/templates", headers=headers)
        surveys = self.client.get("/admin/encuestas/", headers=headers)

        self.assertEqual(templates.status_code, 403)
        self.assertIn(surveys.status_code, {403, 404})
        if surveys.status_code != 404:
            self.assertEqual(surveys.status_code, 403)

    def test_ambiguous_owner_requires_and_honors_exact_owned_tenant_selection(self):
        owner = self._user(email="multi-owner@test.com", role="admin")
        tenant_a = self._tenant(owner, slug="multi-owner-a")
        tenant_b = self._tenant(owner, slug="multi-owner-b")
        owner.tenant_id = tenant_a.id
        outsider = self._user(email="outsider@test.com", role="admin")
        outsider_tenant = self._tenant(outsider, slug="outsider")
        outsider.tenant_id = outsider_tenant.id
        db.session.commit()

        def endpoint(_user):
            return "ok"

        protected = admin_o_empleado_requerido(endpoint)
        with self.app.test_request_context("/"):
            self.assertEqual(self._status(protected(owner)), 403)
        with self.app.test_request_context(
            "/",
            headers={"X-Tenant-Slug": tenant_b.slug},
        ):
            self.assertEqual(self._status(protected(owner)), 200)
            self.assertEqual(self._status(protected(outsider)), 403)
        with self.app.test_request_context(
            "/?tenant_slug=multi-owner-a",
            headers={"X-Tenant-Slug": tenant_b.slug},
        ):
            self.assertEqual(self._status(protected(owner)), 403)

    def test_registered_owner_without_tenant_cannot_select_market_tenant(self):
        victim_owner = self._user(email="victim@test.com", role="admin")
        victim_tenant = self._tenant(victim_owner, slug="victim")
        victim_owner.tenant_id = victim_tenant.id
        db.session.commit()

        register_payload = {
            "name": "Unprovisioned Owner",
            "email": "unprovisioned@test.com",
            "password": "secret-password",
            "nombre_empresa": "Unprovisioned",
            "rubro": self.rubro.clave,
            "tipo_chat": "pyme",
            "acepto_terminos": True,
        }
        with patch("routes.auth._send_verification_email"), patch(
            "routes.auth._apply_welcome_points_if_configured"
        ):
            registered = self.client.post("/auth/register", json=register_payload)
        self.assertEqual(registered.status_code, 201, registered.get_json())
        token = registered.get_json()["token"]

        exploited = self.client.post(
            "/api/admin/market/catalog",
            json={
                "tenant_id": victim_tenant.id,
                "nombre": "Producto inyectado",
                "precio": "10",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(exploited.status_code, 403, exploited.get_json())
        self.assertEqual(CatalogoItem.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
