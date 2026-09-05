from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

import jwt
import pytest
import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app
from config import Config
from models import (
    AuditEvent,
    DomainEffectOutbox,
    MunicipioTicket,
    MunicipioTicketHandoffEvent,
    MunicipioTicketReplyEvent,
    Notification,
    TenantProfile,
    TicketComentario,
    User,
    db,
)
from services.municipio_ticket_handoff import list_handoff_events

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT
    / "migrations"
    / "versions"
    / "20260905_add_municipio_ticket_handoff_event_v1.py"
)


class HandoffConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS: ClassVar = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


@pytest.fixture()
def handoff_env():
    app = create_app(HandoffConfig)
    ctx = app.app_context()
    ctx.push()
    db.create_all()

    owner = User(
        name="Municipal owner",
        email="handoff-owner@example.test",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="municipio-handoff",
    )
    owner.set_password("secret123")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="municipio-handoff",
        nombre="Municipio Handoff",
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id

    operator = User(
        name="Operador luminarias",
        email="handoff-operator@example.test",
        rol="empleado",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        es_empleado=True,
        accesibilidad={
            "employee_scope": {
                "categorias": ["luminarias"],
                "zonas": ["centro"],
                "channels": ["whatsapp"],
                "permisos": ["tickets.read"],
            }
        },
    )
    operator.set_password("secret123")
    db.session.add(operator)
    db.session.flush()
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=owner.id,
        nro_ticket="M-HANDOFF-001",
        consulta_pin="740001",
        pregunta="Luminaria apagada",
        asunto="Luminaria apagada",
        categoria="luminarias",
        detalles="Sin luz frente a la plaza",
        direccion="San Martin 100, Junin, Mendoza",
        distrito="Centro",
        estado="en_proceso",
        asignado_a_id=operator.id,
        canal_ingreso="whatsapp",
        nombre_vecino="Persona de prueba",
        telefono_vecino="+5492613000000",
        datos_extra={},
    )
    db.session.add(ticket)
    db.session.commit()

    def auth(user: User, scoped_tenant: TenantProfile | None = None):
        selected = scoped_tenant or tenant
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": selected.slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": selected.slug,
        }

    yield app, app.test_client(), tenant, owner, operator, ticket, auth

    db.session.remove()
    db.drop_all()
    ctx.pop()


def _post_handoff(
    client, ticket, headers, *, key=None, reason="Escalamiento operativo"
):
    request_headers = dict(headers)
    if key is not None:
        request_headers["Idempotency-Key"] = key
    return client.post(
        f"/api/v2/inbox/omnichannel/{ticket.id}/actions",
        json={
            "action": "handoff",
            "source_model": "MunicipioTicket",
            "channel": "operator",
            "reason": reason,
        },
        headers=request_headers,
    )


def test_handoff_requires_header_reason_and_channel_before_mutation(handoff_env):
    _, client, _, _, operator, ticket, auth = handoff_env

    missing_key = _post_handoff(client, ticket, auth(operator))
    assert missing_key.status_code == 400, missing_key.get_json()
    assert missing_key.get_json()["reason_code"] == "handoff_idempotency_key_required"

    missing_reason = client.post(
        f"/api/v2/inbox/omnichannel/{ticket.id}/actions",
        json={
            "action": "handoff",
            "source_model": "MunicipioTicket",
            "channel": "operator",
        },
        headers={**auth(operator), "Idempotency-Key": "handoff-required-0001"},
    )
    assert missing_reason.status_code == 400, missing_reason.get_json()
    assert missing_reason.get_json()["reason_code"] == "handoff_reason_required"

    missing_channel = client.post(
        f"/api/v2/inbox/omnichannel/{ticket.id}/actions",
        json={
            "action": "handoff",
            "source_model": "MunicipioTicket",
            "reason": "Escalamiento operativo",
        },
        headers={**auth(operator), "Idempotency-Key": "handoff-required-0002"},
    )
    assert missing_channel.status_code == 400, missing_channel.get_json()
    assert missing_channel.get_json()["reason_code"] == "handoff_channel_required"
    assert MunicipioTicketHandoffEvent.query.count() == 0
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 0


def test_handoff_replays_same_digest_and_conflicts_on_payload_change(handoff_env):
    _, client, tenant, _, operator, ticket, auth = handoff_env
    key = "municipio-handoff-idempotent-0001"

    first = _post_handoff(client, ticket, auth(operator), key=key)
    replay = _post_handoff(client, ticket, auth(operator), key=key)
    conflict = _post_handoff(
        client,
        ticket,
        auth(operator),
        key=key,
        reason="Otro motivo para la misma clave",
    )

    assert first.status_code == 200, first.get_json()
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["delivery"]["idempotency"]["replayed"] is True
    assert (
        replay.get_json()["handoff_event"]["id"]
        == first.get_json()["handoff_event"]["id"]
    )
    assert conflict.status_code == 409, conflict.get_json()
    assert conflict.get_json()["reason_code"] == "handoff_idempotency_payload_conflict"
    assert MunicipioTicketHandoffEvent.query.filter_by(tenant_id=tenant.id).count() == 1
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 1
    assert (
        AuditEvent.query.filter_by(
            tenant_id=tenant.id,
            event_type="municipio_ticket.handoff.requested",
        ).count()
        == 1
    )


def test_handoff_ledger_projects_compatibly_audits_without_pii_and_never_sends(
    handoff_env,
):
    _, client, tenant, _, operator, ticket, auth = handoff_env
    key = "municipio-handoff-ledger-0001"
    pii_reason = "Escalar por dni 12345678 y persona@example.test"

    response = _post_handoff(
        client,
        ticket,
        auth(operator),
        key=key,
        reason=pii_reason,
    )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["delivery"]["external_dispatch"] is False
    assert payload["delivery"]["ledger"]["normalized_event"] is True
    assert payload["handoff_event"]["reason"] == pii_reason
    row = MunicipioTicketHandoffEvent.query.one()
    assert row.ticket_id == ticket.id
    assert row.actor_user_id == operator.id
    assert row.previous_assignee_user_id == operator.id
    assert row.idempotency_key_hash != key
    assert len(row.idempotency_key_hash) == 64
    assert len(row.request_digest) == 64

    db.session.refresh(ticket)
    projection = ticket.datos_extra["handoff"]
    assert projection["contract_version"] == "inbox.handoff.v1"
    assert projection["ledger_contract_version"] == row.CONTRACT_VERSION
    assert projection["event_id"] == row.event_id
    assert projection["status"] == "requested"
    assert projection["channel"] == "operator"
    assert projection["reason"] == pii_reason

    audit = AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="municipio_ticket.handoff.requested",
    ).one()
    audit_payload = json.dumps(audit.details, ensure_ascii=False)
    assert "12345678" not in audit_payload
    assert "persona@example.test" not in audit_payload
    assert key not in audit_payload
    assert audit.details["external_dispatch"] is False
    assert DomainEffectOutbox.query.count() == 0
    assert Notification.query.count() == 0
    assert MunicipioTicketReplyEvent.query.count() == 0

    detail = client.get(
        f"/api/v2/inbox/omnichannel/{ticket.id}?source_model=MunicipioTicket",
        headers=auth(operator),
    )
    assert detail.status_code == 200, detail.get_json()
    item = detail.get_json()["item"]
    assert [event["id"] for event in item["handoff_events"]] == [row.event_id]
    accept_contract = next(
        action for action in item["allowed_actions"] if action["id"] == "accept_handoff"
    )
    assert accept_contract["ledger"]["normalized_event"] is False
    assert accept_contract["ledger"]["mode"] == "legacy_projection_only"


def test_handoff_event_window_keeps_latest_one_hundred_in_chronological_order(
    handoff_env,
):
    _, _, tenant, _, operator, ticket, _ = handoff_env
    base = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    for index in range(101):
        comment = TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario=f"Handoff historico {index}",
            user_id=operator.id,
            es_admin=True,
            origen="internal",
            estado_ticket="handoff",
        )
        db.session.add(comment)
        db.session.flush()
        db.session.add(
            MunicipioTicketHandoffEvent(
                tenant_id=tenant.id,
                ticket_id=ticket.id,
                comment_id=comment.id,
                event_id=f"mhe_window_{index:03d}",
                channel="operator",
                reason=f"Motivo {index}",
                actor_user_id=operator.id,
                previous_assignee_user_id=operator.id,
                idempotency_key_hash=f"{index + 1:064x}",
                request_digest=f"{index + 1001:064x}",
                created_at=base + timedelta(seconds=index),
            )
        )
    db.session.commit()

    events = list_handoff_events(tenant_id=tenant.id, ticket_id=ticket.id)

    assert len(events) == 100
    assert events[0]["id"] == "mhe_window_001"
    assert events[-1]["id"] == "mhe_window_100"


def test_handoff_enforces_tenant_role_category_and_ownership(handoff_env):
    _, client, tenant, _, _, ticket, auth = handoff_env
    viewer = User(
        name="Viewer",
        email="handoff-viewer@example.test",
        rol="usuario",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )
    viewer.set_password("secret123")
    wrong_category = User(
        name="Operador transito",
        email="handoff-transito@example.test",
        rol="empleado",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        es_empleado=True,
        accesibilidad={"employee_scope": {"categorias": ["transito"]}},
    )
    wrong_category.set_password("secret123")
    other_operator = User(
        name="Operador sin ownership",
        email="handoff-other@example.test",
        rol="empleado",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        es_empleado=True,
        accesibilidad={"employee_scope": {"categorias": ["luminarias"]}},
    )
    other_operator.set_password("secret123")
    foreign_owner = User(
        name="Foreign owner",
        email="handoff-foreign@example.test",
        rol="admin",
        tenant_slug="foreign-handoff",
    )
    foreign_owner.set_password("secret123")
    db.session.add_all([viewer, wrong_category, other_operator, foreign_owner])
    db.session.flush()
    foreign_tenant = TenantProfile(
        slug="foreign-handoff",
        nombre="Foreign Handoff",
        tipo="municipio",
        municipio_id=foreign_owner.id,
        is_active=True,
    )
    db.session.add(foreign_tenant)
    db.session.flush()
    foreign_owner.tenant_id = foreign_tenant.id
    db.session.commit()

    denied_role = _post_handoff(
        client, ticket, auth(viewer), key="handoff-role-denied-0001"
    )
    denied_category = _post_handoff(
        client,
        ticket,
        auth(wrong_category),
        key="handoff-category-denied-0001",
    )
    denied_owner = _post_handoff(
        client,
        ticket,
        auth(other_operator),
        key="handoff-owner-denied-0001",
    )
    denied_tenant = _post_handoff(
        client,
        ticket,
        auth(foreign_owner, foreign_tenant),
        key="handoff-tenant-denied-0001",
    )

    assert denied_role.status_code == 403, denied_role.get_json()
    assert denied_category.status_code == 404, denied_category.get_json()
    assert denied_owner.status_code == 409, denied_owner.get_json()
    assert denied_owner.get_json()["reason_code"] == "ticket_assigned_to_other"
    assert denied_tenant.status_code == 404, denied_tenant.get_json()
    assert MunicipioTicketHandoffEvent.query.count() == 0
    assert (
        AuditEvent.query.filter_by(
            event_type="municipio_ticket.handoff.requested"
        ).count()
        == 0
    )


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "municipio_ticket_handoff_event_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_handoff_migration_is_linear_constrained_immutable_and_reversible():
    migration = _load_migration()
    assert migration.revision == "20260905_municipio_handoff_v1"
    assert migration.down_revision == "20260905_municipio_reply_v1"

    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "tenant_profile", metadata, sa.Column("id", sa.Integer(), primary_key=True)
    )
    sa.Table("user", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table(
        "municipio_ticket", metadata, sa.Column("id", sa.Integer(), primary_key=True)
    )
    sa.Table(
        "ticket_comentario", metadata, sa.Column("id", sa.Integer(), primary_key=True)
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        inspector = sa.inspect(connection)
        assert "municipio_ticket_handoff_event" in inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("municipio_ticket_handoff_event")
        }
        assert {
            "tenant_id",
            "source_model",
            "ticket_id",
            "comment_id",
            "event_id",
            "channel",
            "reason",
            "actor_user_id",
            "idempotency_key_hash",
            "request_digest",
            "projection_contract_version",
        }.issubset(columns)
        unique_sets = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(
                "municipio_ticket_handoff_event"
            )
        }
        assert ("tenant_id", "idempotency_key_hash") in unique_sets
        assert ("tenant_id", "event_id") in unique_sets
        indexes = {
            item["name"]: tuple(item["column_names"])
            for item in inspector.get_indexes("municipio_ticket_handoff_event")
        }
        assert indexes["ix_municipio_handoff_ticket"] == (
            "tenant_id",
            "ticket_id",
            "created_at",
            "id",
        )

        connection.execute(sa.text("INSERT INTO tenant_profile (id) VALUES (1)"))
        connection.execute(sa.text('INSERT INTO "user" (id) VALUES (10), (11)'))
        connection.execute(sa.text("INSERT INTO municipio_ticket (id) VALUES (20)"))
        connection.execute(sa.text("INSERT INTO ticket_comentario (id) VALUES (30)"))
        connection.execute(
            sa.text(
                "INSERT INTO municipio_ticket_handoff_event "
                "(id, tenant_id, source_model, ticket_id, comment_id, event_id, "
                "action, status, channel, reason, actor_user_id, "
                "previous_assignee_user_id, idempotency_key_hash, request_digest, "
                "projection_contract_version, contract_version) VALUES "
                "(1, 1, 'MunicipioTicket', 20, 30, 'mhe_test', 'handoff', "
                "'requested', 'operator', 'Escalamiento operativo', 10, 11, "
                ":key_hash, :request_digest, 'inbox.handoff.v1', "
                "'municipio_ticket.handoff_event.v1')"
            ),
            {"key_hash": "a" * 64, "request_digest": "b" * 64},
        )
        with pytest.raises(sa.exc.DatabaseError):
            connection.execute(
                sa.text(
                    "UPDATE municipio_ticket_handoff_event "
                    "SET channel = 'phone' WHERE id = 1"
                )
            )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text('DELETE FROM "user" WHERE id = 11'))
        # Authorized retention/purge may delete the immutable receipt itself.
        connection.execute(
            sa.text("DELETE FROM municipio_ticket_handoff_event WHERE id = 1")
        )
        connection.execute(sa.text('DELETE FROM "user" WHERE id = 11'))
        remaining = connection.execute(
            sa.text("SELECT count(*) FROM municipio_ticket_handoff_event")
        ).scalar_one()
        assert remaining == 0

        with Operations.context(context):
            migration.downgrade()
        assert (
            "municipio_ticket_handoff_event"
            not in sa.inspect(connection).get_table_names()
        )


def test_handoff_migration_is_the_single_alembic_head():
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260905_municipio_handoff_v1"
    ]
