import copy
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from models import ProviderConnection, ProviderSender, TenantProfile, db
from scripts import promote_tenant_provider_connection as promote


ACCOUNT_SID = "AC0123456789abcdef0123456789abcdef"
SENDER_SID = "XE0123456789abcdef0123456789abcdef"
SERVICE_SID = "MG0123456789abcdef0123456789abcdef"
CREDENTIAL_ENV = "JUNIN_TWILIO_AUTH_TOKEN"
ACCOUNT_ENV = "JUNIN_TWILIO_ACCOUNT_SID"
BINDING_HMAC_ENV = "CUTOVER_CREDENTIAL_BINDING_HMAC_SECRET"
DATABASE_ENV = "DATABASE_URL"
SNAPSHOT_ENV = "JUNIN_PROVIDER_READ_ONLY_SNAPSHOT"
ATTESTATION_ENV = "JUNIN_RUNTIME_CREDENTIAL_ATTESTATION"
SNAPSHOT_HMAC_ENV = "CUTOVER_PROVIDER_SNAPSHOT_HMAC_SECRET"
ATTESTATION_HMAC_ENV = "CUTOVER_RUNTIME_ATTESTATION_HMAC_SECRET"
WEBHOOK_ENV = "JUNIN_EXPECTED_WHATSAPP_WEBHOOK_URL"
CALLBACK_ENV = "JUNIN_EXPECTED_WHATSAPP_STATUS_CALLBACK_URL"
SNAPSHOT_HMAC_SECRET = "test-only-snapshot-signing-secret-0123456789"
ATTESTATION_HMAC_SECRET = "test-only-attestation-signing-secret-01234567"
WEBHOOK = "https://candidate.example.test/api/webhooks/twilio/whatsapp"
CALLBACK = "https://candidate.example.test/api/webhooks/twilio/status"
REVISION = "a" * 40
DATABASE_IDENTITY = "b" * 64
BINDING_DIGEST = "c" * 64
PROJECT_ID = "prj_0123456789abcdef"
DEPLOYMENT_ID = "dpl_0123456789abcdef"
DEPLOYMENT_URL = "https://chatboc-backend-test.vercel.app/"
ATTESTATION_NONCE = "runtime-nonce-20260829-001"
WINDOW_EVIDENCE = "cutover-window-20260829-001"
NOW = datetime(2026, 8, 29, 12, 5, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _envelope(document, secret):
    encoded = _canonical_json(document)
    return {
        "document": document,
        "document_sha256": hashlib.sha256(encoded).hexdigest(),
        "signature_hmac_sha256": hmac.new(
            secret.encode("utf-8"),
            encoded,
            hashlib.sha256,
        ).hexdigest(),
    }


def _snapshot(base_tenant_id, **overrides):
    document = {
        "contract_version": promote.SNAPSHOT_CONTRACT,
        "source_kind": "provider_api_read",
        "read_only": True,
        "mutations_performed": False,
        "messages_sent": False,
        "evidence_id": "twilio-provider-read-20260829-001",
        "observed_at": OBSERVED.isoformat(),
        "tenant_slug": "junin",
        "tenant_id": base_tenant_id,
        "provider": "twilio",
        "channel": "whatsapp",
        "environment": "production",
        "resource_count": 1,
        "account_sid": ACCOUNT_SID,
        "credential_account_sid": ACCOUNT_SID,
        "credential_environment_variable": CREDENTIAL_ENV,
        "signing_key_environment_variable": SNAPSHOT_HMAC_ENV,
        "credential_binding_key_environment_variable": BINDING_HMAC_ENV,
        "credential_binding_hmac_sha256": BINDING_DIGEST,
        "sender_count": 1,
        "sender_sid": SENDER_SID,
        "messaging_service_sid": SERVICE_SID,
        "phone_number": promote.OFFICIAL_PHONE,
        "sender_status": "ONLINE",
        "webhook_url": WEBHOOK,
        "status_callback_url": CALLBACK,
        "destination_project_id": PROJECT_ID,
        "destination_deployment_id": DEPLOYMENT_ID,
        "destination_deployment_revision": REVISION,
        "database_identity_sha256": DATABASE_IDENTITY,
        "challenge_nonce": ATTESTATION_NONCE,
        "cutover_window_evidence_id": WINDOW_EVIDENCE,
    }
    document.update(overrides)
    return document


def _attestation(base_tenant_id, **overrides):
    document = {
        "contract_version": promote.ATTESTATION_CONTRACT,
        "source_kind": "destination_runtime_configuration_read",
        "runtime_platform": "vercel",
        "read_only": True,
        "mutations_performed": False,
        "provider_calls_performed": False,
        "evidence_id": "vercel-runtime-read-20260829-001",
        "observed_at": OBSERVED.isoformat(),
        "tenant_slug": "junin",
        "tenant_id": base_tenant_id,
        "environment": "production",
        "vercel_project_id": PROJECT_ID,
        "vercel_deployment_id": DEPLOYMENT_ID,
        "vercel_url": DEPLOYMENT_URL,
        "destination_deployment_revision": REVISION,
        "database_environment_variable": DATABASE_ENV,
        "database_identity_sha256": DATABASE_IDENTITY,
        "external_account_id": ACCOUNT_SID,
        "account_sid_environment_variable": ACCOUNT_ENV,
        "credential_environment_variable": CREDENTIAL_ENV,
        "signing_key_environment_variable": ATTESTATION_HMAC_ENV,
        "credential_binding_key_environment_variable": BINDING_HMAC_ENV,
        "credential_binding_hmac_sha256": BINDING_DIGEST,
        "resolved_credential_scope": "subaccount",
        "secret_present": True,
        "secret_value_disclosed": False,
        "challenge_nonce": ATTESTATION_NONCE,
        "cutover_window_evidence_id": WINDOW_EVIDENCE,
    }
    document.update(overrides)
    return document


def _seed(owner_user):
    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status=promote.PENDING_STATUS,
        external_account_id=ACCOUNT_SID,
        credentials_ref=f"env:{CREDENTIAL_ENV}",
        config={
            promote.MANAGEMENT_MARKER: {
                "contract_version": (
                    "chatboc.tenant_provider_connection_reconciliation.v1"
                ),
                "enabled": True,
                "promotion_required": True,
            }
        },
    )
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number=promote.OFFICIAL_PHONE,
        sender_id=f"whatsapp:{promote.OFFICIAL_PHONE}",
        status="registered",
    )
    db.session.add(sender)
    db.session.commit()
    return tenant, connection, sender


def _request(
    tenant_id,
    *,
    snapshot=None,
    attestation=None,
    window=WINDOW_EVIDENCE,
    maximum_evidence_age_seconds=900,
):
    snapshot_document = snapshot or _snapshot(
        tenant_id,
        cutover_window_evidence_id=window,
    )
    attestation_document = attestation or _attestation(
        tenant_id,
        cutover_window_evidence_id=window,
    )
    environ = {
        SNAPSHOT_ENV: json.dumps(
            _envelope(snapshot_document, SNAPSHOT_HMAC_SECRET)
        ),
        ATTESTATION_ENV: json.dumps(
            _envelope(attestation_document, ATTESTATION_HMAC_SECRET)
        ),
        SNAPSHOT_HMAC_ENV: SNAPSHOT_HMAC_SECRET,
        ATTESTATION_HMAC_ENV: ATTESTATION_HMAC_SECRET,
        WEBHOOK_ENV: WEBHOOK,
        CALLBACK_ENV: CALLBACK,
        # This value must remain unread by the promotion module.
        CREDENTIAL_ENV: "twilio-token-must-not-be-read-or-rendered",
    }
    return promote.build_request_from_environment(
        database_identity_sha256=DATABASE_IDENTITY,
        snapshot_environment_variable=SNAPSHOT_ENV,
        credential_attestation_environment_variable=ATTESTATION_ENV,
        snapshot_hmac_secret_environment_variable=SNAPSHOT_HMAC_ENV,
        attestation_hmac_secret_environment_variable=ATTESTATION_HMAC_ENV,
        webhook_environment_variable=WEBHOOK_ENV,
        callback_environment_variable=CALLBACK_ENV,
        destination_project_id=PROJECT_ID,
        destination_deployment_id=DEPLOYMENT_ID,
        destination_deployment_url=DEPLOYMENT_URL,
        destination_deployment_revision=REVISION,
        runtime_attestation_nonce=ATTESTATION_NONCE,
        cutover_window_evidence_id=window,
        maximum_evidence_age_seconds=maximum_evidence_age_seconds,
        environ=environ,
    )


def test_canonical_signed_evidence_plans_promotion_without_secret_or_payload(
    client,
    owner_user,
):
    tenant, connection, sender = _seed(owner_user)
    request = _request(tenant.id)

    result = promote.promote_tenant_provider_connection(
        db.session,
        request,
        now=NOW,
    )

    assert result["status"] == "dry_run"
    assert result["writes_performed"] is False
    assert result["plan"]["proposed"]["connection_action"] == "promote"
    assert result["plan"]["proposed"]["connection_status"] == "online"
    assert result["plan"]["proposed"]["sender_action"] == "update"
    assert result["plan"]["evidence_binding"]["provider_snapshot_sha256"]
    db.session.refresh(connection)
    db.session.refresh(sender)
    assert connection.status == promote.PENDING_STATUS
    assert sender.status == "registered"

    rendered = json.dumps(result, sort_keys=True)
    assert ACCOUNT_SID not in rendered
    assert SENDER_SID not in rendered
    assert SERVICE_SID not in rendered
    assert promote.OFFICIAL_PHONE not in rendered
    assert SNAPSHOT_HMAC_SECRET not in rendered
    assert ATTESTATION_HMAC_SECRET not in rendered
    assert "twilio-token-must-not-be-read-or-rendered" not in rendered
    assert WEBHOOK not in rendered
    assert CALLBACK not in rendered


def test_arbitrary_mutated_evidence_id_fails_signature_authentication(
    client,
    owner_user,
):
    tenant, _, _ = _seed(owner_user)
    snapshot_envelope = _envelope(_snapshot(tenant.id), SNAPSHOT_HMAC_SECRET)
    snapshot_envelope["document"]["evidence_id"] = "arbitrary-evidence-id-999"
    environ = {
        SNAPSHOT_ENV: json.dumps(snapshot_envelope),
        ATTESTATION_ENV: json.dumps(
            _envelope(_attestation(tenant.id), ATTESTATION_HMAC_SECRET)
        ),
        SNAPSHOT_HMAC_ENV: SNAPSHOT_HMAC_SECRET,
        ATTESTATION_HMAC_ENV: ATTESTATION_HMAC_SECRET,
        WEBHOOK_ENV: WEBHOOK,
        CALLBACK_ENV: CALLBACK,
    }

    with pytest.raises(
        promote.ProviderConnectionPromotionError,
        match="provider_snapshot_digest_mismatch",
    ):
        promote.build_request_from_environment(
            database_identity_sha256=DATABASE_IDENTITY,
            snapshot_environment_variable=SNAPSHOT_ENV,
            credential_attestation_environment_variable=ATTESTATION_ENV,
            snapshot_hmac_secret_environment_variable=SNAPSHOT_HMAC_ENV,
            attestation_hmac_secret_environment_variable=ATTESTATION_HMAC_ENV,
            webhook_environment_variable=WEBHOOK_ENV,
            callback_environment_variable=CALLBACK_ENV,
            destination_project_id=PROJECT_ID,
            destination_deployment_id=DEPLOYMENT_ID,
            destination_deployment_url=DEPLOYMENT_URL,
            destination_deployment_revision=REVISION,
            runtime_attestation_nonce=ATTESTATION_NONCE,
            cutover_window_evidence_id=WINDOW_EVIDENCE,
            environ=environ,
        )


@pytest.mark.parametrize(
    ("snapshot_overrides", "attestation_overrides", "reason"),
    [
        (
            {"observed_at": (OBSERVED - timedelta(hours=2)).isoformat()},
            {},
            "evidence_stale",
        ),
        (
            {"account_sid": "ACabcdef0123456789abcdef0123456789"},
            {},
            "provider_snapshot_account_mismatch",
        ),
        ({"tenant_id": 999999}, {}, "provider_snapshot_tenant_id_mismatch"),
        (
            {"destination_deployment_revision": "c" * 40},
            {},
            "provider_snapshot_revision_mismatch",
        ),
        ({"sender_count": 2}, {}, "provider_snapshot_sender_count_mismatch"),
        (
            {"status_callback_url": "https://wrong.example.test/status"},
            {},
            "provider_snapshot_callback_mismatch",
        ),
        (
            {},
            {"destination_deployment_revision": "d" * 40},
            "credential_attestation_revision_mismatch",
        ),
        (
            {"destination_project_id": "prj_aaaaaaaaaaaaaaaa"},
            {},
            "provider_snapshot_project_mismatch",
        ),
        (
            {},
            {"vercel_deployment_id": "dpl_aaaaaaaaaaaaaaaa"},
            "credential_attestation_deployment_mismatch",
        ),
        (
            {"database_identity_sha256": "d" * 64},
            {},
            "provider_snapshot_database_identity_mismatch",
        ),
        (
            {},
            {"challenge_nonce": "runtime-nonce-20260829-999"},
            "credential_attestation_nonce_mismatch",
        ),
        (
            {},
            {"credential_binding_hmac_sha256": "d" * 64},
            "credential_binding_mismatch",
        ),
        (
            {},
            {"secret_value_disclosed": True},
            "credential_attestation_secret_value_disclosed",
        ),
    ],
)
def test_mismatched_or_stale_evidence_blocks_promotion(
    client,
    owner_user,
    snapshot_overrides,
    attestation_overrides,
    reason,
):
    tenant, connection, sender = _seed(owner_user)
    request = _request(
        tenant.id,
        snapshot=_snapshot(tenant.id, **snapshot_overrides),
        attestation=_attestation(tenant.id, **attestation_overrides),
    )

    with pytest.raises(promote.ProviderConnectionPromotionError, match=reason):
        promote.promote_tenant_provider_connection(
            db.session,
            request,
            now=NOW,
        )

    db.session.refresh(connection)
    db.session.refresh(sender)
    assert connection.status == promote.PENDING_STATUS
    assert sender.status == "registered"


def test_changing_signed_evidence_or_window_changes_plan_sha(
    client,
    owner_user,
):
    tenant, _, _ = _seed(owner_user)
    first = promote.promote_tenant_provider_connection(
        db.session,
        _request(tenant.id),
        now=NOW,
    )
    changed_evidence = promote.promote_tenant_provider_connection(
        db.session,
        _request(
            tenant.id,
            snapshot=_snapshot(
                tenant.id,
                evidence_id="twilio-provider-read-20260829-002",
            ),
        ),
        now=NOW,
    )
    changed_window = promote.promote_tenant_provider_connection(
        db.session,
        _request(tenant.id, window="cutover-window-20260829-002"),
        now=NOW,
    )
    changed_maximum_age = promote.promote_tenant_provider_connection(
        db.session,
        _request(tenant.id, maximum_evidence_age_seconds=600),
        now=NOW,
    )

    assert first["plan_sha256"] != changed_evidence["plan_sha256"]
    assert first["plan_sha256"] != changed_window["plan_sha256"]
    assert first["plan_sha256"] != changed_maximum_age["plan_sha256"]


@pytest.mark.parametrize("sender_status", ["registered", "ready", "offline", "online", "ONLINE "])
def test_provider_snapshot_requires_canonical_online_sender_status(
    client,
    owner_user,
    sender_status,
):
    tenant, _, _ = _seed(owner_user)
    with pytest.raises(
        promote.ProviderConnectionPromotionError,
        match="provider_snapshot_sender_status_must_be_online",
    ):
        promote.promote_tenant_provider_connection(
            db.session,
            _request(
                tenant.id,
                snapshot=_snapshot(tenant.id, sender_status=sender_status),
            ),
            now=NOW,
        )


def test_snapshot_and_attestation_signing_keys_are_independent_and_not_credentials(
    client,
    owner_user,
):
    tenant, _, _ = _seed(owner_user)
    common = dict(
        database_identity_sha256=DATABASE_IDENTITY,
        snapshot_environment_variable=SNAPSHOT_ENV,
        credential_attestation_environment_variable=ATTESTATION_ENV,
        webhook_environment_variable=WEBHOOK_ENV,
        callback_environment_variable=CALLBACK_ENV,
        destination_project_id=PROJECT_ID,
        destination_deployment_id=DEPLOYMENT_ID,
        destination_deployment_url=DEPLOYMENT_URL,
        destination_deployment_revision=REVISION,
        runtime_attestation_nonce=ATTESTATION_NONCE,
        cutover_window_evidence_id=WINDOW_EVIDENCE,
    )
    environ = {
        SNAPSHOT_ENV: json.dumps(_envelope(_snapshot(tenant.id), SNAPSHOT_HMAC_SECRET)),
        ATTESTATION_ENV: json.dumps(_envelope(_attestation(tenant.id), SNAPSHOT_HMAC_SECRET)),
        SNAPSHOT_HMAC_ENV: SNAPSHOT_HMAC_SECRET,
        WEBHOOK_ENV: WEBHOOK,
        CALLBACK_ENV: CALLBACK,
    }
    with pytest.raises(promote.ProviderConnectionPromotionError):
        promote.build_request_from_environment(
            **common,
            snapshot_hmac_secret_environment_variable=SNAPSHOT_HMAC_ENV,
            attestation_hmac_secret_environment_variable=SNAPSHOT_HMAC_ENV,
            environ=environ,
        )

    with pytest.raises(promote.ProviderConnectionPromotionError):
        promote.build_request_from_environment(
            **common,
            snapshot_hmac_secret_environment_variable=CREDENTIAL_ENV,
            attestation_hmac_secret_environment_variable=ATTESTATION_HMAC_ENV,
            environ={**environ, CREDENTIAL_ENV: SNAPSHOT_HMAC_SECRET},
        )


def test_distinct_signing_env_names_cannot_hide_reused_secret_value(
    client,
    owner_user,
):
    tenant, _, _ = _seed(owner_user)
    shared = SNAPSHOT_HMAC_SECRET
    environ = {
        SNAPSHOT_ENV: json.dumps(_envelope(_snapshot(tenant.id), shared)),
        ATTESTATION_ENV: json.dumps(_envelope(_attestation(tenant.id), shared)),
        SNAPSHOT_HMAC_ENV: shared,
        ATTESTATION_HMAC_ENV: shared,
        WEBHOOK_ENV: WEBHOOK,
        CALLBACK_ENV: CALLBACK,
    }
    with pytest.raises(
        promote.ProviderConnectionPromotionError,
        match="evidence_hmac_secret_values_must_differ",
    ):
        promote.build_request_from_environment(
            database_identity_sha256=DATABASE_IDENTITY,
            snapshot_environment_variable=SNAPSHOT_ENV,
            credential_attestation_environment_variable=ATTESTATION_ENV,
            snapshot_hmac_secret_environment_variable=SNAPSHOT_HMAC_ENV,
            attestation_hmac_secret_environment_variable=ATTESTATION_HMAC_ENV,
            webhook_environment_variable=WEBHOOK_ENV,
            callback_environment_variable=CALLBACK_ENV,
            destination_project_id=PROJECT_ID,
            destination_deployment_id=DEPLOYMENT_ID,
            destination_deployment_url=DEPLOYMENT_URL,
            destination_deployment_revision=REVISION,
            runtime_attestation_nonce=ATTESTATION_NONCE,
            cutover_window_evidence_id=WINDOW_EVIDENCE,
            environ=environ,
        )


def test_signing_secret_value_cannot_equal_available_runtime_credential(
    client,
    owner_user,
):
    tenant, _, _ = _seed(owner_user)
    environ = {
        SNAPSHOT_ENV: json.dumps(
            _envelope(_snapshot(tenant.id), SNAPSHOT_HMAC_SECRET)
        ),
        ATTESTATION_ENV: json.dumps(
            _envelope(_attestation(tenant.id), ATTESTATION_HMAC_SECRET)
        ),
        SNAPSHOT_HMAC_ENV: SNAPSHOT_HMAC_SECRET,
        ATTESTATION_HMAC_ENV: ATTESTATION_HMAC_SECRET,
        CREDENTIAL_ENV: SNAPSHOT_HMAC_SECRET,
        WEBHOOK_ENV: WEBHOOK,
        CALLBACK_ENV: CALLBACK,
    }
    with pytest.raises(
        promote.ProviderConnectionPromotionError,
        match="evidence_hmac_secret_value_must_differ_from_runtime_credential",
    ):
        promote.build_request_from_environment(
            database_identity_sha256=DATABASE_IDENTITY,
            snapshot_environment_variable=SNAPSHOT_ENV,
            credential_attestation_environment_variable=ATTESTATION_ENV,
            snapshot_hmac_secret_environment_variable=SNAPSHOT_HMAC_ENV,
            attestation_hmac_secret_environment_variable=ATTESTATION_HMAC_ENV,
            webhook_environment_variable=WEBHOOK_ENV,
            callback_environment_variable=CALLBACK_ENV,
            destination_project_id=PROJECT_ID,
            destination_deployment_id=DEPLOYMENT_ID,
            destination_deployment_url=DEPLOYMENT_URL,
            destination_deployment_revision=REVISION,
            runtime_attestation_nonce=ATTESTATION_NONCE,
            cutover_window_evidence_id=WINDOW_EVIDENCE,
            environ=environ,
        )


def test_exact_apply_promotes_and_is_idempotent(
    client,
    owner_user,
    monkeypatch,
):
    tenant, connection, sender = _seed(owner_user)
    request = _request(tenant.id)
    plan = promote.promote_tenant_provider_connection(
        db.session,
        request,
        now=NOW,
    )
    monkeypatch.setattr(promote, "acquire_postgres_advisory_lock", lambda *_: None)

    applied = promote.promote_tenant_provider_connection(
        db.session,
        request,
        apply=True,
        approved_plan_sha256=plan["plan_sha256"],
        now=NOW,
    )

    assert applied["status"] == "promoted"
    assert applied["writes_performed"] is True
    db.session.refresh(connection)
    db.session.refresh(sender)
    assert connection.status == "online"
    assert sender.status == "online"
    assert sender.sender_sid == SENDER_SID
    assert sender.messaging_service_sid == SERVICE_SID
    assert sender.webhook_url == WEBHOOK
    assert sender.status_callback_url == CALLBACK

    second_plan = promote.promote_tenant_provider_connection(
        db.session,
        request,
        now=NOW,
    )
    second = promote.promote_tenant_provider_connection(
        db.session,
        request,
        apply=True,
        approved_plan_sha256=second_plan["plan_sha256"],
        now=NOW,
    )
    assert second["status"] == "already_promoted"
    assert second["writes_performed"] is False


def test_changed_evidence_invalidates_previously_approved_plan(
    client,
    owner_user,
    monkeypatch,
):
    tenant, connection, sender = _seed(owner_user)
    original_request = _request(tenant.id)
    original_plan = promote.promote_tenant_provider_connection(
        db.session,
        original_request,
        now=NOW,
    )
    changed_request = _request(
        tenant.id,
        snapshot=_snapshot(
            tenant.id,
            evidence_id="twilio-provider-read-20260829-002",
        ),
    )
    monkeypatch.setattr(promote, "acquire_postgres_advisory_lock", lambda *_: None)

    with pytest.raises(
        promote.ProviderConnectionPromotionError,
        match="approved_plan_sha256_mismatch",
    ):
        promote.promote_tenant_provider_connection(
            db.session,
            changed_request,
            apply=True,
            approved_plan_sha256=original_plan["plan_sha256"],
            now=NOW,
        )

    db.session.refresh(connection)
    db.session.refresh(sender)
    assert connection.status == promote.PENDING_STATUS
    assert sender.status == "registered"


def test_provider_snapshot_sender_resource_must_match_database(
    client,
    owner_user,
):
    tenant, connection, sender = _seed(owner_user)
    sender.sender_sid = "XEabcdef0123456789abcdef0123456789"
    db.session.commit()

    with pytest.raises(
        promote.ProviderConnectionPromotionError,
        match="provider_sender_sid_mismatch",
    ):
        promote.promote_tenant_provider_connection(
            db.session,
            _request(tenant.id),
            now=NOW,
        )

    db.session.refresh(connection)
    assert connection.status == promote.PENDING_STATUS
