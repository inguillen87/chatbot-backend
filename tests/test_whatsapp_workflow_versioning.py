from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid
from unittest.mock import patch

import jwt
import pytest

from app import db
from models import Notification, TenantProfile, TenantTicket, User
from models_whatsapp_workflows import (
    WhatsAppWorkflowActivation,
    WhatsAppWorkflowDraftRevision,
    WhatsAppWorkflowReview,
    WhatsAppWorkflowVersion,
)
import services.whatsapp_workflow_versioning as workflow_versioning
from services.whatsapp_workflow_versioning import (
    WorkflowStudioError,
    workflow_durable_control_plane_gate,
)


def _auth_headers(app, user: User, tenant_slug: str) -> dict[str, str]:
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
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant_slug}


def _seed_tenant(*, slug: str) -> tuple[User, User, TenantProfile]:
    owner = User(
        name=f"Owner {slug}",
        email=f"owner-{slug}@test.com",
        rol="admin",
        tenant_slug=slug,
        tipo_chat="pyme",
    )
    owner.set_password("secret")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="pyme",
        pyme_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    reviewer = User(
        name=f"Reviewer {slug}",
        email=f"reviewer-{slug}@test.com",
        rol="admin",
        tenant_slug=slug,
        tenant_id=tenant.id,
        tipo_chat="pyme",
    )
    reviewer.set_password("secret")
    db.session.add(reviewer)
    db.session.commit()
    return owner, reviewer, tenant


def _draft(*, name: str, category: str = "evidencia_visual") -> dict:
    return {
        "schema_version": "whatsapp.workflow_draft.v1",
        "name": name,
        "trigger": {"type": "inbound_message"},
        "entry_step_id": "create_case",
        "steps": [
            {
                "id": "create_case",
                "type": "create_ticket",
                "category": category,
                "next": "stop",
            },
            {"id": "stop", "type": "stop", "outcome": "completed"},
        ],
    }


def _enable_canary(monkeypatch, app, tenant: TenantProfile) -> None:
    monkeypatch.setitem(
        app.config,
        "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1",
        True,
    )
    monkeypatch.setitem(
        app.config,
        "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS",
        str(tenant.id),
    )


def _create_draft(client, app, owner, tenant, *, draft, key):
    return client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/drafts",
        headers=_auth_headers(app, owner, tenant.slug),
        json={"draft": draft, "idempotency_key": key},
    )


def _review(
    client,
    app,
    reviewer,
    tenant,
    workflow_id,
    *,
    operation,
    subject_id,
    key,
    decision="approved",
):
    return client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/"
        f"workflows/{workflow_id}/reviews",
        headers=_auth_headers(app, reviewer, tenant.slug),
        json={
            "operation": operation,
            "subject_id": subject_id,
            "decision": decision,
            "note": f"Revision independiente {decision} para {operation}.",
            "idempotency_key": key,
        },
    )


def _publish(client, app, owner, tenant, workflow_id, *, draft_revision_id, review_id, key):
    return client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/"
        f"workflows/{workflow_id}/publish",
        headers=_auth_headers(app, owner, tenant.slug),
        json={
            "draft_revision_id": draft_revision_id,
            "review_id": review_id,
            "idempotency_key": key,
        },
    )


def test_workflow_control_plane_gate_requires_boolean_opt_in_and_canonical_allowlist():
    truthy_string = workflow_durable_control_plane_gate(
        {
            "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1": "true",
            "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS": "1",
        },
        tenant_id=1,
    )
    malformed_allowlist = workflow_durable_control_plane_gate(
        {
            "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1": True,
            "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS": "01",
        },
        tenant_id=1,
    )
    ready = workflow_durable_control_plane_gate(
        {
            "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1": True,
            "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS": "1,2",
        },
        tenant_id=1,
    )

    assert truthy_string["available"] is False
    assert "workflow_durable_control_plane_disabled" in truthy_string["reason_codes"]
    assert malformed_allowlist["available"] is False
    assert "workflow_durable_tenant_allowlist_invalid" in malformed_allowlist["reason_codes"]
    assert ready["available"] is True
    assert ready["runtime_binding"]["available"] is False


def test_durable_control_plane_is_fail_closed_before_parsing_body(client, app):
    owner, _, tenant = _seed_tenant(slug="workflow-gate")

    with patch(
        "flask.wrappers.Request.get_json",
        side_effect=AssertionError("gate_must_precede_json_body_parse"),
    ):
        response = client.post(
            f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/drafts",
            headers=_auth_headers(app, owner, tenant.slug),
            data='{"draft":{"must_not":"parse"}}',
            content_type="application/json",
        )

    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["reason_code"] == "workflow_durable_control_plane_disabled"
    assert WhatsAppWorkflowDraftRevision.query.count() == 0


def test_durable_control_plane_contract_and_writes_respect_plan_gate(
    client,
    app,
    monkeypatch,
):
    owner, _, tenant = _seed_tenant(slug="workflow-plan")
    tenant.plan = "free"
    db.session.commit()
    _enable_canary(monkeypatch, app, tenant)
    headers = _auth_headers(app, owner, tenant.slug)

    contract = client.get(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio",
        headers=headers,
    )
    with patch(
        "flask.wrappers.Request.get_json",
        side_effect=AssertionError("plan_gate_must_precede_json_body_parse"),
    ):
        write = client.post(
            f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/drafts",
            headers=headers,
            data='{"draft":{"must_not":"parse"}}',
            content_type="application/json",
        )

    assert contract.status_code == 200
    assert contract.get_json()["durable_control_plane_gate"]["available"] is False
    assert "workflow_durable_plan_required" in contract.get_json()[
        "durable_control_plane_gate"
    ]["reason_codes"]
    assert contract.get_json()["capabilities"]["draft"]["persistent"] is False
    assert write.status_code == 403
    assert write.get_json()["error"] == "plan_required"
    assert write.headers["Cache-Control"] == "no-store"
    assert WhatsAppWorkflowDraftRevision.query.count() == 0


def test_durable_draft_revisions_are_tenant_scoped_idempotent_and_optimistic(
    client,
    app,
    monkeypatch,
):
    owner, _, tenant = _seed_tenant(slug="workflow-drafts")
    _enable_canary(monkeypatch, app, tenant)
    headers = _auth_headers(app, owner, tenant.slug)

    contract = client.get(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio",
        headers=headers,
    )
    assert contract.status_code == 200
    contract_payload = contract.get_json()
    assert contract_payload["mode"] == "durable_control_plane"
    assert contract_payload["capabilities"]["draft"]["persistent"] is True
    assert contract_payload["capabilities"]["publish"]["status"] == "control_plane_only"
    assert contract_payload["capabilities"]["review"][
        "publish_reviewer_must_differ_from"
    ] == ["draft_author", "publisher"]
    assert contract_payload["capabilities"]["runtime"]["available"] is False

    created = _create_draft(
        client,
        app,
        owner,
        tenant,
        draft=_draft(name="Ingreso visual"),
        key="draft-create-0001",
    )
    replay = _create_draft(
        client,
        app,
        owner,
        tenant,
        draft=_draft(name="Ingreso visual"),
        key="draft-create-0001",
    )
    mismatch = _create_draft(
        client,
        app,
        owner,
        tenant,
        draft=_draft(name="Contenido diferente"),
        key="draft-create-0001",
    )

    assert created.status_code == 201
    assert replay.status_code == 200
    assert mismatch.status_code == 409
    assert mismatch.get_json()["reason_code"] == "workflow_idempotency_conflict"
    assert created.headers["Cache-Control"] == "no-store"
    first = created.get_json()["draft_revision"]
    assert replay.get_json()["draft_revision"]["draft_revision_id"] == first["draft_revision_id"]
    assert WhatsAppWorkflowDraftRevision.query.count() == 1

    stale = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/"
        f"workflows/{first['workflow_id']}/drafts",
        headers=headers,
        json={
            "draft": _draft(name="Revision dos", category="documentacion"),
            "expected_revision": 2,
            "idempotency_key": "draft-update-stale-0001",
        },
    )
    revised = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/"
        f"workflows/{first['workflow_id']}/drafts",
        headers=headers,
        json={
            "draft": _draft(name="Revision dos", category="documentacion"),
            "expected_revision": 1,
            "idempotency_key": "draft-update-good-0001",
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["reason_code"] == "workflow_draft_revision_conflict"
    assert revised.status_code == 201
    assert revised.get_json()["draft_revision"]["revision"] == 2
    assert WhatsAppWorkflowDraftRevision.query.count() == 2

    listing = client.get(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/workflows",
        headers=headers,
    )
    detail = client.get(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/"
        f"workflows/{first['workflow_id']}",
        headers=headers,
    )
    assert listing.status_code == 200
    assert listing.get_json()["workflows"][0]["latest_draft_revision"] == 2
    assert detail.status_code == 200
    assert detail.get_json()["active_version_id"] is None
    assert detail.get_json()["runtime_binding"]["consumes_active_version"] is False


def test_draft_author_cannot_supply_the_independent_publish_review(
    client,
    app,
    monkeypatch,
):
    owner, publisher, tenant = _seed_tenant(slug="workflow-independent-review")
    _enable_canary(monkeypatch, app, tenant)
    created = _create_draft(
        client,
        app,
        owner,
        tenant,
        draft=_draft(name="Autor no revisor"),
        key="independent-draft-0001",
    ).get_json()["draft_revision"]
    author_review = _review(
        client,
        app,
        owner,
        tenant,
        created["workflow_id"],
        operation="publish",
        subject_id=created["draft_revision_id"],
        key="independent-self-review-0001",
    ).get_json()["review"]

    response = _publish(
        client,
        app,
        publisher,
        tenant,
        created["workflow_id"],
        draft_revision_id=created["draft_revision_id"],
        review_id=author_review["review_id"],
        key="independent-publish-0001",
    )

    assert response.status_code == 409
    assert (
        response.get_json()["reason_code"]
        == "workflow_independent_content_review_required"
    )
    assert WhatsAppWorkflowVersion.query.count() == 0
    assert WhatsAppWorkflowActivation.query.count() == 0


def test_only_latest_subject_review_can_publish_after_rejection_and_reapproval(
    client,
    app,
    monkeypatch,
):
    owner, reviewer, tenant = _seed_tenant(slug="workflow-review-order")
    _enable_canary(monkeypatch, app, tenant)
    draft = _create_draft(
        client,
        app,
        owner,
        tenant,
        draft=_draft(name="Review ordenado"),
        key="review-order-draft-0001",
    ).get_json()["draft_revision"]
    workflow_id = draft["workflow_id"]
    approval = _review(
        client,
        app,
        reviewer,
        tenant,
        workflow_id,
        operation="publish",
        subject_id=draft["draft_revision_id"],
        key="review-order-approval-0001",
    ).get_json()["review"]
    rejection = _review(
        client,
        app,
        reviewer,
        tenant,
        workflow_id,
        operation="publish",
        subject_id=draft["draft_revision_id"],
        key="review-order-rejection-0001",
        decision="rejected",
    ).get_json()["review"]

    assert approval["subject_sequence"] == 1
    assert rejection["subject_sequence"] == 2
    superseded = _publish(
        client,
        app,
        owner,
        tenant,
        workflow_id,
        draft_revision_id=draft["draft_revision_id"],
        review_id=approval["review_id"],
        key="review-order-old-approval-0001",
    )
    rejected = _publish(
        client,
        app,
        owner,
        tenant,
        workflow_id,
        draft_revision_id=draft["draft_revision_id"],
        review_id=rejection["review_id"],
        key="review-order-rejected-0001",
    )

    assert superseded.status_code == 409
    assert superseded.get_json()["reason_code"] == "workflow_review_superseded"
    assert rejected.status_code == 409
    assert rejected.get_json()["reason_code"] == "workflow_review_not_approved"
    assert WhatsAppWorkflowVersion.query.count() == 0

    fresh_approval = _review(
        client,
        app,
        reviewer,
        tenant,
        workflow_id,
        operation="publish",
        subject_id=draft["draft_revision_id"],
        key="review-order-approval-0002",
    ).get_json()["review"]
    published = _publish(
        client,
        app,
        owner,
        tenant,
        workflow_id,
        draft_revision_id=draft["draft_revision_id"],
        review_id=fresh_approval["review_id"],
        key="review-order-fresh-publish-0001",
    )

    assert fresh_approval["subject_sequence"] == 3
    assert published.status_code == 201
    assert published.get_json()["version"]["review_id"] == fresh_approval["review_id"]
    assert WhatsAppWorkflowVersion.query.count() == 1


def test_publish_revalidates_latest_draft_after_workflow_lock(monkeypatch, client, app):
    owner, reviewer, tenant = _seed_tenant(slug="workflow-stale-race")
    _enable_canary(monkeypatch, app, tenant)
    draft = _create_draft(
        client,
        app,
        owner,
        tenant,
        draft=_draft(name="Revision inicial"),
        key="stale-race-draft-0001",
    ).get_json()["draft_revision"]
    review = _review(
        client,
        app,
        reviewer,
        tenant,
        draft["workflow_id"],
        operation="publish",
        subject_id=draft["draft_revision_id"],
        key="stale-race-review-0001",
    ).get_json()["review"]
    validation = workflow_versioning.validate_workflow_draft(
        _draft(name="Revision concurrente", category="documentacion"),
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )

    def _inject_newer_draft(*, tenant_id, workflow_id):
        db.session.add(
            WhatsAppWorkflowDraftRevision(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                revision=2,
                schema_version=validation["normalized_draft"]["schema_version"],
                draft_digest=validation["draft_digest"],
                draft_json=validation["normalized_draft"],
                authored_by_user_id=owner.id,
                idempotency_key="stale-race-draft-0002",
                request_hash="1" * 64,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.session.flush()

    monkeypatch.setattr(
        workflow_versioning,
        "_acquire_workflow_write_lock",
        _inject_newer_draft,
    )
    with pytest.raises(WorkflowStudioError) as exc_info:
        workflow_versioning.publish_workflow(
            tenant_id=tenant.id,
            workflow_id=draft["workflow_id"],
            publisher_user_id=owner.id,
            draft_revision_id=draft["draft_revision_id"],
            review_id=review["review_id"],
            idempotency_key="stale-race-publish-0001",
        )

    assert exc_info.value.reason_code == "workflow_draft_revision_stale"
    assert WhatsAppWorkflowVersion.query.count() == 0
    assert WhatsAppWorkflowActivation.query.count() == 0
    db.session.rollback()


def test_competing_publish_revalidates_active_digest_after_workflow_lock(
    monkeypatch,
    client,
    app,
):
    owner, reviewer, tenant = _seed_tenant(slug="workflow-publish-race")
    _enable_canary(monkeypatch, app, tenant)
    draft = _create_draft(
        client,
        app,
        owner,
        tenant,
        draft=_draft(name="Contenido unico"),
        key="publish-race-draft-0001",
    ).get_json()["draft_revision"]
    first_review = _review(
        client,
        app,
        reviewer,
        tenant,
        draft["workflow_id"],
        operation="publish",
        subject_id=draft["draft_revision_id"],
        key="publish-race-review-0001",
    ).get_json()["review"]
    draft_row = db.session.get(
        WhatsAppWorkflowDraftRevision,
        draft["draft_revision_id"],
    )
    second_review_id = str(uuid.uuid4())

    def _inject_winner_then_new_review(*, tenant_id, workflow_id):
        version_id = str(uuid.uuid4())
        activation_id = str(uuid.uuid4())
        winner_hash = "2" * 64
        db.session.add_all(
            [
                WhatsAppWorkflowVersion(
                    id=version_id,
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    version=1,
                    version_kind="publish",
                    source_draft_revision_id=draft_row.id,
                    restored_from_version_id=None,
                    review_id=first_review["review_id"],
                    schema_version=draft_row.schema_version,
                    content_digest=draft_row.draft_digest,
                    content_json=json.loads(json.dumps(draft_row.draft_json)),
                    published_by_user_id=owner.id,
                    idempotency_key="publish-race-winner-0001",
                    request_hash=winner_hash,
                    published_at=datetime.now(timezone.utc),
                ),
                WhatsAppWorkflowActivation(
                    id=activation_id,
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    sequence=1,
                    workflow_version_id=version_id,
                    previous_activation_id=None,
                    activation_kind="publish",
                    activated_by_user_id=owner.id,
                    idempotency_key="publish-race-winner-0001",
                    request_hash=winner_hash,
                    activated_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.session.flush()
        db.session.add(
            WhatsAppWorkflowReview(
                id=second_review_id,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                operation="publish",
                subject_type="draft_revision",
                subject_id=draft_row.id,
                subject_digest=draft_row.draft_digest,
                subject_sequence=2,
                decision="approved",
                review_note="Nueva aprobacion luego de la publicacion ganadora.",
                reviewed_by_user_id=reviewer.id,
                idempotency_key="publish-race-review-0002",
                request_hash="3" * 64,
                reviewed_at=datetime.now(timezone.utc),
            )
        )
        db.session.flush()

    monkeypatch.setattr(
        workflow_versioning,
        "_acquire_workflow_write_lock",
        _inject_winner_then_new_review,
    )
    with pytest.raises(WorkflowStudioError) as exc_info:
        workflow_versioning.publish_workflow(
            tenant_id=tenant.id,
            workflow_id=draft["workflow_id"],
            publisher_user_id=owner.id,
            draft_revision_id=draft["draft_revision_id"],
            review_id=second_review_id,
            idempotency_key="publish-race-contender-0001",
        )

    assert exc_info.value.reason_code == "workflow_content_already_active"
    assert WhatsAppWorkflowVersion.query.count() == 1
    assert WhatsAppWorkflowActivation.query.count() == 1
    assert WhatsAppWorkflowVersion.query.one().version == 1
    db.session.rollback()


def test_workflow_ids_cannot_cross_tenant_boundaries(client, app, monkeypatch):
    owner_a, _, tenant_a = _seed_tenant(slug="workflow-scope-a")
    owner_b, _, tenant_b = _seed_tenant(slug="workflow-scope-b")
    monkeypatch.setitem(
        app.config,
        "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1",
        True,
    )
    monkeypatch.setitem(
        app.config,
        "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS",
        f"{tenant_a.id},{tenant_b.id}",
    )
    foreign_draft = _create_draft(
        client,
        app,
        owner_b,
        tenant_b,
        draft=_draft(name="Tenant B"),
        key="tenant-b-draft-0001",
    ).get_json()["draft_revision"]

    response = client.get(
        f"/api/v2/tenants/{tenant_a.slug}/whatsapp/workflow-studio/"
        f"workflows/{foreign_draft['workflow_id']}",
        headers=_auth_headers(app, owner_a, tenant_a.slug),
    )
    review_response = client.post(
        f"/api/v2/tenants/{tenant_a.slug}/whatsapp/workflow-studio/"
        f"workflows/{foreign_draft['workflow_id']}/reviews",
        headers=_auth_headers(app, owner_a, tenant_a.slug),
        json={
            "operation": "publish",
            "subject_id": foreign_draft["draft_revision_id"],
            "decision": "approved",
            "note": "No debe atravesar el tenant.",
            "idempotency_key": "tenant-cross-review-0001",
        },
    )

    assert response.status_code == 404
    assert response.get_json()["reason_code"] == "workflow_not_found"
    assert review_response.status_code == 404
    assert review_response.get_json()["reason_code"] == "workflow_review_subject_not_found"
    assert WhatsAppWorkflowReview.query.count() == 0


def test_reviewed_publish_and_rollback_append_history_without_runtime_effects(
    client,
    app,
    monkeypatch,
):
    owner, reviewer, tenant = _seed_tenant(slug="workflow-ledger")
    _enable_canary(monkeypatch, app, tenant)
    before_effects = {
        "tickets": TenantTicket.query.count(),
        "notifications": Notification.query.count(),
    }

    first_draft_response = _create_draft(
        client,
        app,
        owner,
        tenant,
        draft=_draft(name="Version uno", category="arbolado"),
        key="ledger-draft-one-0001",
    )
    first_draft = first_draft_response.get_json()["draft_revision"]
    workflow_id = first_draft["workflow_id"]
    first_review_response = _review(
        client,
        app,
        reviewer,
        tenant,
        workflow_id,
        operation="publish",
        subject_id=first_draft["draft_revision_id"],
        key="ledger-review-one-0001",
    )
    assert first_review_response.status_code == 201
    first_review = first_review_response.get_json()["review"]

    self_publish = _publish(
        client,
        app,
        reviewer,
        tenant,
        workflow_id,
        draft_revision_id=first_draft["draft_revision_id"],
        review_id=first_review["review_id"],
        key="ledger-self-publish-0001",
    )
    assert self_publish.status_code == 409
    assert self_publish.get_json()["reason_code"] == "workflow_separation_of_duties_required"

    first_publish = _publish(
        client,
        app,
        owner,
        tenant,
        workflow_id,
        draft_revision_id=first_draft["draft_revision_id"],
        review_id=first_review["review_id"],
        key="ledger-publish-one-0001",
    )
    first_replay = _publish(
        client,
        app,
        owner,
        tenant,
        workflow_id,
        draft_revision_id=first_draft["draft_revision_id"],
        review_id=first_review["review_id"],
        key="ledger-publish-one-0001",
    )
    assert first_publish.status_code == 201
    assert first_replay.status_code == 200
    first_publication = first_publish.get_json()
    first_version = first_publication["version"]
    assert first_version["version"] == 1
    assert first_publication["activation"]["sequence"] == 1
    assert first_publication["runtime_binding"]["consumes_active_version"] is False
    assert first_publication["external_effects"]["provider_calls"] == 0
    assert first_replay.get_json()["version"]["version_id"] == first_version["version_id"]

    second_draft_response = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/"
        f"workflows/{workflow_id}/drafts",
        headers=_auth_headers(app, owner, tenant.slug),
        json={
            "draft": _draft(name="Version dos", category="luminaria"),
            "expected_revision": 1,
            "idempotency_key": "ledger-draft-two-0001",
        },
    )
    second_draft = second_draft_response.get_json()["draft_revision"]
    second_review = _review(
        client,
        app,
        reviewer,
        tenant,
        workflow_id,
        operation="publish",
        subject_id=second_draft["draft_revision_id"],
        key="ledger-review-two-0001",
    ).get_json()["review"]
    second_publish = _publish(
        client,
        app,
        owner,
        tenant,
        workflow_id,
        draft_revision_id=second_draft["draft_revision_id"],
        review_id=second_review["review_id"],
        key="ledger-publish-two-0001",
    )
    assert second_publish.status_code == 201
    assert second_publish.get_json()["version"]["version"] == 2

    rollback_review = _review(
        client,
        app,
        reviewer,
        tenant,
        workflow_id,
        operation="rollback",
        subject_id=first_version["version_id"],
        key="ledger-review-rollback-0001",
    ).get_json()["review"]
    rollback_body = {
        "target_version_id": first_version["version_id"],
        "review_id": rollback_review["review_id"],
        "idempotency_key": "ledger-rollback-one-0001",
    }
    rollback_response = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/"
        f"workflows/{workflow_id}/rollback",
        headers=_auth_headers(app, owner, tenant.slug),
        json=rollback_body,
    )
    rollback_replay = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/"
        f"workflows/{workflow_id}/rollback",
        headers=_auth_headers(app, owner, tenant.slug),
        json=rollback_body,
    )

    assert rollback_response.status_code == 201
    assert rollback_replay.status_code == 200
    rolled_back = rollback_response.get_json()
    assert rolled_back["version"]["version"] == 3
    assert rolled_back["version"]["version_kind"] == "rollback"
    assert rolled_back["version"]["restored_from_version_id"] == first_version["version_id"]
    assert rolled_back["version"]["version_id"] != first_version["version_id"]
    assert rolled_back["activation"]["sequence"] == 3
    assert rollback_replay.get_json()["version"]["version_id"] == rolled_back["version"]["version_id"]
    historical_publish_replay = _publish(
        client,
        app,
        owner,
        tenant,
        workflow_id,
        draft_revision_id=first_draft["draft_revision_id"],
        review_id=first_review["review_id"],
        key="ledger-publish-one-0001",
    )
    assert historical_publish_replay.status_code == 200
    assert historical_publish_replay.get_json()["currently_active"] is False
    assert WhatsAppWorkflowDraftRevision.query.count() == 2
    assert WhatsAppWorkflowReview.query.count() == 3
    assert WhatsAppWorkflowVersion.query.count() == 3
    assert WhatsAppWorkflowActivation.query.count() == 3
    assert before_effects == {
        "tickets": TenantTicket.query.count(),
        "notifications": Notification.query.count(),
    }

    persisted_first = db.session.get(WhatsAppWorkflowVersion, first_version["version_id"])
    assert persisted_first.content_digest == first_version["content_digest"]
    persisted_first.content_digest = "0" * 64
    with pytest.raises(ValueError, match="history is immutable"):
        db.session.commit()
    db.session.rollback()
    assert db.session.get(
        WhatsAppWorkflowVersion,
        first_version["version_id"],
    ).content_digest == first_version["content_digest"]
