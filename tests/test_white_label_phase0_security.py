import io
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from flask import g

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "https://example.com")

from app import create_app
from config import TestConfig
from middleware.tenant_context import _resolve_tenant_profile
from models import CatalogoItem, CatalogUpload, Role, TenantProfile, TwilioNumber, User, UserRole, WidgetSettings, db
from services.tenant_resolver import (
    TenantResolutionError,
    resolve_tenant_and_user,
    resolve_tenant_only,
)
from utils.auth_helpers import auth_session_version
from utils.tenant_admin_access import can_manage_tenant_catalog


class WhiteLabelPhaseZeroSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _tenant(
        self,
        slug: str,
        *,
        role: str = "admin",
        active: bool = True,
        plan: str = "full",
        domain: str | None = None,
        widget_token: str | None = None,
        whatsapp_number: str | None = None,
    ) -> tuple[User, TenantProfile]:
        owner = User(
            email=f"{slug}@test.local",
            name=f"Owner {slug}",
            rol=role,
            tipo_chat="municipio",
            tenant_slug=slug,
        )
        owner.set_password("pass")
        db.session.add(owner)
        db.session.flush()

        configuration = {}
        if widget_token:
            configuration["widget_tokens"] = [widget_token]
        if whatsapp_number:
            configuration["whatsapp_numbers"] = [whatsapp_number]
        tenant = TenantProfile(
            slug=slug,
            nombre=f"Tenant {slug}",
            tipo="municipio",
            municipio_id=owner.id,
            plan=plan,
            is_active=active,
            dominio=domain,
            whatsapp_sender_id=f"whatsapp:{whatsapp_number}" if whatsapp_number else None,
            configuracion=configuration,
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        owner.municipio_id = owner.id
        db.session.commit()
        return owner, tenant

    def _employee(self, tenant: TenantProfile, *, email_prefix: str = "employee") -> User:
        employee = User(
            email=f"{email_prefix}-{tenant.slug}@test.local",
            name="Employee",
            rol="empleado",
            tipo_chat="municipio",
            es_empleado=True,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            municipio_id=tenant.municipio_id,
        )
        employee.set_password("pass")
        db.session.add(employee)
        db.session.commit()
        return employee

    def _token(self, user: User) -> str:
        now = datetime.now(timezone.utc)
        payload = {"user_id": user.id, "exp": now + timedelta(hours=1)}
        if user.rol == "super_admin":
            payload.update(
                {
                    "rol": user.rol,
                    "auth_provider": "clerk",
                    "session_kind": "clerk",
                    "sid": "sess_white_label_phase0",
                    "clerk_sid": "sess_white_label_phase0",
                    "jti": "jti_white_label_phase0",
                    "sv": auth_session_version(user),
                    "iat": now,
                }
            )
        return jwt.encode(payload, self.app.config["SECRET_KEY"], algorithm="HS256")

    def _headers(self, user: User, tenant: TenantProfile) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token(user)}",
            "X-Tenant": tenant.slug,
        }

    def test_unknown_custom_host_never_uses_default_or_first_tenant(self):
        _owner, tenant = self._tenant("default-active")
        self.app.config["PUBLIC_CATALOG_DEFAULT_TENANT"] = tenant.slug

        with self.app.test_request_context("/", base_url="https://unmapped.customer.example"):
            with self.assertRaises(TenantResolutionError):
                resolve_tenant_only(
                    host="unmapped.customer.example",
                    allow_lazy_demo_creation=False,
                    register_widget_token=False,
                )

    def test_public_selectors_ignore_inactive_tenant(self):
        self._tenant("default-active")
        _owner, inactive = self._tenant(
            "inactive-brand",
            active=False,
            domain="inactive.example",
            widget_token="inactive-widget-token",
            whatsapp_number="+15550009999",
        )
        self.app.config["PUBLIC_CATALOG_DEFAULT_TENANT"] = "default-active"

        cases = (
            {"tenant_slug": inactive.slug},
            {"host": inactive.dominio},
            {"widget_token": "inactive-widget-token"},
            {"whatsapp_destination_number": "+15550009999"},
        )
        with self.app.test_request_context("/", base_url="https://testserver"):
            for kwargs in cases:
                with self.subTest(selector=kwargs):
                    with self.assertRaises(TenantResolutionError):
                        resolve_tenant_only(
                            **kwargs,
                            allow_lazy_demo_creation=False,
                            register_widget_token=False,
                        )

    def test_active_public_selectors_resolve_exact_tenant(self):
        _owner, tenant = self._tenant(
            "active-brand",
            domain="active.example",
            widget_token="active-widget-token",
            whatsapp_number="+15550008888",
        )

        cases = (
            {"tenant_slug": tenant.slug},
            {"host": tenant.dominio},
            {"widget_token": "active-widget-token"},
            {"whatsapp_destination_number": "whatsapp:+15550008888"},
        )
        with self.app.test_request_context("/", base_url="https://testserver"):
            for kwargs in cases:
                with self.subTest(selector=kwargs):
                    resolved = resolve_tenant_only(
                        **kwargs,
                        allow_lazy_demo_creation=False,
                        register_widget_token=False,
                    )
                    self.assertEqual(resolved.id, tenant.id)

    def test_duplicate_active_domain_fails_closed(self):
        self._tenant("domain-a", domain="shared.customer.example")
        self._tenant("domain-b", domain="shared.customer.example")

        with self.app.test_request_context("/", base_url="https://shared.customer.example"):
            with self.assertRaises(TenantResolutionError):
                resolve_tenant_only(
                    host="shared.customer.example",
                    allow_lazy_demo_creation=False,
                    register_widget_token=False,
                )

    def test_invalid_or_ambiguous_widget_token_never_falls_back_in_middleware(self):
        _owner, default_tenant = self._tenant("middleware-default")
        self._tenant("token-a", widget_token="shared-token")
        self._tenant("token-b", widget_token="shared-token")
        self.app.config["PUBLIC_CATALOG_DEFAULT_TENANT"] = default_tenant.slug

        for token in ("unknown-token", "shared-token"):
            with self.subTest(token=token):
                with self.app.test_request_context(
                    "/",
                    headers={"X-Widget-Token": token},
                    base_url="https://chatboc.ar",
                ):
                    self.assertIsNone(_resolve_tenant_profile())

    def test_explicit_internal_context_can_keep_inactive_tenant_for_recovery(self):
        _owner, inactive = self._tenant("inactive-recovery", active=False)

        with self.app.test_request_context("/", base_url="https://unknown.internal.example"):
            g.tenant_profile = inactive
            resolved = resolve_tenant_only(
                host="unknown.internal.example",
                allow_fallback=False,
                allow_lazy_demo_creation=False,
                register_widget_token=False,
            )

        self.assertEqual(resolved.id, inactive.id)

    def test_public_tenant_id_rejects_inactive_but_internal_opt_in_is_preserved(self):
        owner, inactive = self._tenant("inactive-by-id", active=False)

        with self.app.test_request_context("/", base_url="https://testserver"):
            with self.assertRaises(TenantResolutionError):
                resolve_tenant_and_user(
                    tenant_id=inactive.id,
                    current_user=owner,
                    allow_fallback=False,
                )

            resolved, resolved_user, resolved_as_anon = resolve_tenant_and_user(
                tenant_id=inactive.id,
                current_user=owner,
                allow_fallback=False,
                allow_inactive_tenant_id=True,
            )

        self.assertEqual(resolved.id, inactive.id)
        self.assertEqual(resolved_user.id, owner.id)
        self.assertFalse(resolved_as_anon)

    def test_inactive_tenant_id_fails_closed_on_public_pwa_routes(self):
        _owner, inactive = self._tenant("inactive-pwa-id", active=False)

        for route in (
            "/api/pwa/public/tenant-info",
            "/api/pwa/public/surveys",
        ):
            with self.subTest(route=route):
                response = self.client.get(
                    route,
                    query_string={"tenant_id": inactive.id},
                )
                self.assertEqual(response.status_code, 404, response.get_json())
                self.assertNotEqual(
                    (response.get_json() or {}).get("id"),
                    inactive.id,
                )

    def test_inactive_tenant_id_fails_closed_on_public_commerce_routes(self):
        _owner, inactive = self._tenant("inactive-commerce-id", active=False)

        cases = (
            (
                "GET",
                "/api/puntos/saldo",
                {"query_string": {"tenant_id": inactive.id}},
            ),
            (
                "POST",
                "/api/checkout/crear-preferencia",
                {"json": {"tenant_id": inactive.id}},
            ),
            (
                "POST",
                "/api/pedidos/from-file",
                {
                    "query_string": {"tenant_id": inactive.id},
                    "json": {"pedido_text": "Una caja de tornillos"},
                },
            ),
            (
                "GET",
                "/productos",
                {
                    "query_string": {"tenant_id": inactive.id},
                    "headers": {"Accept": "application/json"},
                },
            ),
        )

        for method, route, kwargs in cases:
            with self.subTest(route=route):
                response = self.client.open(route, method=method, **kwargs)
                self.assertIn(response.status_code, {400, 404}, response.get_json())

    def test_employee_can_read_but_cannot_update_tenant_widget_config(self):
        _owner, tenant = self._tenant("read-only-employee")
        employee = self._employee(tenant)
        headers = self._headers(employee, tenant)

        read_response = self.client.get(
            f"/api/tenant/config?tenant_slug={tenant.slug}",
            headers=headers,
        )
        update_response = self.client.put(
            f"/api/tenant/config?tenant_slug={tenant.slug}",
            json={"welcome_message": "Unauthorized mutation"},
            headers=headers,
        )

        self.assertEqual(read_response.status_code, 200, read_response.get_json())
        self.assertEqual(update_response.status_code, 403, update_response.get_json())
        self.assertEqual(update_response.get_json()["reason_code"], "tenant_admin_required")

    def test_employee_and_cross_tenant_admin_cannot_update_admin_config(self):
        owner_a, tenant_a = self._tenant("config-a")
        _owner_b, tenant_b = self._tenant("config-b")
        employee_a = self._employee(tenant_a)

        employee_response = self.client.put(
            f"/api/admin/tenants/{tenant_a.slug}/config",
            json={"tenant": {"nombre": "Employee mutation"}},
            headers=self._headers(employee_a, tenant_a),
        )
        cross_tenant_response = self.client.put(
            f"/api/admin/tenants/{tenant_b.slug}/config",
            json={"tenant": {"nombre": "Cross tenant mutation"}},
            headers={
                "Authorization": f"Bearer {self._token(owner_a)}",
                "X-Tenant": tenant_b.slug,
            },
        )

        self.assertEqual(employee_response.status_code, 403, employee_response.get_json())
        self.assertEqual(employee_response.get_json()["reason_code"], "tenant_admin_required")
        self.assertEqual(cross_tenant_response.status_code, 403, cross_tenant_response.get_json())
        db.session.refresh(tenant_a)
        db.session.refresh(tenant_b)
        self.assertEqual(tenant_a.nombre, "Tenant config-a")
        self.assertEqual(tenant_b.nombre, "Tenant config-b")

    def test_tenant_admin_can_update_both_configuration_routes(self):
        owner, tenant = self._tenant("config-owner")
        headers = self._headers(owner, tenant)

        admin_response = self.client.put(
            f"/api/admin/tenants/{tenant.slug}/config",
            json={"tenant": {"nombre": "Updated owner tenant"}},
            headers=headers,
        )
        widget_response = self.client.put(
            f"/api/tenant/config?tenant_slug={tenant.slug}",
            json={"welcome_message": "Welcome owner"},
            headers=headers,
        )

        self.assertEqual(admin_response.status_code, 200, admin_response.get_json())
        self.assertEqual(widget_response.status_code, 200, widget_response.get_json())
        db.session.refresh(tenant)
        self.assertEqual(tenant.nombre, "Updated owner tenant")
        self.assertEqual(tenant.widget_settings.welcome_title, "Welcome owner")

    def test_whatsapp_inventory_requires_full_tenant_admin(self):
        owner, tenant = self._tenant("whatsapp-full", plan="full")
        employee = self._employee(tenant)
        number = TwilioNumber(
            phone_number="+15550007777",
            sender_id="whatsapp:+15550007777",
            status="available",
        )
        db.session.add(number)
        db.session.commit()

        denied = self.client.post(
            f"/api/admin/tenants/{tenant.slug}/assign-whatsapp-number",
            headers=self._headers(employee, tenant),
        )
        self.assertEqual(denied.status_code, 403, denied.get_json())
        db.session.refresh(number)
        self.assertEqual(number.status, "available")

        allowed = self.client.post(
            f"/api/admin/tenants/{tenant.slug}/assign-whatsapp-number",
            headers=self._headers(owner, tenant),
        )
        self.assertEqual(allowed.status_code, 200, allowed.get_json())
        db.session.refresh(number)
        self.assertEqual(number.status, "assigned")
        self.assertEqual(number.tenant_id, tenant.id)

    def test_free_plan_admin_cannot_consume_whatsapp_inventory(self):
        owner, tenant = self._tenant("whatsapp-free", plan="free")
        number = TwilioNumber(
            phone_number="+15550006666",
            sender_id="whatsapp:+15550006666",
            status="available",
        )
        db.session.add(number)
        db.session.commit()

        response = self.client.post(
            f"/api/admin/tenants/{tenant.slug}/assign-whatsapp-number",
            headers=self._headers(owner, tenant),
        )

        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "plan_full_required")
        db.session.refresh(number)
        self.assertEqual(number.status, "available")

    def test_allowlisted_superadmin_keeps_control_plane_access(self):
        _owner, tenant = self._tenant("superadmin-target")
        super_admin = User(
            email="guillen.marce@gmail.com",
            name="Platform Admin",
            rol="super_admin",
        )
        super_admin.set_password("pass")
        db.session.add(super_admin)
        db.session.commit()

        response = self.client.put(
            f"/api/admin/tenants/{tenant.slug}/config",
            json={"tenant": {"nombre": "Managed by platform"}},
            headers={
                "Authorization": f"Bearer {self._token(super_admin)}",
                "X-Tenant": tenant.slug,
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.refresh(tenant)
        self.assertEqual(tenant.nombre, "Managed by platform")

    def test_legacy_catalog_import_is_tenant_scoped(self):
        owner_a, tenant_a = self._tenant("catalog-import-a", plan="full")
        _owner_b, tenant_b = self._tenant("catalog-import-b", plan="full")
        headers = {"Authorization": f"Bearer {self._token(owner_a)}"}

        with patch(
            "routes.catalog_import.extract_table_from_file",
            return_value=[{"titulo": "Producto A", "precio": "100", "stock": "2"}],
        ):
            denied = self.client.post(
                "/api/admin/catalogo/importar",
                data={
                    "tenant_slug": tenant_b.slug,
                    "archivo": (io.BytesIO(b"catalog-b"), "catalogo.pdf"),
                },
                headers=headers,
                content_type="multipart/form-data",
            )
            allowed = self.client.post(
                "/api/admin/catalogo/importar",
                data={
                    "tenant_slug": tenant_a.slug,
                    "archivo": (io.BytesIO(b"catalog-a"), "catalogo.pdf"),
                },
                headers=headers,
                content_type="multipart/form-data",
            )

        self.assertEqual(denied.status_code, 403, denied.get_json())
        self.assertEqual(
            denied.get_json()["codigo"],
            "catalog_tenant_scope_forbidden",
        )
        self.assertEqual(allowed.status_code, 200, allowed.get_json())
        self.assertEqual(
            CatalogoItem.query.filter_by(tenant_id=tenant_b.id).count(),
            0,
        )
        imported = CatalogoItem.query.filter_by(tenant_id=tenant_a.id).one()
        self.assertEqual(imported.user_id, owner_a.id)
        self.assertEqual(imported.nombre, "Producto A")

    def test_catalog_import_preview_update_is_tenant_scoped(self):
        owner_a, tenant_a = self._tenant("catalog-preview-a", plan="full")
        _owner_b, tenant_b = self._tenant("catalog-preview-b", plan="full")
        upload_a = CatalogUpload(
            tenant_id=tenant_a.id,
            filename="a.csv",
            processor_slug="generic_v2",
            status="processing",
        )
        upload_b = CatalogUpload(
            tenant_id=tenant_b.id,
            filename="b.csv",
            processor_slug="generic_v2",
            status="processing",
        )
        db.session.add_all([upload_a, upload_b])
        db.session.commit()

        denied = self.client.put(
            f"/api/admin/catalog/import/{upload_b.id}",
            json={"preview_data": {"items": [{"nombre": "Cross tenant"}]}},
            headers=self._headers(owner_a, tenant_b),
        )
        allowed = self.client.put(
            f"/api/admin/catalog/import/{upload_a.id}",
            json={"preview_data": {"items": [{"nombre": "Own tenant"}]}},
            headers=self._headers(owner_a, tenant_a),
        )

        self.assertEqual(denied.status_code, 403, denied.get_json())
        self.assertEqual(denied.get_json()["codigo"], "catalog_tenant_scope_forbidden")
        self.assertEqual(allowed.status_code, 200, allowed.get_json())
        db.session.refresh(upload_b)
        self.assertEqual(upload_b.status, "processing")
        self.assertIsNone(upload_b.preview_data)

    def test_catalog_manager_permission_is_scoped_to_assigned_tenant(self):
        _owner_a, tenant_a = self._tenant("catalog-role-a", plan="full")
        _owner_b, tenant_b = self._tenant("catalog-role-b", plan="full")
        employee = self._employee(tenant_a, email_prefix="catalog-worker")
        catalog_role = Role(name="catalog_manager", description="Catalog scoped")
        db.session.add(catalog_role)
        db.session.flush()
        db.session.add(UserRole(user_id=employee.id, role_id=catalog_role.id, tenant_id=tenant_a.id))
        db.session.commit()

        self.assertTrue(can_manage_tenant_catalog(employee, tenant_a))
        self.assertFalse(can_manage_tenant_catalog(employee, tenant_b))

    def test_widget_update_is_validated_merged_and_visible_in_public_contract(self):
        owner, tenant = self._tenant("brand-runtime")
        settings = WidgetSettings(
            tenant_id=tenant.id,
            theme_config={
                "dark": {"background": "#020617", "foreground": "#f8fafc"},
                "extension": {"keep": True},
            },
        )
        db.session.add(settings)
        db.session.commit()

        response = self.client.put(
            f"/api/tenant/config?tenant_slug={tenant.slug}",
            headers=self._headers(owner, tenant),
            json={
                "theme_json": {
                    "light": {"primary": "#0f8f4f", "secondary": "#075f36"},
                    "border_radius": 18,
                    "behavior": {"position": "left", "side_offset": 24, "bottom_offset": 18},
                },
                "welcome_message": "Hola Junín",
                "welcome_subtitle": "Asistente Junín",
                "avatar_url": "https://cdn.example.com/junin.svg",
                "primary_color": "#0f8f4f",
                "secondary_color": "#075f36",
                "bottom": "18px",
                "side_offset": "24px",
                "position": "left",
                "bubble_shape": "rounded",
                "cta_messages": [{"text": "Iniciá tu consulta"}],
                "font_family": "Inter",
                "default_open": True,
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.refresh(settings)
        self.assertEqual(settings.position, "left")
        self.assertEqual(settings.cta_messages, [{"text": "Iniciá tu consulta"}])
        self.assertEqual(settings.theme_config["extension"], {"keep": True})
        self.assertEqual(settings.theme_config["dark"]["background"], "#020617")

        public_response = self.client.get(f"/api/public/tenants/{tenant.slug}/widget-config")
        self.assertEqual(public_response.status_code, 200, public_response.get_json())
        public_payload = public_response.get_json()
        self.assertEqual(public_payload["primary_color"], "#0f8f4f")
        self.assertEqual(public_payload["welcome_title"], "Hola Junín")
        self.assertEqual(public_payload["position"], "left")
        self.assertEqual(public_payload["bottom"], "18px")
        self.assertEqual(public_payload["side_offset"], "24px")
        self.assertEqual(public_payload["cta_messages"], [{"text": "Iniciá tu consulta"}])
        self.assertEqual(public_payload["widget"]["attributes"]["data-left"], "24px")
        self.assertEqual(public_payload["widget"]["attributes"]["data-bottom"], "18px")

    def test_invalid_widget_payload_fails_without_mutating_settings(self):
        owner, tenant = self._tenant("brand-validation")
        headers = self._headers(owner, tenant)

        response = self.client.put(
            f"/api/tenant/config?tenant_slug={tenant.slug}",
            headers=headers,
            json={
                "avatar_url": "data:image/png;base64,not-allowed",
                "primary_color": "green",
                "position": "center",
                "default_open": "false",
            },
        )

        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "invalid_widget_config")
        self.assertEqual(
            set(response.get_json()["fields"]),
            {"avatar_url", "default_open", "position", "primary_color"},
        )
        self.assertIsNone(WidgetSettings.query.filter_by(tenant_id=tenant.id).first())


if __name__ == "__main__":
    unittest.main()
