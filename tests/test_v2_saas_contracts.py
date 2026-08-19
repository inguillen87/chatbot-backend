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
    AnalyticsEventV2,
    CatalogoItem,
    DomainEffectOutbox,
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
    PymeTicket,
    TenantConfig,
    TenantProfile,
    TenantTicket,
    TenantTicketReplyEvent,
    TicketComentario,
    TicketDomainEffectReceipt,
    User,
    WhatsAppContactState,
    WhatsAppEnterpriseRule,
)
from services.meta_flow_json import SURVEY_VOTE_DATA_CONTRACT
from services.tts_orchestrator import reset_tts_cache_metrics


class V2SaasTestConfig(Config):
    TESTING = True
    TWILIO_ALLOW_NETWORK_IN_TESTS = True
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
        reset_tts_cache_metrics()
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
                last_sync_at=datetime.now(timezone.utc),
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
                last_sync_at=datetime.now(timezone.utc),
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
                last_sync_at=datetime.now(timezone.utc),
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
        self.super_admin.accesibilidad = {
            "auth": {
                "provider": "clerk",
                "session_version": 1,
                "clerk": {"user_id": "user_test_superadmin"},
            }
        }
        db.session.add(self.super_admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self, user: User):
        payload = {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": getattr(user, "tenant_slug", None),
                "exp": datetime.utcnow() + timedelta(hours=1),
            }
        if user.rol == "super_admin":
            payload.update(
                {
                    "auth_provider": "clerk",
                    "session_kind": "clerk",
                    "sid": "sess_test_superadmin",
                    "clerk_sid": "sess_test_superadmin",
                    "jti": "jti_test_superadmin",
                    "sv": 1,
                }
            )
        token = jwt.encode(
            payload,
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug}

    def _enable_municipal_domain_outbox(self):
        self.tenant.tipo = "municipio"
        self.tenant.municipio_id = self.owner.id
        self.tenant.pyme_id = None
        db.session.add(self.tenant)
        db.session.commit()
        self.app.config.update(
            DOMAIN_EFFECT_OUTBOX_MODE="queue",
            DOMAIN_EFFECT_OUTBOX_SECRET="v2-saas-domain-effect-secret-32-bytes-minimum",
            DOMAIN_EFFECT_OUTBOX_TENANT_IDS=str(self.tenant.id),
            DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES=4096,
            DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS=8,
        )

    def _enable_tenant_domain_outbox(self):
        self.app.config.update(
            DOMAIN_EFFECT_OUTBOX_MODE="queue",
            DOMAIN_EFFECT_OUTBOX_SECRET="v2-saas-tenant-reply-secret-32-bytes-minimum",
            DOMAIN_EFFECT_OUTBOX_TENANT_IDS=str(self.tenant.id),
            DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES=4096,
            DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS=8,
        )

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

    def test_employee_routing_does_not_recommend_or_apply_incompatible_assignee(self):
        restricted = TenantTicket(
            tenant_id=self.tenant.id,
            user_id=self.owner.id,
            categoria="restricted",
            descripcion="Caso fuera del alcance del unico agente",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={"title": "Caso restringido", "priority": "high"},
        )
        db.session.add(restricted)
        db.session.commit()

        preview = self.client.get(
            "/api/v2/employee-routing",
            headers=self._auth(self.owner),
        )
        self.assertEqual(preview.status_code, 200, preview.get_json())
        recommendation = next(
            item
            for item in preview.get_json()["recommendations"]
            if item["ticket"]["id"] == restricted.id
            and item["ticket"]["source_model"] == "TenantTicket"
        )
        self.assertIsNone(recommendation["suggested_assignee"])
        self.assertEqual(recommendation["reasons"], ["no_employee_available"])

        stale_payload = {
            "recommendations": [
                {
                    "ticket": {
                        "source_model": "TenantTicket",
                        "id": restricted.id,
                        "ticket_id": restricted.id,
                        "category": "restricted",
                    },
                    "suggested_assignee": {"id": self.employee.id},
                    "score": 99,
                    "reasons": ["stale_or_tampered_recommendation"],
                }
            ]
        }
        with patch("routes.v2.saas.build_employee_routing_payload", return_value=stale_payload):
            apply_response = self.client.post(
                "/api/v2/employee-routing/auto-assign",
                json={
                    "dry_run": False,
                    "tickets": [{"source_model": "TenantTicket", "id": restricted.id}],
                },
                headers=self._auth(self.owner),
            )

        self.assertEqual(apply_response.status_code, 200, apply_response.get_json())
        body = apply_response.get_json()
        self.assertEqual(body["applied_count"], 0)
        self.assertEqual(body["items"][0]["assignment_reason"], "assignee_category_scope_mismatch")
        db.session.expire_all()
        self.assertIsNone(
            (db.session.get(TenantTicket, restricted.id).datos_extra or {}).get("assignee_id")
        )

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
        self.assertEqual(payload["frontend_contract"]["primary_action"], "prepare_activation")
        self.assertIn("operator_checklist", payload["frontend_contract"]["sections"])
        self.assertEqual(payload["setup_health"]["contract_version"], "twilio.tech_provider.setup_health.v1")
        self.assertEqual(payload["setup_health"]["status"], "action_required")
        self.assertEqual(payload["setup_health"]["recommended_next_action"], "prepare_activation")
        self.assertGreater(payload["setup_health"]["activation_score"], 0)
        self.assertTrue(any(item["id"] == "platform_env" and item["done"] for item in payload["operator_checklist"]))
        self.assertTrue(any(item["id"] == "embedded_signup" for item in payload["operator_checklist"]))
        self.assertIn("provider_status", payload["smoke_tests"])
        self.assertIn("whatsapp_experience", payload["smoke_tests"])
        self.assertEqual(payload["smoke_playbook"]["contract_version"], "twilio.tech_provider.smoke_playbook.v1")
        self.assertTrue(payload["smoke_playbook"]["safe_by_default"])
        self.assertTrue(any(item["id"] == "template_registry" and item["execution_mode"] == "dry_run_first" for item in payload["smoke_playbook"]["tests"]))
        self.assertTrue(any(item["id"] == "live_whatsapp_message" and item["confirmation_required"] for item in payload["smoke_playbook"]["tests"]))
        live_smoke = next(item for item in payload["smoke_playbook"]["tests"] if item["id"] == "live_whatsapp_message")
        self.assertEqual(
            live_smoke["endpoint"],
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/smoke-test/live_whatsapp_message",
        )
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

    def test_admin_whatsapp_legacy_connect_returns_twilio_tech_provider_adapter(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            PUBLIC_FRONTEND_URL="https://app.chatboc.ar",
        )

        response = self.client.get(
            f"/api/admin/tenants/{self.tenant.slug}/integrations/whatsapp/connect",
            headers=self._auth(self.owner),
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "tenant.integration.connect.v2_adapter")
        self.assertEqual(payload["provider"], "twilio_tech_provider")
        self.assertEqual(payload["contract"]["contract_version"], "twilio.tech_provider.v1")
        self.assertEqual(payload["frontend_contract"]["render_as"], "twilio_tech_provider_onboarding")
        self.assertFalse(payload["frontend_contract"]["show_twilio_console"])
        self.assertEqual(
            payload["completion_endpoint"],
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/embedded-signup",
        )
        self.assertIn("/integracion/whatsapp/connect?", payload["redirect_url"])
        self.assertIn(f"tenant={self.tenant.slug}", payload["redirect_url"])
        self.assertIn("app_id=meta-app", payload["redirect_url"])
        self.assertIn("config_id=cfg-123", payload["redirect_url"])

    def test_legacy_whatsapp_oauth_callback_fails_closed(self):
        response = self.client.get("/api/integrations/whatsapp/callback?code=meta-code&state=1")

        self.assertEqual(response.status_code, 410, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "tenant.integration.callback.deprecated.v1")
        self.assertEqual(payload["reason_code"], "use_twilio_tech_provider_flow")
        self.assertFalse(payload["retryable"])
        self.assertIn("embedded_signup_completion", payload["replacement_endpoints"])

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

    def test_twilio_tech_provider_smoke_test_executes_safe_checks_and_blocks_real_message(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=False,
        )

        provider_response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/smoke-test/provider_status",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-smoke-provider-1"},
            json={},
        )

        self.assertEqual(provider_response.status_code, 200, provider_response.get_json())
        provider_payload = provider_response.get_json()
        self.assertEqual(provider_payload["contract_version"], "twilio.tech_provider.smoke_execution.v1")
        self.assertEqual(provider_payload["test_id"], "provider_status")
        self.assertEqual(provider_payload["execution_mode"], "read_only")
        self.assertFalse(provider_payload["sends_real_message"])
        self.assertIn("checks_total", provider_payload["details"])

        template_response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/smoke-test/template_registry",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-smoke-template-1"},
            json={"dry_run": True},
        )

        self.assertEqual(template_response.status_code, 200, template_response.get_json())
        template_payload = template_response.get_json()
        self.assertEqual(template_payload["test_id"], "template_registry")
        self.assertEqual(template_payload["execution_mode"], "dry_run_first")
        self.assertEqual(template_payload["details"]["manifest_contract"], "twilio.content.creation_manifest.v1")
        self.assertIn("by_content_family", template_payload["details"])

        real_response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/smoke-test/live_whatsapp_message",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-smoke-real-1"},
            json={},
        )

        self.assertEqual(real_response.status_code, 409, real_response.get_json())
        real_payload = real_response.get_json()
        self.assertEqual(real_payload["status"], "blocked")
        self.assertEqual(real_payload["next_action"], "confirm_real_message_required")
        self.assertTrue(real_payload["sends_real_message"])

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
        self.assertEqual(payload["onboarding"]["contract_version"], "tenant.whatsapp_onboarding.v1")
        self.assertEqual(payload["onboarding"]["provider"], "twilio_tech_provider")
        self.assertEqual(payload["onboarding"]["status"], "plan_ready")
        self.assertEqual(payload["channel_activation"]["contract_version"], "tenant.channel_activation.v1")
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        state = refreshed.configuracion["twilio_tech_provider"]
        self.assertEqual(state["requested_phone_number"], "+5491112223333")
        self.assertEqual(state["display_name"], "Colegio SaaS")
        self.assertEqual(refreshed.configuracion["whatsapp_onboarding"]["status"], "plan_ready")

        signup_response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/embedded-signup",
            headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-signup-1"},
            json={"waba_id": "123456789", "phone_number_id": "987654321", "session_id": "fb-session", "code": "meta-code"},
        )

        self.assertEqual(signup_response.status_code, 200)
        signup = signup_response.get_json()
        self.assertEqual(signup["contract_version"], "twilio.tech_provider.embedded_signup.v1")
        self.assertEqual(signup["state"]["waba_id"], "123456789")
        self.assertTrue(signup["state"]["embedded_signup_code_present"])
        self.assertNotIn("embedded_signup_code", signup["state"])
        self.assertNotIn("meta-code", json.dumps(signup, sort_keys=True))
        self.assertEqual(signup["next_action"], "register_whatsapp_sender_via_senders_api")
        self.assertEqual(signup["onboarding"]["status"], "pending_sender_registration")
        signup_channels = {item["id"]: item for item in signup["channel_activation"]["channels"]}
        self.assertEqual(signup_channels["whatsapp"]["status"], "action_required")
        self.assertEqual(signup_channels["whatsapp"]["reason_code"], "register_sender")
        refreshed_after_signup = db.session.get(TenantProfile, self.tenant.id)
        self.assertNotIn("meta-code", json.dumps(refreshed_after_signup.configuracion, sort_keys=True))
        self.assertEqual(refreshed_after_signup.configuracion["whatsapp_onboarding"]["status"], "pending_sender_registration")

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
        ledger_blob = json.dumps(
            [
                {"payload": item.payload, "metadata_json": item.metadata_json, "error_message": item.error_message}
                for item in MessagingEventLedger.query.filter_by(tenant_id=self.tenant.id, channel="whatsapp").all()
            ],
            sort_keys=True,
        )
        self.assertNotIn("meta-code", ledger_blob)

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

    def test_twilio_tech_provider_live_provision_retries_without_duplicate_resources(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=True,
            PUBLIC_API_BASE_URL="https://www.chatboc.ar",
        )
        calls = []
        messaging_service_attempts = 0

        def fake_post(url, **kwargs):
            nonlocal messaging_service_attempts
            calls.append((url, kwargs))
            if url.endswith("/Accounts.json"):
                return _FakeTwilioResponse({"sid": "ACchild", "auth_token": "child-secret"})
            if url == "https://messaging.twilio.com/v1/Services":
                messaging_service_attempts += 1
                if messaging_service_attempts == 1:
                    raise RuntimeError("temporary messaging service failure")
                return _FakeTwilioResponse({"sid": "MGchild"})
            raise AssertionError(f"unexpected Twilio URL {url}")

        endpoint = f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/provision"
        with patch("services.twilio_tech_provider.requests.post", side_effect=fake_post):
            first_response = self.client.post(
                endpoint,
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-retry-1"},
                json={"phone_number": "+5491112223333"},
            )

            self.app.config["TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD"] = "child-secret"
            second_response = self.client.post(
                endpoint,
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-retry-2"},
                json={"phone_number": "+5491112223333"},
            )

            refreshed_for_replay = db.session.get(TenantProfile, self.tenant.id)
            replay_config = dict(refreshed_for_replay.configuracion)
            replay_state = dict(replay_config["twilio_tech_provider"])
            replay_state.update(
                {
                    "status": "pending_sender_registration",
                    "last_step": "embedded_signup",
                    "waba_id": "123456789",
                }
            )
            replay_config["twilio_tech_provider"] = replay_state
            refreshed_for_replay.configuracion = replay_config
            db.session.add(refreshed_for_replay)
            db.session.commit()

            replay_response = self.client.post(
                endpoint,
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-retry-3"},
                json={"phone_number": "+5491112223333"},
            )

        self.assertEqual(first_response.status_code, 400, first_response.get_json())
        first_payload = first_response.get_json()
        self.assertEqual(first_payload["reason_code"], "twilio_messaging_service_creation_failed")
        self.assertEqual(first_payload["state"]["twilio_account_sid"], "ACchild")
        self.assertIsNone(first_payload["state"]["messaging_service_sid"])

        self.assertEqual(second_response.status_code, 200, second_response.get_json())
        second_payload = second_response.get_json()
        second_steps = {step["id"]: step for step in second_payload["steps"]}
        self.assertEqual(second_steps["create_subaccount"]["status"], "done")
        self.assertEqual(second_steps["create_subaccount"]["operation"], "reuse")
        self.assertEqual(second_steps["create_messaging_service"]["status"], "done")
        self.assertEqual(second_payload["state"]["messaging_service_sid"], "MGchild")

        self.assertEqual(replay_response.status_code, 200, replay_response.get_json())
        replay_payload = replay_response.get_json()
        replay_steps = {step["id"]: step for step in replay_payload["steps"]}
        self.assertTrue(replay_payload["idempotent_replay"])
        self.assertEqual(replay_steps["create_subaccount"]["status"], "done")
        self.assertEqual(replay_steps["create_subaccount"]["operation"], "reuse")
        self.assertEqual(replay_steps["create_messaging_service"]["status"], "done")
        self.assertEqual(replay_steps["create_messaging_service"]["operation"], "reuse")
        self.assertEqual(replay_payload["state"]["twilio_account_sid"], "ACchild")
        self.assertEqual(replay_payload["state"]["messaging_service_sid"], "MGchild")
        self.assertEqual(replay_payload["state"]["status"], "pending_sender_registration")
        self.assertEqual(replay_payload["state"]["last_step"], "embedded_signup")

        account_calls = [call for call in calls if call[0].endswith("/Accounts.json")]
        messaging_service_calls = [call for call in calls if call[0] == "https://messaging.twilio.com/v1/Services"]
        self.assertEqual(len(account_calls), 1)
        self.assertEqual(len(messaging_service_calls), 2)

    def test_twilio_provision_retry_never_uses_an_unscoped_child_token(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_SUBACCOUNT_AUTH_TOKEN="unrelated-child-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=True,
            PUBLIC_API_BASE_URL="https://www.chatboc.ar",
        )
        self.tenant.configuracion = {
            "twilio_tech_provider": {
                "twilio_account_sid": "ACchild-without-specific-secret",
                "status": "messaging_service_failed",
            }
        }
        db.session.add(self.tenant)
        db.session.commit()

        with patch("services.twilio_tech_provider.requests.post") as post_request:
            response = self.client.post(
                f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/provision",
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-unscoped-secret-1"},
                json={"phone_number": "+5491112223333"},
            )

        self.assertEqual(response.status_code, 400, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["reason_code"], "twilio_subaccount_token_missing")
        self.assertNotIn("TWILIO_SUBACCOUNT_AUTH_TOKEN", payload.get("required_env", []))
        post_request.assert_not_called()

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
        self.assertEqual(payload["onboarding"]["status"], "sender_registered")
        register_channels = {item["id"]: item for item in payload["channel_activation"]["channels"]}
        self.assertEqual(register_channels["whatsapp"]["status"], "pending")
        self.assertFalse(register_channels["whatsapp"]["ready"])
        self.assertEqual(register_channels["whatsapp"]["reason_code"], "sender_not_online")
        self.assertEqual(register_channels["whatsapp"]["actions"][0]["id"], "open_sender_status")
        self.assertEqual(payload["voice_app"]["contract_version"], "twilio.tech_provider.voice_application.v1")
        self.assertEqual(len(calls), 4)
        sender = ProviderSender.query.filter_by(tenant_id=self.tenant.id, channel="whatsapp").first()
        self.assertIsNotNone(sender)
        self.assertEqual(sender.sender_sid, "XE123")
        self.assertEqual(sender.messaging_service_sid, "MGchild")
        self.assertEqual(sender.status, "pending")
        refreshed_after_register = db.session.get(TenantProfile, self.tenant.id)
        self.assertEqual(refreshed_after_register.configuracion["whatsapp_onboarding"]["status"], "sender_registered")

        def fake_get(url, **kwargs):
            self.assertEqual(url, "https://messaging.twilio.com/v2/Channels/Senders/XE123")
            return _FakeTwilioResponse(
                {
                    "sid": "XE123",
                    "status": "ACTIVE",
                    "sender_id": "whatsapp:+5491112223333",
                }
            )

        with patch("services.twilio_tech_provider.requests.get", side_effect=fake_get):
            status_response = self.client.post(
                f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/sender-status",
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-sender-status-1"},
            )

        self.assertEqual(status_response.status_code, 200, status_response.get_json())
        status_payload = status_response.get_json()
        self.assertEqual(status_payload["state"]["status"], "sender_online")
        self.assertEqual(status_payload["state"]["sender_status"], "ACTIVE")
        self.assertEqual(status_payload["onboarding"]["status"], "online")
        status_channels = {item["id"]: item for item in status_payload["channel_activation"]["channels"]}
        self.assertEqual(status_channels["whatsapp"]["status"], "ready")
        refreshed_after_status = db.session.get(TenantProfile, self.tenant.id)
        self.assertEqual(refreshed_after_status.configuracion["whatsapp_onboarding"]["status"], "online")

    def test_twilio_register_sender_keeps_success_when_optional_voice_fails(self):
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
                return _FakeTwilioResponse(
                    {
                        "sid": "XE123",
                        "status": "PENDING",
                        "sender_id": "whatsapp:+5491112223333",
                    },
                    status_code=201,
                )
            if url == "https://messaging.twilio.com/v1/Services/MGchild/ChannelSenders":
                return _FakeTwilioResponse({"sid": "XE123"}, status_code=201)
            if url == "https://api.twilio.com/2010-04-01/Accounts/ACchild/Applications.json":
                raise RuntimeError("voice application unavailable")
            raise AssertionError(f"unexpected Twilio URL {url}")

        with patch("services.twilio_tech_provider.requests.post", side_effect=fake_post):
            response = self.client.post(
                f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/register-sender",
                headers={**self._auth(self.owner), "X-Request-Id": "tech-provider-sender-voice-warning-1"},
                json={},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"]["status"], "sender_attached")
        self.assertEqual(payload["state"]["sender_sid"], "XE123")
        self.assertEqual(payload["state"]["sender_status"], "PENDING")
        self.assertEqual(payload["state"]["voice_status"], "voice_application_failed")
        self.assertFalse(payload["voice_app"]["ok"])
        self.assertTrue(payload["voice_app"]["optional"])
        self.assertEqual(payload["voice_app"]["reason_code"], "twilio_voice_application_upsert_failed")
        self.assertEqual(
            payload["voice_retry"],
            {
                "required": True,
                "method": "POST",
                "endpoint": f"/api/v2/tenants/{self.tenant.slug}/whatsapp/tech-provider/voice-app",
                "reason_code": "twilio_voice_application_upsert_failed",
            },
        )
        self.assertEqual(payload["warnings"][0]["code"], "optional_voice_provisioning_failed")
        self.assertEqual(payload["warnings"][0]["retry"], payload["voice_retry"])
        self.assertEqual(payload["onboarding"]["status"], "sender_registered")

        sender = ProviderSender.query.filter_by(tenant_id=self.tenant.id, channel="whatsapp").first()
        self.assertIsNotNone(sender)
        self.assertEqual(sender.sender_sid, "XE123")
        self.assertEqual(sender.status, "pending")
        refreshed = db.session.get(TenantProfile, self.tenant.id)
        state = refreshed.configuracion["twilio_tech_provider"]
        self.assertEqual(state["status"], "sender_attached")
        self.assertEqual(state["sender_sid"], "XE123")
        self.assertEqual(state["voice_status"], "voice_application_failed")
        self.assertEqual(len(calls), 3)

    def test_admin_catalog_exposes_and_saves_draft_endpoint(self):
        get_response = self.client.get(
            f"/api/admin/tenants/{self.tenant.slug}/catalog",
            headers=self._auth(self.owner),
        )

        self.assertEqual(get_response.status_code, 200)
        get_payload = get_response.get_json()
        self.assertEqual(get_payload["draft_endpoint"], f"/api/admin/tenants/{self.tenant.slug}/catalog/draft")
        self.assertEqual(get_payload["links"]["draft_endpoint"], f"/api/admin/tenants/{self.tenant.slug}/catalog/draft")
        self.assertTrue(get_payload["view_url"].endswith(f"/t/{self.tenant.slug}/market"))
        self.assertNotIn(f"/{self.tenant.slug}/catalogo", get_payload["view_url"])
        self.assertEqual(get_payload["contract_version"], "tenant.catalog_admin.v1")
        self.assertEqual(get_payload["frontend_contract"]["render_as"], "tenant_catalog_inventory_admin")
        self.assertTrue(get_payload["inventory"]["columns"]["stock_columns"])
        self.assertTrue(get_payload["frontend_contract"]["supports_promotions_command_center"])
        self.assertEqual(get_payload["promotions"]["contract_version"], "tenant.catalog_promotions_ops.v1")
        self.assertEqual(get_payload["promotions"]["endpoint"], f"/api/pymes/{self.owner.id}/promociones")
        self.assertEqual(get_payload["promotions"]["active"], 1)
        self.assertEqual(get_payload["promotions"]["catalog_items_with_promo_badge"], 1)
        self.assertEqual(get_payload["promotions"]["items"][0]["nombre_promocion"], "Promo vuelta a clases")
        self.assertEqual(
            get_payload["promotions"]["frontend_contract"]["render_as"],
            "catalog_promotions_command_center",
        )
        self.assertTrue(get_payload["frontend_contract"]["supports_marketplace_readiness"])
        readiness = get_payload["marketplace_readiness"]
        self.assertEqual(readiness["contract_version"], "tenant.marketplace_readiness.v1")
        self.assertEqual(readiness["frontend_contract"]["render_as"], "marketplace_readiness_panel")
        self.assertEqual(readiness["metrics"]["products_total"], 1)
        self.assertEqual(readiness["metrics"]["products_with_images"], 1)
        self.assertEqual(readiness["metrics"]["products_with_promotions"], 1)
        self.assertTrue(readiness["metrics"]["checkout_configured"])
        self.assertFalse(readiness["ready"])
        self.assertIn("missing_prices", {item["id"] for item in readiness["warnings"]})

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

    def test_omnichannel_inbox_live_chat_contract_reports_online_offline_and_queue(self):
        self.tenant.configuracion = {
            **self.tenant.configuracion,
            "live_chat_schedule": {
                "enabled": True,
                "days": ["fri"],
                "start_time": "09:00",
                "end_time": "18:00",
                "timezone": "UTC",
                "offline_message": "Dejanos tu mensaje y el equipo lo responde desde el ticket.",
            },
        }
        self.ticket.datos_extra = {
            **self.ticket.datos_extra,
            "comments": [
                {
                    "id": 1,
                    "body": "Hola",
                    "origin": "public_tracking",
                    "visibility": "public",
                    "created_at": "2026-07-10T09:15:00Z",
                },
                {
                    "id": 2,
                    "body": "Lo revisamos desde mesa de entrada.",
                    "origin": "admin_panel",
                    "visibility": "public",
                    "created_at": "2026-07-10T09:20:00Z",
                    "actor": {"id": self.employee.id, "type": "agent", "name": self.employee.name},
                },
            ],
        }
        db.session.commit()

        with patch("services.live_chat_schedule.datetime") as clock:
            clock.now.return_value = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
            online_response = self.client.get("/api/v2/inbox/omnichannel", headers=self._auth(self.owner))

        self.assertEqual(online_response.status_code, 200)
        online_payload = online_response.get_json()
        self.assertEqual(online_payload["live_chat"]["contract_version"], "inbox.live_chat_channel.v1")
        self.assertEqual(online_payload["live_chat"]["channel_state"], "online")
        self.assertEqual(online_payload["live_chat"]["availability"]["state"], "online")
        self.assertIn("09:00", online_payload["live_chat"]["availability"]["schedule_label"])
        self.assertEqual(
            online_payload["live_chat"]["offline_message"]["message"],
            "Dejanos tu mensaje y el equipo lo responde desde el ticket.",
        )
        online_item = online_payload["items"][0]
        self.assertEqual(online_item["live_chat"]["channel_state"], "online")
        self.assertEqual(online_payload["summary"]["queued_live_chat"], 0)

        with patch("services.live_chat_schedule.datetime") as clock:
            clock.now.return_value = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
            offline_response = self.client.get("/api/v2/inbox/omnichannel", headers=self._auth(self.owner))

        self.assertEqual(offline_response.status_code, 200)
        offline_payload = offline_response.get_json()
        self.assertEqual(offline_payload["live_chat"]["channel_state"], "offline")
        self.assertEqual(offline_payload["live_chat"]["availability"]["state"], "offline")
        self.assertEqual(offline_payload["items"][0]["live_chat"]["channel_state"], "offline")
        self.assertEqual(
            offline_payload["items"][0]["live_chat"]["availability"]["offline_fallback_message"],
            "Dejanos tu mensaje y el equipo lo responde desde el ticket.",
        )

        self.ticket.datos_extra = {
            **self.ticket.datos_extra,
            "comments": [
                *self.ticket.datos_extra["comments"],
                {
                    "id": 3,
                    "body": "Sigo esperando respuesta.",
                    "origin": "public_tracking",
                    "visibility": "public",
                    "created_at": "2026-07-10T20:05:00Z",
                },
            ],
        }
        db.session.commit()

        with patch("services.live_chat_schedule.datetime") as clock:
            clock.now.return_value = datetime(2026, 7, 10, 20, 10, tzinfo=timezone.utc)
            queued_response = self.client.get("/api/v2/inbox/omnichannel", headers=self._auth(self.owner))

        self.assertEqual(queued_response.status_code, 200)
        queued_payload = queued_response.get_json()
        queued_item = queued_payload["items"][0]
        self.assertEqual(queued_payload["live_chat"]["channel_state"], "offline")
        self.assertEqual(queued_item["live_chat"]["channel_state"], "queued")
        self.assertEqual(queued_item["live_chat"]["availability"]["base_state"], "offline")
        self.assertEqual(queued_item["live_chat"]["queue"]["state"], "waiting_team_response")
        self.assertEqual(queued_item["live_chat"]["queue"]["pending_customer_messages"], 1)
        self.assertEqual(queued_payload["summary"]["queued_live_chat"], 1)
        self.assertEqual(queued_payload["frontend_contract"]["live_chat_contract"], "inbox.live_chat_channel.v1")

    def test_omnichannel_pyme_inbound_creates_tenant_ticket_visible_in_inbox(self):
        from services.omnichannel_service import registrar_interaccion_omnicanal

        pyme_ticket_count = PymeTicket.query.count()

        result = registrar_interaccion_omnicanal(
            {
                "tipo_ticket": "pyme",
                "tenant_id": self.tenant.id,
                "canal": "WhatsApp",
                "mensaje": "Quiero cotizar uniformes para primer grado.",
                "asunto": "Consulta commerce",
                "categoria": "ventas",
                "source": "commerce_widget",
                "chat_session_id": "chat-commerce-1",
                "pedido_reference": "pedido:tmp-123",
                "lead_profile": {"interes": "uniformes"},
                "contacto": {
                    "nombre": "Cliente Anon",
                    "email": "cliente-anon@example.com",
                    "telefono": "+5492611111111",
                    "external_id": "anon-commerce-1",
                },
            }
        )

        self.assertTrue(result["exito"], result)
        self.assertTrue(result["nuevo_ticket"])
        self.assertEqual(result["source_model"], "TenantTicket")
        self.assertEqual(result["tenant_id"], self.tenant.id)
        self.assertEqual(PymeTicket.query.count(), pyme_ticket_count)

        ticket = db.session.get(TenantTicket, result["ticket_id"])
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.tenant_id, self.tenant.id)
        self.assertEqual(ticket.origen, "whatsapp")
        self.assertTrue(any("uniformes" in item.get("body", "") for item in ticket.datos_extra["comments"]))

        response = self.client.get("/api/v2/inbox/omnichannel?limit=10", headers=self._auth(self.owner))
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        item = next(entry for entry in payload["items"] if entry["source_model"] == "TenantTicket" and entry["id"] == ticket.id)
        self.assertEqual(item["channel"], "whatsapp")
        self.assertTrue(any("uniformes" in event["body"] for event in item["timeline"]))
        self.assertEqual(item["source_metadata"]["source"], "commerce_widget")
        self.assertEqual(item["source_metadata"]["chat_session_id"], "chat-commerce-1")
        self.assertEqual(item["source_metadata"]["anon_id"], "anon-commerce-1")
        self.assertEqual(item["source_metadata"]["pedido_reference"], "pedido:tmp-123")
        self.assertEqual(item["source_metadata"]["lead_profile"]["interes"], "uniformes")
        self.assertEqual(item["source_metadata"]["lead_profile"]["tenant_slug"], self.tenant.slug)
        self.assertEqual(item["source_metadata"]["contact"]["phone"], "+5492611111111")
        reply_action = next(action for action in item["allowed_actions"] if action["id"] == "reply")
        self.assertEqual(
            reply_action["delivery_mode"],
            "durable_queue_or_provider_acceptance",
        )
        self.assertEqual(reply_action["fallback"], "http_polling")
        self.assertTrue(reply_action["external_dispatch"])
        self.assertIn(
            "client_message_id_or_idempotency_key",
            reply_action["requires"],
        )
        self.assertEqual(
            reply_action["idempotency"]["preferred_header"],
            "Idempotency-Key",
        )

    def test_omnichannel_inbox_reads_source_attachment_as_regular_attachment(self):
        self.ticket.datos_extra = {
            **self.ticket.datos_extra,
            "attachments": [],
            "source_attachment": {
                "id": "source-att-1",
                "name": "pedido-manuscrito.jpg",
                "url": "https://cdn.example.com/pedido-manuscrito.jpg",
                "mimeType": "image/jpeg",
                "source": "pyme_multimodal",
            },
        }
        db.session.commit()

        response = self.client.get("/api/v2/inbox/omnichannel", headers=self._auth(self.owner))

        self.assertEqual(response.status_code, 200)
        item = response.get_json()["items"][0]
        self.assertEqual(item["attachments"][0]["id"], "source-att-1")
        self.assertEqual(item["attachments"][0]["url"], "https://cdn.example.com/pedido-manuscrito.jpg")
        self.assertEqual(item["attachments"][0]["source"], "pyme_multimodal")

    def test_omnichannel_inbox_includes_legacy_municipio_tracking_chat(self):
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-900144",
            consulta_pin="900144",
            pregunta="Arreglo de calle",
            asunto="Arreglo de calle",
            categoria="arreglo_de_calle",
            detalles="Bache abierto frente al domicilio",
            estado="nuevo",
            canal_ingreso="whatsapp",
            direccion="Don Bosco 55, Junin, Mendoza",
            latitud=-33.145,
            longitud=-68.47,
            nombre_vecino="Marcelo",
            telefono_vecino="+5492613168608",
            url_avatar_whatsapp="https://example.com/whatsapp-avatar.jpg",
        )
        db.session.add(legacy)
        db.session.flush()
        db.session.add(
            TicketComentario(
                municipio_ticket_id=legacy.id,
                comentario="Hola, quiero hablar con alguien en vivo.",
                es_admin=False,
                origen="public_tracking",
            )
        )
        db.session.commit()

        response = self.client.get("/api/v2/inbox/omnichannel?limit=20", headers=self._auth(self.owner))

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        legacy_item = next(
            item for item in payload["items"] if item.get("source_model") == "MunicipioTicket" and item.get("legacy_id") == legacy.id
        )
        self.assertEqual(legacy_item["legacy_kind"], "claim")
        self.assertEqual(legacy_item["ticket_id"], legacy.id)
        self.assertEqual(legacy_item["source_metadata"]["admin_surface"], "tenant_claims_inbox")
        self.assertEqual(legacy_item["source_metadata"]["read_model"], "TicketComentario")
        self.assertTrue(any("quiero hablar" in event["body"].lower() for event in legacy_item["timeline"]))
        legacy_reply_action = next(
            action for action in legacy_item["allowed_actions"] if action["id"] == "reply"
        )
        self.assertIn(
            "client_message_id_or_idempotency_key",
            legacy_reply_action["requires"],
        )
        self.assertEqual(
            legacy_reply_action["idempotency"]["preferred_header"],
            "Idempotency-Key",
        )
        self.assertEqual(
            legacy_reply_action["delivery_contract_version"],
            "inbox.action_delivery.v2",
        )
        tracking_action = next(action for action in legacy_item["allowed_actions"] if action["id"] == "open_tracking")
        self.assertEqual(
            tracking_action["endpoint"],
            "/api/public/tracking/experience?kind=claim&code=M-900144",
        )
        self.assertEqual(tracking_action["href"], "/tracking/claim/M-900144#pin=900144")
        self.assertEqual(tracking_action["frontend_path"], "/tracking/claim/M-900144#pin=900144")
        self.assertEqual(tracking_action["credential_transport"], "x-tracking-pin-header")
        self.assertEqual(legacy_item["source_metadata"]["tracking_code"], "M-900144")
        self.assertEqual(
            legacy_item["source_metadata"]["tracking_endpoint"],
            "/api/public/tracking/experience?kind=claim&code=M-900144",
        )
        self.assertEqual(legacy_item["source_metadata"]["tracking_href"], "/tracking/claim/M-900144#pin=900144")
        self.assertEqual(
            legacy_item["source_metadata"]["tracking_credential_transport"],
            "x-tracking-pin-header",
        )
        self.assertIsNone(legacy_item["contact"]["avatar"]["url"])
        self.assertEqual(legacy_item["frontend_contract"]["avatar_policy"], "consented_real_image_or_deterministic_fallback")

        detail = self.client.get(
            f"/api/v2/inbox/omnichannel/{legacy.id}?source_model=MunicipioTicket",
            headers=self._auth(self.owner),
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["item"]["source_model"], "MunicipioTicket")

        with patch(
            "services.notification_dispatcher.dispatch_ticket_update",
            return_value={"email": False, "sms": False, "whatsapp": False},
        ) as dispatch_update, patch("socket_service.socketio.emit") as socket_emit:
            reply = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json={
                    "source_model": "MunicipioTicket",
                    "legacy_id": legacy.id,
                    "action": "reply",
                    "body": "Te respondemos desde mesa de ayuda.",
                },
                headers={**self._auth(self.owner), "Idempotency-Key": "legacy-helpdesk-reply-1"},
            )
        self.assertEqual(reply.status_code, 200, reply.get_json())
        dispatch_update.assert_called_once()
        reply_payload = reply.get_json()
        delivery = reply_payload["delivery"]
        self.assertEqual(delivery["contract_version"], "inbox.action_delivery.v2")
        self.assertEqual(delivery["legacy_contract_version"], "inbox.action_delivery.v1")
        self.assertEqual(delivery["mode"], "timeline_only")
        self.assertEqual(delivery["status"], "saved_to_crm")
        self.assertEqual(delivery["reason"], "external_dispatch_no_channel_confirmed")
        self.assertEqual(delivery["reply_status"], "saved_to_timeline")
        self.assertEqual(delivery["admin_surface"], "tenant_claims_inbox")
        self.assertFalse(delivery["external_dispatch"])
        self.assertEqual(delivery["delivery_results"], {"email": False, "sms": False, "whatsapp": False})
        self.assertTrue(delivery["timeline_updated"])
        self.assertEqual(
            delivery["realtime"],
            {
                "emitted": True,
                "event": "new_chat_message",
                "events": ["new_chat_message", "ticket.status.changed"],
                "room": f"ticket_municipio_{legacy.id}",
                "fallback": "http_polling",
            },
        )
        public_calls = [
            item for item in socket_emit.call_args_list
            if item.args and item.args[0] == "new_chat_message"
        ]
        self.assertEqual(len(public_calls), 1)
        public_payload = public_calls[0].args[1]
        self.assertEqual(public_calls[0].kwargs["room"], f"ticket_municipio_{legacy.id}")
        self.assertEqual(public_payload["mensaje"], "Te respondemos desde mesa de ayuda.")
        self.assertEqual(public_payload["actor"], "agent")
        self.assertNotIn("user_id", public_payload["comment"])
        self.assertNotIn("anon_id", public_payload["comment"])
        public_status = next(
            item
            for item in socket_emit.call_args_list
            if item.args
            and item.args[0] == "ticket.status.changed"
            and item.kwargs.get("room") == f"ticket_municipio_{legacy.id}"
        )
        self.assertEqual(public_status.args[1]["estado"], "en_proceso")
        self.assertNotIn("municipio_id", public_status.args[1])
        updated = reply_payload["ticket"]
        self.assertEqual(updated["status"], "en_proceso")
        self.assertTrue(any("mesa de ayuda" in event["body"].lower() for event in updated["timeline"]))

        with patch("socket_service.socketio.emit") as close_socket_emit:
            closed = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json={
                    "source_model": "MunicipioTicket",
                    "legacy_id": legacy.id,
                    "action": "close",
                },
                headers=self._auth(self.owner),
            )
        self.assertEqual(closed.status_code, 200, closed.get_json())
        self.assertEqual(closed.get_json()["delivery"]["realtime"]["events"], ["ticket.status.changed"])
        closed_public_status = next(
            item
            for item in close_socket_emit.call_args_list
            if item.args
            and item.args[0] == "ticket.status.changed"
            and item.kwargs.get("room") == f"ticket_municipio_{legacy.id}"
        )
        self.assertEqual(closed_public_status.args[1]["estado"], "cerrado")

    def test_omnichannel_legacy_claim_reply_reports_provider_acceptance_only(self):
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-777001",
            consulta_pin="777001",
            pregunta="Luminaria apagada",
            asunto="Luminaria",
            categoria="luminaria",
            detalles="Lampara apagada en la esquina",
            estado="nuevo",
            canal_ingreso="whatsapp",
            direccion="Rivadavia 100, Junin, Mendoza",
            nombre_vecino="Marcelo",
            telefono_vecino="+5492613168608",
        )
        db.session.add(legacy)
        db.session.commit()

        with patch(
            "services.notification_dispatcher.dispatch_ticket_update",
            return_value={"email": False, "sms": False, "whatsapp": True},
        ) as dispatch_update:
            response = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json={
                    "source_model": "MunicipioTicket",
                    "legacy_id": legacy.id,
                    "action": "reply",
                    "body": "Recibimos tu reclamo y el equipo ya fue avisado.",
                },
                headers={**self._auth(self.owner), "X-Request-Id": "legacy-whatsapp-delivery-1"},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        dispatch_update.assert_called_once()
        payload = response.get_json()
        self.assertEqual(payload.get("request_id"), "legacy-whatsapp-delivery-1")
        delivery = payload["delivery"]
        self.assertEqual(delivery["contract_version"], "inbox.action_delivery.v2")
        self.assertEqual(delivery["legacy_contract_version"], "inbox.action_delivery.v1")
        self.assertEqual(delivery["mode"], "real_message")
        self.assertEqual(delivery["channel"], "whatsapp")
        self.assertEqual(delivery["status"], "provider_accepted")
        self.assertEqual(delivery["reason"], "provider_accepted")
        self.assertEqual(delivery["reply_status"], "provider_accepted")
        self.assertEqual(delivery["evidence_stage"], "provider_accepted")
        self.assertEqual(delivery["delivery_results_semantics"], "provider_acceptance")
        self.assertEqual(delivery["final_delivery"]["status"], "pending_provider_callback")
        self.assertEqual(delivery["final_delivery"]["authoritative_source"], "provider_status_callback")
        self.assertNotEqual(delivery["final_delivery"]["status"], "delivered")
        self.assertEqual(delivery["admin_surface"], "tenant_claims_inbox")
        self.assertTrue(delivery["external_dispatch"])
        self.assertEqual(delivery["delivery_results"], {"email": False, "sms": False, "whatsapp": True})
        self.assertTrue(any("equipo ya fue avisado" in event["body"].lower() for event in payload["ticket"]["timeline"]))

    def test_omnichannel_legacy_claim_reply_survives_dispatcher_failure(self):
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-777002",
            consulta_pin="777002",
            pregunta="Perdida de agua",
            asunto="Agua",
            categoria="perdida_de_agua",
            detalles="Perdida en la vereda",
            estado="nuevo",
            canal_ingreso="whatsapp",
            direccion="Belgrano 200, Junin, Mendoza",
            nombre_vecino="Marcelo",
            telefono_vecino="+5492613168608",
        )
        db.session.add(legacy)
        db.session.commit()

        with patch(
            "services.notification_dispatcher.dispatch_ticket_update",
            side_effect=RuntimeError("twilio down"),
        ):
            response = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json={
                    "source_model": "MunicipioTicket",
                    "legacy_id": legacy.id,
                    "action": "reply",
                    "body": "Guardamos tu mensaje y seguimos el caso.",
                },
                headers={**self._auth(self.owner), "Idempotency-Key": "legacy-dispatch-failure-1"},
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        delivery = payload["delivery"]
        self.assertEqual(delivery["mode"], "timeline_only")
        self.assertEqual(delivery["status"], "saved_to_crm")
        self.assertEqual(delivery["reason"], "notification_dispatch_failed")
        self.assertFalse(delivery["external_dispatch"])
        self.assertEqual(delivery["delivery_results"], {"email": False, "sms": False, "whatsapp": False})
        self.assertTrue(any("seguimos el caso" in event["body"].lower() for event in payload["ticket"]["timeline"]))

    def test_omnichannel_legacy_claim_reply_replays_once_without_second_dispatch(self):
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-777003",
            consulta_pin="777003",
            pregunta="Semaforo sin funcionar",
            asunto="Semaforo",
            categoria="semaforo",
            detalles="Semaforo intermitente",
            estado="nuevo",
            canal_ingreso="whatsapp",
            nombre_vecino="Marcelo",
            telefono_vecino="+5492613168608",
        )
        db.session.add(legacy)
        db.session.commit()
        request_payload = {
            "source_model": "MunicipioTicket",
            "legacy_id": legacy.id,
            "action": "reply",
            "body": "La cuadrilla ya recibio el aviso.",
            "client_message_id": "crm-client-message-777003",
        }

        with patch(
            "services.notification_dispatcher.dispatch_ticket_update",
            return_value={"email": False, "sms": False, "whatsapp": True},
        ) as dispatch_update, patch(
            "services.email_service.enviar_email_ticket_novedad"
        ) as service_email, patch(
            "services.email_service.enviar_sms_ticket_novedad"
        ) as service_sms, patch(
            "services.email_service.enviar_whatsapp_ticket_novedad"
        ) as service_whatsapp, patch(
            "socket_service.emit_new_chat_message"
        ) as service_socket, patch("socket_service.socketio.emit"):
            first = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json=request_payload,
                headers=self._auth(self.owner),
            )
            replay = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json=request_payload,
                headers=self._auth(self.owner),
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        dispatch_update.assert_called_once()
        service_email.assert_not_called()
        service_sms.assert_not_called()
        service_whatsapp.assert_not_called()
        service_socket.assert_not_called()
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=legacy.id).count(),
            1,
        )
        receipt = TicketDomainEffectReceipt.query.filter_by(tenant_id=self.tenant.id).one()
        self.assertTrue(receipt.idempotency_key.startswith("crm-reply:"))
        self.assertNotIn("crm-client-message-777003", receipt.idempotency_key)
        self.assertEqual(DomainEffectOutbox.query.filter_by(tenant_id=self.tenant.id).count(), 0)
        self.assertEqual(first.get_json()["delivery"]["status"], "provider_accepted")
        replay_delivery = replay.get_json()["delivery"]
        self.assertEqual(replay_delivery["mode"], "idempotent_replay")
        self.assertEqual(replay_delivery["status"], "already_recorded")
        self.assertEqual(replay_delivery["reason"], "idempotent_replay_no_redispatch")
        self.assertFalse(replay_delivery["external_dispatch"])
        self.assertFalse(replay_delivery["timeline_updated"])
        self.assertTrue(replay_delivery["idempotency"]["replayed"])
        self.assertEqual(
            replay_delivery["final_delivery"]["status"],
            "preserved_from_original_attempt",
        )

    def test_omnichannel_legacy_claim_reply_rejects_same_key_with_different_payload(self):
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-777004",
            consulta_pin="777004",
            pregunta="Bache peligroso",
            asunto="Bache",
            categoria="arreglo_de_calle",
            detalles="Bache frente a la escuela",
            estado="nuevo",
            canal_ingreso="whatsapp",
            nombre_vecino="Marcelo",
            telefono_vecino="+5492613168608",
        )
        db.session.add(legacy)
        db.session.commit()
        headers = {**self._auth(self.owner), "Idempotency-Key": "crm-reply-conflict-777004"}
        base_payload = {
            "source_model": "MunicipioTicket",
            "legacy_id": legacy.id,
            "action": "reply",
        }

        with patch(
            "services.notification_dispatcher.dispatch_ticket_update",
            return_value={"email": False, "sms": False, "whatsapp": False},
        ) as dispatch_update, patch("socket_service.socketio.emit"):
            first = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json={**base_payload, "body": "Primera respuesta."},
                headers=headers,
            )
            conflict = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json={**base_payload, "body": "Respuesta distinta."},
                headers=headers,
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(
            conflict.get_json()["reason_code"],
            "reply_idempotency_payload_conflict",
        )
        dispatch_update.assert_called_once()
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=legacy.id).count(),
            1,
        )
        self.assertEqual(
            TicketDomainEffectReceipt.query.filter_by(tenant_id=self.tenant.id).count(),
            1,
        )

    def test_omnichannel_legacy_claim_reply_requires_stable_client_identity(self):
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-777007",
            consulta_pin="777007",
            pregunta="Luminaria apagada",
            asunto="Luminaria",
            categoria="luminaria",
            detalles="Sin luz desde anoche",
            estado="nuevo",
            canal_ingreso="whatsapp",
        )
        db.session.add(legacy)
        db.session.commit()

        response = self.client.post(
            "/api/v2/inbox/omnichannel/actions",
            json={
                "source_model": "MunicipioTicket",
                "legacy_id": legacy.id,
                "action": "reply",
                "body": "Estamos revisando el reclamo.",
            },
            headers=self._auth(self.owner),
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(
            response.get_json()["reason_code"],
            "reply_idempotency_key_required",
        )
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=legacy.id).count(),
            0,
        )
        self.assertEqual(
            TicketDomainEffectReceipt.query.filter_by(tenant_id=self.tenant.id).count(),
            0,
        )

    def test_omnichannel_legacy_claim_canary_stages_effects_and_replays_without_direct_io(self):
        self._enable_municipal_domain_outbox()
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-777005",
            consulta_pin="777005",
            pregunta="Arbol caido",
            asunto="Arbolado",
            categoria="arbolado",
            detalles="Rama bloqueando la calle",
            estado="nuevo",
            canal_ingreso="whatsapp",
            nombre_vecino="Marcelo",
            telefono_vecino="+5492613168608",
            email_vecino="marcelo@example.com",
        )
        db.session.add(legacy)
        db.session.commit()
        headers = {**self._auth(self.owner), "Idempotency-Key": "crm-canary-reply-777005"}
        request_payload = {
            "source_model": "MunicipioTicket",
            "legacy_id": legacy.id,
            "action": "reply",
            "body": "El equipo de arbolado ya tiene el caso.",
        }

        with patch(
            "routes.v2.saas._dispatch_legacy_claim_reply"
        ) as direct_dispatch, patch(
            "routes.v2.saas._emit_legacy_claim_realtime_reply"
        ) as direct_reply_socket, patch(
            "routes.v2.saas._emit_legacy_claim_realtime_state"
        ) as direct_state_socket, patch(
            "services.domain_effect_worker.enqueue_domain_effect_dispatch"
        ) as enqueue_dispatch:
            first = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json=request_payload,
                headers=headers,
            )
            replay = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json=request_payload,
                headers=headers,
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        direct_dispatch.assert_not_called()
        direct_reply_socket.assert_not_called()
        direct_state_socket.assert_not_called()
        enqueue_dispatch.assert_called_once_with(tenant_id=self.tenant.id)
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=legacy.id).count(),
            1,
        )
        self.assertEqual(
            TicketDomainEffectReceipt.query.filter_by(tenant_id=self.tenant.id).count(),
            1,
        )
        effects = DomainEffectOutbox.query.filter_by(tenant_id=self.tenant.id).all()
        self.assertEqual(len(effects), 4)
        self.assertEqual(
            {row.channel for row in effects},
            {"email", "sms", "whatsapp", "realtime"},
        )
        first_delivery = first.get_json()["delivery"]
        self.assertEqual(first_delivery["mode"], "durable_queue")
        self.assertEqual(first_delivery["status"], "durably_staged")
        self.assertEqual(first_delivery["reason"], "domain_effects_durably_staged")
        self.assertEqual(first_delivery["reply_status"], "queued_for_delivery")
        self.assertFalse(first_delivery["external_dispatch"])
        self.assertEqual(first_delivery["outbox"]["effect_count"], 4)
        self.assertFalse(first_delivery["outbox"]["direct_dispatch_performed"])
        self.assertEqual(
            first_delivery["final_delivery"]["status"],
            "pending_provider_callback",
        )
        replay_delivery = replay.get_json()["delivery"]
        self.assertEqual(replay_delivery["mode"], "idempotent_replay")
        self.assertEqual(replay_delivery["status"], "already_recorded")
        self.assertEqual(
            replay_delivery["reason"],
            "idempotent_replay_domain_effects_preserved",
        )
        self.assertTrue(replay_delivery["idempotency"]["replayed"])

    def test_omnichannel_legacy_claim_canary_rolls_back_comment_receipt_and_partial_effects(self):
        self._enable_municipal_domain_outbox()
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-777006",
            consulta_pin="777006",
            pregunta="Perdida de agua",
            asunto="Agua",
            categoria="perdida_de_agua",
            detalles="Canio roto en la vereda",
            estado="nuevo",
            canal_ingreso="whatsapp",
            nombre_vecino="Marcelo",
            telefono_vecino="+5492613168608",
        )
        db.session.add(legacy)
        db.session.commit()
        from services import ticket_domain_effects

        original_stage = ticket_domain_effects.stage_domain_effect
        stage_count = 0

        def fail_after_partial_stage(**kwargs):
            nonlocal stage_count
            stage_count += 1
            result = original_stage(**kwargs)
            if stage_count == 2:
                raise RuntimeError("simulated outbox staging crash")
            return result

        with patch.object(
            ticket_domain_effects,
            "stage_domain_effect",
            side_effect=fail_after_partial_stage,
        ), patch("routes.v2.saas._dispatch_legacy_claim_reply") as direct_dispatch:
            with self.assertRaisesRegex(RuntimeError, "simulated outbox staging crash"):
                self.client.post(
                    "/api/v2/inbox/omnichannel/actions",
                    json={
                        "source_model": "MunicipioTicket",
                        "legacy_id": legacy.id,
                        "action": "reply",
                        "body": "Recibimos el aviso.",
                    },
                    headers={
                        **self._auth(self.owner),
                        "Idempotency-Key": "crm-canary-crash-777006",
                    },
                )

        direct_dispatch.assert_not_called()
        self.assertEqual(stage_count, 2)
        self.assertEqual(
            TicketComentario.query.filter_by(municipio_ticket_id=legacy.id).count(),
            0,
        )
        self.assertEqual(
            TicketDomainEffectReceipt.query.filter_by(tenant_id=self.tenant.id).count(),
            0,
        )
        self.assertEqual(DomainEffectOutbox.query.filter_by(tenant_id=self.tenant.id).count(), 0)
        persisted = db.session.get(MunicipioTicket, legacy.id)
        self.assertEqual(persisted.estado, "nuevo")

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
        self.assertTrue(payload["frontend_contract"]["show_e2e_flow_readiness"])
        e2e = payload["e2e_flow_readiness"]
        self.assertEqual(e2e["contract_version"], "platform.e2e_flow_readiness.v1")
        self.assertGreaterEqual(e2e["summary"]["total"], 6)
        flow_ids = {item["id"] for item in e2e["flows"]}
        self.assertIn("gov_claim_text_to_tracking", flow_ids)
        self.assertIn("claim_live_or_offline_helpdesk", flow_ids)
        self.assertIn("pyme_catalog_order_checkout", flow_ids)
        self.assertIn("survey_vote_realtime", flow_ids)
        self.assertIn("school_family_case", flow_ids)
        self.assertIn("finance_in_chat_transactional", flow_ids)
        self.assertIn("finance_servicing_transfer_insurance", flow_ids)
        self.assertIn("analytics_heatmap", flow_ids)
        claim_flow = next(item for item in e2e["flows"] if item["id"] == "gov_claim_text_to_tracking")
        self.assertEqual(claim_flow["qa_scenario_id"], "gov_claim_text_to_tracking")
        self.assertEqual(claim_flow["endpoint"], "/api/public/tracking/experience?kind=claim&code={code}")
        self.assertEqual(claim_flow["frontend_entry"], "/perfil?tab=tickets")
        self.assertGreaterEqual(len(claim_flow["manual_test_steps"]), 3)
        self.assertGreaterEqual(len(claim_flow["acceptance_criteria"]), 3)
        self.assertTrue(claim_flow["automation"]["safe_by_default"])
        self.assertFalse(claim_flow["automation"]["live_side_effects"])
        self.assertFalse(claim_flow["automation"]["uses_real_whatsapp"])
        survey_flow = next(item for item in e2e["flows"] if item["id"] == "survey_vote_realtime")
        self.assertEqual(survey_flow["frontend_entry"], "/perfil?tab=surveys")
        self.assertIn("test_surveys_live_vote_flow.py", survey_flow["automation"]["suggested_command"])
        finance_flow = next(item for item in e2e["flows"] if item["id"] == "finance_in_chat_transactional")
        self.assertEqual(finance_flow["qa_scenario_id"], "finance_onboarding_collection_signature")
        self.assertEqual(finance_flow["evidence"]["templates_group"], "financial_services")
        finance_advanced_flow = next(item for item in e2e["flows"] if item["id"] == "finance_servicing_transfer_insurance")
        self.assertEqual(finance_advanced_flow["qa_scenario_id"], "finance_account_servicing")
        self.assertEqual(finance_advanced_flow["evidence"]["finance_runtime"], "finance.transactional_whatsapp.v1")
        self.assertIn("next_action", claim_flow)

    def test_omnichannel_inbox_action_updates_ticket(self):
        with patch(
            "utils.whatsapp.enviar_mensaje_whatsapp_con_fallback",
            return_value=True,
        ) as send_whatsapp:
            response = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={"action": "reply", "body": "Estamos revisando tu caso.", "visibility": "public"},
                headers={**self._auth(self.owner), "X-Request-Id": "inbox-action-1"},
            )

        self.assertEqual(response.status_code, 200)
        send_whatsapp.assert_called_once_with(
            "+5491111111111",
            "Estamos revisando tu caso.",
            from_number="whatsapp:+100",
        )
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "inbox.omnichannel.action.v1")
        self.assertEqual(payload.get("request_id"), "inbox-action-1")
        self.assertEqual(payload["delivery"]["contract_version"], "inbox.action_delivery.v2")
        self.assertEqual(payload["delivery"]["legacy_contract_version"], "inbox.action_delivery.v1")
        self.assertEqual(payload["delivery"]["mode"], "real_message")
        self.assertEqual(payload["delivery"]["delivery_mode"], "real_message")
        self.assertEqual(payload["delivery"]["status"], "provider_accepted")
        self.assertEqual(payload["delivery"]["fallback"], "none")
        self.assertEqual(payload["delivery"]["reply_status"], "provider_accepted")
        self.assertEqual(payload["delivery"]["final_delivery"]["status"], "pending_provider_callback")
        self.assertEqual(payload["delivery"]["admin_surface"], "omnichannel_inbox")
        self.assertTrue(payload["delivery"]["external_dispatch"])
        self.assertTrue(payload["delivery"]["timeline_updated"])
        self.assertEqual(payload["delivery"]["requested_channels"], ["whatsapp"])
        self.assertEqual(
            payload["delivery"]["delivery_results"],
            {"email": False, "sms": False, "whatsapp": True},
        )
        self.assertIn("acepto", payload["delivery"]["operator_message"].lower())
        self.assertTrue(payload["ticket"]["timeline"])
        self.assertTrue(any(item.get("body") == "Estamos revisando tu caso." for item in payload["ticket"]["timeline"]))
        db.session.refresh(self.ticket)
        delivery_history = self.ticket.datos_extra.get("reply_delivery_history") or []
        self.assertEqual(delivery_history[-1]["mode"], "real_message")
        self.assertEqual(delivery_history[-1]["status"], "provider_accepted")
        self.assertEqual(
            delivery_history[-1]["final_delivery"]["status"],
            "pending_provider_callback",
        )
        self.assertTrue(delivery_history[-1]["external_dispatch"])

    def test_omnichannel_tenant_reply_can_remain_timeline_only(self):
        with patch("utils.whatsapp.enviar_mensaje_whatsapp_con_fallback") as send_whatsapp:
            response = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={
                    "action": "reply",
                    "body": "Nota operativa sin envio externo.",
                    "visibility": "internal",
                    "send_external": True,
                    "delivery_channels": ["whatsapp"],
                    "client_message_id": "crm-reply:internal-note-0001",
                },
                headers=self._auth(self.owner),
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        send_whatsapp.assert_not_called()
        delivery = response.get_json()["delivery"]
        self.assertEqual(delivery["mode"], "timeline_only")
        self.assertEqual(delivery["status"], "saved_to_crm")
        self.assertEqual(delivery["reason"], "external_dispatch_no_channel_requested")
        self.assertEqual(delivery["requested_channels"], [])
        self.assertFalse(delivery["external_dispatch"])
        self.assertEqual(
            delivery["delivery_results"],
            {"email": False, "sms": False, "whatsapp": False},
        )

    def test_omnichannel_tenant_reply_dispatches_email_from_user_profile(self):
        with patch("services.email_service.enviar_email", return_value=True) as send_email:
            response = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={
                    "action": "reply",
                    "body": "<Gracias>\nSeguimos con tu caso.",
                    "visibility": "public",
                    "delivery_channels": ["email"],
                    "client_message_id": "crm-reply:email-profile-0001",
                },
                headers=self._auth(self.owner),
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        send_email.assert_called_once_with(
            self.owner.email,
            f"Respuesta de {self.tenant.nombre} - solicitud #{self.ticket.id}",
            "<p>&lt;Gracias&gt;<br>Seguimos con tu caso.</p>",
            cuerpo_texto="<Gracias>\nSeguimos con tu caso.",
        )
        delivery = response.get_json()["delivery"]
        self.assertEqual(delivery["mode"], "real_message")
        self.assertEqual(delivery["channel"], "email")
        self.assertEqual(delivery["requested_channels"], ["email"])
        self.assertEqual(
            delivery["delivery_results"],
            {"email": True, "sms": False, "whatsapp": False},
        )
        self.assertTrue(delivery["external_dispatch"])

    def test_omnichannel_tenant_reply_retries_once_without_duplicate_dispatch(self):
        client_message_id = "crm-reply:tenant-retry-0001"
        request_payload = {
            "action": "reply",
            "body": "Estamos revisando tu solicitud.",
            "visibility": "public",
            "client_message_id": client_message_id,
        }
        headers = {
            **self._auth(self.owner),
            "Idempotency-Key": client_message_id,
        }

        with patch(
            "utils.whatsapp.enviar_mensaje_whatsapp_con_fallback",
            return_value=True,
        ) as send_whatsapp, patch(
            "routes.v2.saas._emit_tenant_ticket_realtime_reply",
            return_value=True,
        ) as emit_realtime:
            first = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json=request_payload,
                headers=headers,
            )
            replay = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json=request_payload,
                headers=headers,
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        send_whatsapp.assert_called_once()
        emit_realtime.assert_called_once()
        db.session.refresh(self.ticket)
        matching_events = [
            event
            for event in self.ticket.datos_extra.get("comments", [])
            if event.get("body") == request_payload["body"]
        ]
        self.assertEqual(len(matching_events), 1)
        receipts = TicketDomainEffectReceipt.query.filter_by(
            tenant_id=self.tenant.id,
            effect_kind="ticket.comment.tenant",
        ).all()
        self.assertEqual(len(receipts), 1)
        self.assertNotIn(client_message_id, receipts[0].idempotency_key)
        self.assertEqual(first.get_json()["delivery"]["mode"], "real_message")
        replay_delivery = replay.get_json()["delivery"]
        self.assertEqual(replay_delivery["mode"], "idempotent_replay")
        self.assertEqual(replay_delivery["status"], "already_recorded")
        self.assertFalse(replay_delivery["timeline_updated"])
        self.assertTrue(replay_delivery["idempotency"]["replayed"])
        self.assertEqual(
            len(self.ticket.datos_extra.get("reply_delivery_history", [])),
            1,
        )

    def test_omnichannel_tenant_reply_rejects_key_reuse_for_another_body(self):
        client_message_id = "crm-reply:tenant-conflict-0001"
        headers = {
            **self._auth(self.owner),
            "Idempotency-Key": client_message_id,
        }
        common = {
            "action": "reply",
            "visibility": "public",
            "send_external": False,
            "client_message_id": client_message_id,
        }

        with patch(
            "routes.v2.saas._emit_tenant_ticket_realtime_reply",
            return_value=True,
        ) as emit_realtime:
            first = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={**common, "body": "Primera respuesta."},
                headers=headers,
            )
            conflict = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={**common, "body": "Texto diferente."},
                headers=headers,
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(
            conflict.get_json()["reason_code"],
            "reply_idempotency_payload_conflict",
        )
        emit_realtime.assert_called_once()
        db.session.refresh(self.ticket)
        bodies = [
            event.get("body")
            for event in self.ticket.datos_extra.get("comments", [])
        ]
        self.assertIn("Primera respuesta.", bodies)
        self.assertNotIn("Texto diferente.", bodies)
        self.assertEqual(
            TenantTicketReplyEvent.query.filter_by(
                tenant_id=self.tenant.id,
                ticket_id=self.ticket.id,
            ).count(),
            1,
        )

    def test_omnichannel_tenant_reply_provider_failure_is_durable_and_not_resent(self):
        client_message_id = "crm-reply:tenant-provider-failure-0001"
        request_payload = {
            "action": "reply",
            "body": "Tu caso quedo registrado para seguimiento.",
            "visibility": "public",
            "client_message_id": client_message_id,
        }
        headers = {
            **self._auth(self.owner),
            "Idempotency-Key": client_message_id,
        }

        with patch(
            "utils.whatsapp.enviar_mensaje_whatsapp_con_fallback",
            side_effect=RuntimeError("provider unavailable"),
        ) as send_whatsapp, patch(
            "routes.v2.saas._emit_tenant_ticket_realtime_reply",
            return_value=True,
        ):
            first = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json=request_payload,
                headers=headers,
            )
            replay = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json=request_payload,
                headers=headers,
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        send_whatsapp.assert_called_once()
        first_delivery = first.get_json()["delivery"]
        self.assertEqual(first_delivery["mode"], "timeline_only")
        self.assertEqual(first_delivery["reason"], "external_dispatch_failed")
        self.assertEqual(
            first_delivery["delivery_skipped"],
            {"whatsapp": "provider_error"},
        )
        self.assertFalse(first_delivery["external_dispatch"])
        self.assertEqual(
            replay.get_json()["delivery"]["reason"],
            "idempotent_replay_no_redispatch",
        )
        db.session.refresh(self.ticket)
        self.assertEqual(
            sum(
                1
                for event in self.ticket.datos_extra.get("comments", [])
                if event.get("body") == request_payload["body"]
            ),
            1,
        )

    def test_omnichannel_tenant_reply_outbox_stages_once_and_replays(self):
        self._enable_tenant_domain_outbox()
        client_message_id = "crm-reply:tenant-outbox-0001"
        request_payload = {
            "action": "reply",
            "body": "La respuesta quedo encolada de forma durable.",
            "visibility": "public",
            "client_message_id": client_message_id,
        }
        headers = {
            **self._auth(self.owner),
            "Idempotency-Key": client_message_id,
        }

        with patch(
            "routes.v2.saas._dispatch_tenant_ticket_reply"
        ) as direct_dispatch, patch(
            "routes.v2.saas._emit_tenant_ticket_realtime_reply"
        ) as direct_realtime, patch(
            "services.domain_effect_worker.enqueue_domain_effect_dispatch"
        ) as enqueue_dispatch:
            first = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json=request_payload,
                headers=headers,
            )
            replay = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json=request_payload,
                headers=headers,
            )

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        direct_dispatch.assert_not_called()
        direct_realtime.assert_not_called()
        enqueue_dispatch.assert_called_once_with(tenant_id=self.tenant.id)
        rows = DomainEffectOutbox.query.filter_by(
            tenant_id=self.tenant.id,
            aggregate_type="tenant_ticket_reply",
        ).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.channel for row in rows}, {"whatsapp", "realtime"})
        self.assertEqual(
            TicketDomainEffectReceipt.query.filter_by(
                tenant_id=self.tenant.id,
                effect_kind="ticket.comment.tenant",
            ).count(),
            1,
        )
        first_delivery = first.get_json()["delivery"]
        self.assertEqual(first_delivery["mode"], "durable_queue")
        self.assertEqual(first_delivery["outbox"]["effect_count"], 2)
        self.assertTrue(first_delivery["realtime"]["queued"])
        replay_delivery = replay.get_json()["delivery"]
        self.assertEqual(replay_delivery["mode"], "idempotent_replay")
        self.assertEqual(
            replay_delivery["reason"],
            "idempotent_replay_domain_effects_preserved",
        )
        self.assertEqual(replay_delivery["outbox"]["effect_count"], 2)

    def test_tenant_reply_outbox_uses_pinned_event_after_timeline_pruning(self):
        self._enable_tenant_domain_outbox()
        original_email = "citizen-original@test.com"
        original_phone = "+5491111111111"
        current_extra = dict(self.ticket.datos_extra or {})
        current_extra["contact"] = {
            "name": "Familia original",
            "email": original_email,
            "phone": original_phone,
        }
        self.ticket.datos_extra = current_extra

        token_ref = f"TWILIO_SUBACCOUNT_AUTH_TOKEN_REPLY_PIN_{self.tenant.id}"
        account_sid = f"AC-reply-pin-{self.tenant.id}"
        self.app.config[token_ref] = f"reply-pin-token-{self.tenant.id}"
        self.tenant.configuracion = {
            **(self.tenant.configuracion or {}),
            "twilio_tech_provider": {
                "twilio_account_sid": account_sid,
                "twilio_subaccount_token_ref": token_ref,
            },
        }
        connection = ProviderConnection(
            tenant_id=self.tenant.id,
            provider="twilio",
            channel="whatsapp",
            environment="production",
            status="online",
            external_account_id=account_sid,
            credentials_ref=f"env:{token_ref}",
        )
        db.session.add(connection)
        db.session.flush()
        db.session.add(
            ProviderSender(
                tenant_id=self.tenant.id,
                provider_connection_id=connection.id,
                channel="whatsapp",
                sender_type="whatsapp_business",
                phone_number="+15005550006",
                sender_id="whatsapp:+15005550006",
                status="online",
                status_callback_url="https://api.example.test/twilio/whatsapp/status",
            )
        )
        db.session.commit()

        first_body = "Respuesta que debe sobrevivir a la poda del timeline."
        client_message_id = "crm-reply:pinned-pruned-0001"
        request_payload = {
            "action": "reply",
            "body": first_body,
            "visibility": "public",
            "delivery_channels": ["email", "whatsapp"],
            "client_message_id": client_message_id,
        }
        headers = {
            **self._auth(self.owner),
            "Idempotency-Key": client_message_id,
        }
        with patch(
            "services.domain_effect_worker.enqueue_domain_effect_dispatch"
        ) as enqueue_dispatch:
            first = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json=request_payload,
                headers=headers,
            )
        self.assertEqual(first.status_code, 200, first.get_json())
        enqueue_dispatch.assert_called_once_with(tenant_id=self.tenant.id)

        from services.ticket_service import ServicioTickets

        ticket = db.session.get(TenantTicket, self.ticket.id)
        for index in range(100):
            ServicioTickets().crear_respuesta_tenant(
                ticket,
                {
                    "body": f"Respuesta posterior {index:03d}",
                    "visibility": "internal",
                    "actor_user_id": self.owner.id,
                    "actor_name": self.owner.name,
                    "actor_role": self.owner.rol,
                    "requested_channels": [],
                    "emit_socket": False,
                },
                idempotency_key=f"crm-reply:prune-{index:04d}",
                idempotency_tenant_id=self.tenant.id,
            )

        db.session.refresh(ticket)
        timeline = ticket.datos_extra.get("comments") or []
        self.assertEqual(len(timeline), 100)
        self.assertFalse(any(item.get("body") == first_body for item in timeline))
        self.assertEqual(
            TenantTicketReplyEvent.query.filter_by(
                tenant_id=self.tenant.id,
                ticket_id=self.ticket.id,
            ).count(),
            101,
        )
        pinned = TenantTicketReplyEvent.query.filter_by(
            tenant_id=self.tenant.id,
            ticket_id=self.ticket.id,
            body=first_body,
        ).one()
        self.assertEqual(pinned.recipient_email, original_email)
        self.assertEqual(pinned.recipient_phone, original_phone)

        changed_extra = dict(ticket.datos_extra or {})
        changed_extra["contact"] = {
            "name": "Contacto reemplazado",
            "email": "redirected@test.com",
            "phone": "+5492222222222",
        }
        ticket.datos_extra = changed_extra
        self.owner.email = "redirected-owner@test.com"
        db.session.add_all([ticket, self.owner])
        db.session.commit()

        effects = DomainEffectOutbox.query.filter_by(
            tenant_id=self.tenant.id,
            aggregate_type="tenant_ticket_reply",
            aggregate_ref=f"{self.ticket.id}:{pinned.event_id}",
        ).all()
        self.assertEqual(len(effects), 3)
        for effect in effects:
            self.assertTrue(effect.recipient_ref.startswith("recipient_hash:"))
            serialized = json.dumps(effect.payload_json, sort_keys=True)
            self.assertNotIn(first_body, serialized)
            self.assertNotIn(original_email, serialized)
            self.assertNotIn(original_phone, serialized)
        receipt = next(
            item
            for item in TicketDomainEffectReceipt.query.filter_by(
                tenant_id=self.tenant.id,
                effect_kind="ticket.comment.tenant",
                resource_id=self.ticket.id,
            ).all()
            if (item.result_json or {}).get("event_id") == pinned.event_id
        )
        receipt_result = json.dumps(receipt.result_json, sort_keys=True)
        self.assertNotIn(first_body, receipt_result)
        self.assertNotIn(original_email, receipt_result)
        self.assertNotIn(original_phone, receipt_result)

        from services.domain_effect_worker import dispatch_domain_effect_batch

        with patch(
            "services.ticket_domain_effects._email_preflight_error",
            return_value=None,
        ), patch(
            "services.email_service.enviar_email",
            return_value=True,
        ) as send_email, patch(
            "services.ticket_domain_effects.send_prepared_tenant_twilio_message",
            return_value="SM-pinned-reply",
        ) as send_whatsapp, patch(
            "socket_service.emit_new_chat_message"
        ) as emit_realtime:
            batch = dispatch_domain_effect_batch(
                tenant_id=self.tenant.id,
                limit=10,
            )
            replay = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json=request_payload,
                headers=headers,
            )
            empty_batch = dispatch_domain_effect_batch(
                tenant_id=self.tenant.id,
                limit=10,
            )

        self.assertEqual(batch["processed"], 3)
        self.assertEqual(batch["succeeded"], 3)
        self.assertEqual(empty_batch["processed"], 0)
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args.args[0], original_email)
        self.assertNotEqual(send_email.call_args.args[0], "redirected@test.com")
        send_whatsapp.assert_called_once()
        prepared = send_whatsapp.call_args.args[0]
        self.assertEqual(prepared.params["to"], f"whatsapp:{original_phone}")
        self.assertNotEqual(prepared.params["to"], "whatsapp:+5492222222222")
        emit_realtime.assert_called_once()
        self.assertEqual(
            emit_realtime.call_args.args[0]["message"]["texto"],
            first_body,
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["delivery"]["idempotency"]["replayed"])
        self.assertEqual(
            TenantTicketReplyEvent.query.filter_by(
                tenant_id=self.tenant.id,
                ticket_id=self.ticket.id,
                body=first_body,
            ).count(),
            1,
        )

    def test_tenant_reply_transaction_rolls_back_snapshot_receipt_and_timeline(self):
        self._enable_tenant_domain_outbox()
        original_timeline = list(self.ticket.datos_extra.get("comments") or [])
        with patch(
            "services.ticket_domain_effects.stage_tenant_ticket_reply_effects",
            side_effect=RuntimeError("staging failed"),
        ):
            response = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={
                    "action": "reply",
                    "body": "No debe quedar parcialmente persistida.",
                    "visibility": "public",
                    "client_message_id": "crm-reply:rollback-0001",
                },
                headers=self._auth(self.owner),
            )

        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertEqual(
            TenantTicketReplyEvent.query.filter_by(
                tenant_id=self.tenant.id,
                ticket_id=self.ticket.id,
            ).count(),
            0,
        )
        self.assertEqual(
            TicketDomainEffectReceipt.query.filter_by(
                tenant_id=self.tenant.id,
                effect_kind="ticket.comment.tenant",
            ).count(),
            0,
        )
        self.assertEqual(
            DomainEffectOutbox.query.filter_by(
                tenant_id=self.tenant.id,
                aggregate_type="tenant_ticket_reply",
            ).count(),
            0,
        )
        db.session.refresh(self.ticket)
        self.assertEqual(self.ticket.datos_extra.get("comments"), original_timeline)

    def test_omnichannel_tenant_reply_ambiguous_provider_failure_is_not_auto_retried(self):
        self._enable_tenant_domain_outbox()
        token_ref = f"TWILIO_SUBACCOUNT_AUTH_TOKEN_TENANT_REPLY_{self.tenant.id}"
        account_sid = f"AC-tenant-reply-{self.tenant.id}"
        self.app.config[token_ref] = f"tenant-reply-token-{self.tenant.id}"
        self.tenant.configuracion = {
            **(self.tenant.configuracion or {}),
            "twilio_tech_provider": {
                "twilio_account_sid": account_sid,
                "twilio_subaccount_token_ref": token_ref,
            },
        }
        connection = ProviderConnection(
            tenant_id=self.tenant.id,
            provider="twilio",
            channel="whatsapp",
            environment="production",
            status="online",
            external_account_id=account_sid,
            credentials_ref=f"env:{token_ref}",
        )
        db.session.add(connection)
        db.session.flush()
        sender = ProviderSender(
            tenant_id=self.tenant.id,
            provider_connection_id=connection.id,
            channel="whatsapp",
            sender_type="whatsapp_business",
            phone_number="+15005550006",
            sender_id="whatsapp:+15005550006",
            status="online",
            status_callback_url="https://api.example.test/twilio/whatsapp/status",
        )
        db.session.add(sender)
        db.session.commit()

        client_message_id = "crm-reply:tenant-provider-unknown-0001"
        response = self.client.post(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
            json={
                "action": "reply",
                "body": "Mensaje durable antes del proveedor.",
                "visibility": "public",
                "client_message_id": client_message_id,
            },
            headers={
                **self._auth(self.owner),
                "Idempotency-Key": client_message_id,
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["delivery"]["mode"], "durable_queue")

        from services.domain_effect_worker import dispatch_domain_effect_batch

        with patch(
            "services.ticket_domain_effects.send_prepared_tenant_twilio_message",
            side_effect=RuntimeError("provider acknowledgement lost"),
        ) as provider_send, patch("socket_service.emit_new_chat_message"):
            first_batch = dispatch_domain_effect_batch(
                tenant_id=self.tenant.id,
                limit=1,
            )
            second_batch = dispatch_domain_effect_batch(
                tenant_id=self.tenant.id,
                limit=10,
            )
            third_batch = dispatch_domain_effect_batch(
                tenant_id=self.tenant.id,
                limit=10,
            )

        self.assertEqual(first_batch["unknown"], 1)
        self.assertEqual(second_batch["succeeded"], 1)
        self.assertEqual(third_batch["processed"], 0)
        provider_send.assert_called_once()
        whatsapp_effect = DomainEffectOutbox.query.filter_by(
            tenant_id=self.tenant.id,
            aggregate_type="tenant_ticket_reply",
            channel="whatsapp",
        ).one()
        self.assertEqual(whatsapp_effect.status, DomainEffectOutbox.STATUS_UNKNOWN)
        self.assertIsNotNone(whatsapp_effect.io_started_at)

    def test_omnichannel_tenant_reply_rejects_cross_tenant_target_before_receipt(self):
        foreign_owner = User(
            name="Foreign reply owner",
            email="foreign-reply@test.com",
            rol="admin",
            tenant_slug="foreign-reply",
        )
        foreign_owner.set_password("secret123")
        db.session.add(foreign_owner)
        db.session.flush()
        foreign_tenant = TenantProfile(
            slug="foreign-reply",
            nombre="Foreign reply",
            tipo="pyme",
            pyme_id=foreign_owner.id,
        )
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign_owner.tenant_id = foreign_tenant.id
        foreign_ticket = TenantTicket(
            tenant_id=foreign_tenant.id,
            descripcion="Ticket aislado",
            estado="nuevo",
            origen="whatsapp",
            datos_extra={"comments": []},
        )
        db.session.add(foreign_ticket)
        db.session.commit()

        response = self.client.post(
            f"/api/v2/inbox/omnichannel/{foreign_ticket.id}/actions",
            json={
                "action": "reply",
                "body": "No debe cruzar tenants.",
                "client_message_id": "crm-reply:cross-tenant-0001",
            },
            headers={
                **self._auth(self.owner),
                "Idempotency-Key": "crm-reply:cross-tenant-0001",
            },
        )

        self.assertEqual(response.status_code, 404, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "ticket_not_found")
        self.assertEqual(
            TicketDomainEffectReceipt.query.filter_by(
                tenant_id=self.tenant.id,
                effect_kind="ticket.comment.tenant",
            ).count(),
            0,
        )
        self.assertEqual(
            TenantTicketReplyEvent.query.filter_by(
                tenant_id=self.tenant.id,
            ).count(),
            0,
        )
        db.session.refresh(foreign_ticket)
        self.assertEqual(foreign_ticket.datos_extra.get("comments"), [])

    def test_tenant_reply_worker_rejects_cross_tenant_durable_event(self):
        self._enable_tenant_domain_outbox()
        foreign_owner = User(
            name="Foreign event owner",
            email="foreign-event-owner@test.com",
            rol="admin",
            tenant_slug="foreign-reply-event",
        )
        foreign_owner.set_password("secret123")
        db.session.add(foreign_owner)
        db.session.flush()
        foreign_tenant = TenantProfile(
            slug="foreign-reply-event",
            nombre="Foreign reply event",
            tipo="pyme",
            pyme_id=foreign_owner.id,
        )
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign_owner.tenant_id = foreign_tenant.id
        injected_event = TenantTicketReplyEvent(
            tenant_id=foreign_tenant.id,
            ticket_id=self.ticket.id,
            event_id="foreignreplyevent0000000000000001",
            body="Contenido de otro tenant",
            visibility="public",
            recipient_email="foreign-citizen@test.com",
        )
        db.session.add(injected_event)
        db.session.commit()

        from services.domain_effect_outbox import (
            DomainEffectClaim,
            PermanentDomainEffectError,
        )
        from services.ticket_domain_effects import (
            TENANT_REPLY_AGGREGATE,
            TENANT_REPLY_EMAIL_HANDLER,
            _prepare_tenant_reply_email,
            _tenant_reply_binding,
            _tenant_reply_recipient_ref,
        )

        secret = self.app.config["DOMAIN_EFFECT_OUTBOX_SECRET"]
        claim = DomainEffectClaim(
            effect_id=999,
            tenant_id=self.tenant.id,
            contract_version=DomainEffectOutbox.CONTRACT_VERSION,
            aggregate_type=TENANT_REPLY_AGGREGATE,
            aggregate_ref=f"{self.ticket.id}:{injected_event.event_id}",
            effect_type="tenant_ticket.reply.email.requester",
            handler_name=TENANT_REPLY_EMAIL_HANDLER,
            channel="email",
            recipient_ref=_tenant_reply_recipient_ref(
                secret=secret,
                reply_event=injected_event,
                channel="email",
            ),
            effect_key="tenant-ticket-reply-cross-tenant-test",
            intent_hmac="a" * 64,
            payload={
                "tenant_binding": _tenant_reply_binding(
                    self.tenant.id,
                    self.ticket.id,
                )
            },
            attempt_count=0,
            max_attempts=8,
            lease_token="lease-cross-tenant",
        )

        with patch("services.email_service.enviar_email") as send_email:
            with self.assertRaises(PermanentDomainEffectError) as raised:
                _prepare_tenant_reply_email(claim)

        self.assertEqual(str(raised.exception), "tenant_ticket_reply_event_missing")
        send_email.assert_not_called()

    def test_tenant_reply_worker_quarantines_unpinned_legacy_recipient(self):
        legacy_event_id = "legacyreplyevent00000000000000001"
        extra = dict(self.ticket.datos_extra or {})
        comments = list(extra.get("comments") or [])
        comments.append(
            {
                "id": legacy_event_id,
                "origin": "admin_panel",
                "action": "reply",
                "body": "Respuesta pendiente anterior al pinning.",
                "visibility": "public",
                "created_at": "2026-08-15T12:00:00Z",
                "actor": {"id": self.owner.id, "name": self.owner.name},
            }
        )
        extra["comments"] = comments
        self.ticket.datos_extra = extra
        db.session.add(self.ticket)
        db.session.commit()

        from services.domain_effect_outbox import (
            DomainEffectClaim,
            PermanentDomainEffectError,
        )
        from services.ticket_domain_effects import (
            TENANT_REPLY_AGGREGATE,
            TENANT_REPLY_EMAIL_HANDLER,
            _prepare_tenant_reply_email,
            _tenant_reply_binding,
        )

        claim = DomainEffectClaim(
            effect_id=998,
            tenant_id=self.tenant.id,
            contract_version=DomainEffectOutbox.CONTRACT_VERSION,
            aggregate_type=TENANT_REPLY_AGGREGATE,
            aggregate_ref=f"{self.ticket.id}:{legacy_event_id}",
            effect_type="tenant_ticket.reply.email.requester",
            handler_name=TENANT_REPLY_EMAIL_HANDLER,
            channel="email",
            recipient_ref="role:ticket.requester",
            effect_key="tenant-ticket-reply-legacy-recipient-test",
            intent_hmac="b" * 64,
            payload={
                "tenant_binding": _tenant_reply_binding(
                    self.tenant.id,
                    self.ticket.id,
                )
            },
            attempt_count=0,
            max_attempts=8,
            lease_token="lease-legacy-recipient",
        )

        with patch("services.email_service.enviar_email") as send_email:
            with self.assertRaises(PermanentDomainEffectError) as raised:
                _prepare_tenant_reply_email(claim)

        self.assertEqual(
            str(raised.exception),
            "tenant_ticket_reply_legacy_recipient_unpinned",
        )
        send_email.assert_not_called()

    def test_omnichannel_tenant_reply_socket_failure_keeps_http_polling_fallback(self):
        client_message_id = "crm-reply:tenant-polling-0001"
        with patch(
            "routes.v2.saas._emit_tenant_ticket_realtime_reply",
            return_value=False,
        ) as emit_realtime:
            response = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={
                    "action": "reply",
                    "body": "Respuesta visible por polling.",
                    "visibility": "public",
                    "send_external": False,
                    "client_message_id": client_message_id,
                },
                headers={
                    **self._auth(self.owner),
                    "Idempotency-Key": client_message_id,
                },
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        emit_realtime.assert_called_once()
        realtime = response.get_json()["delivery"]["realtime"]
        self.assertFalse(realtime["emitted"])
        self.assertFalse(realtime["queued"])
        self.assertEqual(realtime["room"], f"tenant_{self.tenant.id}")
        self.assertEqual(realtime["scope"], "authenticated_tenant_operators")
        self.assertEqual(realtime["fallback"], "http_polling")
        self.assertEqual(
            realtime["polling"]["href"],
            f"/api/v2/inbox/omnichannel/{self.ticket.id}",
        )

        detail = self.client.get(
            realtime["polling"]["href"],
            headers=self._auth(self.owner),
        )
        self.assertEqual(detail.status_code, 200, detail.get_json())
        self.assertTrue(
            any(
                event.get("body") == "Respuesta visible por polling."
                for event in detail.get_json()["item"]["timeline"]
            )
        )

    def test_omnichannel_tenant_reply_realtime_payload_is_tenant_scoped(self):
        client_message_id = "crm-reply:tenant-socket-scope-0001"
        with patch("socket_service.emit_new_chat_message") as emit_realtime:
            response = self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={
                    "action": "reply",
                    "body": "Evento seguro para operadores.",
                    "visibility": "public",
                    "send_external": False,
                    "client_message_id": client_message_id,
                },
                headers={
                    **self._auth(self.owner),
                    "Idempotency-Key": client_message_id,
                },
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        emit_realtime.assert_called_once()
        event = emit_realtime.call_args.args[0]
        self.assertEqual(event["tenant_profile_id"], self.tenant.id)
        self.assertEqual(event["ticket_id"], self.ticket.id)
        self.assertEqual(event["source_model"], "TenantTicket")
        self.assertEqual(event["tenant_type"], "tenant")
        self.assertNotIn("contact", event)
        self.assertNotIn("phone", event)
        self.assertNotIn("email", event)
        self.assertEqual(
            response.get_json()["delivery"]["realtime"]["room"],
            f"tenant_{self.tenant.id}",
        )

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
        self.assertIn("transactions", module_ids)
        self.assertIn("education", module_ids)
        self.assertNotIn("portal", module_ids)
        self.assertEqual(payload["navigation"]["contract_version"], "tenant.admin_navigation.v1")
        quick_action_ids = {item["id"] for item in payload["navigation"]["quick_actions"]}
        self.assertIn("open_surveys", quick_action_ids)
        self.assertIn("open_employees", quick_action_ids)
        self.assertIn("open_heatmap", quick_action_ids)
        self.assertNotIn("open_portal", quick_action_ids)
        primary_nav_ids = {item["id"] for item in payload["navigation"]["primary"]}
        self.assertIn("transactions", primary_nav_ids)
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
        self.assertEqual(payload["e2e_flow_readiness"]["contract_version"], "platform.e2e_flow_readiness.v1")
        self.assertGreaterEqual(payload["e2e_flow_readiness"]["summary"]["total"], 6)
        self.assertEqual(payload["e2e_flow_readiness"]["frontend_contract"]["render_as"], "e2e_flow_readiness_grid")
        e2e_flow_ids = {item["id"] for item in payload["e2e_flow_readiness"]["flows"]}
        self.assertIn("gov_claim_text_to_tracking", e2e_flow_ids)
        self.assertIn("pyme_catalog_order_checkout", e2e_flow_ids)
        self.assertTrue(payload["frontend_contract"]["show_e2e_flow_readiness"])
        self.assertIn("e2e_flow_readiness", payload["frontend_contract"]["recommended_views"])
        whatsapp_module = next(item for item in payload["modules"] if item["id"] == "widget_whatsapp")
        self.assertEqual(whatsapp_module["label"], "Widget/WhatsApp/Voz")
        self.assertEqual(whatsapp_module["endpoint"], "/api/v2/whatsapp/experience")
        transactions_module = next(item for item in payload["modules"] if item["id"] == "transactions")
        self.assertEqual(transactions_module["label"], "Transacciones")
        self.assertIn("finance_flows", transactions_module["widgets"])
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

    def test_tenant_ops_qa_playbook_and_safe_check_execution(self):
        response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/ops-qa/playbook",
            headers={**self._auth(self.owner), "X-Request-Id": "ops-qa-1"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "tenant.ops_qa.playbook.v1")
        self.assertEqual(payload.get("request_id"), "ops-qa-1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertTrue(payload["safe_by_default"])
        self.assertIn(payload["status"], {"pass", "warning", "fail"})
        self.assertGreaterEqual(payload["summary"]["checks_total"], 7)
        self.assertEqual(payload["summary"]["real_messages_sent"], 0)
        self.assertEqual(payload["frontend_contract"]["render_as"], "tenant_ops_qa_command_center")
        self.assertTrue(payload["frontend_contract"]["show_e2e_flow_readiness"])
        self.assertIn("e2e_flow_matrix", payload["frontend_contract"]["recommended_views"])
        self.assertEqual(payload["e2e_flow_readiness"]["contract_version"], "platform.e2e_flow_readiness.v1")
        self.assertGreaterEqual(payload["summary"]["e2e_flows_total"], 6)
        self.assertGreaterEqual(len(payload["e2e_flow_readiness"]["flows"]), 6)
        first_flow = payload["e2e_flow_readiness"]["flows"][0]
        self.assertIn("frontend_entry", first_flow)
        self.assertIn("manual_test_steps", first_flow)
        self.assertIn("acceptance_criteria", first_flow)
        self.assertTrue(first_flow["automation"]["safe_by_default"])
        check_ids = {item["id"] for item in payload["checks"]}
        self.assertIn("admin_os_contract", check_ids)
        self.assertIn("claims_inbox", check_ids)
        self.assertIn("catalog_orders", check_ids)
        self.assertIn("transactional_finance_flows", check_ids)
        self.assertIn("survey_live_vote", check_ids)
        self.assertIn("heatmap_analytics", check_ids)
        self.assertIn("employee_routing", check_ids)
        self.assertIn("whatsapp_templates_webviews", check_ids)
        self.assertIn("ai_runtime_multimodal", check_ids)
        self.assertLessEqual(payload["summary"]["critical_failed"], payload["summary"]["checks_total"])
        self.assertTrue(any(item["id"] == "whatsapp_templates_webviews" for item in payload["checks"]))
        self.assertEqual(
            payload["execution"]["endpoint"],
            f"/api/v2/tenants/{self.tenant.slug}/ops-qa/check/{{check_id}}",
        )

        run_response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/ops-qa/check/whatsapp_templates_webviews",
            headers={**self._auth(self.owner), "X-Request-Id": "ops-qa-run-1"},
            json={},
        )

        self.assertEqual(run_response.status_code, 200, run_response.get_json())
        run_payload = run_response.get_json()
        self.assertEqual(run_payload["contract_version"], "tenant.ops_qa.execution.v1")
        self.assertEqual(run_payload["check_id"], "whatsapp_templates_webviews")
        self.assertEqual(run_payload["execution_mode"], "read_only")
        self.assertFalse(run_payload["sends_real_message"])
        self.assertIn("webview_flows_total", run_payload["details"])
        matrix = run_payload["details"]["executable_matrix"]
        self.assertEqual(matrix["contract_version"], "whatsapp.qa_script_matrix.v1")
        self.assertTrue(matrix["loaded"])
        self.assertFalse(matrix["sends_real_message"])
        self.assertGreaterEqual(matrix["summary"]["scenarios"], 10)
        self.assertGreaterEqual(matrix["summary"]["cases"], 20)
        self.assertEqual(matrix["local_command"], "python scripts/qa_whatsapp_flows.py")
        coverage = run_payload["details"]["e2e_matrix_coverage"]
        self.assertEqual(coverage["contract_version"], "whatsapp.qa_e2e_matrix_coverage.v1")
        self.assertTrue(coverage["safe_by_default"])
        self.assertIn("gov_claim_text_to_tracking", coverage["covered_scenarios"])
        self.assertEqual(run_payload["details"]["runner"]["sends_real_message"], False)

        missing_response = self.client.post(
            f"/api/v2/tenants/{self.tenant.slug}/ops-qa/check/no-existe",
            headers={**self._auth(self.owner), "X-Request-Id": "ops-qa-missing-1"},
            json={},
        )
        self.assertEqual(missing_response.status_code, 404)
        self.assertEqual(missing_response.get_json()["reason_code"], "ops_qa_check_not_found")

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
        audio_cache = payload["conversation_intelligence"]["audio_cache"]
        self.assertEqual(audio_cache["contract_version"], "tts.audio_cache_observability.v1")
        self.assertTrue(audio_cache["enabled"])
        self.assertTrue(audio_cache["ready"])
        self.assertIn(audio_cache["status"], {"ready", "active", "degraded"})
        self.assertEqual(audio_cache["cache"], "tts_audio_cache")
        self.assertEqual(audio_cache["storage"]["public_path"], "/static/audio_cache")
        self.assertFalse(audio_cache["storage"]["content_text_exposed"])
        self.assertEqual(audio_cache["storage"]["cache_control"], "public, max-age=31536000, immutable")
        self.assertIn(audio_cache["storage"]["next_action"], {"monitor_hit_rate", "configure_cloudflare_audio_cache_public_base_url"})
        self.assertIn("required_env", audio_cache["storage"])
        self.assertFalse(audio_cache["storage"]["privacy"]["content_text_exposed"])
        self.assertFalse(audio_cache["storage"]["privacy"]["pii_in_url"])
        self.assertIn("fixed_whatsapp_menus", audio_cache["warmup"]["recommended_for"])
        self.assertIn("cache_hits", audio_cache["metrics"])
        self.assertIn("generation_failures", audio_cache["metrics"])
        self.assertNotIn("tts_cache_text", audio_cache)
        self.assertNotIn("audio_text", audio_cache)
        self.assertEqual(payload["content_modules"]["catalog"]["items"], 1)
        self.assertEqual(payload["content_modules"]["catalog"]["items_with_images"], 1)
        self.assertGreaterEqual(payload["content_modules"]["surveys_votings"]["responses"], 1)
        self.assertEqual(payload["content_modules"]["news_events"]["by_type"]["noticia"], 1)
        self.assertTrue(payload["content_modules"]["promotions"]["enabled"])
        self.assertEqual(payload["content_modules"]["links"]["tenant_config_links"], 1)
        self.assertEqual(payload["tracking"]["claims"]["open"], 1)
        self.assertEqual(payload["tracking"]["orders"]["total"], 1)
        self.assertEqual(payload["tracking"]["claims"]["experience_endpoint"], "/api/public/tracking/experience?kind=claim&code={code}")
        self.assertEqual(payload["tracking"]["claims"]["credential_transport"], "x-tracking-pin-header")
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
        self.assertFalse(order_checkout_template["status"]["approved"])
        self.assertFalse(order_checkout_template["status"]["configured"])
        self.assertTrue(order_checkout_template["status"]["local_configured"])
        self.assertIsNone(order_checkout_template["status"]["content_sid"])
        self.assertEqual(order_checkout_template["status"]["status"], "stale")
        self.assertEqual(order_checkout_template["readiness"]["state"], "stale")
        self.assertEqual(
            order_checkout_template["readiness"]["next_action"],
            "refresh_provider_status_for_this_tenant",
        )
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
        self.assertEqual(
            gov_claim_sla["variable_contract"]["required"],
            ["claim_code", "category", "tracking_url"],
        )
        self.assertEqual(gov_claim_sla["fallback"]["mode"], "plain_text")
        self.assertEqual(gov_claim_sla["audio_cache_contract"]["kind"], "fixed_menu")
        gov_audio_contract = gov_claim_sla["audio_cache_contract"]
        self.assertEqual(
            gov_audio_contract["namespace_pattern"],
            "whatsapp:menu:{tenant_slug}:{menu_id}:whatsapp:{variant}:v{menu_version}",
        )
        self.assertEqual(
            gov_audio_contract["manifest"]["contract_version"],
            "whatsapp.fixed_menu_audio_manifest.v1",
        )
        self.assertEqual(gov_audio_contract["manifest"]["items"][0]["menu_id"], "claim-categories")
        self.assertEqual(
            gov_audio_contract["manifest"]["prewarm_job"]["executor"],
            "services.tts_orchestrator.warm_tts_cache",
        )
        self.assertFalse(gov_audio_contract["manifest"]["privacy"]["free_user_text_allowed"])
        self.assertTrue(
            gov_audio_contract["accessibility_gate"]["required"]["stable_numbered_options"]
        )
        self.assertIn("cancelar", gov_audio_contract["accessibility_gate"]["must_include_keywords"])
        self.assertIn("junin_confirmacion_reclamo", gov_claim_sla["qa_cases"])
        gov_claim_created = next(item for item in government_group["items"] if item["id"] == "gov_claim_created")
        self.assertEqual(
            gov_claim_created["receipt_contract"]["builder"],
            "services.whatsapp_receipts.build_claim_created_template_pre_message",
        )
        gov_survey_template = next(
            item
            for item in payload["template_blueprint"]["vertical_templates"]["gobierno"]
            if item["id"] == "gov_survey_invite"
        )
        self.assertEqual(gov_survey_template["status"]["resolved_name"], "chatboc_gov_survey_invite_v2")
        self.assertFalse(gov_survey_template["status"]["approved"])
        self.assertEqual(gov_survey_template["status"]["status"], "stale")
        self.assertEqual(gov_survey_template["readiness"]["state"], "stale")
        commerce_group = payload["template_blueprint"]["operational_template_groups"]["commerce_and_payments"]
        pyme_order_ready = next(item for item in commerce_group["items"] if item["id"] == "pyme_order_ready")
        self.assertEqual(pyme_order_ready["variables"], ["order_code", "total", "checkout_url"])
        self.assertEqual(pyme_order_ready["receipt_contract"]["kind"], "pyme_order_receipt")
        self.assertIn("financial_services", payload["template_blueprint"]["operational_template_groups"])
        finance_group = payload["template_blueprint"]["operational_template_groups"]["financial_services"]
        finance_template_ids = {item["id"] for item in finance_group["items"]}
        self.assertIn("finance_account_onboarding", finance_template_ids)
        self.assertIn("finance_secure_payment", finance_template_ids)
        self.assertIn("finance_remittance_transfer", finance_template_ids)
        self.assertIn("finance_insurance_claim", finance_template_ids)
        finance_payment_template = next(item for item in finance_group["items"] if item["id"] == "finance_secure_payment")
        self.assertEqual(finance_payment_template["execution"]["webview"]["role"], "secure_checkout")
        self.assertEqual(finance_payment_template["receipt_contract"]["kind"], "finance_payment_receipt")
        finance_transfer_template = next(item for item in finance_group["items"] if item["id"] == "finance_remittance_transfer")
        self.assertEqual(finance_transfer_template["receipt_contract"]["kind"], "finance_transfer_receipt")
        education_group = payload["template_blueprint"]["operational_template_groups"]["education"]
        school_receipt_ready = next(item for item in education_group["items"] if item["id"] == "school_receipt_ready")
        self.assertEqual(school_receipt_ready["receipt_contract"]["kind"], "school_payment_receipt")
        self.assertIn("colegio_menu_sandbox", school_receipt_ready["qa_cases"])
        self.assertGreaterEqual(payload["template_blueprint"]["registry_summary"]["operational_catalog_total"], 40)
        self.assertGreater(payload["template_blueprint"]["registry_summary"]["operational_webviews"], 0)
        self.assertGreaterEqual(payload["template_blueprint"]["registry_summary"]["operational_receipt_contracts"], 3)
        self.assertGreaterEqual(payload["template_blueprint"]["registry_summary"]["operational_qa_case_links"], 10)
        self.assertGreaterEqual(payload["template_blueprint"]["registry_summary"]["operational_audio_cache_contracts"], 3)
        self.assertGreaterEqual(len(payload["template_blueprint"]["next_actions"]), 1)
        self.assertIn(
            payload["template_blueprint"]["next_actions"][0]["severity"],
            {"blocking", "warning", "ready_with_dependency"},
        )
        creation_manifest = payload["template_blueprint"]["creation_manifest"]
        self.assertEqual(creation_manifest["contract_version"], "twilio.content.creation_manifest.v1")
        self.assertGreaterEqual(creation_manifest["templates_total"], 20)
        self.assertIn("twilio/call-to-action", creation_manifest["by_twilio_type"])
        self.assertTrue(creation_manifest["policy"]["store_content_sid_in_message_template_registry"])
        manifest_items = {item["id"]: item for item in creation_manifest["items"]}
        self.assertIn("order_checkout", manifest_items)
        self.assertIn("finance_secure_payment", manifest_items)
        self.assertIn("finance_account_onboarding", manifest_items)
        self.assertEqual(manifest_items["order_checkout"]["create_request"]["friendly_name"], "chatboc_order_checkout_v1")
        self.assertIn("twilio/text", manifest_items["order_checkout"]["create_request"]["types"])
        order_cta_action = manifest_items["order_checkout"]["create_request"]["types"]["twilio/call-to-action"]["actions"][0]
        self.assertEqual(order_cta_action["type"], "URL")
        self.assertEqual(order_cta_action["url"], "https://www.chatboc.ar/{{3}}")
        self.assertEqual(
            manifest_items["order_checkout"]["create_request"]["variables"]["3"],
            f"t/{self.tenant.slug}/checkout",
        )
        cta_manifest_items = [
            item
            for item in manifest_items.values()
            if item["create_request"]["types"].get("twilio/call-to-action")
        ]
        self.assertGreater(len(cta_manifest_items), 0)
        for item in cta_manifest_items:
            action = item["create_request"]["types"]["twilio/call-to-action"]["actions"][0]
            self.assertTrue(action["url"].startswith("https://www.chatboc.ar/"))
            self.assertNotEqual(action["url"], "{{1}}")
            self.assertNotEqual(action["url"], "{{2}}")
            self.assertNotEqual(action["url"], "{{3}}")
            self.assertIn("{{", action["url"])
        self.assertEqual(manifest_items["order_checkout"]["approval_request"]["category"], "UTILITY")
        self.assertEqual(
            manifest_items["order_checkout"]["send_example"]["content_variables"]["1"],
            "P-123456",
        )
        self.assertEqual(
            manifest_items["order_checkout"]["send_example"]["content_variables"]["3"],
            f"t/{self.tenant.slug}/checkout",
        )
        self.assertEqual(
            manifest_items["finance_secure_payment"]["send_example"]["content_variables"]["3"],
            f"finanzas/{self.tenant.slug}/operacion/OP-1001?session=session-demo-123456",
        )
        self.assertEqual(
            manifest_items["finance_secure_payment"]["create_request"]["types"]["twilio/call-to-action"]["actions"][0]["url"],
            "https://www.chatboc.ar/{{3}}",
        )
        self.assertIn("whatsapp_flows", payload["template_blueprint"]["meta_business_strategy"])
        self.assertIn("signed_webviews", payload["template_blueprint"]["meta_business_strategy"])
        self.assertEqual(payload["webview_blueprint"]["checkout"]["confirmation_source"], "server_to_server_webhook")
        self.assertFalse(payload["webview_blueprint"]["checkout"]["card_data_in_chat"])
        webview_flows = {item["id"]: item for item in payload["webview_blueprint"]["flows"]}
        self.assertIn("claim_tracking_helpdesk", webview_flows)
        self.assertIn("order_checkout", webview_flows)
        self.assertIn("survey_vote", webview_flows)
        self.assertIn("finance_onboarding_kyc", webview_flows)
        self.assertIn("finance_credit_collection_signature", webview_flows)
        self.assertIn("finance_account_servicing", webview_flows)
        self.assertIn("finance_remittance_transfer", webview_flows)
        self.assertIn("finance_insurance_claim", webview_flows)
        self.assertIn("finance_fee_financing_tax", webview_flows)
        self.assertIn("claim_live_or_offline_helpdesk", webview_flows)
        self.assertIn("catalog_order_builder", webview_flows)
        self.assertIn("government_procedure_intake", webview_flows)
        self.assertIn("school_payment_receipt", webview_flows)
        self.assertIn("appointment_reschedule", webview_flows)
        self.assertIn("document_delivery", webview_flows)
        self.assertEqual(
            webview_flows["claim_tracking_helpdesk"]["url_template"],
            "/api/public/tracking/experience?kind=claim&code={code}",
        )
        self.assertEqual(
            webview_flows["claim_live_or_offline_helpdesk"]["availability"]["outside_hours_mode"],
            "offline_message",
        )
        self.assertIn("public_comment_created", webview_flows["claim_tracking_helpdesk"]["server_confirmation"])
        self.assertEqual(webview_flows["catalog_order_builder"]["url_template"], f"/t/{self.tenant.slug}/market")
        self.assertIn("order_checkout", webview_flows["order_checkout"]["template_ids"])
        self.assertIn("identity_verified", webview_flows["finance_onboarding_kyc"]["server_confirmation"])
        self.assertFalse(webview_flows["finance_onboarding_kyc"]["security"]["identity_data_in_chat"])
        self.assertIn("signature_completed", webview_flows["finance_credit_collection_signature"]["server_confirmation"])
        self.assertFalse(webview_flows["finance_account_servicing"]["security"]["balance_data_in_chat"])
        self.assertIn("transfer_receipt_ready", webview_flows["finance_remittance_transfer"]["server_confirmation"])
        self.assertIn("insurance_claim_created", webview_flows["finance_insurance_claim"]["server_confirmation"])
        self.assertGreaterEqual(payload["webview_blueprint"]["summary"]["flows_total"], 11)
        self.assertGreaterEqual(payload["webview_blueprint"]["summary"]["meta_flow_blueprints"], 12)
        self.assertEqual(
            payload["webview_blueprint"]["summary"]["executable_contracts"],
            payload["webview_blueprint"]["summary"]["flows_total"],
        )
        claim_executable_contract = webview_flows["claim_tracking_helpdesk"]["executable_contract"]
        self.assertEqual(
            claim_executable_contract["contract_version"],
            "whatsapp.webview.executable_contract.v1",
        )
        self.assertIn("gov_claim_created", claim_executable_contract["trigger"]["template_ids"])
        self.assertTrue(claim_executable_contract["preconditions"]["requires_signed_session"])
        self.assertIn("create_signed_webview_session", claim_executable_contract["backend_actions"])
        self.assertIn("public_comment_created", claim_executable_contract["crm_writebacks"])
        self.assertIn("claim_tracking_helpdesk:crm_timeline_updated", claim_executable_contract["qa_assertions"])
        claim_flow_blueprint = webview_flows["claim_tracking_helpdesk"]["meta_flow_blueprint"]
        self.assertEqual(claim_flow_blueprint["endpoint_mode"], "data_exchange")
        self.assertIn("ticket_summary", [screen["id"] for screen in claim_flow_blueprint["screens"]])
        finance_flow_blueprint = webview_flows["finance_onboarding_kyc"]["meta_flow_blueprint"]
        self.assertEqual(finance_flow_blueprint["flow_name"], "chatboc_finance_onboarding_kyc")
        self.assertIn("consent_version", finance_flow_blueprint["data_contract"])
        survey_flow_blueprint = webview_flows["survey_vote"]["meta_flow_blueprint"]
        self.assertEqual(survey_flow_blueprint["completion_event"], "survey_response_saved")
        self.assertEqual(
            survey_flow_blueprint["data_contract"],
            list(SURVEY_VOTE_DATA_CONTRACT),
        )
        self.assertEqual(
            survey_flow_blueprint["native_limits"]["question_types"],
            ["opcion_unica"],
        )
        self.assertTrue(
            survey_flow_blueprint["native_limits"]["reward_surveys_use_webview"]
        )
        self.assertTrue(
            survey_flow_blueprint["native_limits"]["adaptive_navigation"]
        )
        self.assertEqual(
            survey_flow_blueprint["native_limits"]["conditional_logic_versions"],
            [1, 2],
        )
        self.assertTrue(
            survey_flow_blueprint["native_limits"]["instrument_revision_pinned"]
        )
        self.assertTrue(
            webview_flows["survey_vote"]["meta_flow_artifact"][
                "publishable_flow_json"
            ]
        )
        self.assertIn(
            "claim_live_or_offline_helpdesk",
            payload["webview_blueprint"]["summary"]["transactional_flows"],
        )
        self.assertIn(
            "finance_credit_collection_signature",
            payload["webview_blueprint"]["summary"]["transactional_flows"],
        )
        self.assertIn(
            "finance_account_servicing",
            payload["webview_blueprint"]["summary"]["transactional_flows"],
        )
        self.assertTrue(payload["webview_blueprint"]["security"]["requires_full_plan"])
        self.assertEqual(payload["flow_runtime"]["contract_version"], "whatsapp.flow_runtime.v1")
        self.assertTrue(payload["flow_runtime"]["runtime_policy"]["pause_conversation_while_webview_open"])
        self.assertEqual(
            payload["flow_runtime"]["public_endpoints"]["checkout"],
            "/api/checkout/crear-preferencia",
        )
        runtime_flows = {item["id"]: item for item in payload["flow_runtime"]["flows"]}
        self.assertIn("claim_tracking_helpdesk", runtime_flows)
        self.assertIn("order_checkout", runtime_flows)
        self.assertIn("survey_vote", runtime_flows)
        self.assertIn("public_checkout", [item["id"] for item in runtime_flows["order_checkout"]["actions"]])
        self.assertIn(
            "claim_public_message",
            [item["id"] for item in runtime_flows["claim_tracking_helpdesk"]["actions"]],
        )
        self.assertIn("commerce", payload["flow_runtime"]["summary"]["families"])
        self.assertEqual(
            payload["flow_runtime"]["frontend_contract"]["render_as"],
            "flow_runtime_command_center",
        )
        self.assertEqual(payload["finance_transactional"]["contract_version"], "finance.transactional_whatsapp.v1")
        self.assertGreaterEqual(payload["finance_transactional"]["summary"]["journeys"], 6)
        self.assertGreaterEqual(payload["finance_transactional"]["summary"]["webview_flows"], 6)
        journey_ids = {item["id"] for item in payload["finance_transactional"]["journeys"]}
        self.assertIn("digital_account_opening", journey_ids)
        self.assertIn("insurance_claim_documentation", journey_ids)
        self.assertFalse(payload["finance_transactional"]["security_policy"]["card_data_in_chat_allowed"])
        self.assertIn("collections", [item["id"] for item in payload["finance_transactional"]["crm_operating_model"]["queues"]])
        finance_activation = payload["finance_transactional"]["activation_plan"]
        self.assertEqual(finance_activation["contract_version"], "finance.activation_plan.v1")
        self.assertEqual(finance_activation["frontend_contract"]["render_as"], "finance_activation_plan")
        capability_ids = {item["id"] for item in finance_activation["required_capabilities"]}
        self.assertIn("identity_or_kyc_provider", capability_ids)
        self.assertIn("document_signature_provider", capability_ids)
        self.assertIn("secure_checkout_or_payment_gateway", capability_ids)
        track_ids = {item["id"] for item in finance_activation["launch_tracks"]}
        self.assertIn("collections_payments_signature", track_ids)
        self.assertIn("fees_taxes_school_government", track_ids)
        self.assertGreaterEqual(len(finance_activation["setup_questions"]), 4)
        self.assertGreaterEqual(len(finance_activation["next_actions"]), 1)
        self.assertEqual(
            payload["finance_transactional"]["summary"]["activation_blockers"],
            finance_activation["blocking_count"],
        )
        self.assertEqual(payload["qa_playbook"]["contract_version"], "whatsapp.qa_playbook.v1")
        self.assertEqual(payload["qa_playbook"]["local_command"], "python scripts/qa_whatsapp_flows.py")
        self.assertGreaterEqual(payload["qa_playbook"]["scenario_count"], 8)
        self.assertGreaterEqual(payload["qa_playbook"]["meta_flow_ready_count"], 4)
        self.assertLess(
            payload["qa_playbook"]["meta_flow_ready_count"],
            payload["qa_playbook"]["scenario_count"],
        )
        qa_scenarios = {item["id"]: item for item in payload["qa_playbook"]["scenarios"]}
        self.assertIn("gov_claim_text_to_tracking", qa_scenarios)
        self.assertIn("pyme_catalog_order_checkout", qa_scenarios)
        self.assertIn("chatboc_demo_hub", qa_scenarios)
        self.assertIn("survey_vote_realtime", qa_scenarios)
        self.assertIn("finance_onboarding_collection_signature", qa_scenarios)
        self.assertIn("finance_account_servicing", qa_scenarios)
        self.assertIn("finance_insurance_claim", qa_scenarios)
        claim_qa = qa_scenarios["gov_claim_text_to_tracking"]
        self.assertEqual(claim_qa["webview_state"]["id"], "claim_tracking_helpdesk")
        self.assertTrue(claim_qa["meta_flow_coverage"]["ready"])
        self.assertIn("CLAIM_LOOKUP", claim_qa["meta_flow_coverage"]["screens"])
        self.assertEqual(claim_qa["meta_flow_coverage"]["flow_json_version"], "7.3")
        self.assertEqual(claim_qa["meta_flow_coverage"]["data_api_version"], "3.0")
        self.assertIn("ticket_number", claim_qa["meta_flow_coverage"]["data_contract"])
        self.assertNotIn("pin", claim_qa["meta_flow_coverage"]["data_contract"])
        self.assertIn("junin_texto_reclamo", claim_qa["script_cases"])
        self.assertIn("gov_claim_created", claim_qa["templates"])
        self.assertIn(claim_qa["status"], {"ready", "blocked_templates", "blocked_webview", "needs_template_review"})
        self.assertIn("chatboc_demo_order_start", qa_scenarios["chatboc_demo_hub"]["script_cases"])
        self.assertIn("chatboc_demo_survey_open", qa_scenarios["survey_vote_realtime"]["script_cases"])
        self.assertIn(
            "payment_state",
            qa_scenarios["finance_onboarding_collection_signature"]["meta_flow_coverage"]["data_contract"],
        )
        self.assertIn(
            "transfer_state",
            qa_scenarios["finance_remittance_transfer"]["meta_flow_coverage"]["data_contract"],
        )
        self.assertEqual(qa_scenarios["survey_vote_realtime"]["meta_flow_coverage"]["completion_event"], "survey_response_saved")
        self.assertIn("requiere TWILIO_AUTH_TOKEN real", payload["qa_playbook"]["live_mode_guardrails"])
        self.assertEqual(payload["message_ux_policy"]["interactive_limits"]["reply_buttons_max"], 3)
        self.assertTrue(payload["message_ux_policy"]["accessibility"]["fixed_menu_audio_cache"]["enabled"])
        self.assertIn(
            "claim_categories",
            payload["message_ux_policy"]["accessibility"]["fixed_menu_audio_cache"]["scope"],
        )
        fixed_menu_cache = payload["message_ux_policy"]["accessibility"]["fixed_menu_audio_cache"]
        fixed_manifest = fixed_menu_cache["manifest"]
        self.assertEqual(fixed_manifest["contract_version"], "whatsapp.fixed_menu_audio_manifest.v1")
        self.assertEqual(
            fixed_manifest["prewarm_job"]["executor"],
            "services.tts_orchestrator.warm_tts_cache",
        )
        self.assertFalse(fixed_manifest["privacy"]["cache_key_contains_pii"])
        manifest_scopes = {item["scope"] for item in fixed_manifest["items"]}
        self.assertIn("main_menu", manifest_scopes)
        self.assertIn("claim_categories", manifest_scopes)
        self.assertIn("survey_menu", manifest_scopes)
        accessibility_gate = fixed_menu_cache["accessibility_gate"]
        self.assertEqual(
            accessibility_gate["contract_version"],
            "whatsapp.fixed_menu_accessibility_gate.v1",
        )
        self.assertTrue(accessibility_gate["required"]["audio_alternative_required"])
        self.assertTrue(accessibility_gate["required"]["emoji_never_required_for_meaning"])
        self.assertIn("ayuda", accessibility_gate["must_include_keywords"])
        self.assertEqual(
            fixed_menu_cache["observability"]["contract_version"],
            "tts.audio_cache_observability.v1",
        )
        self.assertEqual(
            fixed_menu_cache["observability"]["summary"]["requests"],
            audio_cache["summary"]["requests"],
        )
        self.assertFalse(fixed_menu_cache["observability"]["storage"]["content_text_exposed"])
        self.assertEqual(payload["admin_panel"]["inbox"], "/api/v2/inbox/omnichannel")
        self.assertTrue(payload["education"]["enabled"])
        self.assertEqual(payload["frontend_contract"]["render_as"], "whatsapp_operations_hub")
        self.assertIn("commerce_checkout", payload["frontend_contract"]["recommended_views"])
        self.assertIn("template_blueprint", payload["frontend_contract"]["recommended_views"])
        self.assertIn("webview_checkout", payload["frontend_contract"]["recommended_views"])
        self.assertIn("transactional_finance", payload["frontend_contract"]["recommended_views"])
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

    def test_whatsapp_flow_runtime_endpoint_exposes_transactional_webviews(self):
        response = self.client.get(
            f"/api/v2/tenants/{self.tenant.slug}/whatsapp/flow-runtime",
            headers=self._auth(self.owner),
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "whatsapp.flow_runtime.v1")
        self.assertEqual(
            payload["public_endpoints"]["assisted_order_upload"],
            "/api/pedidos/from-file?origen=marketplace",
        )
        self.assertTrue(payload["runtime_policy"]["resume_on_callback_or_timeout"])
        runtime_flows = {item["id"]: item for item in payload["flows"]}
        self.assertIn("order_checkout", runtime_flows)
        self.assertIn(
            "assisted_order_upload",
            [item["id"] for item in runtime_flows["order_checkout"]["actions"]],
        )
        self.assertIn("claim_tracking_helpdesk", runtime_flows)
        self.assertIn(
            "server_to_server_callback",
            [item["id"] for item in runtime_flows["claim_tracking_helpdesk"]["actions"]],
        )
        self.assertEqual(payload["frontend_contract"]["render_as"], "flow_runtime_command_center")

    def test_public_flow_runtime_exposes_whatsapp_widget_action_manifest(self):
        response = self.client.get(
            f"/api/public/flows/runtime?tenant={self.tenant.slug}&channel=whatsapp",
            headers={"X-Request-Id": "public-runtime-1"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "public.whatsapp.flow_runtime.v1")
        self.assertEqual(payload["admin_contract_version"], "whatsapp.flow_runtime.v1")
        self.assertEqual(payload["tenant"]["slug"], self.tenant.slug)
        self.assertEqual(payload["channel"], "whatsapp")
        self.assertFalse(payload["privacy"]["public_runtime_contains_secrets"])
        self.assertTrue(payload["privacy"]["signed_session_required_for_private_records"])
        allowed_actions = payload["action_manifest"]["allowed_actions"]
        self.assertIn("order_checkout:public_checkout", allowed_actions)
        self.assertIn("order_checkout:assisted_order_upload", allowed_actions)
        self.assertIn("claim_tracking_helpdesk:claim_public_message", allowed_actions)
        self.assertIn("survey_vote:public_survey", allowed_actions)
        self.assertIn("survey_vote:survey_response", allowed_actions)
        self.assertEqual(payload["action_manifest"]["post_endpoint"], "/api/public/flows/actions")
        execution_policy = payload["action_manifest"]["execution_policy"]
        self.assertEqual(execution_policy["contract_version"], "public.flow_runtime.execution_policy.v1")
        self.assertEqual(
            execution_policy["callback_endpoint_template"],
            "/api/public/flows/{execution_id}/callback",
        )
        self.assertEqual(
            execution_policy["resume_policy"],
            "resume_conversation_on_callback_or_timeout",
        )
        analytics = payload["action_manifest"]["analytics"]
        self.assertEqual(analytics["contract_version"], "public.flow_runtime.analytics.v1")
        self.assertFalse(analytics["public_client_can_write_events_directly"])
        self.assertIn("checkout_session_created", analytics["recommended_events"])
        self.assertIn("assisted_upload_submitted", analytics["recommended_events"])
        self.assertIn("survey_response_submitted", analytics["recommended_events"])
        self.assertIn("survey_live_results_opened", analytics["recommended_events"])
        self.assertIn("survey", [stage["id"] for stage in analytics["funnel_stages"]])

    def test_public_flow_runtime_action_handoff_requires_idempotency_for_mutations(self):
        missing_key_response = self.client.post(
            f"/api/public/flows/actions?tenant={self.tenant.slug}",
            json={"flow_id": "order_checkout", "action_id": "public_checkout"},
        )
        self.assertEqual(missing_key_response.status_code, 409, missing_key_response.get_json())
        self.assertEqual(missing_key_response.get_json()["reason_code"], "idempotency_key_required")

        response = self.client.post(
            f"/api/public/flows/actions?tenant={self.tenant.slug}",
            headers={"X-Idempotency-Key": "order-checkout-1"},
            json={"flow_id": "order_checkout", "action_id": "public_checkout"},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "public.flow_runtime.action.v1")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["flow"]["family"], "commerce")
        self.assertEqual(payload["action"]["endpoint"], "/api/checkout/crear-preferencia")
        self.assertEqual(payload["handoff"]["method"], "POST")
        self.assertEqual(payload["handoff"]["idempotency_key"], "order-checkout-1")
        self.assertTrue(payload["execution"]["id"].startswith("exec-"))
        self.assertEqual(payload["execution"]["flow_id"], "order_checkout")
        self.assertEqual(payload["execution"]["action_id"], "public_checkout")
        self.assertEqual(payload["execution"]["idempotency_key"], "order-checkout-1")
        self.assertEqual(
            payload["execution"]["callback_endpoint"],
            f"/api/public/flows/{payload['execution']['id']}/callback",
        )
        self.assertEqual(payload["handoff"]["execution_id"], payload["execution"]["id"])
        self.assertEqual(payload["handoff"]["callback_endpoint"], payload["execution"]["callback_endpoint"])
        self.assertEqual(payload["handoff"]["target_endpoint"], "/api/checkout/crear-preferencia")
        self.assertEqual(payload["analytics"]["contract_version"], "public.flow_runtime.analytics.v1")
        self.assertIn("flow_webview_completed", payload["analytics"]["recommended_events"])

        retry_response = self.client.post(
            f"/api/public/flows/actions?tenant={self.tenant.slug}",
            headers={"X-Idempotency-Key": "order-checkout-1"},
            json={"flow_id": "order_checkout", "action_id": "public_checkout"},
        )
        self.assertEqual(retry_response.status_code, 200, retry_response.get_json())
        self.assertEqual(retry_response.get_json()["execution"]["id"], payload["execution"]["id"])

    def test_public_flow_runtime_supports_survey_vote_response_handoff(self):
        missing_key_response = self.client.post(
            f"/api/public/flows/actions?tenant={self.tenant.slug}",
            json={"flow_id": "survey_vote", "action_id": "survey_response", "payload": {"survey_slug": "voto-saas"}},
        )
        self.assertEqual(missing_key_response.status_code, 409, missing_key_response.get_json())
        self.assertEqual(missing_key_response.get_json()["reason_code"], "idempotency_key_required")

        response = self.client.post(
            f"/api/public/flows/actions?tenant={self.tenant.slug}",
            headers={"X-Idempotency-Key": "survey-vote-1"},
            json={"flow_id": "survey_vote", "action_id": "survey_response", "payload": {"survey_slug": "voto-saas"}},
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "public.flow_runtime.action.v1")
        self.assertEqual(payload["flow"]["family"], "surveys")
        self.assertEqual(payload["action"]["endpoint_template"], "/api/pwa/public/surveys/{survey_slug}/respond")
        self.assertEqual(payload["handoff"]["method"], "POST")
        self.assertEqual(payload["handoff"]["target_endpoint"], "/api/pwa/public/surveys/{survey_slug}/respond")
        self.assertTrue(payload["execution"]["id"].startswith("exec-"))
        self.assertEqual(payload["execution"]["action_id"], "survey_response")
        self.assertEqual(payload["handoff"]["execution_id"], payload["execution"]["id"])
        self.assertIn("survey_response_submitted", payload["analytics"]["recommended_events"])
        self.assertIn("survey_live_results_opened", payload["analytics"]["recommended_events"])

    def test_public_flow_runtime_callback_accepts_webview_completion_with_idempotency(self):
        missing_key_response = self.client.post(
            f"/api/public/flows/exec-123/callback?tenant={self.tenant.slug}",
            json={"flow_id": "order_checkout", "status": "completed"},
        )
        self.assertEqual(missing_key_response.status_code, 409, missing_key_response.get_json())
        self.assertEqual(missing_key_response.get_json()["reason_code"], "idempotency_key_required")

        response = self.client.post(
            f"/api/public/flows/exec-123/callback?tenant={self.tenant.slug}",
            headers={"Idempotency-Key": "exec-123-final"},
            json={
                "flow_id": "order_checkout",
                "action_id": "public_checkout",
                "status": "completed",
                "writebacks": ["order_checkout:crm_timeline_updated"],
            },
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "public.flow_runtime.callback.v1")
        self.assertTrue(payload["accepted"])
        self.assertTrue(payload["conversation"]["resume"])
        self.assertEqual(payload["execution"]["id"], "exec-123")
        self.assertEqual(payload["execution"]["idempotency_key"], "exec-123-final")
        self.assertEqual(payload["crm_writeback"]["writebacks"], ["order_checkout:crm_timeline_updated"])
        self.assertEqual(payload["analytics"]["contract_version"], "public.flow_runtime.analytics.v1")
        self.assertEqual(payload["analytics"]["event_name"], "flow_webview_completed")
        saved_event = AnalyticsEventV2.query.filter_by(event_name="flow_webview_completed").first()
        self.assertIsNotNone(saved_event)
        self.assertEqual(saved_event.entity_ref, "exec-123")
        self.assertEqual(saved_event.channel, "whatsapp")
        self.assertEqual(saved_event.metadata_payload["flow_id"], "order_checkout")

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

    def test_tenant_inbox_handoff_lifecycle_is_backend_driven_and_audited(self):
        def post_action(action, user=None, **data):
            return self.client.post(
                f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
                json={"action": action, **data},
                headers=self._auth(user or self.owner),
            )

        detail = self.client.get(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}",
            headers=self._auth(self.owner),
        )
        self.assertEqual(detail.status_code, 200, detail.get_json())
        initial_actions = {item["id"]: item for item in detail.get_json()["item"]["allowed_actions"]}
        self.assertIn("handoff", initial_actions)
        self.assertNotIn("accept_handoff", initial_actions)
        self.assertNotIn("resume_ai", initial_actions)
        self.assertEqual(initial_actions["handoff"]["payload_defaults"], {"channel": "operator"})
        self.assertEqual(initial_actions["handoff"]["requires"], [])
        self.assertFalse(initial_actions["handoff"]["external_dispatch"])

        requested = post_action("handoff", reason="La familia necesita asistencia humana")
        self.assertEqual(requested.status_code, 200, requested.get_json())
        requested_payload = requested.get_json()
        self.assertFalse(requested_payload["delivery"]["external_dispatch"])
        self.assertEqual(requested_payload["delivery"]["mode"], "internal_event")
        handoff = requested_payload["ticket"]["handoff"]
        self.assertEqual(handoff["contract_version"], "inbox.handoff.v1")
        self.assertEqual(handoff["status"], "requested")
        self.assertEqual(handoff["channel"], "operator")
        self.assertEqual(handoff["requested_by"]["id"], self.owner.id)
        requested_actions = {item["id"]: item for item in requested_payload["ticket"]["allowed_actions"]}
        self.assertEqual(requested_actions["accept_handoff"]["label"], "Tomar conversación")
        self.assertNotIn("handoff", requested_actions)

        timeline_size = len(requested_payload["ticket"]["timeline"])
        duplicate_request = post_action("handoff")
        self.assertEqual(duplicate_request.status_code, 409, duplicate_request.get_json())
        self.assertEqual(duplicate_request.get_json()["reason_code"], "invalid_handoff_transition")
        db.session.refresh(self.ticket)
        self.assertEqual(len(self.ticket.datos_extra["comments"]), timeline_size)

        accepted = post_action("accept_handoff", self.employee)
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        accepted_ticket = accepted.get_json()["ticket"]
        accepted_handoff = accepted_ticket["handoff"]
        self.assertEqual(accepted_handoff["status"], "accepted")
        self.assertTrue(accepted_handoff["accepted_at"])
        self.assertEqual(accepted_handoff["accepted_by"]["id"], self.employee.id)
        self.assertEqual(accepted_ticket["assignee"]["id"], self.employee.id)
        accepted_actions = {item["id"]: item for item in accepted_ticket["allowed_actions"]}
        self.assertEqual(accepted_actions["resume_ai"]["label"], "Devolver a IA")
        self.assertNotIn("accept_handoff", accepted_actions)

        accepted_timeline_size = len(accepted_ticket["timeline"])
        duplicate_accept = post_action("accept_handoff", self.employee)
        self.assertEqual(duplicate_accept.status_code, 409, duplicate_accept.get_json())
        db.session.refresh(self.ticket)
        self.assertEqual(len(self.ticket.datos_extra["comments"]), accepted_timeline_size)

        resumed = post_action("resume_ai", self.employee)
        self.assertEqual(resumed.status_code, 200, resumed.get_json())
        resolved_handoff = resumed.get_json()["ticket"]["handoff"]
        self.assertEqual(resolved_handoff["status"], "resolved")
        self.assertEqual(resolved_handoff["resolution"], "resume_ai")
        self.assertTrue(resolved_handoff["resolved_at"])
        self.assertEqual(resolved_handoff["resolved_by"]["id"], self.employee.id)
        resumed_actions = {item["id"]: item for item in resumed.get_json()["ticket"]["allowed_actions"]}
        self.assertIn("handoff", resumed_actions)
        self.assertNotIn("resume_ai", resumed_actions)
        db.session.refresh(self.ticket)
        self.assertEqual(self.ticket.datos_extra["handoff_history"][-1]["resolution"], "resume_ai")

        duplicate_resume = post_action("resume_ai", self.employee)
        self.assertEqual(duplicate_resume.status_code, 409, duplicate_resume.get_json())

    def test_tenant_inbox_handoff_preserves_tenant_isolation(self):
        foreign_owner = User(
            name="Foreign owner",
            email="foreign-handoff@test.com",
            rol="admin",
            tenant_slug="foreign-handoff",
        )
        foreign_owner.set_password("secret123")
        db.session.add(foreign_owner)
        db.session.flush()
        foreign_tenant = TenantProfile(
            slug="foreign-handoff",
            nombre="Foreign tenant",
            tipo="pyme",
            pyme_id=foreign_owner.id,
        )
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign_owner.tenant_id = foreign_tenant.id
        foreign_ticket = TenantTicket(
            tenant_id=foreign_tenant.id,
            descripcion="Ticket de otro tenant",
            estado="nuevo",
            origen="web",
            datos_extra={},
        )
        db.session.add(foreign_ticket)
        db.session.commit()

        response = self.client.post(
            f"/api/v2/inbox/omnichannel/{foreign_ticket.id}/actions",
            json={"action": "handoff"},
            headers=self._auth(self.owner),
        )

        self.assertEqual(response.status_code, 404, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "ticket_not_found")
        db.session.refresh(foreign_ticket)
        self.assertNotIn("handoff", foreign_ticket.datos_extra)

    def test_legacy_claim_handoff_lifecycle_uses_datos_extra_and_internal_comments(self):
        legacy = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-880001",
            consulta_pin="880001",
            pregunta="Necesito hablar con un operador",
            asunto="Atencion ciudadana",
            categoria="educacion",
            estado="nuevo",
            canal_ingreso="whatsapp",
            nombre_vecino="Marcelo",
            telefono_vecino="+5492613168608",
        )
        db.session.add(legacy)
        db.session.commit()

        def post_action(action, user=None):
            return self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json={"source_model": "MunicipioTicket", "legacy_id": legacy.id, "action": action},
                headers=self._auth(user or self.owner),
            )

        detail = self.client.get(
            f"/api/v2/inbox/omnichannel/{legacy.id}?source_model=MunicipioTicket",
            headers=self._auth(self.owner),
        )
        self.assertEqual(detail.status_code, 200, detail.get_json())
        initial_actions = {item["id"]: item for item in detail.get_json()["item"]["allowed_actions"]}
        handoff_defaults = initial_actions["handoff"]["payload_defaults"]
        self.assertEqual(handoff_defaults["source_model"], "MunicipioTicket")
        self.assertEqual(handoff_defaults["legacy_id"], legacy.id)
        self.assertEqual(handoff_defaults["ticket_id"], legacy.id)
        self.assertEqual(handoff_defaults["channel"], "operator")

        requested = post_action("handoff")
        self.assertEqual(requested.status_code, 200, requested.get_json())
        self.assertFalse(requested.get_json()["delivery"]["external_dispatch"])
        self.assertEqual(requested.get_json()["delivery"]["mode"], "internal_event")
        self.assertFalse(requested.get_json()["delivery"]["realtime"]["emitted"])
        self.assertEqual(requested.get_json()["ticket"]["handoff"]["status"], "requested")
        self.assertIn(
            "accept_handoff",
            {item["id"] for item in requested.get_json()["ticket"]["allowed_actions"]},
        )
        self.assertEqual(TicketComentario.query.filter_by(municipio_ticket_id=legacy.id).count(), 1)

        duplicate = post_action("handoff")
        self.assertEqual(duplicate.status_code, 409, duplicate.get_json())
        self.assertEqual(TicketComentario.query.filter_by(municipio_ticket_id=legacy.id).count(), 1)

        accepted = post_action("accept_handoff", self.employee)
        self.assertEqual(accepted.status_code, 200, accepted.get_json())
        accepted_ticket = accepted.get_json()["ticket"]
        self.assertEqual(accepted_ticket["handoff"]["status"], "accepted")
        self.assertEqual(accepted_ticket["handoff"]["accepted_by"]["id"], self.employee.id)
        self.assertEqual(accepted_ticket["assignee"]["id"], self.employee.id)

        resumed = post_action("resume_ai", self.employee)
        self.assertEqual(resumed.status_code, 200, resumed.get_json())
        handoff = resumed.get_json()["ticket"]["handoff"]
        self.assertEqual(handoff["status"], "resolved")
        self.assertEqual(handoff["resolution"], "resume_ai")
        self.assertEqual(handoff["resolved_by"]["id"], self.employee.id)
        self.assertIn("handoff", {item["id"] for item in resumed.get_json()["ticket"]["allowed_actions"]})

        comments = TicketComentario.query.filter_by(municipio_ticket_id=legacy.id).order_by(TicketComentario.id.asc()).all()
        self.assertEqual([item.estado_ticket for item in comments], ["handoff", "accept_handoff", "resume_ai"])
        self.assertTrue(all(item.origen == "internal" for item in comments))
        db.session.refresh(legacy)
        self.assertEqual(legacy.asignado_a_id, self.employee.id)
        self.assertTrue(legacy.asignado_en)
        self.assertEqual(legacy.datos_extra["handoff"]["accepted_by"]["id"], self.employee.id)
        self.assertEqual(legacy.datos_extra["handoff"]["resolution"], "resume_ai")


if __name__ == "__main__":
    unittest.main()
