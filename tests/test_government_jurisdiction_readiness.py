from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from extensions import db
from models import AuditEvent, User
from services.government_jurisdiction_readiness import submit_jurisdiction_evidence
from services.survey_jurisdiction import tenant_verified_jurisdiction
from tests.test_tenant_blueprint_provisioning import _headers, blueprint_context
from utils.auth_helpers import auth_session_version


EVIDENCE_SHA256 = "a" * 64
SECOND_EVIDENCE_SHA256 = "b" * 64


def _url(tenant_slug: str, suffix: str = "") -> str:
    base = f"/api/v2/tenants/{tenant_slug}/government-readiness/jurisdiction"
    return f"{base}/{suffix}" if suffix else base


def _evidence_payload(*, digest: str = EVIDENCE_SHA256) -> dict[str, str]:
    return {
        "jurisdiction_ref": "official:ar:government-a:v1",
        "evidence_ref": "evidence:r2:government-a:boundary-v1",
        "evidence_sha256": digest,
    }


def _submit(
    ctx,
    *,
    tenant_slug: str = "government-a",
    user_key: str = "admin_a",
    idempotency_key: str = "jurisdiction-submit-001",
    payload: dict[str, str] | None = None,
):
    return ctx["client"].post(
        _url(tenant_slug, "evidence"),
        json=payload or _evidence_payload(),
        headers=_headers(ctx, ctx[user_key], idempotency_key=idempotency_key),
    )


def _review(
    ctx,
    submission_sha256: str,
    *,
    decision: str = "verify",
    idempotency_key: str = "jurisdiction-review-001",
    user_key: str = "superadmin",
    reason_code: str | None = None,
    headers: dict[str, str] | None = None,
    tenant_slug: str = "government-a",
):
    payload = {
        "decision": decision,
        "expected_submission_sha256": submission_sha256,
    }
    if decision == "reject":
        payload["reason_code"] = reason_code or "boundary_source_not_official"
    return ctx["client"].post(
        _url(tenant_slug, "review"),
        json=payload,
        headers=headers
        or _headers(
            ctx,
            ctx[user_key],
            idempotency_key=idempotency_key,
        ),
    )


def _platform_operator_headers(ctx, operator: User, *, idempotency_key: str) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    sid = f"platform-reviewer-{operator.id}"
    token = jwt.encode(
        {
            "user_id": int(operator.id),
            "rol": operator.rol,
            "exp": now + timedelta(hours=1),
            "iat": now,
            "auth_provider": "clerk",
            "session_kind": "clerk",
            "sid": sid,
            "clerk_sid": sid,
            "jti": f"jurisdiction-reviewer-jti-{operator.id}",
            "sv": auth_session_version(operator),
            "auth_assurance": {
                "version": "auth.assurance.v1",
                "source": "clerk_v2_fva",
                "status": "verified",
                "first_factor_verified_at": now_epoch,
                "second_factor_verified_at": now_epoch,
            },
        },
        ctx["app"].config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": idempotency_key,
    }


def _submission_sha(response) -> str:
    return response.get_json()["readiness"]["jurisdiction"]["evidence"][
        "submission_sha256"
    ]


def test_read_model_is_fail_closed_tenant_scoped_and_has_clear_next_action(
    blueprint_context,
):
    ctx = blueprint_context

    response = ctx["client"].get(
        _url("government-a"),
        headers=_headers(ctx, ctx["admin_a"]),
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "government.jurisdiction.readiness.v1"
    assert payload["tenant"] == {
        "id": ctx["tenant_a"].id,
        "slug": "government-a",
        "type": "municipio",
    }
    assert payload["state"] == "unverified"
    assert payload["ready_to_publish"] is False
    assert payload["next_action"] == "submit_jurisdiction_evidence"
    assert payload["publication_guard"] == {
        "government_evidence_required": True,
        "allowed_to_publish": False,
        "reason_code": "survey_tenant_jurisdiction_unverified",
        "guard_preserved": True,
    }

    cross_tenant = ctx["client"].get(
        _url("government-b"),
        headers=_headers(ctx, ctx["admin_a"]),
    )
    assert cross_tenant.status_code == 403
    assert cross_tenant.get_json()["reason_code"] == "tenant_control_plane_forbidden"

    employee = ctx["client"].get(
        _url("government-a"),
        headers=_headers(ctx, ctx["employee"]),
    )
    assert employee.status_code == 403

    ambiguous = ctx["client"].get(
        f"{_url('government-a')}?tenant_slug=government-b",
        headers=_headers(ctx, ctx["admin_a"]),
    )
    assert ambiguous.status_code == 400
    assert ambiguous.get_json()["reason_code"] == "tenant_selector_conflict"


def test_tenant_admin_submission_is_unverified_audited_and_idempotent(
    blueprint_context,
):
    ctx = blueprint_context
    response = _submit(ctx)

    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert payload["contract_version"] == "government.jurisdiction.evidence_submission.v1"
    assert payload["replayed"] is False
    assert payload["write_performed"] is True
    assert payload["verification_granted"] is False
    assert payload["readiness"]["state"] == "evidence_submitted"
    assert payload["readiness"]["next_action"] == "await_platform_jurisdiction_review"
    assert payload["readiness"]["ready_to_publish"] is False
    assert response.headers["X-Idempotency-Status"] == "created"

    db.session.refresh(ctx["tenant_a"])
    assert ctx["tenant_a"].jurisdiction_status == "unverified"
    assert ctx["tenant_a"].jurisdiction_verified_by_user_id is None
    assert ctx["tenant_a"].jurisdiction_verified_at is None
    events = AuditEvent.query.filter_by(
        tenant_id=ctx["tenant_a"].id,
        event_type="tenant_jurisdiction_evidence_submitted",
    ).all()
    assert len(events) == 1
    audit_text = str(events[0].details)
    assert _evidence_payload()["evidence_ref"] not in audit_text
    assert _evidence_payload()["jurisdiction_ref"] not in audit_text
    assert "jurisdiction-submit-001" not in audit_text
    assert events[0].details["raw_evidence_content_persisted"] is False
    assert events[0].details["credentials_persisted"] is False

    second_admin = User(
        name="Tenant Admin A2",
        email="tenant-admin-a2@example.test",
        rol="admin",
        tenant_slug="government-a",
        tenant_id=ctx["tenant_a"].id,
    )
    second_admin.set_password("not-used")
    db.session.add(second_admin)
    db.session.commit()
    principal_conflict = ctx["client"].post(
        _url("government-a", "evidence"),
        json=_evidence_payload(),
        headers=_headers(
            ctx,
            second_admin,
            idempotency_key="jurisdiction-submit-001",
        ),
    )
    assert principal_conflict.status_code == 409
    assert principal_conflict.get_json()["reason_code"] == (
        "jurisdiction_idempotency_conflict"
    )

    replay = _submit(ctx)
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["write_performed"] is False
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert AuditEvent.query.filter_by(
        tenant_id=ctx["tenant_a"].id,
        event_type="tenant_jurisdiction_evidence_submitted",
    ).count() == 1

    conflict = _submit(ctx, payload=_evidence_payload(digest=SECOND_EVIDENCE_SHA256))
    assert conflict.status_code == 409
    assert conflict.get_json()["reason_code"] == "jurisdiction_idempotency_conflict"


def test_submission_blocks_cross_tenant_and_platform_self_submission(
    blueprint_context,
):
    ctx = blueprint_context
    cross_tenant = _submit(ctx, tenant_slug="government-b")
    assert cross_tenant.status_code == 403
    assert cross_tenant.get_json()["reason_code"] == "jurisdiction_tenant_admin_required"
    assert ctx["tenant_b"].jurisdiction_evidence_ref is None

    platform_submission = _submit(ctx, user_key="superadmin")
    assert platform_submission.status_code == 403
    assert platform_submission.get_json()["reason_code"] == "jurisdiction_tenant_admin_required"
    assert AuditEvent.query.count() == 0


def test_review_requires_existing_exact_evidence_and_recent_mfa(blueprint_context):
    ctx = blueprint_context
    missing = _review(ctx, "c" * 64)
    assert missing.status_code == 409
    assert missing.get_json()["reason_code"] == "jurisdiction_evidence_not_submitted"

    submitted = _submit(ctx)
    submission_sha = _submission_sha(submitted)
    no_mfa = _review(
        ctx,
        submission_sha,
        idempotency_key="jurisdiction-review-no-mfa",
        headers=_headers(
            ctx,
            ctx["superadmin"],
            idempotency_key="jurisdiction-review-no-mfa",
            assurance_state="missing",
        ),
    )
    assert no_mfa.status_code == 403
    assert no_mfa.get_json()["reason_code"] == "step_up_required"
    assert ctx["tenant_a"].jurisdiction_status == "unverified"

    stale = _review(
        ctx,
        "d" * 64,
        idempotency_key="jurisdiction-review-stale",
    )
    assert stale.status_code == 409
    assert stale.get_json()["reason_code"] == "jurisdiction_submission_hash_conflict"


def test_superadmin_verification_is_atomic_readable_and_idempotent(
    blueprint_context,
):
    ctx = blueprint_context
    submitted = _submit(ctx)
    submission_sha = _submission_sha(submitted)

    verified = _review(ctx, submission_sha)
    assert verified.status_code == 201, verified.get_json()
    payload = verified.get_json()
    readiness = payload["readiness"]
    assert payload["decision"] == "verify"
    assert payload["replayed"] is False
    assert readiness["state"] == "verified"
    assert readiness["ready_to_publish"] is True
    assert readiness["next_action"] == "review_survey_content"
    assert readiness["publication_guard"]["allowed_to_publish"] is True
    assert readiness["publication_guard"]["reason_code"] is None
    assert readiness["workflow"]["separation_of_duties_enforced"] is True
    assert readiness["workflow"]["review_matches_submission"] is True

    db.session.refresh(ctx["tenant_a"])
    assert tenant_verified_jurisdiction(ctx["tenant_a"]) == _evidence_payload()[
        "jurisdiction_ref"
    ]
    assert ctx["tenant_a"].jurisdiction_verified_by_user_id == ctx["superadmin"].id
    assert ctx["tenant_a"].jurisdiction_verified_at is not None

    replay = _review(ctx, submission_sha)
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["write_performed"] is False
    assert AuditEvent.query.filter_by(
        tenant_id=ctx["tenant_a"].id,
        event_type="tenant_jurisdiction_verified",
    ).count() == 1

    read = ctx["client"].get(
        _url("government-a"),
        headers=_headers(ctx, ctx["admin_a"]),
    )
    assert read.status_code == 200
    assert read.get_json()["jurisdiction"]["verified_by_user_id"] == ctx["superadmin"].id
    assert read.get_json()["jurisdiction"]["verified_at"]


def test_allowlisted_unscoped_platform_operator_can_review_but_tenant_operator_cannot(
    blueprint_context,
):
    ctx = blueprint_context
    platform_operator = User(
        name="Platform Jurisdiction Reviewer",
        email="jurisdiction-reviewer@example.test",
        rol="empleado",
        es_empleado=True,
    )
    platform_operator.set_password("not-used")
    db.session.add(platform_operator)
    db.session.commit()
    ctx["app"].config["JURISDICTION_PLATFORM_REVIEWER_EMAILS"] = platform_operator.email

    submitted = _submit(ctx)
    submission_sha = _submission_sha(submitted)
    tenant_operator_denied = _review(
        ctx,
        submission_sha,
        idempotency_key="tenant-operator-review-denied",
        headers=_headers(
            ctx,
            ctx["employee"],
            idempotency_key="tenant-operator-review-denied",
        ),
    )
    assert tenant_operator_denied.status_code == 403
    assert tenant_operator_denied.get_json()["reason_code"] == (
        "jurisdiction_platform_reviewer_required"
    )

    platform_headers = _platform_operator_headers(
        ctx,
        platform_operator,
        idempotency_key="platform-operator-review-001",
    )
    reviewed = _review(
        ctx,
        submission_sha,
        headers=platform_headers,
    )
    assert reviewed.status_code == 201, reviewed.get_json()
    assert reviewed.get_json()["readiness"]["state"] == "verified"
    assert reviewed.get_json()["readiness"]["jurisdiction"][
        "verified_by_user_id"
    ] == platform_operator.id


def test_submitter_cannot_verify_own_audited_submission(blueprint_context):
    ctx = blueprint_context
    # Exercise the service boundary as well as the HTTP reviewer boundary: this
    # protects imported/legacy submissions even if an actor later gains a
    # platform role.
    readiness, replayed = submit_jurisdiction_evidence(
        ctx["tenant_a"],
        actor_user_id=ctx["superadmin"].id,
        jurisdiction_ref=_evidence_payload()["jurisdiction_ref"],
        evidence_ref=_evidence_payload()["evidence_ref"],
        evidence_sha256=EVIDENCE_SHA256,
        idempotency_key="legacy-superadmin-submission",
    )
    assert replayed is False
    submission_sha = readiness["jurisdiction"]["evidence"]["submission_sha256"]

    response = _review(
        ctx,
        submission_sha,
        idempotency_key="self-review-attempt-001",
    )
    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "jurisdiction_self_verification_forbidden"
    assert ctx["tenant_a"].jurisdiction_status == "unverified"
    assert AuditEvent.query.filter_by(event_type="tenant_jurisdiction_verified").count() == 0


def test_rejection_remains_fail_closed_and_requires_fresh_submission(
    blueprint_context,
):
    ctx = blueprint_context
    submitted = _submit(ctx)
    submission_sha = _submission_sha(submitted)

    rejected = _review(
        ctx,
        submission_sha,
        decision="reject",
        idempotency_key="jurisdiction-reject-001",
        reason_code="boundary_source_not_official",
    )
    assert rejected.status_code == 201, rejected.get_json()
    readiness = rejected.get_json()["readiness"]
    assert readiness["state"] == "rejected"
    assert readiness["ready_to_publish"] is False
    assert readiness["next_action"] == "resubmit_jurisdiction_evidence"
    assert readiness["publication_guard"]["reason_code"] == (
        "survey_tenant_jurisdiction_unverified"
    )
    assert readiness["workflow"]["review"]["actor_user_id"] == ctx["superadmin"].id
    assert readiness["workflow"]["review"]["reason_code"] == (
        "boundary_source_not_official"
    )
    db.session.refresh(ctx["tenant_a"])
    assert ctx["tenant_a"].jurisdiction_status == "unverified"
    assert ctx["tenant_a"].jurisdiction_verified_by_user_id is None
    assert ctx["tenant_a"].jurisdiction_verified_at is None

    duplicate_decision = _review(
        ctx,
        submission_sha,
        decision="reject",
        idempotency_key="jurisdiction-reject-different-key",
        reason_code="boundary_source_not_official",
    )
    assert duplicate_decision.status_code == 409
    assert duplicate_decision.get_json()["reason_code"] == (
        "jurisdiction_submission_already_reviewed"
    )

    resubmitted = _submit(
        ctx,
        idempotency_key="jurisdiction-resubmit-002",
        payload=_evidence_payload(digest=SECOND_EVIDENCE_SHA256),
    )
    assert resubmitted.status_code == 201
    assert resubmitted.get_json()["readiness"]["state"] == "evidence_submitted"
    assert _submission_sha(resubmitted) != submission_sha
