from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qsl, urlparse

import jwt
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from alembic.config import Config as AlembicConfig

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app
from config import Config
from models import (
    AuditEvent,
    DomainEffectOutbox,
    MessageTemplateRegistry,
    MessagingEventLedger,
    MunicipioTicket,
    MunicipioTicketReplyEvent,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    TicketComentario,
    TicketDomainEffectReceipt,
    User,
    WhatsAppContactState,
    db,
)
from services.domain_effect_outbox import dispatch_domain_effects
from services.municipio_ticket_reply_delivery import reconcile_provider_callback
from services.ticket_domain_effects import TICKET_DOMAIN_EFFECT_REGISTRY
from services.ticket_service import MunicipioTicketCreator


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260905_add_municipio_ticket_reply_delivery_v1.py"
)


class MunicipioReplyConfig(Config):
    TESTING = True
    TWILIO_ALLOW_NETWORK_IN_TESTS = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    DOMAIN_EFFECT_OUTBOX_MODE = "queue"
    DOMAIN_EFFECT_OUTBOX_SECRET = "municipio-reply-outbox-secret-32-bytes-minimum"
    DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES = 4096
    DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS = 8


class TestMunicipioTicketEnterpriseReply:
    def setup_method(self):
        self.app = create_app(MunicipioReplyConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(
            name="Municipal owner",
            email="municipal-owner@example.test",
            rol="admin",
            tipo_chat="municipio",
            tenant_slug="municipio-reply",
        )
        self.owner.set_password("secret123")
        db.session.add(self.owner)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="municipio-reply",
            nombre="Municipio Reply",
            tipo="municipio",
            municipio_id=self.owner.id,
            plan="full",
            is_active=True,
            configuracion={},
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.owner.tenant_id = self.tenant.id

        self.employee = User(
            name="Operador luminarias",
            email="luminarias@example.test",
            rol="empleado",
            tenant_id=self.tenant.id,
            tenant_slug=self.tenant.slug,
            es_empleado=True,
            accesibilidad={
                "employee_scope": {
                    "categorias": ["luminarias"],
                    "zonas": ["centro"],
                    "channels": ["whatsapp"],
                    "permisos": [],
                }
            },
        )
        self.employee.set_password("secret123")
        db.session.add(self.employee)
        db.session.flush()
        self.ticket = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.owner.id,
            nro_ticket="M-ENTERPRISE-001",
            consulta_pin="410001",
            pregunta="Luminaria apagada",
            asunto="Luminaria apagada",
            categoria="luminarias",
            detalles="Sin luz frente a la plaza",
            direccion="San Martin 100, Junin, Mendoza",
            distrito="Centro",
            estado="en_proceso",
            asignado_a_id=self.employee.id,
            asignado_en=datetime.now(timezone.utc),
            canal_ingreso="whatsapp",
            nombre_vecino="Vecina de prueba",
            telefono_vecino="+5492613168608",
            datos_extra={},
        )
        db.session.add(self.ticket)
        db.session.commit()
        self.app.config["DOMAIN_EFFECT_OUTBOX_TENANT_IDS"] = str(self.tenant.id)

    def teardown_method(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self, user: User, tenant: TenantProfile | None = None):
        scoped_tenant = tenant or self.tenant
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": scoped_tenant.slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": scoped_tenant.slug,
        }

    def _configure_sender(self, *, last_inbound_at: datetime | None = None):
        token_ref = f"TWILIO_SUBACCOUNT_AUTH_TOKEN_MUNICIPIO_{self.tenant.id}"
        account_sid = f"AC-municipio-{self.tenant.id}"
        self.app.config[token_ref] = "test-municipio-token"
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
        db.session.flush()
        db.session.add(
            WhatsAppContactState(
                tenant_id=self.tenant.id,
                provider_sender_id=sender.id,
                recipient="whatsapp:+5492613168608",
                last_inbound_at=last_inbound_at or datetime.now(timezone.utc),
            )
        )
        db.session.add(self.tenant)
        db.session.commit()
        return sender

    def _post_reply(
        self,
        *,
        key: str,
        body: str = "La cuadrilla ya recibio el caso.",
        template_registry_id: int | None = None,
        template_variables: dict[str, str] | None = None,
    ):
        payload = {
            "action": "reply",
            "source_model": "MunicipioTicket",
            "body": body,
            "visibility": "public",
            "delivery_channels": ["whatsapp"],
            "client_message_id": key,
        }
        if template_registry_id is not None:
            payload["template_registry_id"] = template_registry_id
            payload["template_variables"] = template_variables or {}
        return self.client.post(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
            json=payload,
            headers={**self._auth(self.employee), "Idempotency-Key": key},
        )

    def test_happy_path_pins_scope_and_stages_one_whatsapp_effect(self):
        sender = self._configure_sender()

        response = self._post_reply(key="municipio-reply-happy-0001")

        assert response.status_code == 200, response.get_json()
        delivery = response.get_json()["delivery"]
        assert delivery["mode"] == "durable_queue"
        assert delivery["outbox"]["external_effect_count"] == 1
        assert delivery["outbox"]["direct_dispatch_performed"] is False
        record = MunicipioTicketReplyEvent.query.one()
        assert record.tenant_id == self.tenant.id
        assert record.source_model == "MunicipioTicket"
        assert record.ticket_id == self.ticket.id
        assert record.actor_user_id == self.employee.id
        assert record.recipient_phone == "+5492613168608"
        assert record.whatsapp_provider_sender_id == sender.id
        assert record.whatsapp_delivery_status == "queued"
        assert record.comment_id is not None
        effects = DomainEffectOutbox.query.filter_by(
            tenant_id=self.tenant.id,
            aggregate_type="municipio_ticket_reply",
            channel="whatsapp",
        ).all()
        assert len(effects) == 1
        assert effects[0].recipient_ref.startswith("recipient_hash:")
        assert "+5492613168608" not in str(effects[0].payload_json)

        detail = self.client.get(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}"
            "?source_model=MunicipioTicket",
            headers=self._auth(self.employee),
        )
        assert detail.status_code == 200, detail.get_json()
        persisted_deliveries = detail.get_json()["item"]["reply_deliveries"]
        assert len(persisted_deliveries) == 1
        assert persisted_deliveries[0]["event_id"] == record.event_id
        assert persisted_deliveries[0]["status"] == "queued"

    def test_duplicate_request_replays_without_second_comment_event_or_send(self):
        self._configure_sender()
        key = "municipio-reply-idempotent-0001"

        first = self._post_reply(key=key)
        replay = self._post_reply(key=key)

        assert first.status_code == 200, first.get_json()
        assert replay.status_code == 200, replay.get_json()
        assert replay.get_json()["delivery"]["idempotency"]["replayed"] is True
        assert TicketComentario.query.filter_by(
            municipio_ticket_id=self.ticket.id,
            comentario="La cuadrilla ya recibio el caso.",
        ).count() == 1
        assert MunicipioTicketReplyEvent.query.filter_by(
            tenant_id=self.tenant.id,
            ticket_id=self.ticket.id,
        ).count() == 1
        assert DomainEffectOutbox.query.filter_by(
            tenant_id=self.tenant.id,
            aggregate_type="municipio_ticket_reply",
            channel="whatsapp",
        ).count() == 1
        assert TicketDomainEffectReceipt.query.filter_by(
            tenant_id=self.tenant.id,
            effect_kind="ticket.comment.municipio",
        ).count() == 1

    def test_cross_tenant_operator_cannot_reply_or_create_delivery_state(self):
        foreign_owner = User(
            name="Foreign owner",
            email="foreign-owner@example.test",
            rol="admin",
            tenant_slug="foreign-municipio",
        )
        foreign_owner.set_password("secret123")
        db.session.add(foreign_owner)
        db.session.flush()
        foreign_tenant = TenantProfile(
            slug="foreign-municipio",
            nombre="Foreign Municipio",
            tipo="municipio",
            municipio_id=foreign_owner.id,
            is_active=True,
        )
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign_owner.tenant_id = foreign_tenant.id
        db.session.commit()

        response = self.client.post(
            f"/api/v2/inbox/omnichannel/{self.ticket.id}/actions",
            json={
                "action": "reply",
                "source_model": "MunicipioTicket",
                "body": "No autorizado",
                "visibility": "public",
                "delivery_channels": ["whatsapp"],
                "client_message_id": "municipio-cross-tenant-0001",
            },
            headers={
                **self._auth(foreign_owner, foreign_tenant),
                "Idempotency-Key": "municipio-cross-tenant-0001",
            },
        )

        assert response.status_code == 404, response.get_json()
        assert MunicipioTicketReplyEvent.query.count() == 0
        assert DomainEffectOutbox.query.count() == 0

    def test_missing_sender_fails_before_timeline_or_outbox_mutation(self):
        response = self._post_reply(key="municipio-reply-no-sender-0001")

        assert response.status_code == 400, response.get_json()
        assert response.get_json()["reason_code"] == "whatsapp_tenant_sender_missing"
        assert TicketComentario.query.filter_by(
            municipio_ticket_id=self.ticket.id,
        ).count() == 0
        assert MunicipioTicketReplyEvent.query.count() == 0
        assert DomainEffectOutbox.query.count() == 0

    def test_closed_service_window_requires_approved_template_before_mutation(self):
        self._configure_sender(
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=25)
        )

        response = self._post_reply(key="municipio-reply-window-closed-0001")

        assert response.status_code == 409, response.get_json()
        assert response.get_json()["reason_code"] == "whatsapp_template_required_outside_24h"
        assert TicketComentario.query.filter_by(
            municipio_ticket_id=self.ticket.id,
        ).count() == 0
        assert MunicipioTicketReplyEvent.query.count() == 0
        assert DomainEffectOutbox.query.count() == 0

    def test_approved_template_delivers_outside_service_window(self):
        sender = self._configure_sender(
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=25)
        )
        template = MessageTemplateRegistry(
            tenant_id=self.tenant.id,
            provider="twilio",
            channel="whatsapp",
            name="municipio_reclamo_actualizado",
            language="es_AR",
            category="UTILITY",
            status="approved",
            content_sid="HX" + ("f" * 32),
            last_sync_at=datetime.now(timezone.utc),
            body_preview="Actualizamos el reclamo {{1}}: {{2}}.",
        )
        db.session.add(template)
        db.session.commit()
        variables = {"1": "M-ENTERPRISE-001", "2": "cuadrilla asignada"}
        rendered = (
            "Actualizamos el reclamo M-ENTERPRISE-001: cuadrilla asignada."
        )

        response = self._post_reply(
            key="municipio-reply-template-0001",
            body=rendered,
            template_registry_id=template.id,
            template_variables=variables,
        )

        assert response.status_code == 200, response.get_json()
        reply = MunicipioTicketReplyEvent.query.one()
        assert reply.whatsapp_template_registry_id == template.id
        assert reply.whatsapp_template_variables == variables
        assert reply.to_event_dict()["content_source"] == (
            "approved_whatsapp_template"
        )

        with patch("services.tenant_twilio_messaging.Client") as twilio_client, patch(
            "socket_service.emit_ticket_reply_delivery_updated"
        ):
            twilio_client.return_value.messages.create.return_value = SimpleNamespace(
                sid="SM" + ("t" * 32)
            )
            summary = dispatch_domain_effects(
                registry=TICKET_DOMAIN_EFFECT_REGISTRY,
                intent_secret=self.app.config["DOMAIN_EFFECT_OUTBOX_SECRET"],
                tenant_id=self.tenant.id,
                limit=10,
            )

        assert summary.succeeded == 2
        params = twilio_client.return_value.messages.create.call_args.kwargs
        assert params["from_"] == sender.sender_id
        assert params["to"] == "whatsapp:+5492613168608"
        assert params["content_sid"] == template.content_sid
        assert json.loads(params["content_variables"]) == variables
        assert "body" not in params

    def test_outbox_dispatch_uses_pinned_sender_recipient_and_municipio_callback(self):
        sender = self._configure_sender()
        response = self._post_reply(key="municipio-reply-dispatch-0001")
        assert response.status_code == 200, response.get_json()
        reply = MunicipioTicketReplyEvent.query.one()
        provider_message_id = "SM" + ("m" * 32)

        with patch("services.tenant_twilio_messaging.Client") as twilio_client, patch(
            "socket_service.emit_ticket_reply_delivery_updated"
        ):
            twilio_client.return_value.messages.create.return_value = SimpleNamespace(
                sid=provider_message_id
            )
            summary = dispatch_domain_effects(
                registry=TICKET_DOMAIN_EFFECT_REGISTRY,
                intent_secret=self.app.config["DOMAIN_EFFECT_OUTBOX_SECRET"],
                tenant_id=self.tenant.id,
                limit=10,
            )

        assert summary.succeeded == 2
        create_call = twilio_client.return_value.messages.create.call_args
        assert create_call is not None
        params = create_call.kwargs
        assert params["from_"] == sender.sender_id
        assert params["to"] == "whatsapp:+5492613168608"
        assert params["body"] == "La cuadrilla ya recibio el caso."
        callback = urlparse(params["status_callback"])
        assert (callback.scheme, callback.netloc, callback.path) == (
            "https",
            "api.example.test",
            "/twilio/whatsapp/status",
        )
        assert dict(parse_qsl(callback.query)) == {
            "municipio_ticket_reply_event_id": str(reply.id),
        }
        db.session.expire_all()
        stored = db.session.get(MunicipioTicketReplyEvent, reply.id)
        assert stored.whatsapp_delivery_status == "provider_accepted"
        assert stored.whatsapp_provider_message_id == provider_message_id

    def test_provider_callback_advances_delivery_monotonically_and_audits_without_pii(self):
        sender = self._configure_sender()
        response = self._post_reply(key="municipio-reply-callback-0001")
        assert response.status_code == 200, response.get_json()
        reply = MunicipioTicketReplyEvent.query.one()
        provider_message_id = "SM" + ("d" * 32)
        reply.whatsapp_provider_message_id = provider_message_id
        reply.whatsapp_delivery_status = "provider_accepted"
        reply.whatsapp_provider_status = "accepted"
        delivered_event = MessagingEventLedger(
            tenant_id=self.tenant.id,
            provider_connection_id=sender.provider_connection_id,
            provider_sender_id=sender.id,
            channel="whatsapp",
            direction="outbound",
            event_type="status_callback",
            provider="twilio",
            provider_event_id=f"{provider_message_id}:delivered",
            external_message_sid=provider_message_id,
            external_status="delivered",
        )
        db.session.add(delivered_event)
        db.session.commit()

        delivered = reconcile_provider_callback(
            tenant_id=self.tenant.id,
            reply_event_record_id=reply.id,
            provider_message_id=provider_message_id,
            provider_status="delivered",
            provider_sender_id=sender.id,
            delivery_event_id=delivered_event.id,
        )
        assert delivered is not None
        assert delivered.whatsapp_delivery_status == "delivered"
        delivered_status_event_id = delivered.whatsapp_status_event_id

        failed_event = MessagingEventLedger(
            tenant_id=self.tenant.id,
            provider_connection_id=sender.provider_connection_id,
            provider_sender_id=sender.id,
            channel="whatsapp",
            direction="outbound",
            event_type="status_callback",
            provider="twilio",
            provider_event_id=f"{provider_message_id}:failed",
            external_message_sid=provider_message_id,
            external_status="failed",
        )
        db.session.add(failed_event)
        db.session.commit()
        late_failure = reconcile_provider_callback(
            tenant_id=self.tenant.id,
            reply_event_record_id=reply.id,
            provider_message_id=provider_message_id,
            provider_status="failed",
            provider_sender_id=sender.id,
            delivery_event_id=failed_event.id,
            error_code="30007",
        )
        assert late_failure is not None
        assert late_failure.whatsapp_delivery_status == "delivered"
        assert late_failure.whatsapp_status_event_id == delivered_status_event_id

        audits = AuditEvent.query.filter_by(
            tenant_id=self.tenant.id,
            event_type="municipio_ticket.reply.whatsapp.delivery_callback",
        ).all()
        assert len(audits) == 2
        serialized = json.dumps([event.details for event in audits], sort_keys=True)
        assert provider_message_id not in serialized
        assert "+5492613168608" not in serialized
        assert "La cuadrilla ya recibio el caso." not in serialized

    def test_ticket_creator_persists_district_for_assignment_and_territorial_views(self):
        created = MunicipioTicketCreator().create(
            {
                "tenant_id": self.tenant.id,
                "municipio_id": self.owner.id,
                "nro_ticket": "M-DISTRICT-001",
                "categoria": "luminarias",
                "pregunta": "Luminaria apagada",
                "direccion": "San Martin 100",
                "distrito": "Centro",
            }
        )

        assert created.distrito == "Centro"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "municipio_ticket_reply_delivery_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_linear_constrained_indexed_and_reversible():
    migration = _load_migration()
    assert migration.revision == "20260905_municipio_reply_v1"
    assert migration.down_revision == "20260905_government_launch_v1"

    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table("tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("municipio_ticket", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("ticket_comentario", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("message_template_registry", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("provider_sender", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("messaging_event_ledger", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        inspector = sa.inspect(connection)
        assert "municipio_ticket_reply_event" in inspector.get_table_names()
        columns = {column["name"]: column for column in inspector.get_columns("municipio_ticket_reply_event")}
        assert {
            "tenant_id",
            "source_model",
            "ticket_id",
            "comment_id",
            "event_id",
            "actor_user_id",
            "recipient_phone",
            "whatsapp_provider_sender_id",
            "whatsapp_policy_snapshot",
            "whatsapp_delivery_status",
        }.issubset(columns)
        assert columns["tenant_id"]["nullable"] is False
        assert columns["source_model"]["nullable"] is False
        assert columns["ticket_id"]["nullable"] is False
        assert columns["comment_id"]["nullable"] is False
        assert columns["recipient_phone"]["nullable"] is False
        assert columns["whatsapp_provider_sender_id"]["nullable"] is False
        unique_sets = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("municipio_ticket_reply_event")
        }
        assert ("tenant_id", "event_id") in unique_sets
        assert ("tenant_id", "comment_id") in unique_sets
        indexes = {
            item["name"]: tuple(item["column_names"])
            for item in inspector.get_indexes("municipio_ticket_reply_event")
        }
        assert indexes["ix_municipio_reply_ticket"] == (
            "tenant_id",
            "ticket_id",
            "created_at",
        )
        assert indexes["ix_municipio_reply_wa_provider_message"] == (
            "tenant_id",
            "whatsapp_provider_message_id",
        )

        with Operations.context(context):
            migration.downgrade()
        assert "municipio_ticket_reply_event" not in sa.inspect(connection).get_table_names()


def test_migration_is_the_single_alembic_head():
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260905_municipio_handoff_v1"
    ]
