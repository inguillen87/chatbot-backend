from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from models_territorial_geocoding import (
    TerritorialGeocodingAttempt,
    TerritorialGeocodingJob,
    TerritorialGeocodingReview,
)
from services.territorial_geocoding_admin import proposal_digest


class TerritorialGeocodingAdminTestConfig(Config):
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


def _job(
    tenant_id: int,
    *,
    source_id: str,
    category: str,
    zone: str,
    status: str = "needs_review",
    with_proposal: bool = True,
) -> TerritorialGeocodingJob:
    suffix = str(source_id).rjust(8, "0")
    validation = {
        "auto_apply_eligible": False,
        "issues": ["provider_partial_match"],
        "jurisdiction_status": "within",
        "locality_match": True,
        "province_match": True,
        "country_match": True,
    }
    return TerritorialGeocodingJob(
        tenant_id=tenant_id,
        contract_version="operations.territorial_geocoding.v1",
        source_model="tenant_ticket",
        source_id=str(source_id),
        candidate_fingerprint=("a" * 56) + suffix,
        address_digest=("b" * 56) + suffix,
        jurisdiction_digest=("c" * 56) + suffix,
        status=status,
        reason_code=(
            "provider_partial_match" if with_proposal else "provider_execution_disabled"
        ),
        provider="google" if with_proposal else None,
        provider_place_id="place-sensitive-419" if with_proposal else None,
        proposed_lat=-33.0837 if with_proposal else None,
        proposed_lng=-68.4716 if with_proposal else None,
        location_type="GEOMETRIC_CENTER" if with_proposal else None,
        partial_match=True if with_proposal else None,
        validation_json=validation if with_proposal else {
            "auto_apply_eligible": False,
            "issues": ["provider_execution_disabled"],
        },
        result_json={
            "candidate": {
                "record_source": "tenant_ticket",
                "record_id": str(source_id),
                "category": category,
                "zone": zone,
            },
            "proposal": (
                {
                    "lat": -33.0837,
                    "lng": -68.4716,
                    "location_type": "GEOMETRIC_CENTER",
                    "partial_match": True,
                }
                if with_proposal
                else None
            ),
        },
        attempt_count=1 if with_proposal else 0,
        last_attempt_at=datetime.now(timezone.utc) if with_proposal else None,
    )


def _attempt(job: TerritorialGeocodingJob) -> TerritorialGeocodingAttempt:
    return TerritorialGeocodingAttempt(
        job_id=job.id,
        tenant_id=job.tenant_id,
        attempt_number=1,
        request_digest="d" * 64,
        action="resolve",
        idempotency_key_hash="f" * 64,
        provider="google",
        outcome_status="needs_review",
        reason_code="provider_partial_match",
        external_call_performed=True,
        write_performed=False,
        result_digest="e" * 64,
        result_json={
            "execution": {
                "action": "resolve",
                "idempotency_key_hash": "f" * 64,
            },
            "formatted_address": "San Martin 250, Junin, Mendoza",
            "direccion": "San Martin 250, Junin",
            "proposal": {
                "lat": -33.0837,
                "lng": -68.4716,
                "location_type": "GEOMETRIC_CENTER",
                "partial_match": True,
                "place_id": "place-sensitive-419",
            },
            "validation": {
                "auto_apply_eligible": False,
                "issues": ["provider_partial_match"],
                "jurisdiction_status": "within",
                "locality_match": True,
                "province_match": True,
                "country_match": True,
            },
        },
        created_at=datetime.now(timezone.utc),
    )


def _expected_proposal(job: TerritorialGeocodingJob) -> dict[str, object]:
    attempt = TerritorialGeocodingAttempt.query.filter_by(
        job_id=job.id,
        action="resolve",
    ).one()
    return {
        "expected_proposal_digest": proposal_digest(job),
        "expected_attempt_id": attempt.id,
        "expected_attempt_number": attempt.attempt_number,
    }


def _setup():
    app = create_app(TerritorialGeocodingAdminTestConfig)
    context = app.app_context()
    context.push()
    db.create_all()

    admin = User(
        name="Admin Junin",
        email="admin-geocoding@junin.test",
        password_hash="hash",
        rol="admin",
        tenant_slug="junin",
    )
    other_admin = User(
        name="Admin Other",
        email="admin-geocoding@other.test",
        password_hash="hash",
        rol="admin",
        tenant_slug="other",
    )
    db.session.add_all([admin, other_admin])
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
    other_admin.tenant_id = other_tenant.id
    employee = User(
        name="Employee Junin",
        email="employee-geocoding@junin.test",
        password_hash="hash",
        rol="empleado",
        tenant_id=tenant.id,
        tenant_slug="junin",
        es_empleado=True,
    )
    db.session.add(employee)
    db.session.flush()

    reviewable = _job(
        tenant.id,
        source_id="419",
        category="luminarias",
        zone="centro",
    )
    pending = _job(
        tenant.id,
        source_id="420",
        category="bacheo",
        zone="la colonia",
        status="pending",
        with_proposal=False,
    )
    foreign = _job(
        other_tenant.id,
        source_id="991",
        category="categoria-privada",
        zone="zona-privada",
    )
    db.session.add_all([reviewable, pending, foreign])
    db.session.flush()
    db.session.add(_attempt(reviewable))
    db.session.commit()
    return app, context, admin, employee, tenant, other_tenant, reviewable, pending, foreign


def _teardown(context) -> None:
    db.session.remove()
    db.drop_all()
    context.pop()


def test_queue_contract_is_redacted_tenant_scoped_and_get_is_read_only():
    app, context, admin, _employee, tenant, _other, reviewable, pending, foreign = _setup()
    try:
        client = app.test_client()
        before_reviews = TerritorialGeocodingReview.query.count()
        response = client.get(
            "/api/v2/analytics/operations/geocoding-queue",
            headers={**_auth(app, admin, tenant.slug), "X-Request-Id": "geo-list-1"},
        )

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        payload = response.get_json()
        assert payload["contract_version"] == "operations.territorial_geocoding_admin.v1"
        assert payload["request_id"] == "geo-list-1"
        assert payload["summary"]["total"] == 2
        assert payload["summary"]["by_status"] == {
            "needs_review": 1,
            "pending": 1,
        }
        assert {item["id"] for item in payload["items"]} == {
            reviewable.id,
            pending.id,
        }
        assert foreign.id not in {item["id"] for item in payload["items"]}
        selected = next(item for item in payload["items"] if item["id"] == reviewable.id)
        assert selected["ticket_id"] == "419"
        assert selected["source_model"] == "tenant_ticket"
        assert selected["category"] == "luminarias"
        assert selected["zone"] == "centro"
        assert selected["quality"]["state"] == "requires_human_review"
        assert selected["actions"]["review"]["coordinate_application_supported"] is False
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "San Martin 250" not in serialized
        assert "-33.0837" not in serialized
        assert payload["privacy"] == {
            "raw_address_exposed": False,
            "address_digest_exposed": False,
            "exact_coordinates_exposed": False,
            "aggregate_list_only": True,
        }
        assert TerritorialGeocodingReview.query.count() == before_reviews
    finally:
        _teardown(context)


def test_detail_and_attempts_expose_quality_not_raw_address_to_admin_only():
    app, context, admin, employee, tenant, _other, reviewable, _pending, foreign = _setup()
    try:
        client = app.test_client()
        detail = client.get(
            f"/api/v2/analytics/operations/geocoding-queue/{reviewable.id}",
            headers=_auth(app, admin, tenant.slug),
        )
        attempts = client.get(
            f"/api/v2/analytics/operations/geocoding-queue/{reviewable.id}/attempts",
            headers=_auth(app, admin, tenant.slug),
        )

        assert detail.status_code == 200
        assert attempts.status_code == 200
        detail_payload = detail.get_json()
        attempt_payload = attempts.get_json()
        assert detail_payload["selected"]["proposal"]["lat"] == -33.0837
        assert detail_payload["privacy"]["authorized_admin_detail"] is True
        assert detail_payload["write_policy"]["get_is_read_only"] is True
        assert attempt_payload["attempts"][0]["reason_code"] == "provider_partial_match"
        assert attempt_payload["attempts"][0]["external_call_performed"] is True
        assert attempt_payload["attempts"][0]["coordinate_write_performed"] is False
        assert attempt_payload["privacy"]["exact_coordinates_exposed"] is True
        assert (
            attempt_payload["privacy"]["exact_coordinates_access"]
            == "tenant_admin_only"
        )
        assert attempt_payload["privacy"]["provider_place_id_exposed"] is False
        assert "San Martin 250" not in json.dumps(detail_payload, ensure_ascii=False)
        assert "San Martin 250" not in json.dumps(attempt_payload, ensure_ascii=False)
        assert "formatted_address" not in json.dumps(attempt_payload)
        assert "place-sensitive-419" not in json.dumps(detail_payload)
        assert detail_payload["selected"]["proposal"]["provider_reference_present"] is True
        assert detail_payload["privacy"]["provider_place_id_exposed"] is False
        assert (
            detail_payload["privacy"]["exact_coordinates_classification"]
            == "restricted_operational"
        )

        employee_denied = client.get(
            "/api/v2/analytics/operations/geocoding-queue",
            headers=_auth(app, employee, tenant.slug),
        )
        assert employee_denied.status_code == 403
        assert employee_denied.get_json()["reason_code"] == "insufficient_permissions"

        foreign_hidden = client.get(
            f"/api/v2/analytics/operations/geocoding-queue/{foreign.id}",
            headers=_auth(app, admin, tenant.slug),
        )
        assert foreign_hidden.status_code == 404
        assert foreign_hidden.get_json()["reason_code"] == "geocoding_job_not_found"
    finally:
        _teardown(context)


def test_review_is_idempotent_human_only_and_never_applies_coordinates():
    app, context, admin, _employee, tenant, _other, reviewable, pending, _foreign = _setup()
    try:
        client = app.test_client()
        endpoint = f"/api/v2/analytics/operations/geocoding-queue/{reviewable.id}/review"
        headers = {
            **_auth(app, admin, tenant.slug),
            "Idempotency-Key": "review-job-0001",
        }
        body = {
            "decision": "approved",
            "reason_code": "verified_on_map",
            "apply_coordinates": False,
            **_expected_proposal(reviewable),
        }

        created = client.post(endpoint, headers=headers, json=body)
        replay = client.post(endpoint, headers=headers, json=body)

        assert created.status_code == 201
        assert replay.status_code == 200
        created_payload = created.get_json()
        replay_payload = replay.get_json()
        assert created_payload["idempotent_replay"] is False
        assert replay_payload["idempotent_replay"] is True
        assert created_payload["review"]["decision"] == "approved"
        assert created_payload["provider_call_performed"] is False
        assert created_payload["coordinate_write_performed"] is False
        assert TerritorialGeocodingReview.query.count() == 1
        db.session.refresh(reviewable)
        assert reviewable.status == "needs_review"
        assert reviewable.applied_at is None

        conflict = client.post(
            endpoint,
            headers=headers,
            json={
                "decision": "rejected",
                "reason_code": "incorrect_location",
                "apply_coordinates": False,
                **_expected_proposal(reviewable),
            },
        )
        assert conflict.status_code == 409
        assert conflict.get_json()["reason_code"] == "geocoding_review_idempotency_conflict"
        assert TerritorialGeocodingReview.query.count() == 1

        coordinate_write = client.post(
            endpoint,
            headers={
                **_auth(app, admin, tenant.slug),
                "Idempotency-Key": "review-job-0002",
            },
            json={
                "decision": "approved",
                "reason_code": "verified_on_map",
                "apply_coordinates": True,
                **_expected_proposal(reviewable),
            },
        )
        assert coordinate_write.status_code == 409
        assert (
            coordinate_write.get_json()["reason_code"]
            == "geocoding_review_coordinate_application_not_supported"
        )
        assert TerritorialGeocodingReview.query.count() == 1

        approve_without_proposal = client.post(
            f"/api/v2/analytics/operations/geocoding-queue/{pending.id}/review",
            headers={
                **_auth(app, admin, tenant.slug),
                "Idempotency-Key": "review-job-0003",
            },
            json={
                "decision": "approved",
                "reason_code": "verified_against_source",
                "expected_proposal_digest": "0" * 64,
                "expected_attempt_id": "missing-attempt",
                "expected_attempt_number": 1,
            },
        )
        assert approve_without_proposal.status_code == 409
        assert approve_without_proposal.get_json()["reason_code"] == "geocoding_proposal_missing"
    finally:
        _teardown(context)


def test_changed_proposal_marks_previous_review_stale_and_filters_it():
    app, context, admin, _employee, tenant, _other, reviewable, _pending, _foreign = _setup()
    try:
        client = app.test_client()
        reviewed = client.post(
            f"/api/v2/analytics/operations/geocoding-queue/{reviewable.id}/review",
            headers={
                **_auth(app, admin, tenant.slug),
                "Idempotency-Key": "review-job-stale-1",
            },
            json={
                "decision": "rejected",
                "reason_code": "ambiguous_candidate",
                **_expected_proposal(reviewable),
            },
        )
        assert reviewed.status_code == 201

        reviewable.proposed_lat = -33.0842
        reviewable.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        response = client.get(
            "/api/v2/analytics/operations/geocoding-queue?review_state=stale",
            headers=_auth(app, admin, tenant.slug),
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["summary"]["total"] == 1
        assert payload["summary"]["by_review_state"] == {"stale": 1}
        assert payload["items"][0]["id"] == reviewable.id
        assert payload["items"][0]["latest_review"]["proposal_current"] is False
    finally:
        _teardown(context)
