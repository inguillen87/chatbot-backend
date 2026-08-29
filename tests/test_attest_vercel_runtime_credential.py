import json
from datetime import datetime, timezone

import pytest
from flask import Flask

from routes.internal_cutover import internal_cutover_bp
from scripts import attest_vercel_runtime_credential as attestor
from services.provider_cutover_evidence import CutoverEvidenceError, database_identity


ACCOUNT = "AC0123456789abcdef0123456789abcdef"
PROJECT = "prj_0123456789abcdef"
DEPLOYMENT = "dpl_0123456789abcdef"
REVISION = "a" * 40
DATABASE_URL = (
    "postgresql://user:secret@ep-test-pooler.us-east-2.aws.neon.tech/"
    "cutover?sslmode=require"
)
DATABASE_IDENTITY = database_identity(DATABASE_URL)[0]
SIGNING_SECRET = "runtime-signing-secret-0123456789abcdef"
BINDING_SECRET = "credential-binding-secret-0123456789ab"
CREDENTIAL = "twilio-runtime-token-never-rendered"
BEARER = "runtime-attestation-bearer-0123456789"
OBSERVED = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)


def _challenge(**overrides):
    value = {
        "contract_version": attestor.CHALLENGE_CONTRACT,
        "tenant_slug": "junin",
        "tenant_id": 22,
        "environment": "production",
        "vercel_project_id": PROJECT,
        "vercel_deployment_id": DEPLOYMENT,
        "vercel_url": "https://chatboc-backend-test.vercel.app/",
        "destination_deployment_revision": REVISION,
        "database_environment_variable": "DATABASE_URL",
        "database_identity_sha256": DATABASE_IDENTITY,
        "external_account_id": ACCOUNT,
        "account_sid_environment_variable": "JUNIN_TWILIO_ACCOUNT_SID",
        "credential_environment_variable": "JUNIN_TWILIO_AUTH_TOKEN",
        "signing_key_environment_variable": "CUTOVER_RUNTIME_ATTESTATION_HMAC_SECRET",
        "credential_binding_key_environment_variable": "CUTOVER_CREDENTIAL_BINDING_HMAC_SECRET",
        "challenge_nonce": "runtime-nonce-20260829-001",
        "cutover_window_evidence_id": "cutover-window-20260829-001",
        "evidence_id": "vercel-runtime-read-20260829-001",
    }
    value.update(overrides)
    return value


def _environment(**overrides):
    value = {
        "VERCEL": "1",
        "VERCEL_ENV": "production",
        "VERCEL_PROJECT_ID": PROJECT,
        "VERCEL_DEPLOYMENT_ID": DEPLOYMENT,
        "VERCEL_URL": "chatboc-backend-test.vercel.app",
        "VERCEL_GIT_COMMIT_SHA": REVISION,
        "CHATBOC_DEPLOYMENT_REVISION": REVISION,
        "DATABASE_URL": DATABASE_URL,
        "JUNIN_TWILIO_ACCOUNT_SID": ACCOUNT,
        "JUNIN_TWILIO_AUTH_TOKEN": CREDENTIAL,
        "CUTOVER_RUNTIME_ATTESTATION_HMAC_SECRET": SIGNING_SECRET,
        "CUTOVER_CREDENTIAL_BINDING_HMAC_SECRET": BINDING_SECRET,
    }
    value.update(overrides)
    return value


def test_runtime_attestation_is_bound_to_exact_vercel_deployment_and_db():
    envelope = attestor.build_runtime_attestation_envelope(
        _challenge(),
        environ=_environment(),
        observed_at=OBSERVED,
    )
    document = envelope["document"]

    assert document["runtime_platform"] == "vercel"
    assert document["vercel_project_id"] == PROJECT
    assert document["vercel_deployment_id"] == DEPLOYMENT
    assert document["destination_deployment_revision"] == REVISION
    assert document["database_identity_sha256"] == DATABASE_IDENTITY
    assert document["resolved_credential_scope"] == "subaccount"
    assert document["secret_present"] is True
    assert document["secret_value_disclosed"] is False
    assert document["provider_calls_performed"] is False
    rendered = json.dumps(envelope, sort_keys=True)
    assert CREDENTIAL not in rendered
    assert SIGNING_SECRET not in rendered
    assert BINDING_SECRET not in rendered
    assert "secret" not in document["vercel_url"]


@pytest.mark.parametrize(
    ("challenge_overrides", "environment_overrides", "reason"),
    [
        ({"vercel_project_id": "prj_aaaaaaaaaaaaaaaa"}, {}, "runtime_vercel_project_id_mismatch"),
        ({}, {"VERCEL_ENV": "preview"}, "runtime_attestation_not_production"),
        (
            {},
            {"VERCEL_DEPLOYMENT_ID": "dpl_aaaaaaaaaaaaaaaa"},
            "runtime_vercel_deployment_id_mismatch",
        ),
        ({"database_identity_sha256": "d" * 64}, {}, "runtime_database_identity_mismatch"),
        ({}, {"VERCEL_GIT_COMMIT_SHA": "e" * 40}, "runtime_vercel_git_revision_mismatch"),
        (
            {},
            {"CUTOVER_RUNTIME_ATTESTATION_HMAC_SECRET": CREDENTIAL},
            "runtime_attestation_secret_values_must_differ",
        ),
    ],
)
def test_runtime_attestor_fails_closed_on_context_or_secret_alias_drift(
    challenge_overrides,
    environment_overrides,
    reason,
):
    with pytest.raises(CutoverEvidenceError, match=reason):
        attestor.build_runtime_attestation_envelope(
            _challenge(**challenge_overrides),
            environ=_environment(**environment_overrides),
            observed_at=OBSERVED,
        )


def test_runtime_attestation_endpoint_reuses_constant_time_bearer_pattern(monkeypatch):
    for key, value in _environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CUTOVER_RUNTIME_ATTESTATION_BEARER_SECRET", BEARER)
    app = Flask(__name__)
    app.register_blueprint(internal_cutover_bp)
    client = app.test_client()

    unauthorized = client.post(
        "/api/internal/cutover/runtime-credential-attestation",
        json=_challenge(),
    )
    assert unauthorized.status_code == 401
    assert unauthorized.headers["Cache-Control"] == "no-store"

    response = client.post(
        "/api/internal/cutover/runtime-credential-attestation",
        headers={"Authorization": f"Bearer {BEARER}"},
        json=_challenge(),
    )
    assert response.status_code == 200, response.get_json()
    rendered = response.get_data(as_text=True)
    assert CREDENTIAL not in rendered
    assert SIGNING_SECRET not in rendered
    assert BINDING_SECRET not in rendered
    assert BEARER not in rendered


def test_endpoint_blocks_bearer_reuse_as_signing_secret(monkeypatch):
    for key, value in _environment(
        CUTOVER_RUNTIME_ATTESTATION_HMAC_SECRET=BEARER
    ).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CUTOVER_RUNTIME_ATTESTATION_BEARER_SECRET", BEARER)
    app = Flask(__name__)
    app.register_blueprint(internal_cutover_bp)
    response = app.test_client().post(
        "/api/internal/cutover/runtime-credential-attestation",
        headers={"Authorization": f"Bearer {BEARER}"},
        json=_challenge(),
    )
    assert response.status_code == 409
    assert response.get_json()["reason_code"] == (
        "runtime_attestation_bearer_secret_must_be_independent"
    )

