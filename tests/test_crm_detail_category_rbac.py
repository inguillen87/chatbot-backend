import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import (
    ArchivoAdjunto,
    CategoriaTicket,
    MunicipioTicket,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    TicketComentario,
    User,
)
from services.government_pipeline import load_incidents_for_municipio
from services.municipal_stats import build_stats_for_municipio
from services.ticket_service import servicio_tickets


_LEGACY_CRM_PATH = Path(__file__).resolve().parents[1] / "routes" / "crm.py"
_LEGACY_CRM_SPEC = importlib.util.spec_from_file_location(
    "legacy_crm_routes_for_category_rbac_tests",
    _LEGACY_CRM_PATH,
)
assert _LEGACY_CRM_SPEC is not None and _LEGACY_CRM_SPEC.loader is not None
_LEGACY_CRM_MODULE = importlib.util.module_from_spec(_LEGACY_CRM_SPEC)
_LEGACY_CRM_SPEC.loader.exec_module(_LEGACY_CRM_MODULE)


class CrmDetailCategoryRbacConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class CrmDetailCategoryRbacTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(CrmDetailCategoryRbacConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = self._user("Admin", "admin@rbac.test", "admin", "rbac")
        self.tenant = TenantProfile(
            slug="rbac",
            nombre="Tenant RBAC",
            tipo="municipio",
            municipio_id=self.admin.id,
            plan="full",
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.admin.tenant_id = self.tenant.id

        self.employee = self._user(
            "Empleado",
            "employee@rbac.test",
            "empleado",
            self.tenant.slug,
            tenant_id=self.tenant.id,
            accesibilidad={"employee_scope": {"categorias": ["tenant-allowed"]}},
            ticket_categorias="pyme-allowed",
        )
        related_category = CategoriaTicket(
            nombre="categoria-relacionada",
            tenant_id=self.tenant.id,
        )
        db.session.add(related_category)
        db.session.flush()
        self.employee.categorias_ticket = [related_category]

        self.allowed_tenant = self._tenant_ticket(" TENANT-ALLOWED ")
        self.allowed_municipio = self._municipio_ticket(
            "nombre-que-no-coincide",
            category_id=related_category.id,
        )
        self.allowed_pyme = self._pyme_ticket("PYME-ALLOWED")

        self.restricted_tenant = self._tenant_ticket("restricted")
        self.restricted_municipio = self._municipio_ticket("restricted")
        self.restricted_pyme = self._pyme_ticket("restricted")
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _user(self, name, email, role, tenant_slug, **kwargs):
        if role == "empleado":
            kwargs.setdefault("es_empleado", True)
        user = User(
            name=name,
            email=email,
            password_hash="test-hash",
            rol=role,
            tenant_slug=tenant_slug,
            **kwargs,
        )
        db.session.add(user)
        db.session.flush()
        return user

    def _auth(self, actor=None):
        actor = actor or self.employee
        token = jwt.encode(
            {
                "user_id": actor.id,
                "rol": actor.rol,
                "tenant_slug": actor.tenant_slug,
                "tenant_id": actor.tenant_id,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": self.tenant.slug,
        }

    def _tenant_ticket(self, category):
        ticket = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.admin.id,
            categoria=category,
            descripcion=f"Tenant {category}",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={"title": f"Tenant {category}"},
        )
        db.session.add(ticket)
        db.session.flush()
        return ticket

    def _municipio_ticket(self, category, *, category_id=None):
        ticket = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.admin.id,
            pregunta=f"Municipio {category}",
            asunto=f"Municipio {category}",
            categoria=category,
            categoria_id=category_id,
            estado="nuevo",
            asignado_a_id=self.employee.id,
            nro_ticket=f"mun-rbac-{category}-{category_id or 0}",
            canal_ingreso="whatsapp",
        )
        db.session.add(ticket)
        db.session.flush()
        return ticket

    def _pyme_ticket(self, category):
        ticket = PymeTicket(
            tenant_id=self.tenant.id,
            pregunta=f"Pyme {category}",
            asunto=f"Pyme {category}",
            categoria=category,
            estado="nuevo",
            asignado_a_id=self.employee.id,
            nro_ticket=10_000 + self._next_pyme_number(),
        )
        db.session.add(ticket)
        db.session.flush()
        return ticket

    def _next_pyme_number(self):
        return int(PymeTicket.query.count()) + 1

    def _detail_urls(self, tenant_ticket, municipio_ticket, pyme_ticket):
        return {
            "TenantTicket": f"/api/v2/tickets/{tenant_ticket.id}",
            "MunicipioTicket": (
                f"/api/v2/inbox/omnichannel/{municipio_ticket.id}"
                "?source_model=MunicipioTicket"
            ),
            "PymeTicket": f"/api/tickets/pyme/{pyme_ticket.id}",
        }

    def test_three_queue_detail_endpoints_accept_the_same_employee_scope_union(self):
        urls = self._detail_urls(
            self.allowed_tenant,
            self.allowed_municipio,
            self.allowed_pyme,
        )

        for source_model, url in urls.items():
            with self.subTest(source_model=source_model):
                response = self.client.get(url, headers=self._auth())
                self.assertEqual(response.status_code, 200, response.get_json())

        # Every source table starts at id=1. Explicit source routing must not
        # confuse colliding identifiers while applying category authorization.
        self.assertEqual(
            {
                self.allowed_tenant.id,
                self.allowed_municipio.id,
                self.allowed_pyme.id,
            },
            {1},
        )

    def test_out_of_scope_ids_fail_as_not_found_without_existence_or_source_leak(self):
        restricted_urls = self._detail_urls(
            self.restricted_tenant,
            self.restricted_municipio,
            self.restricted_pyme,
        )
        missing_urls = self._detail_urls(
            type("Missing", (), {"id": 999_991})(),
            type("Missing", (), {"id": 999_992})(),
            type("Missing", (), {"id": 999_993})(),
        )

        for source_model, url in restricted_urls.items():
            with self.subTest(source_model=source_model):
                denied = self.client.get(url, headers=self._auth())
                missing = self.client.get(missing_urls[source_model], headers=self._auth())
                self.assertEqual(denied.status_code, 404)
                self.assertEqual(missing.status_code, 404)
                denied_body = denied.get_json() or {}
                missing_body = missing.get_json() or {}
                denied_body.pop("request_id", None)
                missing_body.pop("request_id", None)
                self.assertEqual(denied_body, missing_body)
                self.assertNotIn("restricted", str(denied_body).lower())

    def test_employee_without_any_category_scope_cannot_open_known_ticket_ids(self):
        self.employee.accesibilidad = {"employee_scope": {"categorias": []}}
        self.employee.ticket_categorias = None
        self.employee.categorias_ticket = []
        db.session.commit()

        urls = self._detail_urls(
            self.allowed_tenant,
            self.allowed_municipio,
            self.allowed_pyme,
        )
        for source_model, url in urls.items():
            with self.subTest(source_model=source_model):
                response = self.client.get(url, headers=self._auth())
                self.assertEqual(response.status_code, 404)

    def test_malformed_configured_scope_fails_closed(self):
        self.employee.accesibilidad = {"employee_scope": "invalid"}
        self.employee.ticket_categorias = None
        self.employee.categorias_ticket = []
        db.session.commit()

        urls = self._detail_urls(
            self.allowed_tenant,
            self.allowed_municipio,
            self.allowed_pyme,
        )
        for source_model, url in urls.items():
            with self.subTest(source_model=source_model):
                response = self.client.get(url, headers=self._auth())
                self.assertEqual(response.status_code, 404)

    def test_legacy_employee_without_scope_configuration_fails_closed(self):
        self.employee.accesibilidad = None
        self.employee.ticket_categorias = None
        self.employee.categorias_ticket = []
        db.session.commit()

        urls = self._detail_urls(
            self.allowed_tenant,
            self.allowed_municipio,
            self.allowed_pyme,
        )
        for source_model, url in urls.items():
            with self.subTest(source_model=source_model):
                response = self.client.get(url, headers=self._auth())
                self.assertEqual(response.status_code, 404)

    def test_tenant_admin_keeps_tenant_wide_detail_access(self):
        urls = self._detail_urls(
            self.restricted_tenant,
            self.restricted_municipio,
            self.restricted_pyme,
        )
        for source_model, url in urls.items():
            with self.subTest(source_model=source_model):
                response = self.client.get(url, headers=self._auth(self.admin))
                self.assertEqual(response.status_code, 200, response.get_json())

    def test_employee_lists_only_category_scoped_tickets_across_crm_surfaces(self):
        v2_response = self.client.get(
            "/api/v2/tickets?per_page=100",
            headers=self._auth(),
        )
        self.assertEqual(v2_response.status_code, 200, v2_response.get_json())
        v2_items = v2_response.get_json()["items"]
        self.assertEqual(
            {item["id"] for item in v2_items},
            {self.allowed_tenant.id},
        )

        inbox_response = self.client.get(
            "/api/v2/inbox/omnichannel?limit=200",
            headers=self._auth(),
        )
        self.assertEqual(inbox_response.status_code, 200, inbox_response.get_json())
        inbox_items = inbox_response.get_json()["items"]
        self.assertNotIn("restricted", str(inbox_items).lower())
        self.assertIn("tenant-allowed", str(inbox_items).lower())
        self.assertTrue(
            any(
                str(item.get("source_model") or "").lower() == "municipioticket"
                and item.get("legacy_id") == self.allowed_municipio.id
                for item in inbox_items
            )
        )

    def test_tenant_patch_and_events_are_fail_closed_outside_employee_category(self):
        original_status = self.restricted_tenant.estado
        patch_response = self.client.patch(
            f"/api/v2/tickets/{self.restricted_tenant.id}",
            json={"status": "in_progress"},
            headers=self._auth(),
        )
        events_response = self.client.get(
            f"/api/v2/tickets/{self.restricted_tenant.id}/events",
            headers=self._auth(),
        )

        self.assertEqual(patch_response.status_code, 404, patch_response.get_json())
        self.assertEqual(events_response.status_code, 404, events_response.get_json())
        db.session.expire_all()
        self.assertEqual(
            db.session.get(TenantTicket, self.restricted_tenant.id).estado,
            original_status,
        )

    def test_employee_cannot_recategorize_ticket_outside_their_scope(self):
        response = self.client.patch(
            f"/api/v2/tickets/{self.allowed_tenant.id}",
            json={"category": "restricted"},
            headers=self._auth(),
        )
        self.assertEqual(response.status_code, 404, response.get_json())
        db.session.expire_all()
        self.assertEqual(
            db.session.get(TenantTicket, self.allowed_tenant.id).categoria,
            " TENANT-ALLOWED ",
        )

    def test_omnichannel_actions_do_not_mutate_out_of_scope_sources(self):
        tenant_response = self.client.post(
            f"/api/v2/inbox/omnichannel/{self.restricted_tenant.id}/actions",
            json={
                "action": "set_priority",
                "source_model": "TenantTicket",
                "ticket_id": self.restricted_tenant.id,
                "priority": "high",
            },
            headers=self._auth(),
        )
        municipio_response = self.client.post(
            f"/api/v2/inbox/omnichannel/{self.restricted_municipio.id}/actions",
            json={"action": "close", "source_model": "MunicipioTicket"},
            headers=self._auth(),
        )

        self.assertEqual(tenant_response.status_code, 404, tenant_response.get_json())
        self.assertEqual(municipio_response.status_code, 404, municipio_response.get_json())
        db.session.expire_all()
        tenant_ticket = db.session.get(TenantTicket, self.restricted_tenant.id)
        municipio_ticket = db.session.get(MunicipioTicket, self.restricted_municipio.id)
        self.assertNotEqual((tenant_ticket.datos_extra or {}).get("priority"), "high")
        self.assertEqual(municipio_ticket.estado, "nuevo")

    def test_legacy_mutations_fail_closed_outside_employee_category(self):
        mutation_cases = [
            (
                "post",
                f"/api/tickets/municipio/{self.restricted_municipio.id}/asignar",
                {},
            ),
            (
                "post",
                f"/api/tickets/pyme/{self.restricted_pyme.id}/responder",
                {"comentario": "No debe persistirse"},
            ),
            (
                "put",
                f"/api/tickets/municipio/{self.restricted_municipio.id}/estado",
                {"estado": "cerrado"},
            ),
        ]
        for method, url, payload in mutation_cases:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(
                    url,
                    json=payload,
                    headers=self._auth(),
                )
                self.assertEqual(response.status_code, 404, response.get_json())

        db.session.expire_all()
        self.assertEqual(
            db.session.get(MunicipioTicket, self.restricted_municipio.id).estado,
            "nuevo",
        )

    def test_category_scoped_employee_can_open_unassigned_pyme_queue_detail(self):
        unassigned = self._pyme_ticket("pyme-allowed")
        unassigned.asignado_a_id = None
        db.session.commit()

        response = self.client.get(
            f"/api/tickets/pyme/{unassigned.id}",
            headers=self._auth(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_assignment_rejects_agents_without_ticket_category_scope(self):
        incompatible = self._user(
            "Empleado incompatible",
            "incompatible@rbac.test",
            "empleado",
            self.tenant.slug,
            tenant_id=self.tenant.id,
            accesibilidad={"employee_scope": {"categorias": ["restricted"]}},
            ticket_categorias="restricted",
        )
        db.session.commit()

        cases = [
            (
                "patch",
                f"/api/v2/tickets/{self.allowed_tenant.id}",
                {"assignee_id": incompatible.id, "expected_assignee_id": None},
                self._auth(self.admin),
            ),
            (
                "post",
                f"/api/v2/inbox/omnichannel/{self.allowed_tenant.id}/actions",
                {
                    "action": "assign",
                    "source_model": "TenantTicket",
                    "assignee_id": incompatible.id,
                    "expected_assignee_id": None,
                },
                self._auth(self.admin),
            ),
            (
                "post",
                f"/api/v2/inbox/omnichannel/{self.allowed_municipio.id}/actions",
                {
                    "action": "assign",
                    "source_model": "MunicipioTicket",
                    "assignee_id": incompatible.id,
                    "expected_assignee_id": self.employee.id,
                },
                self._auth(self.admin),
            ),
            (
                "post",
                f"/api/tickets/municipio/{self.allowed_municipio.id}/asignar",
                {"user_id": incompatible.id},
                self._auth(self.admin),
            ),
            (
                "post",
                f"/api/tickets/pyme/{self.allowed_pyme.id}/asignar",
                {"user_id": incompatible.id},
                self._auth(self.admin),
            ),
        ]

        for method, url, payload, headers in cases:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, json=payload, headers=headers)
                self.assertIn(response.status_code, {400, 409}, response.get_json())
                self.assertIn("category_scope_mismatch", str(response.get_json()))

        db.session.expire_all()
        tenant_extra = db.session.get(TenantTicket, self.allowed_tenant.id).datos_extra or {}
        self.assertIsNone(tenant_extra.get("assignee_id"))
        self.assertEqual(
            db.session.get(MunicipioTicket, self.allowed_municipio.id).asignado_a_id,
            self.employee.id,
        )
        self.assertEqual(
            db.session.get(PymeTicket, self.allowed_pyme.id).asignado_a_id,
            self.employee.id,
        )

    def test_assignment_accepts_agent_with_matching_category_scope(self):
        response = self.client.patch(
            f"/api/v2/tickets/{self.allowed_tenant.id}",
            json={"assignee_id": self.employee.id, "expected_assignee_id": None},
            headers=self._auth(self.admin),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.expire_all()
        self.assertEqual(
            (db.session.get(TenantTicket, self.allowed_tenant.id).datos_extra or {}).get("assignee_id"),
            self.employee.id,
        )

    def test_create_and_recategorization_validate_final_assignee_scope_atomically(self):
        incompatible = self._user(
            "Empleado incompatible atomico",
            "incompatible-atomic@rbac.test",
            "empleado",
            self.tenant.slug,
            tenant_id=self.tenant.id,
            accesibilidad={"employee_scope": {"categorias": ["restricted"]}},
            ticket_categorias="restricted",
        )
        db.session.commit()

        create_response = self.client.post(
            "/api/v2/tickets",
            json={
                "title": "Asignacion atomica",
                "description": "No debe crearse",
                "category": "tenant-allowed",
                "assignee_id": incompatible.id,
                "expected_assignee_id": None,
            },
            headers=self._auth(self.admin),
        )
        self.assertEqual(create_response.status_code, 409, create_response.get_json())
        self.assertEqual(create_response.get_json().get("reason_code"), "assignee_category_scope_mismatch")

        self.employee.accesibilidad = {
            "employee_scope": {"categorias": ["tenant-allowed", "new-allowed"]}
        }
        existing_extra = dict(self.allowed_tenant.datos_extra or {})
        existing_extra["assignee_id"] = incompatible.id
        self.allowed_tenant.datos_extra = existing_extra
        db.session.commit()

        recategorize_response = self.client.patch(
            f"/api/v2/tickets/{self.allowed_tenant.id}",
            json={"category": "new-allowed"},
            headers=self._auth(self.admin),
        )
        self.assertEqual(recategorize_response.status_code, 409, recategorize_response.get_json())
        self.assertEqual(
            recategorize_response.get_json().get("reason_code"),
            "assignee_category_scope_mismatch",
        )
        db.session.expire_all()
        self.assertEqual(
            db.session.get(TenantTicket, self.allowed_tenant.id).categoria,
            " TENANT-ALLOWED ",
        )

    def test_employee_create_requires_unambiguous_category_and_rejects_forbidden_category(self):
        omitted = self.client.post(
            "/api/v2/tickets",
            json={"title": "Triage", "description": "Pendiente de clasificacion"},
            headers=self._auth(),
        )
        forbidden = self.client.post(
            "/api/v2/tickets",
            json={"title": "Fuera de alcance", "description": "Intento fuera de alcance", "category": "restricted"},
            headers=self._auth(),
        )

        self.assertEqual(omitted.status_code, 400, omitted.get_json())
        self.assertEqual(omitted.get_json().get("reason_code"), "category_required")
        self.assertEqual(forbidden.status_code, 404, forbidden.get_json())

    def test_send_history_is_category_scoped_before_email_side_effects(self):
        with patch(
            "services.email_service.validar_configuracion_smtp",
            return_value=(True, None),
        ), patch(
            "services.email_service.enviar_email_con_multiples_adjuntos",
            return_value=True,
        ) as send_email:
            allowed = self.client.post(
                f"/api/tickets/pyme/{self.allowed_pyme.id}/send-history",
                headers=self._auth(),
            )
            self.assertEqual(allowed.status_code, 200, allowed.get_json())
            self.assertEqual(send_email.call_count, 1)

            restricted = self.client.post(
                f"/api/tickets/pyme/{self.restricted_pyme.id}/send-history",
                headers=self._auth(),
            )
            missing = self.client.post(
                "/api/tickets/pyme/999991/send-history",
                headers=self._auth(),
            )

            self.assertEqual(restricted.status_code, 404, restricted.get_json())
            self.assertEqual(missing.status_code, 404, missing.get_json())
            self.assertEqual(send_email.call_count, 1)

    def test_attachment_surfaces_scope_before_storage_or_comment_side_effects(self):
        allowed_attachment = ArchivoAdjunto(
            user_id=self.admin.id,
            session_id="shared-category-session",
            filename="allowed-category.txt",
            nombre_original="allowed-category.txt",
            mime="text/plain",
            tamano=7,
            tipo="chat",
            municipio_ticket_id=self.allowed_municipio.id,
            url="https://files.test/allowed-category.txt",
        )
        restricted_attachment = ArchivoAdjunto(
            user_id=self.admin.id,
            session_id="shared-category-session",
            filename="restricted-category.txt",
            nombre_original="restricted-category.txt",
            mime="text/plain",
            tamano=10,
            tipo="chat",
            municipio_ticket_id=self.restricted_municipio.id,
            url="https://files.test/restricted-category.txt",
        )
        allowed_pyme_attachment = ArchivoAdjunto(
            user_id=self.admin.id,
            session_id="shared-category-session",
            filename="allowed-pyme-category.txt",
            nombre_original="allowed-pyme-category.txt",
            mime="text/plain",
            tamano=7,
            tipo="chat",
            pyme_ticket_id=self.allowed_pyme.id,
            url="https://files.test/allowed-pyme-category.txt",
        )
        restricted_pyme_attachment = ArchivoAdjunto(
            user_id=self.admin.id,
            session_id="shared-category-session",
            filename="restricted-pyme-category.txt",
            nombre_original="restricted-pyme-category.txt",
            mime="text/plain",
            tamano=10,
            tipo="chat",
            pyme_ticket_id=self.restricted_pyme.id,
            url="https://files.test/restricted-pyme-category.txt",
        )
        db.session.add_all(
            [
                allowed_attachment,
                restricted_attachment,
                allowed_pyme_attachment,
                restricted_pyme_attachment,
            ]
        )
        db.session.commit()

        session_response = self.client.get(
            "/archivos/sesion/shared-category-session",
            headers=self._auth(),
        )
        self.assertEqual(session_response.status_code, 200, session_response.get_json())
        self.assertEqual(
            {item["nombre"] for item in session_response.get_json()},
            {"allowed-category.txt", "allowed-pyme-category.txt"},
        )

        with patch("routes.archivos.storage.Client") as storage_client:
            restricted_download = self.client.get(
                "/archivos/restricted-category.txt",
                headers=self._auth(),
            )
        self.assertEqual(restricted_download.status_code, 404, restricted_download.get_json())
        storage_client.assert_not_called()

        with patch("routes.archivos.storage.Client") as storage_client:
            blob = storage_client.return_value.bucket.return_value.blob.return_value
            blob.exists.return_value = True
            blob.download_as_bytes.return_value = b"allowed"
            allowed_download = self.client.get(
                "/archivos/allowed-category.txt",
                headers=self._auth(),
            )
        self.assertEqual(allowed_download.status_code, 200)
        self.assertEqual(allowed_download.data, b"allowed")

        comments_before = TicketComentario.query.count()
        attachments_before = ArchivoAdjunto.query.count()
        with patch("routes.archivos.upload_to_gcs") as upload_to_gcs:
            restricted_upload = self.client.post(
                "/archivos/subir",
                headers=self._auth(),
                data={
                    "archivo": (BytesIO(b"restricted"), "restricted.png"),
                    "municipio_ticket_id": str(self.restricted_municipio.id),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(restricted_upload.status_code, 404, restricted_upload.get_json())
        upload_to_gcs.assert_not_called()

        with patch("routes.archivos.guardar_archivo_adjunto_ticket") as save_attachment:
            restricted_admin_upload = self.client.post(
                "/archivos/subir_admin",
                headers=self._auth(),
                data={
                    "archivo": (BytesIO(b"restricted"), "restricted.png"),
                    "ticket_id": str(self.restricted_pyme.id),
                    "tipo_ticket": "pyme",
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(
            restricted_admin_upload.status_code,
            404,
            restricted_admin_upload.get_json(),
        )
        save_attachment.assert_not_called()
        self.assertEqual(TicketComentario.query.count(), comments_before)
        self.assertEqual(ArchivoAdjunto.query.count(), attachments_before)

    def test_crm_ticket_history_and_analytics_are_employee_category_scoped(self):
        self.app.register_blueprint(
            _LEGACY_CRM_MODULE.crm_bp,
            name="legacy_crm_category_scope",
            url_prefix="/legacy-crm",
        )
        client_user = self._user(
            "Cliente RBAC",
            "client@rbac.test",
            "usuario",
            self.tenant.slug,
            tenant_id=self.tenant.id,
        )
        for ticket in (
            self.allowed_municipio,
            self.restricted_municipio,
            self.allowed_pyme,
            self.restricted_pyme,
        ):
            ticket.user_id = client_user.id
        self.restricted_municipio.estado = "cerrado"
        self.restricted_pyme.estado = "cerrado"

        allowed_comment = TicketComentario(
            municipio_ticket_id=self.allowed_municipio.id,
            comentario="allowed-comment-marker",
            user_id=self.admin.id,
            es_admin=True,
        )
        restricted_comment = TicketComentario(
            municipio_ticket_id=self.restricted_municipio.id,
            comentario="restricted-comment-marker",
            user_id=self.admin.id,
            es_admin=True,
        )
        allowed_attachment = ArchivoAdjunto(
            user_id=client_user.id,
            filename="crm-allowed.txt",
            nombre_original="crm-allowed.txt",
            mime="text/plain",
            tamano=1,
            tipo="ticket",
            municipio_ticket_id=self.allowed_municipio.id,
            url="https://files.test/crm-allowed.txt",
        )
        restricted_attachment = ArchivoAdjunto(
            user_id=client_user.id,
            filename="crm-restricted.txt",
            nombre_original="crm-restricted.txt",
            mime="text/plain",
            tamano=1,
            tipo="ticket",
            municipio_ticket_id=self.restricted_municipio.id,
            url="https://files.test/crm-restricted.txt",
        )
        db.session.add_all(
            [
                allowed_comment,
                restricted_comment,
                allowed_attachment,
                restricted_attachment,
            ]
        )
        db.session.commit()

        interactions = self.client.get(
            f"/legacy-crm/clientes/{client_user.id}/interacciones",
            headers=self._auth(),
        )
        self.assertEqual(interactions.status_code, 200, interactions.get_json())
        interaction_refs = {
            (item.get("tipo"), item.get("id"))
            for item in interactions.get_json()
            if item.get("tipo", "").startswith("ticket_")
        }
        self.assertEqual(
            interaction_refs,
            {
                ("ticket_municipio", self.allowed_municipio.id),
                ("ticket_pyme", self.allowed_pyme.id),
            },
        )

        history = self.client.get(
            f"/legacy-crm/clientes/{client_user.id}/historial",
            headers=self._auth(),
        )
        self.assertEqual(history.status_code, 200, history.get_json())
        history_payload = history.get_json()
        history_refs = {
            (item.get("tipo"), item.get("id"))
            for item in history_payload["tickets"]
        }
        self.assertEqual(
            history_refs,
            {
                ("municipio", self.allowed_municipio.id),
                ("pyme", self.allowed_pyme.id),
            },
        )
        serialized_history = str(history_payload).lower()
        self.assertIn("allowed-comment-marker", serialized_history)
        self.assertIn("crm-allowed.txt", serialized_history)
        self.assertNotIn("restricted-comment-marker", serialized_history)
        self.assertNotIn("crm-restricted.txt", serialized_history)

        analytics = self.client.get("/legacy-crm/analytics", headers=self._auth())
        self.assertEqual(analytics.status_code, 200, analytics.get_json())
        analytics_payload = analytics.get_json()
        self.assertEqual(analytics_payload["tickets_abiertos"], 2)
        self.assertEqual(analytics_payload["tickets_cerrados"], 0)
        self.assertEqual(TicketComentario.query.count(), 2)
        self.assertEqual(ArchivoAdjunto.query.count(), 2)

    def test_closed_restricted_reply_does_not_leak_ticket_status(self):
        self.restricted_pyme.estado = "cerrado"
        db.session.commit()

        denied = self.client.post(
            f"/api/tickets/pyme/{self.restricted_pyme.id}/responder",
            json={"comentario": "No debe persistirse"},
            headers=self._auth(),
        )
        missing = self.client.post(
            "/api/tickets/pyme/999992/responder",
            json={"comentario": "No debe persistirse"},
            headers=self._auth(),
        )

        self.assertEqual(denied.status_code, 404, denied.get_json())
        self.assertEqual(missing.status_code, 404, missing.get_json())

    def test_geo_and_government_pipelines_only_receive_employee_categories(self):
        self.allowed_municipio.latitud = -34.6001
        self.allowed_municipio.longitud = -58.4001
        self.restricted_municipio.latitud = -35.7002
        self.restricted_municipio.longitud = -59.5002
        db.session.commit()

        incidents = load_incidents_for_municipio(
            self.admin.id,
            tenant_id=self.tenant.id,
            actor=self.employee,
        )
        self.assertEqual({record.id for record in incidents}, {self.allowed_municipio.id})

        stats = build_stats_for_municipio(self.admin.id, actor=self.employee)
        self.assertEqual((stats.get("resumen") or {}).get("total"), 1)
        self.assertNotIn("restricted", str(stats).lower())

        locations = servicio_tickets.obtener_locations_de_tickets(
            municipio_id=self.admin.id,
            actor=self.employee,
        )
        self.assertEqual(locations, [{"lat": -34.6001, "lng": -58.4001}])

        heatmap = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
            tipo_ticket="municipio",
            municipio_id=self.admin.id,
            tenant_id=self.tenant.id,
            actor=self.employee,
        )
        serialized = str(heatmap).lower()
        self.assertIn("nombre-que-no-coincide", serialized)
        self.assertNotIn("restricted", serialized)
        self.assertNotIn("-35.7002", serialized)
        self.assertEqual(
            heatmap[0].get("categoria_id"),
            self.allowed_municipio.categoria_id,
        )
