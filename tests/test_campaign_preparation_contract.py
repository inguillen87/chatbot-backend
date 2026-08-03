from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from models import (
    AdminAuditLog,
    Notification,
    NotificationAttempt,
    NotificationTemplate,
    TenantProfile,
    User,
    db,
)
from models_campaigns import CampaignDeliveryIntent, CampaignPreparation
from models_memory import Contact, InteractionEvent


def _auth_headers(app, user: User, tenant_slug: str, idempotency_key: str) -> dict:
    token = jwt.encode(
        {
            "user_id": user.id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant": tenant_slug,
        "Idempotency-Key": idempotency_key,
    }


def _seed_campaign_scope():
    owner = User(
        email="campaign-owner@test.com",
        name="Campaign Owner",
        rol="admin",
        tipo_chat="pyme",
    )
    foreign_owner = User(
        email="foreign-campaign-owner@test.com",
        name="Foreign Owner",
        rol="admin",
        tipo_chat="pyme",
    )
    owner.set_password("pass")
    foreign_owner.set_password("pass")
    db.session.add_all([owner, foreign_owner])
    db.session.flush()

    tenant = TenantProfile(
        slug="campaign-tenant",
        nombre="Campaign Tenant",
        tipo="pyme",
        pyme_id=owner.id,
    )
    foreign_tenant = TenantProfile(
        slug="foreign-campaign-tenant",
        nombre="Foreign Campaign Tenant",
        tipo="pyme",
        pyme_id=foreign_owner.id,
    )
    db.session.add_all([tenant, foreign_tenant])
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    foreign_owner.tenant_id = foreign_tenant.id
    foreign_owner.tenant_slug = foreign_tenant.slug

    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="municipal_update",
        channel="whatsapp",
        subject_template=None,
        body_template="Hola $name, hay una novedad municipal.",
        is_active=True,
    )
    foreign_template = NotificationTemplate(
        tenant_id=foreign_tenant.id,
        key="foreign_update",
        channel="whatsapp",
        body_template="Contenido ajeno",
        is_active=True,
    )
    eligible = Contact(
        id=str(uuid4()),
        tenant_id=tenant.id,
        name="Eligible",
        phone="+5492613000001",
        preferences={"marketing_opt_in": True},
    )
    opted_out = Contact(
        id=str(uuid4()),
        tenant_id=tenant.id,
        name="Opted out",
        phone="+5492613000002",
        preferences={"marketing_opt_out": True},
    )
    missing_channel = Contact(
        id=str(uuid4()),
        tenant_id=tenant.id,
        name="Missing channel",
        phone=None,
        preferences={"marketing_opt_in": True},
    )
    consent_missing = Contact(
        id=str(uuid4()),
        tenant_id=tenant.id,
        name="No recorded consent",
        phone="+5492613000003",
        preferences={},
    )
    foreign_contact = Contact(
        id=str(uuid4()),
        tenant_id=foreign_tenant.id,
        name="Foreign contact",
        phone="+5492613999999",
        preferences={"marketing_opt_in": True},
    )
    db.session.add_all(
        [
            template,
            foreign_template,
            eligible,
            opted_out,
            missing_channel,
            consent_missing,
            foreign_contact,
        ]
    )
    db.session.commit()
    return {
        "owner": owner,
        "foreign_owner": foreign_owner,
        "tenant": tenant,
        "foreign_tenant": foreign_tenant,
        "template": template,
        "foreign_template": foreign_template,
        "eligible": eligible,
        "opted_out": opted_out,
        "missing_channel": missing_channel,
        "consent_missing": consent_missing,
        "foreign_contact": foreign_contact,
    }


def _payload(scope: dict) -> dict:
    return {
        "template_id": scope["template"].id,
        "context": {"name": "vecino"},
        "content_variables": {},
        "contact_ids": [
            scope["eligible"].id,
            scope["opted_out"].id,
            scope["missing_channel"].id,
            scope["foreign_contact"].id,
            scope["eligible"].id,
        ],
        "max_per_week": 2,
        "min_interval_hours": 24,
    }


def test_prepare_campaign_persists_held_receipts_without_transport(client, app):
    scope = _seed_campaign_scope()
    response = client.post(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/prepare",
        headers=_auth_headers(
            app, scope["owner"], scope["tenant"].slug, "campaign-test-0001"
        ),
        json=_payload(scope),
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["contract_version"] == "crm_campaign_prepare.v1"
    assert payload["campaign"]["status"] == "draft"
    assert payload["campaign"]["idempotent_replay"] is False
    assert payload["audience"] == {
        "requested": 5,
        "unique_requested": 4,
        "resolved": 3,
        "eligible": 1,
        "excluded": 2,
        "unresolved": 1,
        "duplicates_ignored": 1,
        "exclusion_counts": {
            "opt_out": 1,
            "consent_missing": 0,
            "missing_whatsapp": 1,
            "missing_email": 0,
            "frequency_window": 0,
        },
        "rate_limit_policy": {
            "max_per_week": 2,
            "min_interval_hours": 24,
            "legacy_records_are_conservative_guards_only": True,
        },
        "consent_policy": {
            "purpose": "marketing",
            "explicit_opt_in_required": True,
        },
    }
    assert payload["readiness"]["preview_valid"] is True
    assert payload["readiness"]["transport_readiness_checked"] is False
    assert payload["readiness"]["production_send_allowed"] is False
    assert "campaign_dispatch_not_authorized" in payload["readiness"]["blockers"]
    assert payload["queue"]["state"] == "held"
    assert payload["queue"]["dispatch_authorized"] is False
    assert payload["queue"]["transport_outcomes"] == {
        "not_attempted": 3,
        "unknown": 0,
        "accepted": 0,
        "sent": 0,
        "delivered": 0,
        "read": 0,
        "failed": 0,
    }
    assert payload["side_effects"] == {
        "campaigns_created": 1,
        "queue_receipts_created": 3,
        "notifications_queued": 0,
        "provider_calls_performed": False,
        "messages_sent": 0,
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert scope["foreign_contact"].id not in response.get_data(as_text=True)

    assert CampaignPreparation.query.count() == 1
    intents = CampaignDeliveryIntent.query.order_by(
        CampaignDeliveryIntent.contact_id
    ).all()
    assert len(intents) == 3
    assert {intent.transport_status for intent in intents} == {"not_attempted"}
    assert {intent.attempt_count for intent in intents} == {0}
    assert all(intent.provider_message_id is None for intent in intents)
    assert Notification.query.count() == 0
    assert NotificationAttempt.query.count() == 0

    audit = AdminAuditLog.query.filter_by(
        action="campaign_preparation_created"
    ).one()
    serialized_audit = str(audit.details)
    assert "+549261" not in serialized_audit
    assert "novedad municipal" not in serialized_audit
    assert "vecino" not in serialized_audit


def test_prepare_campaign_requires_explicit_marketing_opt_in(client, app):
    scope = _seed_campaign_scope()
    response = client.post(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/prepare",
        headers=_auth_headers(
            app, scope["owner"], scope["tenant"].slug, "campaign-consent-0001"
        ),
        json={
            "template_id": scope["template"].id,
            "context": {"name": "vecino"},
            "content_variables": {},
            "contact_ids": [scope["consent_missing"].id],
            "max_per_week": 2,
            "min_interval_hours": 24,
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["audience"]["eligible"] == 0
    assert payload["audience"]["excluded"] == 1
    assert payload["audience"]["exclusion_counts"]["consent_missing"] == 1
    assert payload["audience"]["consent_policy"] == {
        "purpose": "marketing",
        "explicit_opt_in_required": True,
    }
    assert payload["queue"]["receipts"][0]["queue_status"] == "excluded"
    assert payload["queue"]["receipts"][0]["exclusion_reason"] == "consent_missing"
    assert payload["side_effects"]["provider_calls_performed"] is False
    assert Notification.query.count() == 0


def test_prepare_campaign_replay_is_idempotent_and_conflicts_on_payload_change(
    client, app
):
    scope = _seed_campaign_scope()
    headers = _auth_headers(
        app, scope["owner"], scope["tenant"].slug, "campaign-test-0002"
    )
    request_payload = _payload(scope)

    first = client.post(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/prepare",
        headers=headers,
        json=request_payload,
    )
    replay = client.post(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/prepare",
        headers=headers,
        json=request_payload,
    )
    changed = dict(request_payload)
    changed["context"] = {"name": "otra persona"}
    conflict = client.post(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/prepare",
        headers=headers,
        json=changed,
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert (
        replay.get_json()["campaign"]["id"]
        == first.get_json()["campaign"]["id"]
    )
    assert replay.get_json()["campaign"]["idempotent_replay"] is True
    assert replay.get_json()["side_effects"]["queue_receipts_created"] == 0
    assert CampaignPreparation.query.count() == 1
    assert CampaignDeliveryIntent.query.count() == 3

    assert conflict.status_code == 409
    assert conflict.get_json() == {
        "contract_version": "crm_campaign_prepare.v1",
        "error": "campaign_idempotency_conflict",
    }
    assert CampaignPreparation.query.count() == 1


def test_prepare_campaign_fails_closed_for_foreign_template_and_raw_message(
    client, app
):
    scope = _seed_campaign_scope()
    endpoint = f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/prepare"

    foreign = client.post(
        endpoint,
        headers=_auth_headers(
            app, scope["owner"], scope["tenant"].slug, "campaign-test-0003"
        ),
        json={
            "template_id": scope["foreign_template"].id,
            "context": {},
            "content_variables": {},
            "contact_ids": [scope["eligible"].id],
        },
    )
    raw_message = client.post(
        endpoint,
        headers=_auth_headers(
            app, scope["owner"], scope["tenant"].slug, "campaign-test-0004"
        ),
        json={
            "message": "texto libre ambiguo",
            "contact_ids": [scope["eligible"].id],
        },
    )

    assert foreign.status_code == 404
    assert foreign.get_json()["error"] == "notification_template_not_found"
    assert raw_message.status_code == 400
    assert raw_message.get_json() == {
        "contract_version": "crm_campaign_prepare.v1",
        "error": "campaign_request_fields_unsupported",
        "field": "body",
    }
    assert CampaignPreparation.query.count() == 0
    assert Notification.query.count() == 0


def test_prepare_campaign_schedule_remains_draft_without_scheduler(client, app):
    scope = _seed_campaign_scope()
    request_payload = _payload(scope)
    request_payload["scheduled_for"] = "2026-08-10T09:00:00"
    request_payload["timezone"] = "America/Argentina/Buenos_Aires"
    response = client.post(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/prepare",
        headers=_auth_headers(
            app, scope["owner"], scope["tenant"].slug, "campaign-test-0005"
        ),
        json=request_payload,
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["campaign"]["status"] == "draft"
    assert payload["campaign"]["scheduled_for_utc"].endswith("+00:00")
    assert "campaign_scheduler_not_connected" in payload["readiness"]["blockers"]
    assert payload["queue"]["transport_outcomes"]["not_attempted"] == 3


def test_prepare_campaign_requires_idempotency_key_and_exact_request_fields(
    client, app
):
    scope = _seed_campaign_scope()
    endpoint = f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/prepare"
    no_key_headers = _auth_headers(
        app, scope["owner"], scope["tenant"].slug, "campaign-test-0006"
    )
    no_key_headers.pop("Idempotency-Key")

    missing_key = client.post(
        endpoint,
        headers=no_key_headers,
        json=_payload(scope),
    )
    string_boolean = _payload(scope)
    string_boolean["dry_run"] = "false"
    ambiguous = client.post(
        endpoint,
        headers=_auth_headers(
            app, scope["owner"], scope["tenant"].slug, "campaign-test-0007"
        ),
        json=string_boolean,
    )

    assert missing_key.status_code == 400
    assert missing_key.get_json()["error"] == "idempotency_key_required"
    assert ambiguous.status_code == 400
    assert ambiguous.get_json()["error"] == "campaign_request_fields_unsupported"
    assert CampaignPreparation.query.count() == 0


def test_campaign_prepare_rejects_cross_tenant_actor_idor(client, app):
    scope = _seed_campaign_scope()
    response = client.post(
        f"/api/admin/tenants/{scope['foreign_tenant'].slug}/campaigns/prepare",
        headers=_auth_headers(
            app,
            scope["owner"],
            scope["foreign_tenant"].slug,
            "campaign-test-idor-0001",
        ),
        json={
            "template_id": scope["foreign_template"].id,
            "context": {},
            "content_variables": {},
            "contact_ids": [scope["foreign_contact"].id],
            "max_per_week": 2,
            "min_interval_hours": 24,
        },
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "tenant_access_denied"
    assert CampaignPreparation.query.count() == 0
    assert CampaignDeliveryIntent.query.count() == 0
    assert Notification.query.count() == 0


def test_legacy_campaign_send_is_preview_only_and_parses_boolean_exactly(
    client, app
):
    scope = _seed_campaign_scope()
    endpoint = f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/send"
    headers = _auth_headers(
        app, scope["owner"], scope["tenant"].slug, "legacy-unused-0001"
    )
    base_payload = {
        "contact_ids": [scope["eligible"].id],
        "channel": "whatsapp",
        "message": "Legacy audience preview only",
        "max_per_week": 2,
        "min_interval_hours": 24,
    }

    ambiguous_bool = client.post(
        endpoint,
        headers=headers,
        json={**base_payload, "dry_run": "false"},
    )
    blocked_mutation = client.post(
        endpoint,
        headers=headers,
        json=base_payload,
    )
    preview = client.post(
        endpoint,
        headers=headers,
        json={**base_payload, "dry_run": True},
    )
    ambiguous_selector = client.post(
        endpoint,
        headers=headers,
        json={
            **base_payload,
            "dry_run": True,
            "template_slug": "legacy-template",
        },
    )
    invalid_schedule = client.post(
        endpoint,
        headers=headers,
        json={
            **base_payload,
            "dry_run": True,
            "scheduled_for": "not-a-date-DO_NOT_ECHO_THIS_VALUE",
        },
    )
    invalid_timezone = client.post(
        endpoint,
        headers=headers,
        json={
            **base_payload,
            "dry_run": True,
            "scheduled_for": "2026-08-02T10:30:00",
            "timezone": "../DO_NOT_ECHO_THIS_TIMEZONE",
        },
    )
    oversized_audience = client.post(
        endpoint,
        headers=headers,
        json={
            **base_payload,
            "dry_run": True,
            "contact_ids": [f"contact-{index}" for index in range(501)],
        },
    )
    non_string_contact = client.post(
        endpoint,
        headers=headers,
        json={**base_payload, "dry_run": True, "contact_ids": [42]},
    )
    missing_consent_preview = client.post(
        endpoint,
        headers=headers,
        json={
            **base_payload,
            "dry_run": True,
            "contact_ids": [scope["consent_missing"].id],
        },
    )

    assert ambiguous_bool.status_code == 400
    assert ambiguous_bool.get_json()["error"] == "dry_run_must_be_boolean"
    assert ambiguous_bool.headers["Cache-Control"] == "no-store"
    assert ambiguous_bool.headers["Pragma"] == "no-cache"
    assert blocked_mutation.status_code == 410
    assert blocked_mutation.get_json() == {
        "contract_version": "crm_campaign_legacy_audience.v1",
        "reason_code": "campaign_send_endpoint_deprecated",
        "action_hint": "use_campaign_prepare",
        "retryable": False,
        "side_effects": {
            "interaction_events_created": 0,
            "notifications_queued": 0,
            "provider_calls_performed": False,
            "messages_sent": 0,
        },
    }
    assert blocked_mutation.headers["Cache-Control"] == "no-store"
    assert blocked_mutation.headers["Pragma"] == "no-cache"
    assert preview.status_code == 200
    assert preview.headers["Cache-Control"] == "no-store"
    assert preview.headers["Pragma"] == "no-cache"
    preview_payload = preview.get_json()
    assert preview_payload["mode"] == "dry_run"
    assert preview_payload["readiness"]["production_send_allowed"] is False
    assert preview_payload["side_effects"]["messages_sent"] == 0
    assert "eligible_contacts" not in preview_payload
    assert "excluded_optout" not in preview_payload
    assert ambiguous_selector.status_code == 400
    assert ambiguous_selector.get_json()["error"] == "campaign_template_selector_ambiguous"
    assert invalid_schedule.status_code == 400
    assert invalid_schedule.get_json()["error"] == "scheduled_for_invalid"
    assert "DO_NOT_ECHO" not in invalid_schedule.get_data(as_text=True)
    assert invalid_schedule.headers["Cache-Control"] == "no-store"
    assert invalid_timezone.status_code == 400
    assert invalid_timezone.get_json()["error"] == "timezone_invalid"
    assert "DO_NOT_ECHO" not in invalid_timezone.get_data(as_text=True)
    assert oversized_audience.status_code == 400
    assert oversized_audience.get_json() == {
        "contract_version": "crm_campaign_legacy_audience.v1",
        "error": "campaign_recipient_limit_exceeded",
        "field": "contact_ids",
        "max_recipients": 500,
    }
    assert non_string_contact.status_code == 400
    assert non_string_contact.get_json()["error"] == "contact_id_must_be_nonempty_string"
    assert missing_consent_preview.status_code == 200
    missing_consent_payload = missing_consent_preview.get_json()
    assert missing_consent_payload["totals"]["eligible"] == 0
    assert missing_consent_payload["totals"]["excluded_consent_missing"] == 1
    assert missing_consent_payload["consent_policy"] == {
        "purpose": "marketing",
        "explicit_opt_in_required": True,
    }
    assert InteractionEvent.query.count() == 0
    assert Notification.query.count() == 0


def test_legacy_registered_event_is_never_reported_as_sent_or_delivered(
    client, app
):
    scope = _seed_campaign_scope()
    campaign_id = str(uuid4())
    db.session.add(
        InteractionEvent(
            tenant_id=scope["tenant"].id,
            contact_id=scope["eligible"].id,
            channel="whatsapp",
            direction="outbound",
            content="legacy registration without provider receipt",
            metadata_payload={
                "event_type": "campaign_send",
                "campaign_id": campaign_id,
                "status": "queued",
                "channel": "whatsapp",
            },
        )
    )
    db.session.commit()
    headers = _auth_headers(
        app, scope["owner"], scope["tenant"].slug, "legacy-history-0001"
    )

    history = client.get(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/history",
        headers=headers,
    )
    metrics = client.get(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/{campaign_id}/metrics",
        headers=headers,
    )
    ledger = client.get(
        f"/api/admin/tenants/{scope['tenant'].slug}/campaigns/ledger",
        headers=headers,
    )

    assert history.status_code == 200
    history_item = history.get_json()["items"][0]
    assert history_item["sent_count"] == 0
    assert history_item["delivered_count"] == 0
    assert history_item["read_count"] == 0
    assert history_item["registered_without_delivery_evidence_count"] == 1

    assert metrics.status_code == 200
    metric_values = metrics.get_json()["metrics"]
    assert metric_values["sent_or_queued"] == 0
    assert metric_values["sent"] == 0
    assert metric_values["delivered"] == 0
    assert metric_values["read"] == 0
    assert metric_values["registered_without_delivery_evidence"] == 1
    assert metric_values["queued"] == 1

    ledger_item = ledger.get_json()["items"][0]
    assert ledger_item["event_type"] == "campaign_registered_legacy"
    assert ledger_item["delivery_evidence"] is False
    assert ledger_item["provider_receipt_present"] is False
