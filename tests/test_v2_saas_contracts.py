import os
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config
from models import (
    CatalogoItem,
    EncEncuesta,
    EncRespuesta,
    MunicipioTicket,
    MunicipioPost,
    Notification,
    NotificationTemplate,
    PedidoConversacional,
    MessageTemplateRegistry,
    MessagingEventLedger,
    Promocion,
    ProviderConnection,
    ProviderSender,
    TenantConfig,
    TenantProfile,
    TenantTicket,
    User,
    WhatsAppContactState,
    WhatsAppEnterpriseRule,
)


class V2SaasTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class _FakeTwilioResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class V2SaasContractsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2SaasTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(name="Owner SaaS", email="owner-saas@test.com", rol="admin", tenant_slug="saas-tenant")
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="saas-tenant",
            nombre="SaaS Tenant",
            tipo="pyme",
            vertical="educacion",
            subvertical="colegio",
            plan="full",
            pyme_id=self.owner.id,
            configuracion={"widget_tokens": ["widget-saas"], "mercadopago_access_token": "mp-token"},
            whatsapp_sender_id="whatsapp:+100",
        )
        db.session.add(self.tenant)
        db.session.commit()
        self.owner.tenant_id = self.tenant.id
        db.session.add(self.owner)

        self.employee = User(
            name="Mesa de entrada",
            email="mesa@test.com",
            rol="empleado",
            tenant_slug=self.tenant.slug,
            tenant_id=self.tenant.id,
            es_empleado=True,
            accesibilidad={
                "employee_scope": {
                    "categorias": ["educacion"],
                    "zonas": ["centro"],
                    "permisos": ["tickets_assign"],
                    "channels": ["whatsapp"],
                }
            },
        )
        self.employee.set_password("secret123")
        db.session.add(self.employee)
        db.session.flush()

        self.ticket = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            categoria="educacion",
            descripcion="Consulta por beca",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={
                "title": "Consulta por beca",
                "priority": "high",
                "assignee_id": self.employee.id,
                "assignee_name": self.employee.name,
                "assignee_email": self.employee.email,
                "conversation_id": "conv-beca-1",
                "demo_session_id": "demo-beca-1",
                "widget_id": "landing-widget",
                "contact_key": "whatsapp:+5491111111111",
                "intent": "consulta_beca",
                "contact": {"name": "Familia Gomez", "phone": "+5491111111111"},
                "zone": "centro",
                "channel": "whatsapp",
                "attachments": [
                    {
                        "id": "att-1",
                        "name": "comprobante.jpg",
                        "url": "https://cdn.example.com/comprobante.jpg",
                        "mimeType": "image/jpeg",
                    }
                ],
                "comments": [{"id": 1, "body": "Hola", "visibility": "public", "created_at": "2026-05-01T12:00:00Z"}],
            },
            latitud=-34.6,
            longitud=-58.4,
        )
        db.session.add(self.ticket)

        template = NotificationTemplate(
            tenant_id=self.tenant.id,
            key="ticket_created",
            channel="email",
            subject_template="Nuevo ticket",
            body_template="Ticket {{id}}",
        )
        db.session.add(template)
        db.session.add(
            Notification(
                tenant_id=self.tenant.id,
                user_id=self.owner.id,
                template_id=template.id,
                channel="email",
                recipient="ops@test.com",
                subject="Nuevo ticket",
                body="Ticket creado",
                status="sent",
                idempotency_key="notif-1",
            )
        )
        db.session.add(
            CatalogoItem(
                user_id=self.owner.id,
                tenant_id=self.tenant.id,
                nombre="Uniforme escolar",
                descripcion="Chomba institucional",
                categoria="indumentaria",
                imagen_url="https://cdn.example.com/uniforme.jpg",
                promocion_info="10% off familia",
            )
        )
        db.session.add(
            PedidoConversacional(
                tenant_id=self.tenant.id,
                user_id=self.owner.id,
                estado="preparando",
                origen="whatsapp",
                monto_monetario=1500,
                items=[{"nombre": "Uniforme escolar", "cantidad": 1}],
            )
        )
        db.session.add(
            MunicipioPost(
                municipio_id=self.owner.id,
                tipo_post="noticia",
                titulo="Reunion de familias",
                descripcion="Comunicado para familias del colegio.",
            )
        )
        db.session.add(
            Promocion(
                pyme_user_id=self.owner.id,
                nombre_promocion="Promo vuelta a clases",
                descripcion_publica="Descuento en uniformes.",
                tipo_promocion="TOTAL_CARRITO_DESCUENTO_PORCENTAJE",
                valor_descuento=10,
            )
        )
        db.session.add(
            TenantConfig(
                tenant_id=self.tenant.id,
                key="links",
                channel="whatsapp",
                json_value={"links": [{"label": "Portal familias", "url": "https://demo.chatboc.ar/familias"}]},
            )
        )
        db.session.add(
            WhatsAppEnterpriseRule(
                tenant_id=self.tenant.id,
                enforce_template_outside_24h=True,
                max_outbound_per_hour=200,
                quiet_hours_start=22,
                quiet_hours_end=7,
                blocked_keywords=["spam"],
            )
        )
        db.session.add(
            MessageTemplateRegistry(
                tenant_id=self.tenant.id,
                provider="twilio",
                channel="whatsapp",
                name="chatboc_welcome_menu_v2",
                language="es",
                category="UTILITY",
                status="approved",
                content_sid="HXwelcomev2",
                body_preview="Hola {{1}}, soy {{2}}. Te ayudo por WhatsApp con reclamos, pedidos y pagos.",
                components=[{"type": "quick_reply", "actions": ["Crear caso", "Pagar o pedir", "Hablar equipo"]}],
            )
        )
        db.session.add(
            MessageTemplateRegistry(
                tenant_id=self.tenant.id,
                provider="twilio",
                channel="whatsapp",
                name="chatboc_school_payment_due_v2",
                language="es",
                category="UTILITY",
                status="approved",
                content_sid="HXschoolpayv2",
                body_preview="Hola {{1}}, tenes una cuota pendiente de {{2}}.",
                components=[{"type": "call_to_action", "title": "Pagar cuota"}],
            )
        )
        db.session.add(
            MessageTemplateRegistry(
                tenant_id=self.tenant.id,
                provider="twilio",
                channel="whatsapp",
                name="gobiernos_reclamo_sla",
                language="es",
                category="UTILITY",
                status="approved",
                content_sid="HXgovsla",
                body_preview="Confirmamos tu reclamo municipal y te mostramos las acciones disponibles.",
                components=[{"type": "quick_reply", "actions": ["Confirmar", "Editar", "Cancelar"]}],
            )
        )
        db.session.add(
            WhatsAppContactState(
                tenant_id=self.tenant.id,
                recipient="whatsapp:+5491111111111",
                last_inbound_at=datetime.now(timezone.utc),
            )
        )
        encuesta = EncEncuesta(
            tenant_id=self.tenant.id,
            slug="voto-saas",
            titulo="Votacion comedor",
            tipo="votacion",
            estado="publicada",
            es_votacion_envivo=True,
            mostrar_resultados_envivo=True,
        )
        db.session.add(encuesta)
        db.session.flush()
        db.session.add(EncRespuesta(encuesta_id=encuesta.id, tenant_id=self.tenant.id, canal="widget"))

        self.super_admin = User(name="Super", email="super@test.com", rol="super_admin")
        self.super_admin.set_password("secret123")
        db.session.add(self.super_admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self, user: User):
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": getattr(user, "tenant_slug", None),
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug}

    def test_employee_coverage_contract(self):
        response = self.client.get(
            "/api/v2/employee-coverage",
            headers={**self._auth(self.owner), "X-Request-Id": "coverage-1"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "employee.coverage.v1")
        self.assertEqual(payload.get("request_id"), "coverage-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertGreaterEqual(payload["summary"]["coverage_rate"], 0)
        self.assertTrue(payload["employees"])
        self.assertIn("educacion", payload["coverage"]["categorias"])

    def test_employee_coverage_exposes_setup_dimensions_without_tickets(self):
        owner = User(name="Owner Municipio", email="owner-municipio@test.com", rol="admin", tenant_slug="muni-empty")
        owner.set_password("secret123")
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(
            slug="muni-empty",
            nombre="Municipio Empty",
            tipo="municipio",
            municipio_id=owner.id,
            configuracion={},
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        db.session.commit()

        headers = {**self._auth(owner), "X-Tenant-Slug": tenant.slug, "X-Request-Id": "coverage-empty-1"}
        response = self.client.get(
            f"/api/v2/tenants/{tenant.slug}/employee-coverage",
            headers=headers,
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("request_id"), "coverage-empty-1")
        self.assertGreater(payload["summary"]["total_dimensions"], 0)
        self.assertTrue(payload["coverage"]["categorias"])
        self.assertIn("whatsapp", payload["coverage"]["channels"])
        self.assertIn("web", payload["coverage"]["channels"])
        self.assertIn("municipio_baseline_taxonomy", payload["coverage"]["dimension_sources"]["categorias"])
        self.assertTrue(any(alert["reason_code"] == "no_employees" for alert in payload["alerts"]))

    def test_employee_routing_includes_legacy_municipio_tickets_by_owner(self):
        owner = User(name="Owner Municipio Legacy", email="owner-muni-legacy@test.com", rol="admin", tipo_chat="municipio", tenant_slug="muni-legacy")
        owner.set_password("secret123")
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(
            slug="muni-legacy",
            nombre="Municipio Legacy",
            tipo="municipio",
            municipio_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id

        employee = User(
            name="Alumbrado",
            email="alumbrado@test.com",
            rol="empleado",
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            es_empleado=True,
            accesibilidad={
                "employee_scope": {
                    "categorias": ["alumbrado"],
                    "zonas": ["centro"],
                    "channels": ["whatsapp"],
                    "permisos": ["tickets_assign"],
                }
            },
        )
        employee.set_password("secret123")
        db.session.add(employee)
        db.session.flush()

        ticket = MunicipioTicket(
            pregunta="Poste sin luz",
            asunto="Alumbrado publico",
            categoria="alumbrado",
            municipio_id=owner.id,
            tenant_id=None,
            estado="nuevo",
            direccion="Plaza principal",
            distrito="centro",
            canal_ingreso="whatsapp",
        )
        db.session.add(ticket)
        db.session.commit()

        response = self.client.get(
            f"/api/v2/tenants/{tenant.slug}/employee-routing",
            headers={**self._auth(owner), "X-Tenant-Slug": tenant.slug, "X-Request-Id": "routing-legacy-muni-1"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        legacy_item = next(item for item in payload["queues"]["open"] if item["source_model"] == "MunicipioTicket" and item["id"] == ticket.id)
        self.assertEqual(legacy_item["category"], "alumbrado")
        recommendation = next(item for item in payload["recommendations"] if item["ticket"]["id"] == ticket.id)
        self.assertEqual(recommendation["suggested_assignee"]["id"], employee.id)

        assign_response = self.client.post(
            f"/api/v2/tenants/{tenant.slug}/employee-routing/auto-assign",
            json={"dry_run": False, "tickets": [{"source_model": "MunicipioTicket", "id": ticket.id}]},
            headers={**self._auth(owner), "X-Tenant-Slug": tenant.slug, "X-Request-Id": "assign-legacy-muni-1"},
        )

        self.assertEqual(assign_response.status_code, 200)
        self.assertEqual(assign_response.get_json()["applied_count"], 1)
        refreshed = db.session.get(MunicipioTicket, ticket.id)
        self.assertEqual(refreshed.asignado_a_id, employee.id)

    def test_employee_routing_contract_scope_update_and_auto_assign(self):
        unassigned = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            categoria="educacion",
            descripcion="Necesito retirar documentacion",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={"title": "Retiro de documentacion", "zone": "centro", "channel": "whatsapp"},
        )
        db.session.add(unassigned)
        db.session.commit()

        response = self.client.get(
            "/api/v2/employee-routing",
            headers={**self._auth(self.owner), "X-Request-Id": "routing-1"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "employee.routing.v1")
        self.assertEqual(payload.get("request_id"), "routing-1")
        self.assertGreaterEqual(payload["queues"]["unassigned_count"], 1)
        recommendation = next(item for item in payload["recommendations"] if item["ticket"]["id"] == unassigned.id)
        self.assertEqual(recommendation["suggested_assignee"]["id"], self.employee.id)
        self.assertIn("category_match", recommendation["reasons"])

        scope_response = self.client.patch(
            f"/api/v2/employees/{self.employee.id}/routing-scope",
            json={
                "categorias": ["educacion", "pagos"],
                "zonas": ["centro", "norte"],
                "channels": ["whatsapp", "widget"],
                "permisos": ["tickets_assign", "orders_assign"],
            },
            headers=self._auth(self.owner),
        )

        self.assertEqual(scope_response.status_code, 200)
        scope_payload = scope_response.get_json()
        self.assertEqual(scope_payload.get("contract_version"), "employee.routing_scope.v1")
        self.assertIn("pagos", scope_payload["employee"]["scope"]["categorias"])
        self.assertIn("widget", scope_payload["employee"]["scope"]["channels"])

        assign_response = self.client.post(
            "/api/v2/employee-routing/auto-assign",
            json={
                "dry_run": False,
                "tickets": [{"source_model": "TenantTicket", "id": unassigned.id}],
            },
            headers={**self._auth(self.owner), "X-Request-Id": "routing-assign-1"},
        )

        self.assertEqual(assign_response.status_code, 200)
        assign_payload = assign_response.get_json()
        self.assertEqual(assign_payload.get("contract_version"), "employee.routing.auto_assign.v1")
        self.assertEqual(assign_payload.get("request_id"), "routing-assign-1")
        self.assertEqual(assign_payload["applied_count"], 1)
        refreshed = db.session.get(TenantTicket, unassigned.id)
        self.assertEqual(refreshed.datos_extra["assignee_id"], self.employee.id)

    def test_tenant_health_contract(self):
        response = self.client.get("/api/v2/tenant-health", headers=self._auth(self.owner))

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "tenant.health.v1")
        self.assertIn(payload["health"]["status"], {"healthy", "warning", "critical"})
        self.assertIn("integrations", payload)
        self.assertIn("queues", payload)
        self.assertIn("recommended_actions", payload)

    def test_catalog_quality_contract_surfaces_image_price_and_stock_gaps(self):
        db.session.add(
            CatalogoItem(
                user_id=self.owner.id,
                tenant_id=self.tenant.id,
                nombre="Remera sin foto",
                descripcion="Producto para completar desde marketplace.",
                categoria="indumentaria",
                precio="2500",
                cantidad="12",
                disponible=True,
            )
        )
        db.session.add(
            CatalogoItem(
                user_id=self.owner.id,
                tenant_id=self.tenant.id,
                nombre="Cuaderno sin precio",
                descripcion="Falta precio para poder vender.",
                categoria="libreria",
                imagen_url="https://cdn.example.com/cuaderno.jpg",
                cantidad="20",
                disponible=True,
            )
        )
        db.session.commit()

        response = self.client.get(
            "/api/v2/catalog/quality",
            headers={**self._auth(self.owner), "X-Request-Id": "catalog-quality-1"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "catalog.quality.v1")
        self.assertEqual(payload.get("request_id"), "catalog-quality-1")
        self.assertEqual(payload.get("tenant_slug"), self.tenant.slug)
        self.assertTrue(payload.get("catalog_version"))
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertGreaterEqual(payload["summary"]["products"], 3)
        self.assertEqual(payload["summary"]["items_total"], payload["summary"]["products"])
        self.assertEqual(payload["summary"]["items_sellable"], payload["summary"]["ready_to_sell"])
        self.assertGreaterEqual(payload["summary"]["missing_images"], 1)
        self.assertGreaterEqual(payload["summary"]["missing_price"], 1)
        self.assertTrue(payload["recommendations"])
        self.assertTrue(payload["queues"]["missing_images"])
        self.assertTrue(payload["queues"]["missing_price"])
        self.assertEqual(payload["inventory"]["contract_version"], "catalog.inventory_ops.v1")
        self.assertTrue(payload["inventory"]["columns"]["stock_columns"])
        self.assertTrue(payload["imports"]["stock_only_import"])
        self.assertEqual(payload["frontend_contract"]["render_as"], "catalog_quality_command_center")
        self.assertTrue(payload["frontend_contract"]["allow_stock_only_import"])

    def test_whatsapp_sandbox_session_returns_deeplink_contract(self):
        response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/sandbox-session",
            headers={**self._auth(self.owner), "X-Request-Id": "sandbox-1"},
            json={
                "tenant_slug": self.tenant.slug,
                "whatsapp": "+5491111111111",
                "join_phrase": "join brief-yesterday",
                "rubro": "colegio",
                "brief": "Probar menu del tenant y crear un caso escolar",
                "test_message": "Hola, quiero probar el asistente",
                "menu_preview": [{"id": "casos", "label": "Casos escolares"}],
                "source": "tenant_integrations_panel",
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "whatsapp.sandbox_session.v1")
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("request_id"), "sandbox-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(payload["twilio"]["sandbox_number"], "whatsapp:+14155238886")
        self.assertIn("wa.me/14155238886", payload["twilio"]["wa_deeplink"])
        self.assertEqual(payload["demo_context"]["rubro"], "colegio")
        self.assertEqual(payload["whatsapp_sandbox"]["contract_version"], "demo.whatsapp_sandbox.v1")
        self.assertEqual(payload["whatsapp_sandbox"]["trial_policy"]["max_messages"], 10)
        self.assertTrue(payload["whatsapp_sandbox"]["scenario_scripts"])
        self.assertTrue(payload["demo_context"]["trial_policy"])
        self.assertFalse(payload["session"]["sends_real_message"])

    def test_whatsapp_sandbox_setup_and_test_contracts_are_backend_first(self):
        setup_response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/sandbox-setup",
            headers={**self._auth(self.owner), "X-Request-Id": "sandbox-setup-1"},
        )

        self.assertEqual(setup_response.status_code, 200)
        setup = setup_response.get_json()
        self.assertEqual(setup["contract_version"], "whatsapp.sandbox_setup.v1")
        self.assertEqual(setup["request_id"], "sandbox-setup-1")
        self.assertEqual(setup["tenant_slug"], self.tenant.slug)
        self.assertEqual(setup["provider"], "twilio_whatsapp")
        self.assertTrue(setup["sandbox"]["enabled"])
        self.assertEqual(setup["sandbox"]["join_number"], "whatsapp:+14155238886")
        self.assertEqual(setup["test"]["endpoint"], f"/api/v2/tenants/{self.tenant.slug}/whatsapp/sandbox-test")
        self.assertEqual(setup["frontend_contract"]["render_as"], "whatsapp_sandbox_onboarding")
        self.assertIsInstance(setup["demo_context"]["quick_menu"], list)
        self.assertEqual(setup["whatsapp_sandbox"]["contract_version"], "demo.whatsapp_sandbox.v1")
        self.assertTrue(setup["whatsapp_sandbox"]["supported_inputs"]["audio"])
        self.assertTrue(setup["whatsapp_sandbox"]["supported_inputs"]["image"])
        self.assertTrue(setup["whatsapp_sandbox"]["supported_inputs"]["location"])
        self.assertTrue(setup["whatsapp_sandbox"]["supported_inputs"]["file"])

        test_response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/sandbox-test",
            json={"to": "+5491111111111", "message": "Hola menu"},
            headers={**self._auth(self.owner), "X-Request-Id": "sandbox-test-1"},
        )

        self.assertEqual(test_response.status_code, 200)
        payload = test_response.get_json()
        self.assertEqual(payload["contract_version"], "whatsapp.sandbox_test.v1")
        self.assertEqual(payload["request_id"], "sandbox-test-1")
        self.assertFalse(payload["sends_real_message"])
        self.assertEqual(payload["mode"], "copy_or_deeplink")
        self.assertIn("wa.me/14155238886", payload["twilio"]["wa_deeplink"])
        self.assertEqual(payload["message_preview"]["message"], "Hola menu")
        self.assertEqual(payload["whatsapp_sandbox"]["trial_policy"]["max_messages"], 10)

    def test_twilio_tech_provider_onboarding_contract_hides_twilio_console(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=False,
        )

        response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-1"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "twilio.tech_provider.v1")
        self.assertEqual(payload["request_id"], "tech-provider-1")
        self.assertEqual(payload["provider"], "twilio_tech_provider")
        self.assertFalse(payload["automation"]["customer_sees_twilio_console"])
        self.assertFalse(payload["automation"]["manual_twilio_console_allowed"])
        self.assertTrue(payload["automation"]["env"]["ready"])
        self.assertEqual(payload["frontend_contract"]["render_as"], "twilio_tech_provider_onboarding")
        self.assertFalse(payload["frontend_contract"]["show_twilio_brand"])
        self.assertTrue(any(step["id"] == "create_subaccount" for step in payload["api_workflow"]))
        self.assertTrue(any(step["id"] == "create_or_update_voice_twiml_app" for step in payload["api_workflow"]))
        self.assertEqual(payload["voice"]["completion_endpoint"], f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/voice-app")
        self.assertEqual(payload["embedded_signup"]["meta_app_id"], "meta-app")
        self.assertEqual(payload["embedded_signup"]["configuration_id"], "cfg-123")
        self.assertIn("/integracion/whatsapp/connect?", payload["embedded_signup"]["start_url"])
        self.assertIn(f"tenant={self.tenant.slug}", payload["embedded_signup"]["start_url"])
        self.assertIn("app_id=meta-app", payload["embedded_signup"]["start_url"])
        self.assertIn("config_id=cfg-123", payload["embedded_signup"]["start_url"])
        self.assertIn("/webhook/whatsapp", payload["webhooks"]["inbound_message_url"])

    def test_twilio_tech_provider_requires_full_plan(self):
        self.tenant.plan = "free"
        db.session.add(self.tenant)
        db.session.commit()

        response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-locked-1"},
        )

        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertEqual(payload["error"], "plan_required")
        self.assertEqual(payload["reason_code"], "plan_full_required")
        self.assertFalse(payload["access"]["enabled"])
        self.assertEqual(payload["frontend"]["render_as"], "integration_locked")

    def test_twilio_tech_provider_provision_dry_run_persists_plan_without_live_api(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=False,
        )

        response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/provision",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-provision-1"},
            json={"phone_number": "+5491112223333", "display_name": "Colegio SaaS"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "twilio.tech_provider.provisioning.v1")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["state"]["status"], "provisioning_plan_ready")
        self.assertTrue(any(step["id"] == "embedded_signup" for step in payload["steps"]))
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        state = refreshed.configuracion["twilio_tech_provider"]
        self.assertEqual(state["requested_phone_number"], "+5491112223333")
        self.assertEqual(state["display_name"], "Colegio SaaS")

        signup_response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/embedded-signup",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-signup-1"},
            json={"waba_id": "123456789", "phone_number_id": "987654321", "session_id": "fb-session", "code": "meta-code"},
        )

        self.assertEqual(signup_response.status_code, 200)
        signup = signup_response.get_json()
        self.assertEqual(signup["contract_version"], "twilio.tech_provider.embedded_signup.v1")
        self.assertEqual(signup["state"]["waba_id"], "123456789")
        self.assertEqual(signup["state"]["embedded_signup_code"], "meta-code")
        self.assertEqual(signup["next_action"], "register_whatsapp_sender_via_senders_api")

        connection = ProviderConnection.query.filter_by(tenant_id=self.tenant.id, provider="twilio", channel="whatsapp").first()
        self.assertIsNotNone(connection)
        self.assertEqual(connection.status, "pending_sender_registration")
        self.assertEqual(connection.external_business_id, "123456789")
        sender = ProviderSender.query.filter_by(tenant_id=self.tenant.id, channel="whatsapp").first()
        self.assertIsNotNone(sender)
        self.assertEqual(sender.phone_number, "+5491112223333")
        self.assertEqual(sender.waba_id, "123456789")
        self.assertEqual(sender.phone_number_id, "987654321")
        self.assertGreaterEqual(MessagingEventLedger.query.filter_by(tenant_id=self.tenant.id, channel="whatsapp").count(), 2)

        status_response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/integrations/whatsapp/status",
            headers={**self._auth(self.owner), "X-Request-Id": "provider-status-1"},
        )

        self.assertEqual(status_response.status_code, 200)
        status = status_response.get_json()
        self.assertEqual(status["contract_version"], "provider.platform_status.v1")
        self.assertEqual(status["request_id"], "provider-status-1")
        self.assertEqual(status["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(status["connection"]["external_business_id"], "123456789")
        self.assertEqual(status["sender"]["phone_number"], "+5491112223333")
        self.assertEqual(status["frontend_contract"]["render_as"], "whatsapp_provider_status")
        self.assertTrue(any(check["id"] == "subaccount" for check in status["readiness_checks"]))
        self.assertTrue(status["recent_events"])

    def test_twilio_tech_provider_live_provision_creates_subaccount_and_messaging_service_without_persisting_token(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=True,
            PUBLIC_API_BASE_URL="https://www.chatboc.ar",
        )
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/Accounts.json"):
                self.assertEqual(kwargs["data"]["FriendlyName"], "Chatboc - saas-tenant")
                return _FakeTwilioResponse({"sid": "ACchild", "auth_token": "child-secret"})
            if url == "https://messaging.twilio.com/v1/Services":
                self.assertEqual(kwargs["data"]["InboundRequestUrl"], "https://www.chatboc.ar/webhook/whatsapp")
                self.assertEqual(kwargs["data"]["StatusCallback"], "https://www.chatboc.ar/twilio/whatsapp/status")
                return _FakeTwilioResponse({"sid": "MGchild"})
            raise AssertionError(f"unexpected Twilio URL {url}")

        with patch("services.twilio_tech_provider.requests.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/provision",
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-live-1"},
                json={"phone_number": "+5491112223333", "display_name": "Colegio SaaS"},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["mode"], "live")
        self.assertEqual(payload["state"]["status"], "ready_for_embedded_signup")
        self.assertEqual(payload["state"]["twilio_account_sid"], "ACchild")
        self.assertEqual(payload["state"]["messaging_service_sid"], "MGchild")
        self.assertEqual(payload["secure_secret_required"]["required_env"][0], "TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD")
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        state_text = json.dumps(refreshed.configuracion, sort_keys=True)
        self.assertIn("TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD", state_text)
        self.assertNotIn("child-secret", state_text)
        self.assertEqual(len(calls), 2)

    def test_twilio_live_provision_syncs_subaccount_secret_to_render_when_enabled(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=True,
            PUBLIC_API_BASE_URL="https://www.chatboc.ar",
            RENDER_ENV_SYNC_ENABLED=True,
            RENDER_API_KEY="render-secret",
            RENDER_SERVICE_ID="srv-backend",
            RENDER_ENV_SYNC_TRIGGER_DEPLOY_ENABLED=False,
        )
        twilio_calls = []
        render_calls = []

        def fake_twilio_post(url, **kwargs):
            twilio_calls.append((url, kwargs))
            if url.endswith("/Accounts.json"):
                return _FakeTwilioResponse({"sid": "ACchild", "auth_token": "child-secret"})
            if url == "https://messaging.twilio.com/v1/Services":
                return _FakeTwilioResponse({"sid": "MGchild"})
            raise AssertionError(f"unexpected Twilio URL {url}")

        def fake_render_put(url, **kwargs):
            render_calls.append((url, kwargs))
            self.assertEqual(url, "https://api.render.com/v1/services/srv-backend/env-vars/TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD")
            self.assertEqual(kwargs["json"], {"value": "child-secret"})
            self.assertIn("Bearer render-secret", kwargs["headers"]["Authorization"])
            return _FakeTwilioResponse({"key": "TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD"}, status_code=200)

        with patch("services.twilio_tech_provider.requests.post", side_effect=fake_twilio_post), patch(
            "services.render_env_sync.requests.put",
            side_effect=fake_render_put,
        ):
            response = self.client.post(
                f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/provision",
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-render-sync-1"},
                json={"phone_number": "+5491112223333"},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        render_sync = payload["secure_secret_required"]["render_env_sync"]
        self.assertTrue(render_sync["secret_value_stored"])
        self.assertEqual(render_sync["target"], "service")
        self.assertTrue(payload["state"]["render_subaccount_secret_synced"])
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        state_text = json.dumps(refreshed.configuracion, sort_keys=True)
        self.assertIn("TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD", state_text)
        self.assertNotIn("child-secret", state_text)
        self.assertEqual(len(twilio_calls), 2)
        self.assertEqual(len(render_calls), 1)

    def test_twilio_tech_provider_voice_app_dry_run_persists_tenant_urls(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=False,
            PUBLIC_API_BASE_URL="https://www.chatboc.ar",
        )

        response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/voice-app",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-voice-dry-1"},
            json={},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "twilio.tech_provider.voice_application.v1")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["state"]["voice_status"], "voice_application_plan_ready")
        self.assertIn("/twilio/voice?tenant=saas-tenant&vertical=educacion&intent=secretaria", payload["state"]["voice_url"])
        self.assertIn("/voice/fallback?tenant=saas-tenant&vertical=educacion&intent=secretaria", payload["state"]["voice_fallback_url"])
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        state = refreshed.configuracion["twilio_tech_provider"]
        self.assertEqual(state["voice_vertical"], "educacion")
        self.assertEqual(state["voice_intent"], "secretaria")
        self.assertEqual(refreshed.configuracion["voice_vertical"], "educacion")

    def test_twilio_tech_provider_voice_app_live_creates_app_and_attaches_sender(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=True,
            TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD="child-secret",
            PUBLIC_API_BASE_URL="https://api.chatboc.ar",
        )
        self.tenant.configuracion = {
            "twilio_tech_provider": {
                "twilio_account_sid": "ACchild",
                "sender_sid": "XE123",
                "sender_id": "whatsapp:+5491112223333",
            }
        }
        db.session.add(self.tenant)
        db.session.commit()
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if url == "https://api.twilio.com/2010-04-01/Accounts/ACchild/Applications.json":
                self.assertEqual(kwargs["data"]["FriendlyName"], "Chatboc Voice - saas-tenant")
                self.assertEqual(kwargs["data"]["VoiceUrl"], "https://api.chatboc.ar/twilio/voice?tenant=saas-tenant&vertical=educacion&intent=secretaria")
                self.assertEqual(kwargs["data"]["VoiceFallbackUrl"], "https://api.chatboc.ar/voice/fallback?tenant=saas-tenant&vertical=educacion&intent=secretaria")
                return _FakeTwilioResponse({"sid": "APvoice"})
            if url == "https://messaging.twilio.com/v2/Channels/Senders/XE123":
                self.assertEqual(kwargs["json"]["configuration"]["voice_application_sid"], "APvoice")
                return _FakeTwilioResponse({"sid": "XE123", "status": "ONLINE"})
            raise AssertionError(f"unexpected Twilio URL {url}")

        with patch("services.twilio_tech_provider.requests.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/voice-app",
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-voice-live-1"},
                json={},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["mode"], "live")
        self.assertEqual(payload["state"]["voice_twiml_app_sid"], "APvoice")
        self.assertTrue(payload["state"]["voice_sender_attached"])
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        state = refreshed.configuracion["twilio_tech_provider"]
        self.assertEqual(state["voice_twiml_app_sid"], "APvoice")
        self.assertEqual(refreshed.configuracion["voice_twiml_app_sid"], "APvoice")
        self.assertEqual(len(calls), 2)

    def test_twilio_tech_provider_register_sender_blocks_without_subaccount_secret(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=True,
        )
        self.tenant.configuracion = {
            "twilio_tech_provider": {
                "status": "pending_sender_registration",
                "twilio_account_sid": "ACchild",
                "messaging_service_sid": "MGchild",
                "requested_phone_number": "+5491112223333",
                "waba_id": "123456789",
                "phone_number_id": "987654321",
            }
        }
        db.session.add(self.tenant)
        db.session.commit()

        response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/register-sender",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-sender-blocked-1"},
            json={},
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "twilio.tech_provider.sender_registration.v1")
        self.assertEqual(payload["reason_code"], "sender_registration_prerequisites_missing")
        self.assertIn("twilio_subaccount_auth_token", payload["missing"])
        self.assertIn("TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD", payload["required_env"])

    def test_twilio_tech_provider_register_sender_attaches_to_messaging_service_and_syncs_status(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=True,
            TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD="child-secret",
            PUBLIC_API_BASE_URL="https://www.chatboc.ar",
        )
        self.tenant.configuracion = {
            "twilio_tech_provider": {
                "status": "pending_sender_registration",
                "twilio_account_sid": "ACchild",
                "messaging_service_sid": "MGchild",
                "requested_phone_number": "+5491112223333",
                "display_name": "Colegio SaaS",
                "waba_id": "123456789",
                "phone_number_id": "987654321",
            }
        }
        db.session.add(self.tenant)
        db.session.commit()
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            if url == "https://messaging.twilio.com/v2/Channels/Senders":
                body = kwargs["json"]
                self.assertEqual(body["sender_id"], "whatsapp:+5491112223333")
                self.assertEqual(body["configuration"]["waba_id"], "123456789")
                self.assertEqual(body["webhook"]["callback_url"], "https://www.chatboc.ar/webhook/whatsapp")
                return _FakeTwilioResponse(
                    {
                        "sid": "XE123",
                        "status": "PENDING",
                        "sender_id": "whatsapp:+5491112223333",
                    },
                    status_code=201,
                )
            if url == "https://messaging.twilio.com/v1/Services/MGchild/ChannelSenders":
                self.assertEqual(kwargs["data"], {"Sid": "XE123"})
                return _FakeTwilioResponse({"sid": "XE123"}, status_code=201)
            if url == "https://api.twilio.com/2010-04-01/Accounts/ACchild/Applications.json":
                self.assertEqual(kwargs["data"]["VoiceUrl"], "https://www.chatboc.ar/twilio/voice?tenant=saas-tenant&vertical=educacion&intent=secretaria")
                return _FakeTwilioResponse({"sid": "APvoice"})
            if url == "https://messaging.twilio.com/v2/Channels/Senders/XE123":
                self.assertEqual(kwargs["json"]["configuration"]["voice_application_sid"], "APvoice")
                return _FakeTwilioResponse({"sid": "XE123", "status": "ONLINE"})
            raise AssertionError(f"unexpected Twilio URL {url}")

        with patch("services.twilio_tech_provider.requests.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/register-sender",
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-sender-live-1"},
                json={},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["state"]["status"], "sender_attached")
        self.assertEqual(payload["state"]["sender_sid"], "XE123")
        self.assertEqual(payload["state"]["sender_status"], "PENDING")
        self.assertEqual(payload["state"]["voice_twiml_app_sid"], "APvoice")
        self.assertTrue(payload["state"]["voice_sender_attached"])
        self.assertEqual(payload["voice_app"]["contract_version"], "twilio.tech_provider.voice_application.v1")
        self.assertEqual(len(calls), 4)
        sender = ProviderSender.query.filter_by(tenant_id=self.tenant.id, channel="whatsapp").first()
        self.assertIsNotNone(sender)
        self.assertEqual(sender.sender_sid, "XE123")
        self.assertEqual(sender.messaging_service_sid, "MGchild")
        self.assertEqual(sender.status, "pending")

    def test_admin_catalog_exposes_and_saves_draft_endpoint(self):
        get_response = self.client.get(
            f"/api/admin/tenants/{self.tenant.slug}/catalog",
            headers=self._auth(self.owner),
        )

        self.assertEqual(get_response.status_code, 200)
        get_payload = get_response.get_json()
        self.assertEqual(get_payload["draft_endpoint"], f"/api/admin/tenants/{self.tenant.slug}/catalog/draft")
        self.assertEqual(get_payload["links"]["draft_endpoint"], f"/api/admin/tenants/{self.tenant.slug}/catalog/draft")
        self.assertEqual(get_payload["contract_version"], "tenant.catalog_admin.v1")
        self.assertEqual(get_payload["frontend_contract"]["render_as"], "tenant_catalog_inventory_admin")
        self.assertTrue(get_payload["inventory"]["columns"]["stock_columns"])

        draft_response = self.client.put(
            f"/api/admin/tenants/{self.tenant.slug}/catalog/draft",
            headers=self._auth(self.owner),
            json={
                "source": "tenant_catalog_editor",
                "title": "Borrador escolar",
                "items": [{"nombre": "Uniforme", "imagen_url": "https://cdn.example.com/u.jpg"}],
            },
        )

        self.assertEqual(draft_response.status_code, 200)
        draft_payload = draft_response.get_json()
        self.assertEqual(draft_payload.get("contract_version"), "tenant.catalog_draft.v1")
        self.assertTrue(draft_payload.get("ok"))
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        self.assertEqual(refreshed.configuracion["catalog_draft"]["title"], "Borrador escolar")

    def test_superadmin_executive_summary_contract(self):
        response = self.client.get(
            "/api/v2/superadmin/executive-summary",
            headers=self._auth(self.super_admin),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "superadmin.executive_summary.v1")
        self.assertEqual(payload["summary"]["tenants"], 1)
        self.assertIn("tenant_health", payload)

    def test_notification_hooks_contract_and_update(self):
        response = self.client.post(
            "/api/v2/notifications/hooks",
            json={
                "preferences": {"email": True, "whatsapp": False},
                "triggers": [{"event": "ticket.created", "channel": "email"}],
                "delivery": {"quiet_hours": {"start": 22, "end": 7}},
            },
            headers=self._auth(self.owner),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "notifications.hooks.v1")
        self.assertTrue(payload["preferences"]["email"])
        self.assertTrue(payload["templates"])
        self.assertEqual(payload["delivery_status"]["totals"]["sent"], 1)

    def test_omnichannel_inbox_contract(self):
        response = self.client.get("/api/v2/inbox/omnichannel", headers=self._auth(self.owner))

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "inbox.omnichannel.v1")
        self.assertEqual(payload["summary"]["total"], 1)
        item = payload["items"][0]
        self.assertEqual(item["channel"], "whatsapp")
        self.assertTrue(item["timeline"])
        self.assertEqual(item["conversation_id"], "conv-beca-1")
        self.assertTrue(item["attachments"])
        self.assertIn("sla", item)
        self.assertIn("allowed_actions", item)
        self.assertIn("next_steps", item)
        self.assertEqual(item["source_metadata"]["demo_session_id"], "demo-beca-1")
        self.assertTrue(item["map"]["can_render"])
        self.assertEqual(item["frontend_contract"]["render_as"], "inbox_360_drawer")
        self.assertEqual(payload["frontend_contract"]["drawer_contract"], "inbox.omnichannel.detail.v1")

    def test_omnichannel_inbox_detail_contract_for_drawer_360(self):
        response = self.client.get(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}",
            headers={**self._auth(self.owner), "X-Request-Id": "inbox-detail-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "inbox.omnichannel.detail.v1")
        self.assertEqual(payload.get("request_id"), "inbox-detail-1")
        item = payload["item"]
        self.assertEqual(item["ticket_id"], self.ticket.id)
        self.assertTrue(item["allowed_actions"])
        self.assertTrue(any(action["id"] == "reply" for action in item["allowed_actions"]))
        self.assertEqual(item["source_metadata"]["contact_key"], "whatsapp:+5491111111111")
        self.assertEqual(item["sla"]["priority"], "high")

    def test_production_smoke_contract_checks_critical_runtime_surfaces(self):
        response = self.client.get(
            "/api/v2/platform/production-smoke",
            headers={**self._auth(self.super_admin), "X-Request-Id": "smoke-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "platform.production_smoke.v1")
        self.assertEqual(payload.get("request_id"), "smoke-1")
        self.assertIn(payload["status"], {"pass", "warning", "fail"})
        check_ids = {item["id"] for item in payload["checks"]}
        self.assertIn("widget_platform_onboarding", check_ids)
        self.assertIn("socket_disabled_for_landing", check_ids)
        self.assertIn("tenant_admin_experience", check_ids)
        self.assertIn("inbox_360", check_ids)
        self.assertEqual(payload["frontend_contract"]["render_as"], "production_smoke_report")

    def test_omnichannel_inbox_action_updates_ticket(self):
        response = self.client.post(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
            json={"action": "reply", "body": "Estamos revisando tu caso.", "visibility": "public"},
            headers={**self._auth(self.owner), "X-Request-Id": "inbox-action-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "inbox.omnichannel.action.v1")
        self.assertEqual(payload.get("request_id"), "inbox-action-1")
        self.assertTrue(payload["ticket"]["timeline"])
        self.assertTrue(any(item.get("body") == "Estamos revisando tu caso." for item in payload["ticket"]["timeline"]))

    def test_tenant_admin_experience_contract_unifies_profile_operations_and_modules(self):
        response = self.client.get(
            "/api/v2/tenant/admin-experience",
            headers={**self._auth(self.owner), "X-Request-Id": "admin-exp-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "tenant.admin_experience.v1")
        self.assertEqual(payload.get("request_id"), "admin-exp-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(payload["profile"]["vertical"], "educacion")
        self.assertGreaterEqual(payload["profile"]["readiness"]["score"], 0)
        self.assertEqual(payload["operations"]["dashboard"]["contract_version"], "operations.dashboard.v1")
        self.assertEqual(payload["operations"]["freshness"]["contract_version"], "operations.freshness.v1")
        self.assertIsInstance(payload["operations"]["freshness"]["summary"]["can_render_heatmap"], bool)
        self.assertEqual(payload["lead_capture"]["summary"]["open"], 1)
        lead_item = payload["lead_capture"]["items"][0]
        self.assertIn("ticket_id", lead_item)
        self.assertIn("intent", lead_item)
        self.assertIn("next_action", lead_item)
        self.assertEqual(payload["surveys_votings"]["summary"]["live_votes"], 1)
        self.assertEqual(payload["marketplace"]["summary"]["products"], 1)
        self.assertIn("with_images", payload["marketplace"]["summary"])
        self.assertIn("missing_images", payload["marketplace"]["summary"])
        self.assertIn("products_without_image", payload["marketplace"]["summary"])
        self.assertIn("bulk_import_status", payload["marketplace"]["summary"])
        self.assertEqual(payload["marketplace"]["quality"]["contract_version"], "catalog.quality.v1")
        self.assertEqual(payload["marketplace"]["quality"]["summary"]["products"], 1)
        self.assertEqual(payload["whatsapp"]["contract_version"], "whatsapp.experience.v1")
        self.assertTrue(payload["whatsapp"]["channel"]["enabled"])
        self.assertTrue(payload["whatsapp"]["conversation_intelligence"]["inputs"]["image"]["enabled"])
        self.assertIn("route_progress", payload["whatsapp"]["tracking"]["courier_style_map"]["render_contract"]["animations"])
        self.assertEqual(payload["education"]["profile"]["is_education"], True)
        module_ids = {item["id"] for item in payload["modules"]}
        self.assertIn("inbox", module_ids)
        self.assertIn("analytics", module_ids)
        self.assertIn("surveys_votings", module_ids)
        self.assertIn("marketplace", module_ids)
        self.assertIn("education", module_ids)
        self.assertNotIn("portal", module_ids)
        self.assertEqual(payload["navigation"]["contract_version"], "tenant.admin_navigation.v1")
        quick_action_ids = {item["id"] for item in payload["navigation"]["quick_actions"]}
        self.assertIn("open_surveys", quick_action_ids)
        self.assertIn("open_employees", quick_action_ids)
        self.assertIn("open_heatmap", quick_action_ids)
        self.assertNotIn("open_portal", quick_action_ids)
        self.assertIn("workspace", payload["navigation"]["hide_legacy_tabs"])
        self.assertEqual(payload["admin_panel_widgets"]["contract_version"], "tenant.admin_panel_widgets.v1")
        hero_widget_ids = {item["id"] for item in payload["admin_panel_widgets"]["hero_widgets"]}
        self.assertIn("location_widget", hero_widget_ids)
        self.assertIn("heatmap_summary", hero_widget_ids)
        self.assertIn("employee_assignment", hero_widget_ids)
        self.assertEqual(
            payload["admin_panel_widgets"]["ticket_workspace"]["auto_assign_endpoint"],
            "/api/v2/employee-routing/auto-assign",
        )
        whatsapp_module = next(item for item in payload["modules"] if item["id"] == "widget_whatsapp")
        self.assertEqual(whatsapp_module["label"], "Widget/WhatsApp/Voz")
        self.assertEqual(whatsapp_module["endpoint"], "/api/v2/whatsapp/experience")
        for module in payload["modules"]:
            self.assertIn("secondary_endpoints", module)
            self.assertIn("widgets", module)
        for section in payload["education"]["admin_menu"]["panel_sections"]:
            self.assertIn("endpoint", section)
            self.assertIn("route", section)
            self.assertIn("widgets", section)
            self.assertIn("secondary_endpoints", section)

    def test_tenant_admin_experience_degrades_to_json_when_source_fails(self):
        with patch("routes.v2.saas.build_operational_dashboard", side_effect=RuntimeError("analytics down")):
            response = self.client.get(
                "/api/v2/tenant/admin-experience",
                headers={**self._auth(self.owner), "X-Request-Id": "admin-exp-degraded-1"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "tenant.admin_experience.v1")
        self.assertEqual(payload.get("request_id"), "admin-exp-degraded-1")
        self.assertEqual(payload["health"]["status"], "degraded")
        self.assertEqual(payload["health"]["reason_code"], "admin_experience_source_failed")
        module_ids = {item["id"] for item in payload["modules"]}
        self.assertIn("inbox", module_ids)
        self.assertIn("analytics", module_ids)
        self.assertNotIn("portal", module_ids)

    def test_whatsapp_experience_contract_connects_channel_content_tracking_and_admin_panel(self):
        response = self.client.get(
            "/api/v2/whatsapp/experience",
            headers={**self._auth(self.owner), "X-Request-Id": "whatsapp-exp-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "whatsapp.experience.v1")
        self.assertEqual(payload.get("request_id"), "whatsapp-exp-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "whatsapp-exp-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertTrue(payload["channel"]["enabled"])
        self.assertEqual(payload["channel"]["test_endpoint"], "/api/notifications/whatsapp/test")
        self.assertEqual(payload["channel"]["test_method"], "POST")
        self.assertEqual(payload["channel"]["test_label"], "Probar canal")
        self.assertEqual(payload["enterprise_rules"]["max_outbound_per_hour"], 200)
        self.assertEqual(payload["contact_window"]["active_24h"], 1)
        self.assertTrue(payload["conversation_intelligence"]["inputs"]["emoji"]["enabled"])
        self.assertTrue(payload["conversation_intelligence"]["inputs"]["location"]["enabled"])
        self.assertTrue(payload["conversation_intelligence"]["inputs"]["audio_note"]["enabled"])
        self.assertTrue(payload["conversation_intelligence"]["inputs"]["video"]["enabled"])
        self.assertFalse(payload["conversation_intelligence"]["inputs"]["video"]["analysis_ready"])
        self.assertTrue(payload["conversation_intelligence"]["voice_calls"]["enabled"])
        self.assertTrue(payload["conversation_intelligence"]["voice_calls"]["capabilities"]["native_speech_to_speech"])
        self.assertEqual(
            payload["conversation_intelligence"]["huggingface_ai"]["contract_version"],
            "huggingface.whatsapp_ai_runtime.v1",
        )
        self.assertIn(
            "reclamo de servicio publico",
            payload["conversation_intelligence"]["huggingface_ai"]["classification_groups"]["intent"],
        )
        self.assertEqual(payload["content_modules"]["catalog"]["items"], 1)
        self.assertEqual(payload["content_modules"]["catalog"]["items_with_images"], 1)
        self.assertGreaterEqual(payload["content_modules"]["surveys_votings"]["responses"], 1)
        self.assertEqual(payload["content_modules"]["news_events"]["by_type"]["noticia"], 1)
        self.assertTrue(payload["content_modules"]["promotions"]["enabled"])
        self.assertEqual(payload["content_modules"]["links"]["tenant_config_links"], 1)
        self.assertEqual(payload["tracking"]["claims"]["open"], 1)
        self.assertEqual(payload["tracking"]["orders"]["total"], 1)
        self.assertEqual(payload["tracking"]["claims"]["experience_endpoint"], "/api/public/tracking/experience?kind=claim&code={code}&pin={pin}")
        self.assertEqual(payload["tracking"]["orders"]["experience_endpoint"], "/api/public/tracking/experience?kind=order&code={code}")
        self.assertEqual(payload["tracking"]["courier_style_map"]["render_contract"]["fallback_when_no_coordinates"], "timeline_only")
        self.assertIn("route_progress", payload["tracking"]["courier_style_map"]["render_contract"]["animations"])
        self.assertTrue(payload["commerce"]["payments"]["payment_ready"])
        self.assertTrue(payload["commerce"]["payments"]["capabilities"]["whatsapp_checkout"])
        self.assertTrue(payload["commerce"]["payments"]["capabilities"]["widget_checkout"])
        self.assertEqual(
            payload["commerce"]["checkout_experience"]["contract_version"],
            "commerce.conversational_checkout_experience.v1",
        )
        self.assertEqual(payload["commerce"]["checkout_experience"]["active_entrypoint"], "whatsapp")
        self.assertTrue(payload["commerce"]["checkout_experience"]["ready"])
        self.assertFalse(payload["commerce"]["checkout_experience"]["policy"]["card_data_in_chat"])
        self.assertEqual(
            payload["commerce"]["checkout_experience"]["policy"]["confirmation_source"],
            "server_to_server_webhook",
        )
        self.assertEqual(
            payload["commerce"]["checkout_experience"]["endpoints"]["public_checkout_session"],
            "/api/checkout/crear-preferencia",
        )
        self.assertEqual(payload["commerce"]["customer_policy"]["payment_capture"], "external_secure_webview")
        self.assertEqual(payload["template_blueprint"]["provider"], "twilio_content_api")
        self.assertTrue(payload["template_blueprint"]["policy"]["requires_meta_approval_outside_24h"])
        required_template_ids = {item["id"] for item in payload["template_blueprint"]["required_templates"]}
        self.assertIn("order_checkout", required_template_ids)
        order_checkout_template = next(
            item for item in payload["template_blueprint"]["required_templates"] if item["id"] == "order_checkout"
        )
        self.assertEqual(order_checkout_template["status"]["source"], "local_twilio_manifest")
        self.assertEqual(order_checkout_template["status"]["resolved_name"], "chatboc_order_checkout_v1")
        self.assertTrue(order_checkout_template["status"]["approved"])
        self.assertEqual(order_checkout_template["readiness"]["state"], "approved_requires_webview")
        self.assertEqual(order_checkout_template["readiness"]["next_action"], "verify_signed_webview_and_server_webhook")
        welcome_template = next(
            item for item in payload["template_blueprint"]["required_templates"] if item["id"] == "welcome_menu"
        )
        self.assertEqual(welcome_template["friendly_name"], "chatboc_welcome_menu_v2")
        self.assertEqual(welcome_template["status"]["resolved_name"], "chatboc_welcome_menu_v2")
        self.assertEqual(welcome_template["status"]["content_sid"], "HXwelcomev2")
        self.assertTrue(welcome_template["status"]["approved"])
        self.assertEqual(welcome_template["execution"]["twilio_type"], "twilio/quick-reply")
        self.assertTrue(welcome_template["execution"]["automation"]["submit_for_meta_approval"])
        self.assertEqual(payload["template_blueprint"]["endpoints"]["templates_admin"], "/api/admin/templates")
        self.assertIn("colegio", payload["template_blueprint"]["vertical_templates"])
        colegio_template_ids = {
            item["id"] for item in payload["template_blueprint"]["vertical_templates"]["colegio"]
        }
        self.assertIn("school_payment_due", colegio_template_ids)
        school_payment_template = next(
            item
            for item in payload["template_blueprint"]["vertical_templates"]["colegio"]
            if item["id"] == "school_payment_due"
        )
        self.assertEqual(school_payment_template["friendly_name"], "chatboc_school_payment_due_v2")
        self.assertEqual(school_payment_template["status"]["resolved_name"], "chatboc_school_payment_due_v2")
        self.assertTrue(school_payment_template["status"]["approved"])
        self.assertEqual(school_payment_template["execution"]["twilio_type"], "twilio/call-to-action")
        self.assertEqual(school_payment_template["execution"]["webview"]["role"], "secure_checkout")
        self.assertTrue(school_payment_template["execution"]["webview"]["must_confirm_by_webhook"])
        self.assertIn("government", payload["template_blueprint"]["operational_template_groups"])
        government_group = payload["template_blueprint"]["operational_template_groups"]["government"]
        gov_claim_sla = next(item for item in government_group["items"] if item["id"] == "gov_claim_sla")
        self.assertEqual(gov_claim_sla["friendly_name"], "gobiernos_reclamo_sla")
        self.assertEqual(gov_claim_sla["status"]["resolved_name"], "gobiernos_reclamo_sla")
        self.assertEqual(gov_claim_sla["status"]["content_sid"], "HXgovsla")
        self.assertTrue(gov_claim_sla["status"]["approved"])
        self.assertEqual(gov_claim_sla["execution"]["twilio_type"], "twilio/quick-reply")
        self.assertTrue(gov_claim_sla["execution"]["meta_surface"]["whatsapp_flows_candidate"])
        gov_survey_template = next(
            item
            for item in payload["template_blueprint"]["vertical_templates"]["gobierno"]
            if item["id"] == "gov_survey_invite"
        )
        self.assertEqual(gov_survey_template["status"]["resolved_name"], "chatboc_gov_survey_invite_v2")
        self.assertTrue(gov_survey_template["status"]["approved"])
        self.assertEqual(gov_survey_template["readiness"]["state"], "approved_requires_webview")
        self.assertGreaterEqual(payload["template_blueprint"]["registry_summary"]["operational_catalog_total"], 40)
        self.assertGreater(payload["template_blueprint"]["registry_summary"]["operational_webviews"], 0)
        self.assertGreaterEqual(len(payload["template_blueprint"]["next_actions"]), 1)
        self.assertIn(
            payload["template_blueprint"]["next_actions"][0]["severity"],
            {"blocking", "warning", "ready_with_dependency"},
        )
        self.assertIn("whatsapp_flows", payload["template_blueprint"]["meta_business_strategy"])
        self.assertIn("signed_webviews", payload["template_blueprint"]["meta_business_strategy"])
        self.assertEqual(payload["webview_blueprint"]["checkout"]["confirmation_source"], "server_to_server_webhook")
        self.assertFalse(payload["webview_blueprint"]["checkout"]["card_data_in_chat"])
        webview_flows = {item["id"]: item for item in payload["webview_blueprint"]["flows"]}
        self.assertIn("claim_tracking_helpdesk", webview_flows)
        self.assertIn("order_checkout", webview_flows)
        self.assertIn("survey_vote", webview_flows)
        self.assertEqual(
            webview_flows["claim_tracking_helpdesk"]["url_template"],
            "/api/public/tracking/experience?kind=claim&code={code}&pin={pin}",
        )
        self.assertIn("public_comment_created", webview_flows["claim_tracking_helpdesk"]["server_confirmation"])
        self.assertIn("order_checkout", webview_flows["order_checkout"]["template_ids"])
        self.assertEqual(payload["webview_blueprint"]["summary"]["flows_total"], 4)
        self.assertTrue(payload["webview_blueprint"]["security"]["requires_full_plan"])
        self.assertEqual(payload["qa_playbook"]["contract_version"], "whatsapp.qa_playbook.v1")
        self.assertEqual(payload["qa_playbook"]["local_command"], "python scripts/qa_whatsapp_flows.py")
        self.assertGreaterEqual(payload["qa_playbook"]["scenario_count"], 5)
        qa_scenarios = {item["id"]: item for item in payload["qa_playbook"]["scenarios"]}
        self.assertIn("gov_claim_text_to_tracking", qa_scenarios)
        self.assertIn("pyme_catalog_order_checkout", qa_scenarios)
        self.assertIn("chatboc_demo_hub", qa_scenarios)
        self.assertIn("survey_vote_realtime", qa_scenarios)
        claim_qa = qa_scenarios["gov_claim_text_to_tracking"]
        self.assertEqual(claim_qa["webview_state"]["id"], "claim_tracking_helpdesk")
        self.assertIn("junin_texto_reclamo", claim_qa["script_cases"])
        self.assertIn("gov_claim_created", claim_qa["templates"])
        self.assertIn(claim_qa["status"], {"ready", "blocked_templates", "blocked_webview", "needs_template_review"})
        self.assertIn("chatboc_demo_order_start", qa_scenarios["chatboc_demo_hub"]["script_cases"])
        self.assertIn("chatboc_demo_survey_open", qa_scenarios["survey_vote_realtime"]["script_cases"])
        self.assertIn("requiere TWILIO_AUTH_TOKEN real", payload["qa_playbook"]["live_mode_guardrails"])
        self.assertEqual(payload["message_ux_policy"]["interactive_limits"]["reply_buttons_max"], 3)
        self.assertEqual(payload["admin_panel"]["inbox"], "/api/v2/inbox/omnichannel")
        self.assertTrue(payload["education"]["enabled"])
        self.assertEqual(payload["frontend_contract"]["render_as"], "whatsapp_operations_hub")
        self.assertIn("commerce_checkout", payload["frontend_contract"]["recommended_views"])
        self.assertIn("template_blueprint", payload["frontend_contract"]["recommended_views"])
        self.assertIn("webview_checkout", payload["frontend_contract"]["recommended_views"])
        self.assertIn("qa_playbook", payload["frontend_contract"]["recommended_views"])
        self.assertIn("huggingface_ai", payload["frontend_contract"]["recommended_views"])

        alias_response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/experience",
            headers={**self._auth(self.super_admin), "X-Request-Id": "whatsapp-exp-alias-1"},
        )
        self.assertEqual(alias_response.status_code, 200)
        alias_payload = alias_response.get_json()
        self.assertEqual(alias_payload.get("contract_version"), "whatsapp.experience.v1")
        self.assertEqual(alias_payload.get("request_id"), "whatsapp-exp-alias-1")
        self.assertEqual(alias_payload["tenant"]["slug"], self.tenant.slug)

    def test_superadmin_command_center_contract(self):
        response = self.client.get(
            "/api/v2/superadmin/command-center",
            headers={**self._auth(self.super_admin), "X-Request-Id": "command-center-1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "superadmin.command_center.v1")
        self.assertEqual(payload.get("request_id"), "command-center-1")
        self.assertEqual(payload["summary"]["tenants"], 1)
        self.assertIn("tenant_creation", payload)
        self.assertEqual(payload["tenant_creation"]["endpoint"], "/api/admin/tenants")
        self.assertTrue(payload["tenants"]["items"])
        tenant_item = payload["tenants"]["items"][0]
        self.assertEqual(tenant_item["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(tenant_item["tenant_slug"], self.tenant.slug)
        self.assertEqual(tenant_item["display_name"], self.tenant.nombre)
        self.assertIn("health_score", tenant_item)
        self.assertIn("status", tenant_item)
        self.assertIn("risk_reason", tenant_item)
        if payload["tenants"]["top_risky"]:
            risky_item = payload["tenants"]["top_risky"][0]
            self.assertIn("tenant_slug", risky_item)
            self.assertIn("risk_reason", risky_item)
        self.assertIn("drilldown_endpoint_template", payload["frontend_contract"])


if __name__ == "__main__":
    unittest.main()
