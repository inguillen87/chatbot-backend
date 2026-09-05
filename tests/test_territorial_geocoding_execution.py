from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace

import jwt
import pytest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, TenantTicket, User
from models_territorial_geocoding import TerritorialGeocodingAttempt, TerritorialGeocodingJob
from services.territorial_evidence import resolve_tenant_jurisdiction
from services.territorial_geocoding import (
    TerritorialGeocodingProviderReceipt,
    build_territorial_geocoding_candidate,
)
from services.territorial_geocoding_admin import (
    TerritorialGeocodingAdminError,
    proposal_digest,
    review_geocoding_job,
)
from services.territorial_geocoding_execution import (
    apply_territorial_geocoding_job,
    resolve_territorial_geocoding_job,
)


class ExecutionConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    TERRITORIAL_GEOCODING_PROVIDER_ENABLED = True
    TERRITORIAL_GEOCODING_WRITES_ENABLED = True
    MAPS_API_KEY = "test-only-not-a-real-credential"


def _provider(_address, context):
    assert context["max_results"] == 1
    assert context["provider_timeout_seconds"] == 3.0
    return {
        "place_id": "provider-place-419",
        "geometry": {
            "location": {"lat": -33.05, "lng": -68.47},
            "location_type": "ROOFTOP",
        },
        "address_components": [
            {"long_name": "Junín", "short_name": "Junín", "types": ["locality"]},
            {"long_name": "Mendoza", "short_name": "Mendoza", "types": ["administrative_area_level_1"]},
            {"long_name": "Argentina", "short_name": "AR", "types": ["country"]},
        ],
    }


def _official_jurisdiction():
    ring = [
        [-68.6, -33.2],
        [-68.3, -33.2],
        [-68.3, -32.9],
        [-68.6, -32.9],
        [-68.6, -33.2],
    ]
    return {
        "contract_version": "operations.tenant_jurisdiction.v1",
        "state": "configured",
        "enforced": True,
        "city": "Junín",
        "state_name": "Mendoza",
        "country": "AR",
        "locale": "es-AR",
        "region_hint": "ar",
        "bounds": {"west": -68.6, "south": -33.2, "east": -68.3, "north": -32.9},
        "containment_method": "point_in_polygon",
        "containment_verified": True,
        "boundary_authority": {"kind": "official", "snapshot_sha256": "a" * 64},
        "boundary_geometry": {"type": "Polygon", "coordinates": [ring]},
        "source": {"kind": "test_fixture"},
    }


def _setup(*, jurisdiction=None):
    app = create_app(ExecutionConfig)
    context = app.app_context()
    context.push()
    db.create_all()
    admin = User(
        name="Admin territorial",
        email="admin-territorial-execution@test.invalid",
        password_hash="hash",
        rol="admin",
        tenant_slug="junin",
    )
    db.session.add(admin)
    db.session.flush()
    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=admin.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    admin.tenant_id = tenant.id
    ticket = TenantTicket(
        tenant_id=tenant.id,
        descripcion="Alumbrado sin coordenadas",
        categoria="luminarias",
        origen="whatsapp",
        datos_extra={"address": "San Martín 250, Junín", "zone": "centro"},
    )
    db.session.add(ticket)
    db.session.flush()
    jurisdiction = jurisdiction or resolve_tenant_jurisdiction(tenant)
    candidate = build_territorial_geocoding_candidate(
        {
            "record_source": "tenant_ticket",
            "record_id": ticket.id,
            "address": "San Martín 250, Junín",
            "category": "luminarias",
            "zone": "centro",
        },
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        jurisdiction=jurisdiction,
    )
    assert candidate is not None
    job = TerritorialGeocodingJob(
        tenant_id=tenant.id,
        source_model="tenant_ticket",
        source_id=str(ticket.id),
        candidate_fingerprint=candidate.fingerprint,
        address_digest=candidate.address_digest,
        jurisdiction_digest=candidate.jurisdiction_digest,
        status="pending",
        reason_code="candidate_discovered",
        validation_json={"auto_apply_eligible": False, "issues": ["provider_not_requested"]},
        result_json={"candidate": candidate.audit_identity()},
    )
    db.session.add(job)
    db.session.commit()
    return app, context, admin, tenant, ticket, job


def _teardown(context):
    db.session.remove()
    db.drop_all()
    context.pop()


def _auth(app, user, tenant_slug):
    token = jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "tenant_slug": tenant_slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant_slug,
    }


def test_resolve_audits_bounded_wgs84_proposal_without_writing_ticket(monkeypatch):
    official = _official_jurisdiction()
    monkeypatch.setattr(
        "services.territorial_geocoding_execution.resolve_tenant_jurisdiction",
        lambda _tenant: official,
    )
    _app, context, admin, tenant, ticket, job = _setup(jurisdiction=official)
    try:
        result = resolve_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="resolve-job-419",
            geocoder=_provider,
            provider_timeout_seconds=3,
        )
        db.session.commit()

        db.session.refresh(ticket)
        db.session.refresh(job)
        assert ticket.latitud is None and ticket.longitud is None
        assert result["execution"] == {
            "provider_call_performed": True,
            "coordinate_write_performed": False,
            "coordinates_applied": False,
            "write_performed": False,
        }
        assert result["proposal"]["coordinate_reference"] == "WGS84"
        assert result["proposal"]["provenance"]["source_address_retained"] is False
        assert job.validation_json["auto_apply_eligible"] is True
        assert TerritorialGeocodingAttempt.query.one().write_performed is False
        assert "San Martín 250" not in json.dumps(result, ensure_ascii=False)
        assert "San Martín 250" not in json.dumps(job.result_json, ensure_ascii=False)
    finally:
        _teardown(context)


def test_apply_requires_current_review_and_commits_coordinates_with_applied_attempt(monkeypatch):
    official = _official_jurisdiction()
    monkeypatch.setattr(
        "services.territorial_geocoding_execution.resolve_tenant_jurisdiction",
        lambda _tenant: official,
    )
    _app, context, admin, tenant, ticket, job = _setup(jurisdiction=official)
    try:
        resolve_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="resolve-job-419",
            geocoder=_provider,
            provider_timeout_seconds=3,
        )
        proposal_attempt = TerritorialGeocodingAttempt.query.order_by(
            TerritorialGeocodingAttempt.attempt_number.desc()
        ).first()
        assert proposal_attempt is not None
        reviewed_digest = proposal_digest(job)
        review_geocoding_job(
            db.session,
            tenant_id=tenant.id,
            job_id=job.id,
            reviewer_user_id=admin.id,
            idempotency_key="approve-job-419",
            payload={
                "decision": "approved",
                "reason_code": "verified_on_map",
                "expected_proposal_digest": reviewed_digest,
                "expected_attempt_id": proposal_attempt.id,
                "expected_attempt_number": proposal_attempt.attempt_number,
            },
        )
        result = apply_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="apply-job-419",
            writer_authority_confirmed=True,
            writer_authority_reason="runtime_is_global_writer_owner",
            writer_authority_epoch=41,
            expected_proposal_digest=reviewed_digest,
            expected_attempt_id=proposal_attempt.id,
            expected_attempt_number=proposal_attempt.attempt_number,
        )
        db.session.commit()

        db.session.refresh(ticket)
        db.session.refresh(job)
        assert (ticket.latitud, ticket.longitud) == (-33.05, -68.47)
        assert ticket.datos_extra["territorial_coordinate_provenance"]["coordinate_reference"] == "WGS84"
        assert job.status == "applied"
        assert job.applied_at is not None
        assert proposal_digest(job) == reviewed_digest
        assert result["transition_metrics"] == {
            "contract_version": "operations.territorial_geocoding_transition_metrics.v1",
            "action": "apply",
            "from_status": "pending",
            "to_status": "applied",
        }
        attempts = TerritorialGeocodingAttempt.query.order_by(TerritorialGeocodingAttempt.attempt_number).all()
        assert [attempt.write_performed for attempt in attempts] == [False, True]

        replay = apply_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="apply-job-419",
            writer_authority_confirmed=True,
            writer_authority_reason="runtime_is_global_writer_owner",
            writer_authority_epoch=41,
            expected_proposal_digest=proposal_digest(job),
            expected_attempt_id=proposal_attempt.id,
            expected_attempt_number=proposal_attempt.attempt_number,
        )
        assert replay["idempotent_replay"] is True
        assert TerritorialGeocodingAttempt.query.count() == 2

        with pytest.raises(TerritorialGeocodingAdminError) as stale:
            apply_territorial_geocoding_job(
                db.session,
                tenant=tenant,
                job_id=job.id,
                actor_user_id=admin.id,
                idempotency_key="apply-job-419",
                writer_authority_confirmed=True,
                writer_authority_reason="runtime_is_global_writer_owner",
                writer_authority_epoch=41,
                expected_proposal_digest=reviewed_digest,
                expected_attempt_id="00000000-0000-0000-0000-000000000000",
                expected_attempt_number=proposal_attempt.attempt_number,
            )
        assert stale.value.reason_code == "geocoding_apply_expected_version_stale"
    finally:
        _teardown(context)


def test_resolve_idempotency_is_durable_across_newer_searches(monkeypatch):
    official = _official_jurisdiction()
    monkeypatch.setattr(
        "services.territorial_geocoding_execution.resolve_tenant_jurisdiction",
        lambda _tenant: official,
    )
    _app, context, admin, tenant, _ticket, job = _setup(jurisdiction=official)
    calls = []
    try:
        def provider(_address, provider_context):
            calls.append(provider_context["provider_timeout_seconds"])
            return TerritorialGeocodingProviderReceipt(
                payload=_provider(_address, provider_context),
                external_call_performed=True,
            )

        first = resolve_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="resolve-stable-key-1",
            geocoder=provider,
            provider_timeout_seconds=3,
        )
        second = resolve_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="resolve-explicit-new-2",
            geocoder=provider,
            provider_timeout_seconds=3,
        )
        replay = resolve_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="resolve-stable-key-1",
            geocoder=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("an old durable key must never invoke the provider")
            ),
            provider_timeout_seconds=3,
        )

        assert calls == [3.0, 3.0]
        assert first["proposal_version"]["attempt_number"] == 1
        assert second["proposal_version"]["attempt_number"] == 2
        assert replay["idempotent_replay"] is True
        assert replay["proposal_version"] == first["proposal_version"]
        assert TerritorialGeocodingAttempt.query.count() == 2
    finally:
        _teardown(context)


def test_provider_receipt_and_timeout_are_truthful_and_bounded():
    _app, context, admin, tenant, _ticket, job = _setup()
    observed = []
    try:
        for index, requested in enumerate((0, 100, float("nan"), float("inf"), "bad")):
            def unavailable_provider(_address, provider_context):
                observed.append(provider_context["provider_timeout_seconds"])
                return TerritorialGeocodingProviderReceipt(
                    payload=None,
                    external_call_performed=False,
                )

            result = resolve_territorial_geocoding_job(
                db.session,
                tenant=tenant,
                job_id=job.id,
                actor_user_id=admin.id,
                idempotency_key=f"resolve-timeout-{index}",
                geocoder=unavailable_provider,
                provider_timeout_seconds=requested,
            )
            assert result["execution"]["provider_call_performed"] is False

        assert observed == [1.0, 10.0, 5.0, 5.0, 5.0]
        assert all(
            attempt.external_call_performed is False
            for attempt in TerritorialGeocodingAttempt.query.all()
        )
    finally:
        _teardown(context)


@pytest.mark.parametrize(
    ("source_mutation", "expected_reason"),
    [
        ("coordinates", "geocoding_source_coordinates_already_present"),
        ("provenance", "geocoding_source_provenance_conflict"),
    ],
)
def test_apply_locks_and_rejects_changed_source_state(
    monkeypatch, source_mutation, expected_reason
):
    official = _official_jurisdiction()
    monkeypatch.setattr(
        "services.territorial_geocoding_execution.resolve_tenant_jurisdiction",
        lambda _tenant: official,
    )
    _app, context, admin, tenant, ticket, job = _setup(jurisdiction=official)
    try:
        resolved = resolve_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="resolve-source-guard",
            geocoder=_provider,
            provider_timeout_seconds=3,
        )
        version = resolved["proposal_version"]
        digest = resolved["proposal_digest"]
        review_geocoding_job(
            db.session,
            tenant_id=tenant.id,
            job_id=job.id,
            reviewer_user_id=admin.id,
            idempotency_key="approve-source-guard",
            payload={
                "decision": "approved",
                "reason_code": "verified_on_map",
                "expected_proposal_digest": digest,
                "expected_attempt_id": version["attempt_id"],
                "expected_attempt_number": version["attempt_number"],
            },
        )
        if source_mutation == "coordinates":
            ticket.latitud = -33.01
        else:
            ticket.datos_extra = {
                **ticket.datos_extra,
                "territorial_coordinate_provenance": {"source": "external"},
            }
        db.session.flush()

        with pytest.raises(TerritorialGeocodingAdminError) as blocked:
            apply_territorial_geocoding_job(
                db.session,
                tenant=tenant,
                job_id=job.id,
                actor_user_id=admin.id,
                idempotency_key=f"apply-source-{source_mutation}",
                writer_authority_confirmed=True,
                writer_authority_reason="runtime_is_global_writer_owner",
                writer_authority_epoch=41,
                expected_proposal_digest=digest,
                expected_attempt_id=version["attempt_id"],
                expected_attempt_number=version["attempt_number"],
            )
        assert blocked.value.reason_code == expected_reason
        assert TerritorialGeocodingAttempt.query.count() == 1
    finally:
        _teardown(context)


def test_http_resolve_and_apply_are_explicit_and_fail_closed_before_provider_or_writer(monkeypatch):
    app, context, admin, tenant, ticket, job = _setup()
    try:
        calls = {"provider": 0}

        def provider(address, context):
            calls["provider"] += 1
            return _provider(address, context)

        monkeypatch.setattr("routes.v2.analytics.google_geocoding_adapter", provider)
        client = app.test_client()
        headers = {**_auth(app, admin, tenant.slug), "Idempotency-Key": "resolve-http-419"}
        resolved = client.post(
            f"/api/v2/analytics/operations/geocoding-queue/{job.id}/resolve",
            headers=headers,
            json={},
        )
        assert resolved.status_code == 201
        assert calls["provider"] == 1
        assert ticket.latitud is None

        unconfirmed = client.post(
            f"/api/v2/analytics/operations/geocoding-queue/{job.id}/apply",
            headers={**_auth(app, admin, tenant.slug), "Idempotency-Key": "apply-http-419"},
            json={},
        )
        assert unconfirmed.status_code == 400
        assert unconfirmed.get_json()["reason_code"] == "geocoding_apply_confirmation_required"
        assert ticket.latitud is None
    finally:
        _teardown(context)


def test_http_apply_rolls_back_when_authority_epoch_changes_before_commit(monkeypatch):
    official = _official_jurisdiction()
    monkeypatch.setattr(
        "services.territorial_geocoding_execution.resolve_tenant_jurisdiction",
        lambda _tenant: official,
    )
    app, context, admin, tenant, ticket, job = _setup(jurisdiction=official)
    try:
        resolved = resolve_territorial_geocoding_job(
            db.session,
            tenant=tenant,
            job_id=job.id,
            actor_user_id=admin.id,
            idempotency_key="resolve-epoch-guard",
            geocoder=_provider,
            provider_timeout_seconds=3,
        )
        version = resolved["proposal_version"]
        review_geocoding_job(
            db.session,
            tenant_id=tenant.id,
            job_id=job.id,
            reviewer_user_id=admin.id,
            idempotency_key="approve-epoch-guard",
            payload={
                "decision": "approved",
                "reason_code": "verified_on_map",
                "expected_proposal_digest": resolved["proposal_digest"],
                "expected_attempt_id": version["attempt_id"],
                "expected_attempt_number": version["attempt_number"],
            },
        )
        db.session.commit()

        class ChangedEpochLease:
            decision = SimpleNamespace(
                enabled=True,
                allowed=True,
                epoch=41,
                reason_code="runtime_is_global_writer_owner",
            )

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def revalidate(self, _config):
                return SimpleNamespace(
                    enabled=True,
                    allowed=True,
                    epoch=42,
                    reason_code="runtime_is_global_writer_owner",
                )

        monkeypatch.setattr(
            "routes.v2.analytics.global_writer_authority_lease",
            lambda _config: ChangedEpochLease(),
        )
        response = app.test_client().post(
            f"/api/v2/analytics/operations/geocoding-queue/{job.id}/apply",
            headers={
                **_auth(app, admin, tenant.slug),
                "Idempotency-Key": "apply-epoch-guard",
            },
            json={
                "confirmed": True,
                "expected_proposal_digest": resolved["proposal_digest"],
                "expected_attempt_id": version["attempt_id"],
                "expected_attempt_number": version["attempt_number"],
            },
        )

        assert response.status_code == 503
        assert response.get_json()["reason_code"] == "geocoding_writer_authority_lease_lost"
        db.session.expire_all()
        persisted_ticket = db.session.get(TenantTicket, ticket.id)
        assert persisted_ticket.latitud is None
        assert persisted_ticket.longitud is None
        assert TerritorialGeocodingAttempt.query.count() == 1
    finally:
        _teardown(context)
