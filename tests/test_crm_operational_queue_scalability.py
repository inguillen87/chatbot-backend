import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import pytest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from extensions import limiter
from models import MunicipioTicket, TenantProfile, TenantTicket, User


class OperationalQueueScalabilityConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"
    CRM_OPERATIONAL_QUEUE_CURSOR_SECRET = "operational-queue-scalability-secret"
    CRM_OPERATIONAL_QUEUE_RATE_LIMIT = "1000 per 60 seconds"
    CRM_OPERATIONAL_QUEUE_MAX_SCANNED_ROWS = 100


@pytest.fixture()
def queue_env():
    app = create_app(OperationalQueueScalabilityConfig)
    context = app.app_context()
    context.push()
    db.create_all()
    client = app.test_client()

    admin = User(
        name="Queue Admin",
        email="queue-admin@test.local",
        password_hash="test-hash",
        rol="admin",
        tenant_slug="queue-scale",
    )
    db.session.add(admin)
    db.session.flush()
    tenant = TenantProfile(
        slug="queue-scale",
        nombre="Queue Scale",
        tipo="municipio",
        municipio_id=admin.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    admin.tenant_id = tenant.id
    db.session.commit()

    sequence = {"value": 1000}

    def auth(actor=None):
        actor = actor or admin
        token = jwt.encode(
            {
                "user_id": actor.id,
                "rol": actor.rol,
                "tenant_slug": actor.tenant_slug,
                "tenant_id": actor.tenant_id,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": tenant.slug,
        }

    def get(query="", actor=None):
        suffix = f"?{query}" if query else ""
        return client.get(
            f"/api/v2/inbox/operational-queue{suffix}",
            headers=auth(actor),
        )

    def tenant_ticket(*, created_at, assignee_id=None, sla_state=None):
        extra = {"title": "Tenant queue ticket"}
        if assignee_id is not None:
            extra["assignee_id"] = assignee_id
        if sla_state is not None:
            extra["sla_status"] = sla_state
        row = TenantTicket(
            tenant_id=tenant.id,
            user_id=admin.id,
            categoria="alumbrado",
            descripcion="Queue scalability",
            estado="nuevo",
            origen="whatsapp",
            datos_extra=extra,
            created_at=created_at,
            updated_at=created_at,
        )
        db.session.add(row)
        db.session.flush()
        return row

    def municipio_ticket(*, created_at, assignee_id=None):
        sequence["value"] += 1
        row = MunicipioTicket(
            tenant_id=tenant.id,
            municipio_id=admin.id,
            pregunta="Queue scalability",
            asunto="Municipio queue ticket",
            categoria="arbolado",
            estado="nuevo",
            asignado_a_id=assignee_id,
            nro_ticket=f"scale-{sequence['value']}",
            fecha=created_at,
            ultima_actividad=created_at,
        )
        db.session.add(row)
        db.session.flush()
        return row

    environment = SimpleNamespace(
        app=app,
        admin=admin,
        tenant=tenant,
        client=client,
        auth=auth,
        get=get,
        tenant_ticket=tenant_ticket,
        municipio_ticket=municipio_ticket,
    )
    try:
        yield environment
    finally:
        db.session.remove()
        db.drop_all()
        context.pop()


def test_age_filter_pushdown_reaches_old_match_with_tiny_budget(queue_env):
    now = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
    queue_env.app.config["CRM_OPERATIONAL_QUEUE_MAX_SCANNED_ROWS"] = 2
    for offset in range(20):
        queue_env.tenant_ticket(created_at=now - timedelta(minutes=offset + 1))
    expected = queue_env.tenant_ticket(created_at=now - timedelta(days=8))
    db.session.commit()

    with patch("services.crm_operational_queue._utc_now", return_value=now):
        response = queue_env.get("source_model=TenantTicket&age=gte_7d&limit=1")

    assert response.status_code == 200
    assert [item["queue_id"] for item in response.get_json()["items"]] == [
        f"TenantTicket:{expected.id}"
    ]
    assert response.headers["X-RateLimit-Limit"] == "1000"


def test_native_assignee_pushdown_reaches_match_with_tiny_budget(queue_env):
    now = datetime(2026, 8, 2, 18, 30, tzinfo=timezone.utc)
    queue_env.app.config["CRM_OPERATIONAL_QUEUE_MAX_SCANNED_ROWS"] = 2
    for offset in range(20):
        queue_env.municipio_ticket(created_at=now - timedelta(minutes=offset + 1))
    expected = queue_env.municipio_ticket(
        created_at=now - timedelta(hours=2),
        assignee_id=queue_env.admin.id,
    )
    db.session.commit()

    with patch("services.crm_operational_queue._utc_now", return_value=now):
        response = queue_env.get(
            f"source_model=MunicipioTicket&assignee={queue_env.admin.id}&limit=1"
        )

    assert response.status_code == 200
    assert [item["queue_id"] for item in response.get_json()["items"]] == [
        f"MunicipioTicket:{expected.id}"
    ]


@pytest.mark.parametrize(
    ("query", "expected_post_filter"),
    (
        ("source_model=TenantTicket&sla=breached&limit=1", "sla:adapter_metadata"),
        (
            "source_model=TenantTicket&assignee=999999&limit=1",
            "assignee:TenantTicket.datos_extra",
        ),
    ),
)
def test_non_pushable_filter_budget_fails_without_partial_page(
    queue_env,
    query,
    expected_post_filter,
):
    now = datetime(2026, 8, 2, 19, 0, tzinfo=timezone.utc)
    queue_env.app.config["CRM_OPERATIONAL_QUEUE_MAX_SCANNED_ROWS"] = 2
    for offset in range(5):
        queue_env.tenant_ticket(created_at=now - timedelta(minutes=offset + 1))
    db.session.commit()

    with patch("services.crm_operational_queue._utc_now", return_value=now):
        response = queue_env.get(query)

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["contract_version"] == "shared.error.v1"
    assert payload["reason_code"] == "queue_scan_budget_exceeded"
    assert payload["retryable"] is True
    assert payload["details"]["partial_page_returned"] is False
    assert expected_post_filter in payload["details"]["post_query_filters"]
    assert "items" not in payload
    assert "page" not in payload
    assert response.headers["Cache-Control"] == "no-store, private"


def test_rate_limit_is_shared_per_tenant_actor_and_returns_retry_after(queue_env):
    queue_env.app.config["CRM_OPERATIONAL_QUEUE_RATE_LIMIT"] = "2 per 60 seconds"

    first = queue_env.get()
    second = queue_env.get()
    blocked = queue_env.get()

    assert first.status_code == 200
    assert second.status_code == 200
    assert blocked.status_code == 429
    assert blocked.get_json()["reason_code"] == "queue_rate_limited"
    assert blocked.get_json()["retryable"] is True
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.headers["X-RateLimit-Limit"] == "2"
    assert blocked.headers["X-RateLimit-Remaining"] == "0"

    other_actor = User(
        name="Other Queue Admin",
        email="other-queue-admin@test.local",
        password_hash="test-hash",
        rol="admin",
        tenant_slug=queue_env.tenant.slug,
        tenant_id=queue_env.tenant.id,
    )
    db.session.add(other_actor)
    db.session.commit()
    independent = queue_env.get(actor=other_actor)
    assert independent.status_code == 200


def test_rate_limit_storage_failure_is_explicit_and_fail_closed(queue_env):
    strategy = limiter.limiter
    with patch.object(strategy, "hit", side_effect=RuntimeError("storage unavailable")):
        response = queue_env.get()

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["reason_code"] == "queue_rate_limit_unavailable"
    assert payload["retryable"] is True
    assert payload["details"] == {
        "policy": "shared_flask_limiter",
        "fail_mode": "closed",
    }
    assert int(response.headers["Retry-After"]) >= 1
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert "items" not in payload


def test_rate_limit_headers_are_exposed_to_allowed_browser_origins(queue_env):
    response = queue_env.client.get(
        "/api/v2/inbox/operational-queue",
        headers={
            **queue_env.auth(),
            "Origin": "https://www.chatboc.ar",
        },
    )

    assert response.status_code == 200
    exposed = {
        value.strip().lower()
        for value in response.headers.get("Access-Control-Expose-Headers", "").split(",")
        if value.strip()
    }
    assert {
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-window",
        "x-ratelimit-reset-after",
        "retry-after",
    }.issubset(exposed)
