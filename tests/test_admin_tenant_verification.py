import unittest
import json
import jwt
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from app import create_app, db
from models import User, TenantProfile, Role, UserRole, TenantConfig, TwilioNumber, OrgUnit, UserOrgUnit
from config import TestConfig
from services.tenant_factory import create_tenant_from_template
from utils.auth_helpers import auth_session_version

class TestAdminTenantVerification(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def generate_token(self, user):
        now = datetime.now(timezone.utc)
        payload = {'user_id': user.id, 'exp': now + timedelta(days=1)}
        if user.rol == "super_admin":
            payload.update(
                {
                    "rol": user.rol,
                    "auth_provider": "clerk",
                    "session_kind": "clerk",
                    "sid": "sess_admin_tenant_verification",
                    "clerk_sid": "sess_admin_tenant_verification",
                    "jti": "jti_admin_tenant_verification",
                    "sv": auth_session_version(user),
                    "iat": now,
                }
            )
        return jwt.encode(
            payload,
            self.app.config['SECRET_KEY'],
            algorithm="HS256"
        )

    def test_create_tenant(self):
        payload = {
            "slug": "new-city-verify",
            "nombre": "New City Verify",
            "tipo": "municipio"
        }
        response = self.client.post('/api/admin/tenants', json=payload)
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(data['slug'], "new-city-verify")
        self.assertEqual(data["plan"], "free")
        self.assertFalse(data["integration_access"]["enabled"])
        self.assertEqual(data["integration_access"]["reason_code"], "plan_full_required")

        tenant = TenantProfile.query.filter_by(slug="new-city-verify").first()
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.plan, "free")
        self.assertNotIn("whatsapp_onboarding", tenant.configuracion or {})
        self.assertEqual((tenant.configuracion or {})["provisioning"]["status"], "plan_required")
        readiness = data["provisioning_readiness"]
        self.assertEqual(readiness["contract_version"], "tenant.provisioning_readiness.v1")
        self.assertFalse(readiness["ready"])
        self.assertTrue(readiness["checks"]["base_configuration_valid"])
        self.assertFalse(readiness["checks"]["provider_activation_performed"])
        self.assertEqual(
            {config.key for config in TenantConfig.query.filter_by(tenant_id=tenant.id).all()},
            {"menu", "contacts", "links", "widget"},
        )
        self.assertTrue(
            all(
                isinstance(config.json_value, dict) and config.json_value
                for config in TenantConfig.query.filter_by(tenant_id=tenant.id).all()
            )
        )

    def test_tenant_factory_fails_closed_before_writes_when_template_is_missing(self):
        response = self.client.post(
            "/api/admin/tenants",
            json={
                "slug": "missing-template-tenant",
                "nombre": "Missing Template Tenant",
                "tipo": "municipio",
                "template_key": "missing-template",
                "owner_email": "owner@missing-template.test",
            },
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("was not found", response.get_json()["error"])
        self.assertIsNone(TenantProfile.query.filter_by(slug="missing-template-tenant").first())
        self.assertIsNone(User.query.filter_by(email="owner@missing-template.test").first())
        self.assertEqual(TenantConfig.query.count(), 0)

    def test_tenant_factory_rejects_template_path_traversal_without_writes(self):
        response = self.client.post(
            "/api/admin/tenants",
            json={
                "slug": "unsafe-template-tenant",
                "nombre": "Unsafe Template Tenant",
                "tipo": "pyme",
                "template_key": "../municipio_default",
                "owner_email": "owner@unsafe-template.test",
            },
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["error"], "template_key is invalid")
        self.assertIsNone(TenantProfile.query.filter_by(slug="unsafe-template-tenant").first())
        self.assertIsNone(User.query.filter_by(email="owner@unsafe-template.test").first())

    def test_tenant_factory_rejects_template_for_another_vertical(self):
        response = self.client.post(
            "/api/admin/tenants",
            json={
                "slug": "mismatched-template-tenant",
                "nombre": "Mismatched Template Tenant",
                "tipo": "pyme",
                "template_key": "municipio_default",
                "owner_email": "owner@mismatched-template.test",
            },
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("does not support tenant type", response.get_json()["error"])
        self.assertIsNone(TenantProfile.query.filter_by(slug="mismatched-template-tenant").first())
        self.assertIsNone(User.query.filter_by(email="owner@mismatched-template.test").first())

    def test_public_create_tenant_rejects_productive_plan(self):
        response = self.client.post(
            '/api/admin/tenants',
            json={
                "slug": "junin-full-public-blocked",
                "nombre": "Junin Full Public Blocked",
                "tipo": "municipio",
                "plan": "full",
                "owner_email": "admin@junin-full-public-blocked.test",
            },
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertEqual(payload["reason_code"], "productive_plan_requires_super_admin")
        self.assertEqual(payload["allowed_public_plan"], "free")
        self.assertIsNone(TenantProfile.query.filter_by(slug="junin-full-public-blocked").first())

    def test_public_create_tenant_cannot_consume_whatsapp_number_inventory(self):
        number = TwilioNumber(
            phone_number="+15550001001",
            sender_id="whatsapp:+15550001001",
            status="available",
        )
        db.session.add(number)
        db.session.commit()

        response = self.client.post(
            "/api/admin/tenants",
            json={
                "slug": "public-number-drain-attempt",
                "nombre": "Public Number Drain Attempt",
                "tipo": "pyme",
                "autoAssignWhatsappNumber": True,
            },
        )

        self.assertEqual(response.status_code, 403, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["error"], "whatsapp_number_assignment_forbidden")
        self.assertEqual(payload["reason_code"], "super_admin_required")
        self.assertTrue(payload["frontend_contract"]["allow_continue_without_number"])
        self.assertIsNone(TenantProfile.query.filter_by(slug="public-number-drain-attempt").first())
        db.session.refresh(number)
        self.assertEqual(number.status, "available")
        self.assertIsNone(number.tenant_id)

    def test_tenant_factory_rejects_number_assignment_for_free_plan(self):
        number = TwilioNumber(
            phone_number="+15550001003",
            sender_id="whatsapp:+15550001003",
            status="available",
        )
        db.session.add(number)
        db.session.commit()

        with self.assertRaisesRegex(ValueError, "requires a productive integration plan"):
            create_tenant_from_template(
                nombre="Factory Guard",
                slug="factory-number-guard",
                tipo="pyme",
                plan="free",
                auto_assign_whatsapp_number=True,
            )

        self.assertIsNone(TenantProfile.query.filter_by(slug="factory-number-guard").first())
        db.session.refresh(number)
        self.assertEqual(number.status, "available")
        self.assertIsNone(number.tenant_id)

    def test_create_tenant_honors_full_plan_and_prepares_onboarding(self):
        self.app.config.update(
            TWILIO_TENANT_AUTO_BOOTSTRAP_ENABLED=True,
            TWILIO_TENANT_AUTO_PROVISION_ENABLED=False,
        )
        super_admin = User(email="guillen.marce@gmail.com", name="Platform", rol="super_admin")
        super_admin.set_password("pass")
        db.session.add(super_admin)
        db.session.commit()
        payload = {
            "slug": "junin-full-verify",
            "nombre": "Junin Full Verify",
            "tipo": "municipio",
            "plan": "full",
            "owner_email": "admin@junin-full-verify.test",
        }

        response = self.client.post(
            '/api/admin/tenants',
            json=payload,
            headers={"Authorization": f"Bearer {self.generate_token(super_admin)}"},
        )

        self.assertEqual(response.status_code, 201, response.get_json())
        data = response.get_json()
        self.assertEqual(data["plan"], "full")
        self.assertEqual(data["tenant"]["plan"], "full")
        self.assertTrue(data["widget_token"])
        self.assertTrue(data["integration_access"]["enabled"])
        self.assertEqual(data["integration_access"]["status"], "enabled")
        self.assertIsNone(data["integration_access"]["reason_code"])
        self.assertEqual(data["whatsapp_onboarding"]["contract_version"], "tenant.whatsapp_onboarding.v1")
        self.assertEqual(data["whatsapp_onboarding"]["provider"], "twilio_tech_provider")

        tenant = TenantProfile.query.filter_by(slug="junin-full-verify").first()
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.plan, "full")
        self.assertIn("widget_tokens", tenant.configuracion or {})
        self.assertIn("whatsapp_onboarding", tenant.configuracion or {})
        self.assertEqual((tenant.configuracion or {})["provisioning"]["status"], "created")
        owner = db.session.get(User, tenant.municipio_id)
        self.assertIsNotNone(owner)
        self.assertEqual(owner.plan, "full")
        self.assertEqual(owner.tenant_slug, tenant.slug)

    def test_full_plan_factory_never_calls_provider_provisioning(self):
        self.app.config.update(
            TWILIO_TENANT_AUTO_BOOTSTRAP_ENABLED=True,
            TWILIO_TENANT_AUTO_PROVISION_ENABLED=True,
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=True,
        )
        super_admin = User(email="guillen.marce@gmail.com", name="Platform", rol="super_admin")
        super_admin.set_password("pass")
        db.session.add(super_admin)
        db.session.commit()

        with patch(
            "services.tenant_whatsapp_onboarding.provision_twilio_subaccount"
        ) as provision_subaccount, patch(
            "services.tenant_whatsapp_onboarding.provision_twilio_voice_application"
        ) as provision_voice:
            response = self.client.post(
                "/api/admin/tenants",
                json={
                    "slug": "provider-plan-only",
                    "nombre": "Provider Plan Only",
                    "tipo": "municipio",
                    "plan": "full",
                    "owner_email": "owner@provider-plan-only.test",
                },
                headers={"Authorization": f"Bearer {self.generate_token(super_admin)}"},
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        provision_subaccount.assert_not_called()
        provision_voice.assert_not_called()
        onboarding = response.get_json()["whatsapp_onboarding"]
        self.assertFalse(onboarding["auto_provision_enabled"])
        self.assertEqual(onboarding["source"], "tenant_factory_plan")
        self.assertNotEqual(onboarding["status"], "online")

    def test_super_admin_full_plan_can_assign_whatsapp_number(self):
        number = TwilioNumber(
            phone_number="+15550001002",
            sender_id="whatsapp:+15550001002",
            status="available",
        )
        super_admin = User(email="guillen.marce@gmail.com", name="Platform", rol="super_admin")
        super_admin.set_password("pass")
        db.session.add_all([number, super_admin])
        db.session.commit()

        response = self.client.post(
            "/api/admin/tenants",
            json={
                "slug": "authorized-number-assignment",
                "nombre": "Authorized Number Assignment",
                "tipo": "pyme",
                "plan": "full",
                "owner_email": "owner@authorized-number-assignment.test",
                "auto_assign_whatsapp_number": True,
            },
            headers={"Authorization": f"Bearer {self.generate_token(super_admin)}"},
        )

        self.assertEqual(response.status_code, 201, response.get_json())
        tenant = TenantProfile.query.filter_by(slug="authorized-number-assignment").first()
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.whatsapp_sender_id, "whatsapp:+15550001002")
        db.session.refresh(number)
        self.assertEqual(number.status, "assigned")
        self.assertEqual(number.tenant_id, tenant.id)

    def test_public_create_tenant_does_not_reassign_existing_owner_email(self):
        existing = User(email="taken-owner@test.local", name="Taken", rol="admin", tipo_chat="pyme", tenant_slug="existing")
        existing.set_password("old-password")
        db.session.add(existing)
        db.session.commit()

        response = self.client.post(
            '/api/admin/tenants',
            json={
                "slug": "owner-takeover-attempt",
                "nombre": "Owner Takeover Attempt",
                "tipo": "municipio",
                "owner_email": "taken-owner@test.local",
                "owner_password": "new-password",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("owner_email already exists", response.get_json()["error"])
        refreshed = db.session.get(User, existing.id)
        self.assertEqual(refreshed.tenant_slug, "existing")
        self.assertEqual(refreshed.rol, "admin")
        self.assertTrue(refreshed.check_password("old-password"))
        self.assertFalse(refreshed.check_password("new-password"))
        self.assertIsNone(TenantProfile.query.filter_by(slug="owner-takeover-attempt").first())

    def test_create_tenant_rejects_invalid_plan(self):
        response = self.client.post(
            '/api/admin/tenants',
            json={
                "slug": "invalid-plan-verify",
                "nombre": "Invalid Plan Verify",
                "tipo": "pyme",
                "plan": "moonshot",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("plan must be one of", response.get_json()["error"])
        self.assertIsNone(TenantProfile.query.filter_by(slug="invalid-plan-verify").first())

    def _unbound_factory_owner(self, key):
        owner = User(email=f"{key}@example.test", name="Existing owner", rol="usuario")
        owner.set_password("existing-fixture-password")
        db.session.add(owner)
        db.session.commit()
        return owner

    def _reuse_factory_owner(self, owner, slug, **options):
        return create_tenant_from_template(
            nombre="Institutional fixture", slug=slug, tipo="municipio", plan="free",
            owner_email=owner.email, allow_existing_owner=True,
            **options,
        )

    def test_factory_rejects_bound_owners_before_mutation(self):
        reference = self._unbound_factory_owner("reference-owner")
        prior_tenant = TenantProfile(slug="reference-organization", nombre="Existing organization",
                                    tipo="municipio", municipio_id=reference.id, is_active=True)
        db.session.add(prior_tenant)
        db.session.commit()
        for field, value in (("tenant_id", prior_tenant.id), ("tenant_slug", prior_tenant.slug),
                             ("municipio_id", reference.id), ("pyme_id", reference.id),
                             ("empresa_id", reference.id)):
            with self.subTest(field=field):
                owner = self._unbound_factory_owner(f"bound-{field}")
                setattr(owner, field, value)
                db.session.commit()
                owner_id, password_hash = owner.id, owner.password_hash
                original = (owner.tenant_id, owner.tenant_slug, owner.rol, owner.municipio_id, owner.pyme_id, owner.empresa_id)
                before = (TenantProfile.query.count(), TenantConfig.query.count())
                slug = f"attempt-{field.replace('_', '-')}"
                with patch.object(User, "set_password") as change_password:
                    with self.assertRaisesRegex(ValueError, "already linked"):
                        self._reuse_factory_owner(owner, slug, reset_existing_owner_password=True)
                    change_password.assert_not_called()
                owner = db.session.get(User, owner_id)
                self.assertTrue(owner.password_hash == password_hash)
                self.assertEqual((owner.tenant_id, owner.tenant_slug, owner.rol, owner.municipio_id, owner.pyme_id, owner.empresa_id), original)
                self.assertEqual((TenantProfile.query.count(), TenantConfig.query.count()), before)
                self.assertIsNone(TenantProfile.query.filter_by(slug=slug).first())

    def test_factory_rejects_inverse_ownership_even_with_empty_user_scope(self):
        for field in ("municipio_id", "pyme_id"):
            with self.subTest(field=field):
                owner = self._unbound_factory_owner(f"inverse-{field}")
                original = TenantProfile(slug=f"inverse-{field.replace('_', '-')}", nombre="Existing",
                                         tipo="municipio" if field == "municipio_id" else "pyme", is_active=True)
                setattr(original, field, owner.id)
                db.session.add(original)
                db.session.commit()
                owner_id, old_hash, original_id = owner.id, owner.password_hash, original.id
                with self.assertRaisesRegex(ValueError, "already owns"):
                    self._reuse_factory_owner(owner, f"duplicate-{field.replace('_', '-')}")
                owner = db.session.get(User, owner_id)
                self.assertIsNone(owner.tenant_id)
                self.assertTrue(owner.password_hash == old_hash)
                self.assertEqual(getattr(db.session.get(TenantProfile, original_id), field), owner_id)

    def test_factory_preserves_password_when_omitted_even_with_reset_flag(self):
        for index, password in enumerate((None, "", "   ")):
            with self.subTest(index=index):
                owner = self._unbound_factory_owner(f"preserve-{index}")
                before_hash = owner.password_hash
                tenant = self._reuse_factory_owner(owner, f"preserve-{index}", owner_password=password,
                                                  reset_existing_owner_password=True)
                self.assertTrue(owner.password_hash == before_hash)
                self.assertTrue(owner.check_password("existing-fixture-password"))
                self.assertEqual(owner.tenant_id, tenant.id)
                self.assertIsNone(tenant.whatsapp_sender_id)

    def test_factory_rejects_scoped_memberships_with_empty_user_scope(self):
        reference = self._unbound_factory_owner("membership-reference")
        prior_tenant = TenantProfile(slug="membership-organization", nombre="Existing organization",
                                    tipo="municipio", municipio_id=reference.id, is_active=True)
        role = Role(name="factory-scoped-member")
        db.session.add_all([prior_tenant, role])
        db.session.flush()
        unit = OrgUnit(tenant_id=prior_tenant.id, name="Existing team")
        db.session.add(unit)
        db.session.commit()
        for kind in ("role", "org-unit"):
            with self.subTest(kind=kind):
                owner = self._unbound_factory_owner(f"membership-{kind}")
                assignment = (UserRole(user_id=owner.id, role_id=role.id, tenant_id=prior_tenant.id)
                              if kind == "role" else UserOrgUnit(user_id=owner.id, org_unit_id=unit.id,
                                                                  tenant_id=prior_tenant.id))
                db.session.add(assignment)
                db.session.commit()
                owner_id, old_hash, assignment_id = owner.id, owner.password_hash, assignment.id
                before = (TenantProfile.query.count(), TenantConfig.query.count())
                with patch.object(User, "set_password") as change_password:
                    with self.assertRaisesRegex(ValueError, "already linked"):
                        self._reuse_factory_owner(owner, f"membership-attempt-{kind}",
                            owner_password="replacement-fixture-password", reset_existing_owner_password=True)
                    change_password.assert_not_called()
                owner = db.session.get(User, owner_id)
                self.assertTrue(owner.password_hash == old_hash)
                self.assertIsNone(owner.tenant_id)
                self.assertIsNone(owner.tenant_slug)
                self.assertEqual(owner.rol, "usuario")
                preserved = db.session.get(type(assignment), assignment_id)
                self.assertEqual((preserved.user_id, preserved.tenant_id), (owner_id, prior_tenant.id))
                self.assertEqual((TenantProfile.query.count(), TenantConfig.query.count()), before)

    def test_factory_allows_global_role_without_organization_membership(self):
        owner = self._unbound_factory_owner("global-role-only")
        role = Role(name="factory-global-user")
        db.session.add(role)
        db.session.flush()
        assignment = UserRole(user_id=owner.id, role_id=role.id, tenant_id=None)
        db.session.add(assignment)
        db.session.commit()
        old_hash, assignment_id = owner.password_hash, assignment.id
        tenant = self._reuse_factory_owner(owner, "global-role-new-organization")
        self.assertEqual(tenant.municipio_id, owner.id)
        self.assertTrue(owner.password_hash == old_hash)
        self.assertIsNone(db.session.get(UserRole, assignment_id).tenant_id)

    def test_factory_explicit_password_requires_existing_reset_opt_in(self):
        for reset in (False, True):
            with self.subTest(reset=reset):
                owner = self._unbound_factory_owner(f"explicit-{reset}")
                before_hash = owner.password_hash
                self._reuse_factory_owner(owner, f"explicit-{str(reset).lower()}",
                                         owner_password="replacement-fixture-password",
                                         reset_existing_owner_password=reset)
                self.assertEqual(owner.check_password("replacement-fixture-password"), reset)
                self.assertEqual(owner.password_hash == before_hash, not reset)

    def test_factory_reuses_legacy_self_owner_only_for_the_same_organization(self):
        for tipo, field in (("municipio", "municipio_id"), ("pyme", "pyme_id")):
            with self.subTest(tipo=tipo):
                owner = self._unbound_factory_owner(f"legacy-{tipo}")
                owner.tenant_slug = f"legacy-{tipo}"
                setattr(owner, field, owner.id)
                db.session.commit()
                before_hash = owner.password_hash
                tenant = create_tenant_from_template(nombre="Legacy fixture", slug=owner.tenant_slug,
                    tipo=tipo, plan="free", owner_email=owner.email, allow_existing_owner=True)
                self.assertEqual(getattr(tenant, field), owner.id)
                self.assertEqual(getattr(owner, field), owner.id)
                self.assertTrue(owner.password_hash == before_hash)

    def test_factory_does_not_convert_legacy_owner_to_another_organization_type(self):
        owner = self._unbound_factory_owner("legacy-other-type")
        owner.pyme_id = owner.id
        db.session.commit()
        with self.assertRaisesRegex(ValueError, "already linked"):
            self._reuse_factory_owner(owner, "legacy-new-municipality")
        self.assertEqual(owner.pyme_id, owner.id)
        self.assertIsNone(owner.municipio_id)

    def test_factory_case_insensitive_email_reuses_one_identity(self):
        owner = self._unbound_factory_owner("case-owner")
        owner.email = "Case-Owner@example.test"
        db.session.commit()
        user_count = User.query.count()
        tenant = create_tenant_from_template(nombre="Case fixture", slug="case-owner", tipo="municipio",
            plan="free", owner_email="case-owner@example.test", allow_existing_owner=True)
        self.assertEqual(tenant.municipio_id, owner.id)
        self.assertEqual(User.query.count(), user_count)

    def test_factory_does_not_demote_platform_administrator(self):
        owner = self._unbound_factory_owner("platform-owner")
        owner.rol = "super_admin"
        db.session.commit()
        with self.assertRaisesRegex(ValueError, "platform administrator"):
            self._reuse_factory_owner(owner, "platform-owned-institution")
        self.assertEqual(owner.rol, "super_admin")
        self.assertIsNone(owner.tenant_id)

    def test_factory_failure_rolls_back_existing_owner_and_partial_tenant(self):
        owner = self._unbound_factory_owner("rollback-owner")
        owner_id, old_hash = owner.id, owner.password_hash
        before = (User.query.count(), TenantProfile.query.count(), TenantConfig.query.count())
        with patch.object(db.session, "commit", side_effect=RuntimeError("fixture persistence failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture persistence failure"):
                self._reuse_factory_owner(owner, "rollback-institution", owner_password="replacement-fixture-password")
        owner = db.session.get(User, owner_id)
        self.assertTrue(owner.password_hash == old_hash)
        self.assertIsNone(owner.tenant_id)
        self.assertIsNone(owner.tenant_slug)
        self.assertEqual(owner.rol, "usuario")
        self.assertEqual((User.query.count(), TenantProfile.query.count(), TenantConfig.query.count()), before)
        db.session.commit()

    def test_factory_failure_rolls_back_new_owner_as_well(self):
        before = (User.query.count(), TenantProfile.query.count(), TenantConfig.query.count())
        with patch.object(db.session, "commit", side_effect=RuntimeError("fixture persistence failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture persistence failure"):
                create_tenant_from_template(nombre="Rollback fixture", slug="rollback-new", tipo="municipio",
                                           plan="free", owner_email="rollback-new@example.test")
        self.assertEqual((User.query.count(), TenantProfile.query.count(), TenantConfig.query.count()), before)
        db.session.commit()

    def test_create_colegio_without_owner_email_creates_synthetic_admin(self):
        response = self.client.post(
            '/api/admin/tenants',
            json={
                "slug": "colegio-meta-review",
                "nombre": "Colegio Meta Review",
                "tipo": "colegio",
            },
        )

        self.assertEqual(response.status_code, 201, response.get_json())
        data = response.get_json()
        self.assertEqual(data["tenant"]["tipo"], "colegio")
        self.assertTrue(data["tenant"]["owner_email_generated"])
        self.assertEqual(data["tenant"]["owner_email"], "admin@colegio-meta-review.chatboc.local")

        tenant = TenantProfile.query.filter_by(slug="colegio-meta-review").first()
        self.assertIsNotNone(tenant)
        self.assertIsNone(tenant.municipio_id)
        self.assertIsNotNone(tenant.pyme_id)

        owner = db.session.get(User, tenant.pyme_id)
        self.assertEqual(owner.rol, "admin_colegio")
        self.assertEqual(owner.tipo_chat, "colegio")
        self.assertEqual(owner.tenant_slug, "colegio-meta-review")
        self.assertEqual(owner.tenant_id, tenant.id)

    def test_create_tenant_auto_prepares_twilio_provider_plan(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=False,
            TWILIO_TENANT_AUTO_PROVISION_ENABLED=True,
            PUBLIC_API_BASE_URL="https://api.chatboc.ar",
        )
        response = self.client.post(
            "/api/admin/tenants",
            json={"slug": "ferreteria-demo", "nombre": "Ferreteria Demo", "tipo": "pyme"},
        )

        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        self.assertFalse(payload["integration_access"]["enabled"])
        self.assertEqual(payload["integration_access"]["reason_code"], "plan_full_required")
        tenant = TenantProfile.query.filter_by(slug="ferreteria-demo").first()
        self.assertNotIn("twilio_tech_provider", tenant.configuracion or {})
        self.assertNotIn("whatsapp_onboarding", tenant.configuracion or {})
        self.assertEqual(tenant.configuracion["provisioning"]["status"], "plan_required")
        self.assertEqual(tenant.configuracion["provisioning"]["blocked_reason"], "plan_full_required")

    def test_create_employee_with_tenant_context(self):
        # 1. Create Admin User first
        admin = User(email="admin@tenant1.com", name="Admin", rol="admin", tipo_chat="municipio")
        admin.set_password("pass")
        db.session.add(admin)
        db.session.flush()

        # 2. Create Tenant
        tenant = TenantProfile(slug="tenant-1", nombre="Tenant 1", tipo="municipio", municipio_id=admin.id)
        db.session.add(tenant)
        db.session.flush()

        admin.tenant_id = tenant.id
        db.session.commit()

        token = self.generate_token(admin)

        # 3. Call create_employee
        headers = {
            'Authorization': f'Bearer {token}',
            'X-Tenant': 'tenant-1'
        }
        payload = {
            "email": "emp@tenant1.com",
            "name": "Employee 1",
            "password": "pass"
        }

        response = self.client.post('/api/admin/employees', json=payload, headers=headers)
        self.assertEqual(response.status_code, 201)

        emp = User.query.filter_by(email="emp@tenant1.com").first()
        self.assertIsNotNone(emp)
        self.assertEqual(emp.tenant_id, tenant.id)
        self.assertTrue(emp.es_empleado)

        user_role = UserRole.query.filter_by(user_id=emp.id).first()
        self.assertIsNotNone(user_role)
        self.assertEqual(user_role.role.name, "empleado")

    def test_assign_role(self):
        admin = User(email="admin@tenant2.com", name="Admin", rol="admin", tipo_chat="pyme")
        admin.set_password("pass")
        db.session.add(admin)
        db.session.flush()

        tenant = TenantProfile(slug="tenant-2", nombre="Tenant 2", tipo="pyme", pyme_id=admin.id)
        db.session.add(tenant)
        db.session.flush()

        admin.tenant_id = tenant.id

        emp = User(email="emp@tenant2.com", name="Emp", tenant_id=tenant.id)
        emp.set_password("pass")
        db.session.add(emp)
        db.session.commit()

        token = self.generate_token(admin)
        headers = {'Authorization': f'Bearer {token}', 'X-Tenant': 'tenant-2'}

        payload = {"role": "manager"}
        response = self.client.post(f'/api/admin/employees/{emp.id}/roles', json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)

        user_role = UserRole.query.filter_by(user_id=emp.id, tenant_id=tenant.id).all()
        roles = [ur.role.name for ur in user_role]
        self.assertIn("manager", roles)

if __name__ == '__main__':
    unittest.main()
