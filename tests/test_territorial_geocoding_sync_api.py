from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, TenantTicket, User
from models_territorial_geocoding import (
    TerritorialGeocodingAttempt,
    TerritorialGeocodingJob,
    TerritorialGeocodingSyncReceipt,
)


class TerritorialGeocodingSyncTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


def _auth(app, user: User, tenant_slug: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "tenant_slug": user.tenant_slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant_slug,
    }


def _setup():
    app = create_app(TerritorialGeocodingSyncTestConfig)
    context = app.app_context()
    context.push()
    db.create_all()

    admin = User(
        name="Admin Junin",
        email="admin-sync@junin.test",
        password_hash="hash",
        rol="admin",
        tenant_slug="junin",
    )
    second_admin = User(
        name="Second Admin Junin",
        email="admin-sync-2@junin.test",
        password_hash="hash",
        rol="admin",
        tenant_slug="junin",
    )
    other_admin = User(
        name="Admin Other",
        email="admin-sync@other.test",
        password_hash="hash",
        rol="admin",
        tenant_slug="other",
    )
    db.session.add_all([admin, second_admin, other_admin])
    db.session.flush()
    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junin",
        tipo="municipio",
        municipio_id=admin.id,
        plan="full",
    )
    other_tenant = TenantProfile(
        slug="other",
        nombre="Otro Municipio",
        tipo="municipio",
        municipio_id=other_admin.id,
        plan="full",
    )
    db.session.add_all([tenant, other_tenant])
    db.session.flush()
    admin.tenant_id = tenant.id
    second_admin.tenant_id = tenant.id
    other_admin.tenant_id = other_tenant.id
    employee = User(
        name="Employee Junin",
        email="employee-sync@junin.test",
        password_hash="hash",
        rol="empleado",
        tenant_id=tenant.id,
        tenant_slug="junin",
        es_empleado=True,
    )
    db.session.add(employee)
    db.session.flush()

    address = "Av. San Martin 123, Junin"
    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=admin.id,
        categoria="luminarias",
        descripcion="Luminaria apagada informada por un vecino",
        estado="nuevo",
        origen="whatsapp",
        latitud=None,
        longitud=None,
        datos_extra={
            "address": address,
            "zone": "Centro",
            "channel": "whatsapp",
        },
    )
    foreign_ticket = TenantTicket(
        tenant_id=other_tenant.id,
        user_id=other_admin.id,
        categoria="categoria-privada",
        descripcion="Registro territorial de otro tenant",
        estado="nuevo",
        origen="web",
        latitud=None,
        longitud=None,
        datos_extra={
            "address": "Domicilio Secreto 999, Otra Ciudad",
            "zone": "Zona privada",
        },
    )
    db.session.add_all([ticket, foreign_ticket])
    db.session.commit()
    return (
        app,
        context,
        admin,
        second_admin,
        employee,
        tenant,
        other_tenant,
        ticket,
        address,
    )


def _teardown(context) -> None:
    db.session.remove()
    db.drop_all()
    context.pop()


def _sync(client, app, user, tenant, key):
    return client.post(
        "/api/v2/analytics/operations/geocoding-queue/sync",
        headers={**_auth(app, user, tenant.slug), "Idempotency-Key": key},
        json={},
    )


def test_sync_materializes_same_heatmap_candidate_without_address_or_coordinates():
    app, context, admin, _second, _employee, tenant, _other, ticket, address = _setup()
    try:
        client = app.test_client()
        heatmap = client.get(
            "/api/v2/analytics/operations/heatmap?range=all&source=tickets",
            headers=_auth(app, admin, tenant.slug),
        )
        assert heatmap.status_code == 200
        heatmap_candidate = heatmap.get_json()["geocoding"]["candidates"][0]

        response = _sync(client, app, admin, tenant, "sync-junin-0001")
        assert response.status_code == 201
        assert response.headers["Cache-Control"] == "no-store"
        payload = response.get_json()
        assert payload["contract_version"] == "operations.territorial_geocoding_sync.v1"
        assert payload["summary"] == {
            "discovered": 1,
            "created": 1,
            "existing": 0,
            "stale": 0,
            "refreshed": 0,
            "hidden": 0,
        }
        assert payload["provider_call_performed"] is False
        assert payload["coordinate_write_performed"] is False
        assert payload["execution"] == {
            "provider_call_performed": False,
            "coordinate_write_performed": False,
        }

        job = TerritorialGeocodingJob.query.one()
        assert job.tenant_id == tenant.id
        assert job.source_model == "tenant_ticket"
        assert job.source_id == str(ticket.id)
        assert job.status == "pending"
        assert job.reason_code == "candidate_discovered"
        assert job.candidate_fingerprint == heatmap_candidate["queue_identity"]["candidate_fingerprint"]
        assert job.provider is None
        assert job.proposed_lat is None
        assert job.proposed_lng is None
        assert job.attempt_count == 0
        assert TerritorialGeocodingAttempt.query.count() == 0
        safe_candidate = job.result_json["candidate"]
        assert safe_candidate["category"] == "luminarias"
        assert safe_candidate["zone"] == "centro"
        assert address not in json.dumps(job.result_json, ensure_ascii=False)
        assert address not in json.dumps(payload, ensure_ascii=False)
        assert "Domicilio Secreto" not in json.dumps(job.result_json, ensure_ascii=False)
        assert ticket.latitud is None and ticket.longitud is None
    finally:
        _teardown(context)


def test_preview_exposes_current_redacted_candidates_without_provider_or_writes():
    app, context, admin, _second, _employee, tenant, _other, ticket, address = _setup()
    try:
        client = app.test_client()
        with patch("services.location_service.geocode_address") as geocode:
            response = client.get(
                "/api/v2/analytics/operations/geocoding-queue/preview",
                headers=_auth(app, admin, tenant.slug),
            )

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        payload = response.get_json()
        assert payload["contract_version"] == "operations.territorial_geocoding_preview.v1"
        assert payload["summary"] == {
            "discovered": 1,
            "unique": 1,
            "matching": 1,
            "hidden": 0,
            "by_source_model": {"tenant_ticket": 1},
            "by_category": {"luminarias": 1},
            "by_zone": {"centro": 1},
        }
        assert payload["execution"] == {
            "read_only": True,
            "database_write_performed": False,
            "provider_call_performed": False,
            "coordinate_write_performed": False,
        }
        assert payload["population"] == {
            "source_family": "tickets",
            "period": "all_available",
            "eligibility": "persisted_address_without_coordinates",
            "filters_apply_to_unique_candidates": True,
        }
        assert payload["privacy"] == {
            "raw_address_exposed": False,
            "address_digest_exposed": False,
            "candidate_fingerprint_exposed": False,
            "exact_coordinates_exposed": False,
            "tenant_scoped": True,
        }
        item = payload["items"][0]
        assert item["ticket_id"] == str(ticket.id)
        assert item["source_model"] == "tenant_ticket"
        assert item["category"] == "luminarias"
        assert item["zone"] == "centro"
        assert item["state"] == "awaiting_materialization"
        assert item["provenance"]["address_evidence"] == "persisted_on_source"
        assert item["provenance"]["coordinate_evidence"] == "missing_on_source"
        assert item["actions"]["inspect_source"]["mutates_state"] is False
        assert item["actions"]["provider_lookup"]["enabled"] is False
        assert item["actions"]["review"]["enabled"] is False
        assert item["actions"]["coordinate_write"]["enabled"] is False
        serialized = json.dumps(payload, ensure_ascii=False)
        assert address not in serialized
        assert "address_digest" not in serialized.replace("address_digest_exposed", "")
        assert "candidate_fingerprint" not in serialized.replace(
            "candidate_fingerprint_exposed", ""
        )
        assert geocode.call_count == 0
        assert TerritorialGeocodingJob.query.count() == 0
        assert TerritorialGeocodingAttempt.query.count() == 0
        assert TerritorialGeocodingSyncReceipt.query.count() == 0
        assert ticket.latitud is None and ticket.longitud is None
    finally:
        _teardown(context)


def test_preview_filters_exact_population_and_rejects_unsafe_or_unauthorized_access():
    app, context, admin, _second, employee, tenant, other, ticket, _address = _setup()
    try:
        client = app.test_client()
        filtered = client.get(
            "/api/v2/analytics/operations/geocoding-queue/preview"
            "?source_model=tenant_ticket&category=LUMINARIAS&zone=Centro"
            f"&ticket_id={ticket.id}&page=1&per_page=1",
            headers=_auth(app, admin, tenant.slug),
        )
        assert filtered.status_code == 200
        payload = filtered.get_json()
        assert payload["summary"]["matching"] == 1
        assert payload["pagination"] == {
            "page": 1,
            "per_page": 1,
            "total": 1,
            "has_next": False,
        }

        no_match = client.get(
            "/api/v2/analytics/operations/geocoding-queue/preview?category=bacheo",
            headers=_auth(app, admin, tenant.slug),
        )
        assert no_match.status_code == 200
        assert no_match.get_json()["summary"]["matching"] == 0
        assert no_match.get_json()["items"] == []

        unsafe = client.get(
            "/api/v2/analytics/operations/geocoding-queue/preview"
            "?source_model=tenant ticket",
            headers=_auth(app, admin, tenant.slug),
        )
        assert unsafe.status_code == 400
        assert unsafe.get_json()["reason_code"] == "geocoding_preview_filter_invalid"

        employee_denied = client.get(
            "/api/v2/analytics/operations/geocoding-queue/preview",
            headers=_auth(app, employee, tenant.slug),
        )
        assert employee_denied.status_code == 403

        cross_tenant = client.get(
            "/api/v2/analytics/operations/geocoding-queue/preview",
            headers=_auth(app, admin, other.slug),
        )
        assert cross_tenant.status_code == 403
        assert TerritorialGeocodingJob.query.count() == 0
        assert TerritorialGeocodingAttempt.query.count() == 0
        assert TerritorialGeocodingSyncReceipt.query.count() == 0
    finally:
        _teardown(context)


def test_sync_exact_replay_is_stable_and_new_key_refreshes_stale_pending_identity():
    app, context, admin, _second, _employee, tenant, _other, ticket, _address = _setup()
    try:
        client = app.test_client()
        first = _sync(client, app, admin, tenant, "sync-junin-replay-1")
        assert first.status_code == 201
        first_payload = first.get_json()
        first_job = TerritorialGeocodingJob.query.one()
        first_job_id = first_job.id
        first_fingerprint = first_job.candidate_fingerprint

        replay = _sync(client, app, admin, tenant, "sync-junin-replay-1")
        assert replay.status_code == 200
        replay_payload = replay.get_json()
        assert replay_payload["summary"] == first_payload["summary"]
        assert replay_payload["idempotent_replay"] is True
        assert TerritorialGeocodingJob.query.count() == 1
        assert TerritorialGeocodingSyncReceipt.query.count() == 1

        updated_extra = dict(ticket.datos_extra or {})
        updated_extra["address"] = "Calle Rivadavia 456, Junin"
        updated_extra["zone"] = "Villa Talleres"
        ticket.datos_extra = updated_extra
        db.session.commit()

        refreshed = _sync(client, app, admin, tenant, "sync-junin-replay-2")
        assert refreshed.status_code == 201
        summary = refreshed.get_json()["summary"]
        assert summary == {
            "discovered": 1,
            "created": 0,
            "existing": 1,
            "stale": 1,
            "refreshed": 1,
            "hidden": 0,
        }
        job = TerritorialGeocodingJob.query.one()
        assert job.id == first_job_id
        assert job.candidate_fingerprint != first_fingerprint
        assert job.result_json["candidate"]["zone"] == "villa talleres"
        assert "Calle Rivadavia" not in json.dumps(job.result_json, ensure_ascii=False)
        assert TerritorialGeocodingAttempt.query.count() == 0
        assert ticket.latitud is None and ticket.longitud is None
    finally:
        _teardown(context)


def test_sync_fails_closed_for_employee_cross_tenant_and_idempotency_actor_conflict():
    app, context, admin, second, employee, tenant, other, _ticket, _address = _setup()
    try:
        client = app.test_client()
        employee_response = _sync(
            client, app, employee, tenant, "sync-junin-employee-1"
        )
        assert employee_response.status_code == 403
        assert TerritorialGeocodingJob.query.count() == 0

        cross_tenant = client.post(
            "/api/v2/analytics/operations/geocoding-queue/sync",
            headers={
                **_auth(app, admin, other.slug),
                "Idempotency-Key": "sync-cross-tenant-1",
            },
            json={},
        )
        assert cross_tenant.status_code == 403
        assert TerritorialGeocodingJob.query.count() == 0

        assert _sync(client, app, admin, tenant, "sync-shared-actor-1").status_code == 201
        conflict = _sync(client, app, second, tenant, "sync-shared-actor-1")
        assert conflict.status_code == 409
        assert conflict.get_json()["reason_code"] == "idempotency_key_conflict"
        assert TerritorialGeocodingJob.query.count() == 1
    finally:
        _teardown(context)


def test_sync_get_is_read_only_and_invalid_mutations_create_nothing():
    app, context, admin, _second, _employee, tenant, _other, _ticket, _address = _setup()
    try:
        client = app.test_client()
        get_response = client.get(
            "/api/v2/analytics/operations/geocoding-queue/sync",
            headers=_auth(app, admin, tenant.slug),
        )
        assert get_response.status_code == 405
        assert get_response.get_json()["reason_code"] == "sync_requires_post"
        assert TerritorialGeocodingJob.query.count() == 0

        missing_key = client.post(
            "/api/v2/analytics/operations/geocoding-queue/sync",
            headers=_auth(app, admin, tenant.slug),
            json={},
        )
        assert missing_key.status_code == 400
        assert missing_key.get_json()["reason_code"] == "invalid_idempotency_key"

        unsupported_body = client.post(
            "/api/v2/analytics/operations/geocoding-queue/sync",
            headers={
                **_auth(app, admin, tenant.slug),
                "Idempotency-Key": "sync-junin-body-1",
            },
            json={"address": "should never be accepted"},
        )
        assert unsupported_body.status_code == 400
        assert unsupported_body.get_json()["reason_code"] == "unsupported_sync_payload"
        assert TerritorialGeocodingJob.query.count() == 0
        assert TerritorialGeocodingSyncReceipt.query.count() == 0

        tenant.configuracion = {"demo_mode": True}
        db.session.commit()
        feature_locked = _sync(
            client, app, admin, tenant, "sync-junin-feature-1"
        )
        assert feature_locked.status_code == 403
        assert feature_locked.get_json()["error"] == "plan_required"
        assert TerritorialGeocodingJob.query.count() == 0
        assert TerritorialGeocodingSyncReceipt.query.count() == 0
    finally:
        _teardown(context)
