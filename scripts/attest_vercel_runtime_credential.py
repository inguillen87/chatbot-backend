"""Issue a deployment-bound credential attestation from Vercel runtime only.

The attestor performs no provider or database request.  It verifies immutable
Vercel runtime signals, the exact deployment challenge, the target database
fingerprint, and that a tenant-scoped account/credential pair is present.  It
then signs a document containing only metadata and an opaque credential
binding.  Raw secrets are never returned or logged.
"""

from __future__ import annotations

import hmac
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.provider_cutover_evidence import (  # noqa: E402
    CutoverEvidenceError,
    account_sid,
    clean,
    credential_binding,
    database_identity,
    environment_name,
    evidence_id,
    require,
    required_environment_value,
    sha256_value,
    signed_envelope,
)


CONTRACT_VERSION = "chatboc.runtime_credential_attestation.v1"
CHALLENGE_CONTRACT = "chatboc.runtime_credential_attestation_challenge.v1"
TENANT_SLUG = "junin"
_REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$")
_VERCEL_PROJECT_RE = re.compile(r"^prj_[A-Za-z0-9]{8,128}$")
_VERCEL_DEPLOYMENT_RE = re.compile(r"^dpl_[A-Za-z0-9]{8,128}$")
_TENANT_CREDENTIAL_ENV_RE = re.compile(
    r"^(?:JUNIN_TWILIO_AUTH_TOKEN|TWILIO_SUBACCOUNT_AUTH_TOKEN_[A-Z0-9_]+)$"
)
CHALLENGE_KEYS = frozenset(
    {
        "contract_version",
        "tenant_slug",
        "tenant_id",
        "environment",
        "vercel_project_id",
        "vercel_deployment_id",
        "vercel_url",
        "destination_deployment_revision",
        "database_environment_variable",
        "database_identity_sha256",
        "external_account_id",
        "account_sid_environment_variable",
        "credential_environment_variable",
        "signing_key_environment_variable",
        "credential_binding_key_environment_variable",
        "challenge_nonce",
        "cutover_window_evidence_id",
        "evidence_id",
    }
)


def _canonical_vercel_url(value: Any, *, reason_code: str) -> str:
    rendered = clean(value)
    if "://" not in rendered:
        rendered = f"https://{rendered}"
    require(bool(rendered) and len(rendered) <= 300, reason_code)
    try:
        parsed = urlsplit(rendered)
        host = clean(parsed.hostname).lower()
        port = parsed.port
    except ValueError as exc:
        raise CutoverEvidenceError(reason_code) from exc
    require(parsed.scheme.lower() == "https", reason_code)
    require(bool(host) and host.endswith(".vercel.app"), reason_code)
    require(parsed.username is None and parsed.password is None, reason_code)
    require(port in (None, 443) and not parsed.query and not parsed.fragment, reason_code)
    require(parsed.path in ("", "/"), reason_code)
    return urlunsplit(("https", host, "/", "", ""))


def _revision(value: Any, *, reason_code: str) -> str:
    rendered = clean(value).lower()
    require(bool(_REVISION_RE.fullmatch(rendered)), reason_code)
    return rendered


def build_runtime_attestation_envelope(
    challenge: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    runtime = os.environ if environ is None else environ
    require(isinstance(challenge, Mapping), "runtime_attestation_challenge_invalid")
    require(
        frozenset(challenge) == CHALLENGE_KEYS,
        "runtime_attestation_challenge_shape_invalid",
    )
    require(
        challenge.get("contract_version") == CHALLENGE_CONTRACT,
        "runtime_attestation_challenge_contract_mismatch",
    )
    require(challenge.get("tenant_slug") == TENANT_SLUG, "runtime_attestation_tenant_mismatch")
    tenant_id = challenge.get("tenant_id")
    require(
        isinstance(tenant_id, int) and not isinstance(tenant_id, bool) and tenant_id > 0,
        "runtime_attestation_tenant_id_invalid",
    )
    require(challenge.get("environment") == "production", "runtime_attestation_environment_invalid")

    # VERCEL/VERCEL_ENV and the project/deployment/url signals are injected by
    # the platform.  A local process, Preview deployment or a different
    # Production deployment cannot satisfy this exact challenge.
    require(clean(runtime.get("VERCEL")) == "1", "runtime_attestation_not_vercel")
    require(
        clean(runtime.get("VERCEL_ENV")).lower() == "production",
        "runtime_attestation_not_production",
    )
    project_id = clean(runtime.get("VERCEL_PROJECT_ID"))
    deployment_id = clean(runtime.get("VERCEL_DEPLOYMENT_ID"))
    require(bool(_VERCEL_PROJECT_RE.fullmatch(project_id)), "runtime_vercel_project_id_missing")
    require(
        bool(_VERCEL_DEPLOYMENT_RE.fullmatch(deployment_id)),
        "runtime_vercel_deployment_id_missing",
    )
    require(
        clean(challenge.get("vercel_project_id")) == project_id,
        "runtime_vercel_project_id_mismatch",
    )
    require(
        clean(challenge.get("vercel_deployment_id")) == deployment_id,
        "runtime_vercel_deployment_id_mismatch",
    )
    vercel_url = _canonical_vercel_url(
        runtime.get("VERCEL_URL"),
        reason_code="runtime_vercel_url_missing",
    )
    require(
        _canonical_vercel_url(
            challenge.get("vercel_url"),
            reason_code="runtime_challenge_vercel_url_invalid",
        )
        == vercel_url,
        "runtime_vercel_url_mismatch",
    )
    runtime_revision = _revision(
        runtime.get("CHATBOC_DEPLOYMENT_REVISION"),
        reason_code="runtime_deployment_revision_missing",
    )
    require(
        _revision(
            challenge.get("destination_deployment_revision"),
            reason_code="runtime_challenge_revision_invalid",
        )
        == runtime_revision,
        "runtime_deployment_revision_mismatch",
    )
    platform_git_revision = clean(runtime.get("VERCEL_GIT_COMMIT_SHA"))
    if platform_git_revision:
        require(
            _revision(
                platform_git_revision,
                reason_code="runtime_vercel_git_revision_invalid",
            )
            == runtime_revision,
            "runtime_vercel_git_revision_mismatch",
        )

    database_env = environment_name(
        challenge.get("database_environment_variable"),
        reason_code="runtime_database_environment_variable_invalid",
    )
    database_url = required_environment_value(
        runtime,
        database_env,
        missing_reason="runtime_database_url_missing",
    )
    database_fingerprint, _ = database_identity(database_url)
    expected_database = sha256_value(
        challenge.get("database_identity_sha256"),
        reason_code="runtime_challenge_database_identity_invalid",
    )
    require(database_fingerprint == expected_database, "runtime_database_identity_mismatch")

    account_env = environment_name(
        challenge.get("account_sid_environment_variable"),
        reason_code="runtime_account_sid_environment_variable_invalid",
    )
    credential_env = environment_name(
        challenge.get("credential_environment_variable"),
        reason_code="runtime_credential_environment_variable_invalid",
    )
    signing_env = environment_name(
        challenge.get("signing_key_environment_variable"),
        reason_code="runtime_signing_key_environment_variable_invalid",
    )
    binding_env = environment_name(
        challenge.get("credential_binding_key_environment_variable"),
        reason_code="runtime_binding_key_environment_variable_invalid",
    )
    require(
        bool(_TENANT_CREDENTIAL_ENV_RE.fullmatch(credential_env)),
        "runtime_credential_environment_variable_not_tenant_scoped",
    )
    require(
        len({database_env, account_env, credential_env, signing_env, binding_env}) == 5,
        "runtime_attestation_environment_variables_must_differ",
    )
    actual_account = account_sid(
        required_environment_value(
            runtime,
            account_env,
            missing_reason="runtime_account_sid_missing",
        ),
        reason_code="runtime_account_sid_invalid",
    )
    expected_account = account_sid(
        challenge.get("external_account_id"),
        reason_code="runtime_challenge_external_account_invalid",
    )
    require(actual_account == expected_account, "runtime_external_account_mismatch")
    credential = required_environment_value(
        runtime,
        credential_env,
        missing_reason="runtime_credential_missing",
    )
    signing_key = required_environment_value(
        runtime,
        signing_env,
        missing_reason="runtime_attestation_signing_key_missing",
    )
    binding_key = required_environment_value(
        runtime,
        binding_env,
        missing_reason="runtime_credential_binding_key_missing",
    )
    require(
        not hmac.compare_digest(signing_key, credential)
        and not hmac.compare_digest(binding_key, credential)
        and not hmac.compare_digest(signing_key, binding_key),
        "runtime_attestation_secret_values_must_differ",
    )
    binding = credential_binding(
        binding_key=binding_key,
        account_sid_value=actual_account,
        credential_value=credential,
    )

    nonce = evidence_id(
        challenge.get("challenge_nonce"),
        reason_code="runtime_attestation_nonce_invalid",
    )
    window = evidence_id(
        challenge.get("cutover_window_evidence_id"),
        reason_code="runtime_attestation_window_invalid",
    )
    attestation_evidence_id = evidence_id(
        challenge.get("evidence_id"),
        reason_code="runtime_attestation_evidence_id_invalid",
    )
    now = observed_at or datetime.now(timezone.utc)
    require(now.tzinfo is not None, "runtime_attestation_clock_invalid")
    document = {
        "contract_version": CONTRACT_VERSION,
        "source_kind": "destination_runtime_configuration_read",
        "runtime_platform": "vercel",
        "read_only": True,
        "mutations_performed": False,
        "provider_calls_performed": False,
        "evidence_id": attestation_evidence_id,
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "tenant_slug": TENANT_SLUG,
        "tenant_id": int(tenant_id),
        "environment": "production",
        "vercel_project_id": project_id,
        "vercel_deployment_id": deployment_id,
        "vercel_url": vercel_url,
        "destination_deployment_revision": runtime_revision,
        "database_environment_variable": database_env,
        "database_identity_sha256": database_fingerprint,
        "external_account_id": actual_account,
        "account_sid_environment_variable": account_env,
        "credential_environment_variable": credential_env,
        "signing_key_environment_variable": signing_env,
        "credential_binding_key_environment_variable": binding_env,
        "credential_binding_hmac_sha256": binding,
        "resolved_credential_scope": "subaccount",
        "secret_present": True,
        "secret_value_disclosed": False,
        "challenge_nonce": nonce,
        "cutover_window_evidence_id": window,
    }
    return signed_envelope(document, signing_key=signing_key)


__all__ = [
    "CHALLENGE_CONTRACT",
    "CHALLENGE_KEYS",
    "CONTRACT_VERSION",
    "build_runtime_attestation_envelope",
]

