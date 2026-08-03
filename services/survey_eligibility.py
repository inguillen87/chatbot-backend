"""Opaque eligibility grants for controlled surveys and internal votes.

This service deliberately provides pseudonymous *internal* linkability.  It is
not a blind-credential system, ballot secrecy proof or electoral certification.
Raw subject identifiers and plaintext credentials are never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Mapping

from flask import current_app
from sqlalchemy.exc import IntegrityError

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except Exception:  # pragma: no cover - exercised through the fail-closed gate
    hashes = None
    HKDF = None

from database import db
from models import AuditEvent, EncEncuesta, EncRespuesta
from models_survey_eligibility import (
    SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
    SURVEY_ELIGIBILITY_KEY_VERSION,
    SURVEY_ELIGIBILITY_MODES,
    SURVEY_ELIGIBILITY_REDEMPTION_CONTRACT_VERSION,
    SURVEY_ELIGIBILITY_REVOCATION_REASONS,
    SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION,
    SurveyEligibilityGrant,
    SurveyEligibilityTerminal,
)
from models_survey_governance import SurveyGovernanceRelease


SURVEY_ELIGIBILITY_CREDENTIAL_HEADER = "X-Survey-Eligibility-Credential"
# Compatibility alias for the integration layer while the v1 contract lands.
ELIGIBILITY_CREDENTIAL_HEADER = SURVEY_ELIGIBILITY_CREDENTIAL_HEADER
ELIGIBILITY_DECISION_VERIFIED = "verified_by_opaque_grant"
ELIGIBILITY_PRIVACY_ASSURANCE = "pseudonymous_internal_linkability"

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,63}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_OPAQUE_REVIEW_RE = re.compile(
    r"^[a-z][a-z0-9_.-]{1,31}:[A-Za-z][A-Za-z0-9_.:-]{7,127}$"
)
_GRANT_REF_RE = re.compile(r"^seg1_[A-Za-z0-9_-]{43}$")
_CREDENTIAL_RE = re.compile(r"^sec1_[A-Za-z0-9_-]{43}$")
_SUBJECT_REF_RE = re.compile(r"^subj_[A-Za-z0-9_-]{43}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_RESTRICTED_MODES = frozenset(SURVEY_ELIGIBILITY_MODES)
_ATTESTATION_ONLY_MODES = frozenset({"open", "self_attested"})
_REISSUABLE_REVOCATION_REASONS = frozenset(
    {"administrative_revocation", "credential_compromised", "other_reviewed"}
)
_MAX_EXPIRY_DAYS = 366


class SurveyEligibilityError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        reason_code: str = "survey_eligibility_invalid",
        action_hint: str = "review_eligibility_request",
        retryable: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = int(status_code)
        self.reason_code = reason_code
        self.action_hint = action_hint
        self.retryable = bool(retryable)
        self.extra = dict(extra or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.message,
            "message": self.message,
            "contract_version": SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION,
            "reason_code": self.reason_code,
            "action_hint": self.action_hint,
            "retryable": self.retryable,
            **self.extra,
        }


@dataclass(frozen=True)
class SurveyEligibilityGate:
    tenant_id: int
    feature_enabled: bool
    tenant_allowed: bool
    ready: bool
    reason_code: str | None
    key_version: str = SURVEY_ELIGIBILITY_KEY_VERSION
    secret: bytes | None = field(default=None, repr=False)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _config_value(config: Mapping[str, Any] | Any, key: str, default: Any) -> Any:
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    return getter(key, default)


def _parse_tenant_allowlist(raw: Any) -> frozenset[int] | None:
    tokens = [token.strip() for token in str(raw or "").split(",") if token.strip()]
    if not tokens:
        return frozenset()
    values: set[int] = set()
    for token in tokens:
        try:
            tenant_id = int(token)
        except (TypeError, ValueError, OverflowError):
            return None
        if tenant_id <= 0 or str(tenant_id) != token:
            return None
        values.add(tenant_id)
    return frozenset(values)


def resolve_eligibility_gate(
    config: Mapping[str, Any] | Any,
    *,
    tenant_id: int,
) -> SurveyEligibilityGate:
    """Resolve a strict flag + canary + secret gate without leaking its secret."""

    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError, OverflowError):
        normalized_tenant_id = 0
    if normalized_tenant_id <= 0:
        return SurveyEligibilityGate(
            tenant_id=normalized_tenant_id,
            feature_enabled=False,
            tenant_allowed=False,
            ready=False,
            reason_code="survey_eligibility_tenant_invalid",
        )

    enabled = (
        _config_value(config, "ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1", False) is True
    )
    if not enabled:
        return SurveyEligibilityGate(
            tenant_id=normalized_tenant_id,
            feature_enabled=False,
            tenant_allowed=False,
            ready=False,
            reason_code="survey_eligibility_feature_disabled",
        )

    allowlist = _parse_tenant_allowlist(
        _config_value(config, "SURVEY_ELIGIBILITY_GRANT_TENANT_IDS", "")
    )
    if allowlist is None:
        return SurveyEligibilityGate(
            tenant_id=normalized_tenant_id,
            feature_enabled=True,
            tenant_allowed=False,
            ready=False,
            reason_code="survey_eligibility_allowlist_invalid",
        )
    if normalized_tenant_id not in allowlist:
        return SurveyEligibilityGate(
            tenant_id=normalized_tenant_id,
            feature_enabled=True,
            tenant_allowed=False,
            ready=False,
            reason_code="survey_eligibility_tenant_not_allowlisted",
        )

    secret = str(_config_value(config, "SURVEY_ELIGIBILITY_SECRET_V1", "") or "").encode(
        "utf-8"
    )
    if len(secret) < 32:
        return SurveyEligibilityGate(
            tenant_id=normalized_tenant_id,
            feature_enabled=True,
            tenant_allowed=True,
            ready=False,
            reason_code="survey_eligibility_secret_invalid",
        )
    if HKDF is None or hashes is None:
        return SurveyEligibilityGate(
            tenant_id=normalized_tenant_id,
            feature_enabled=True,
            tenant_allowed=True,
            ready=False,
            reason_code="survey_eligibility_cryptography_unavailable",
        )
    return SurveyEligibilityGate(
        tenant_id=normalized_tenant_id,
        feature_enabled=True,
        tenant_allowed=True,
        ready=True,
        reason_code=None,
        secret=secret,
    )


def require_eligibility_gate(
    config: Mapping[str, Any] | Any,
    *,
    tenant_id: int,
) -> SurveyEligibilityGate:
    gate = resolve_eligibility_gate(config, tenant_id=tenant_id)
    if gate.ready:
        return gate
    hidden = gate.reason_code in {
        "survey_eligibility_feature_disabled",
        "survey_eligibility_tenant_not_allowlisted",
    }
    raise SurveyEligibilityError(
        "La elegibilidad opaca no esta disponible para este tenant.",
        status_code=404 if hidden else 503,
        reason_code=(
            "survey_eligibility_not_available"
            if hidden
            else "survey_eligibility_configuration_unavailable"
        ),
        action_hint=("feature_not_available" if hidden else "contact_support"),
    )


def _derive_subkey(
    secret: bytes,
    *,
    purpose: str,
    tenant_id: int,
    key_version: str,
) -> bytes:
    if HKDF is None or hashes is None:
        raise SurveyEligibilityError(
            "El runtime criptografico de elegibilidad no esta disponible.",
            status_code=503,
            reason_code="survey_eligibility_configuration_unavailable",
            action_hint="contact_support",
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(b"chatboc:survey-eligibility:hkdf-salt:v1").digest(),
        info=(
            "chatboc:survey-eligibility:"
            f"tenant:{int(tenant_id)}:{purpose}:key:{key_version}:v1"
        ).encode("ascii"),
    ).derive(secret)


def _hmac_hex(key: bytes, value: Any) -> str:
    return hmac.new(key, _canonical_json(value).encode("utf-8"), hashlib.sha256).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _normalize_namespace(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _NAMESPACE_RE.fullmatch(normalized):
        raise SurveyEligibilityError(
            f"{field_name} debe ser un namespace opaco valido.",
            status_code=422,
            reason_code="survey_eligibility_namespace_invalid",
            action_hint="provide_namespaced_authority",
            extra={"field": field_name},
        )
    return normalized


def _normalize_version(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _VERSION_RE.fullmatch(normalized):
        raise SurveyEligibilityError(
            f"{field_name} debe ser una version opaca valida.",
            status_code=422,
            reason_code="survey_eligibility_version_invalid",
            action_hint="provide_versioned_authority_adapter",
            extra={"field": field_name},
        )
    return normalized


def _normalize_subject(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _SUBJECT_REF_RE.fullmatch(normalized):
        raise SurveyEligibilityError(
            "subject_ref debe ser una referencia opaca canonica.",
            status_code=422,
            reason_code="survey_eligibility_subject_opaque_ref_required",
            action_hint="resolve_subject_through_trusted_authority_adapter",
        )
    return normalized


def _normalize_review_reference(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _OPAQUE_REVIEW_RE.fullmatch(normalized):
        raise SurveyEligibilityError(
            "review_reference debe ser una referencia opaca namespaced.",
            status_code=422,
            reason_code="survey_eligibility_review_reference_invalid",
            action_hint="provide_opaque_review_reference",
        )
    return normalized


def _review_reference_hmac(
    secret: bytes,
    *,
    tenant_id: int,
    survey_id: int,
    release_id: int,
    policy_version: str,
    authority_namespace: str,
    authority_adapter_version: str,
    review_reference: str,
) -> str:
    """Bind opaque review evidence without a dictionary-guessable plain hash."""

    return _hmac_hex(
        _derive_subkey(
            secret,
            purpose="review-reference-hmac",
            tenant_id=tenant_id,
            key_version=SURVEY_ELIGIBILITY_KEY_VERSION,
        ),
        {
            "contract": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
            "tenant_id": int(tenant_id),
            "survey_id": int(survey_id),
            "release_id": int(release_id),
            "policy_version": policy_version,
            "authority_namespace": authority_namespace,
            "authority_adapter_version": authority_adapter_version,
            "review_reference": review_reference,
        },
    )


def _validate_idempotency_key(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(normalized):
        raise SurveyEligibilityError(
            "Idempotency-Key es obligatorio y debe ser seguro.",
            status_code=400,
            reason_code="survey_eligibility_idempotency_key_invalid",
            action_hint="send_valid_idempotency_key",
        )
    return normalized


def _release_eligibility_policy(release: SurveyGovernanceRelease) -> dict[str, Any]:
    raw_snapshot = str(release.snapshot_json or "")
    actual_snapshot_hash = hashlib.sha256(raw_snapshot.encode("utf-8")).hexdigest()
    expected_snapshot_hash = str(release.snapshot_sha256 or "")
    if not _HASH_RE.fullmatch(expected_snapshot_hash) or not hmac.compare_digest(
        actual_snapshot_hash, expected_snapshot_hash
    ):
        raise SurveyEligibilityError(
            "La integridad del snapshot de elegibilidad no coincide.",
            status_code=500,
            reason_code="survey_eligibility_snapshot_corrupt",
            action_hint="contact_support",
        )
    try:
        snapshot = json.loads(raw_snapshot)
    except (TypeError, ValueError) as exc:
        raise SurveyEligibilityError(
            "El snapshot de elegibilidad esta corrupto.",
            status_code=500,
            reason_code="survey_eligibility_snapshot_corrupt",
            action_hint="contact_support",
        ) from exc
    if _canonical_json(snapshot) != raw_snapshot:
        raise SurveyEligibilityError(
            "El snapshot de elegibilidad no es canonico.",
            status_code=500,
            reason_code="survey_eligibility_snapshot_corrupt",
            action_hint="contact_support",
        )
    governance = snapshot.get("governance") if isinstance(snapshot, Mapping) else None
    eligibility = governance.get("eligibility") if isinstance(governance, Mapping) else None
    if not isinstance(eligibility, Mapping):
        raise SurveyEligibilityError(
            "El release no contiene una politica de elegibilidad valida.",
            status_code=500,
            reason_code="survey_eligibility_snapshot_corrupt",
            action_hint="contact_support",
        )
    mode = str(eligibility.get("mode") or "").strip().lower()
    version = str(eligibility.get("policy_version") or "").strip()
    if mode not in _RESTRICTED_MODES | _ATTESTATION_ONLY_MODES or not _VERSION_RE.fullmatch(
        version
    ):
        raise SurveyEligibilityError(
            "La politica de elegibilidad del release es invalida.",
            status_code=500,
            reason_code="survey_eligibility_snapshot_corrupt",
            action_hint="contact_support",
        )
    if not hmac.compare_digest(str(release.eligibility_policy_version or ""), version):
        raise SurveyEligibilityError(
            "La version de elegibilidad no coincide con el snapshot.",
            status_code=500,
            reason_code="survey_eligibility_snapshot_corrupt",
            action_hint="contact_support",
        )
    return {"mode": mode, "policy_version": version}


def public_eligibility_contract(
    release: SurveyGovernanceRelease,
    *,
    config: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    policy = _release_eligibility_policy(release)
    mode = policy["mode"]
    base = {
        "contract_version": SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION,
        "policy_version": policy["policy_version"],
        "mode": mode,
        "eligible_population": None,
        "participation_rate": None,
        "abstentions": None,
        "denominator_status": {
            "available": False,
            "reason_code": "survey_eligible_population_not_sealed",
        },
        "privacy_assurance": (
            "attestation_only"
            if mode in _ATTESTATION_ONLY_MODES
            else ELIGIBILITY_PRIVACY_ASSURANCE
        ),
        "subject_identifier_exposed": False,
        "plaintext_credential_persisted": False,
        "persist_client_side": False,
        "assurance_level": (
            "attestation_only"
            if mode in _ATTESTATION_ONLY_MODES
            else "human_reviewed_opaque_grant"
        ),
        "authority_binding": (
            None
            if mode in _ATTESTATION_ONLY_MODES
            else "operator_attested_v1"
        ),
        "ballot_secrecy_certified": False,
        "regulated_election_certified": False,
        "result_certified": False,
    }
    if mode in _ATTESTATION_ONLY_MODES:
        return {
            **base,
            "credential_required": False,
            "gate_status": "attestation_only",
            "intake_available": True,
            "decision": "not_evaluated",
            "transport": None,
            "blocked_reason_code": None,
        }
    runtime_config = config if config is not None else current_app.config
    gate = resolve_eligibility_gate(runtime_config, tenant_id=release.tenant_id)
    return {
        **base,
        "credential_required": True,
        "gate_status": "ready" if gate.ready else "unavailable",
        "intake_available": bool(gate.ready),
        "decision": "credential_pending" if gate.ready else "unavailable",
        "transport": {
            "kind": "http_header",
            "header_name": ELIGIBILITY_CREDENTIAL_HEADER,
            "meta_flow_supported": False,
        },
        "blocked_reason_code": (
            None if gate.ready else "survey_eligibility_gate_unavailable"
        ),
    }


def assert_restricted_release_gate_ready(
    release: SurveyGovernanceRelease,
    *,
    config: Mapping[str, Any] | Any | None = None,
) -> None:
    policy = _release_eligibility_policy(release)
    if policy["mode"] in _ATTESTATION_ONLY_MODES:
        return
    gate = resolve_eligibility_gate(
        config if config is not None else current_app.config,
        tenant_id=release.tenant_id,
    )
    if not gate.ready:
        raise SurveyEligibilityError(
            "El release requiere elegibilidad opaca, pero su gate no esta disponible.",
            status_code=503,
            reason_code="survey_eligibility_gate_unavailable",
            action_hint="configure_eligibility_gate_before_publish",
        )


def _subject_hmac(
    secret: bytes,
    *,
    tenant_id: int,
    survey_id: int,
    release_id: int,
    policy_version: str,
    subject_namespace: str,
    normalized_subject: str,
) -> str:
    return _hmac_hex(
        _derive_subkey(
            secret,
            purpose="subject-hmac",
            tenant_id=tenant_id,
            key_version=SURVEY_ELIGIBILITY_KEY_VERSION,
        ),
        {
            "contract": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
            "tenant_id": int(tenant_id),
            "survey_id": int(survey_id),
            "release_id": int(release_id),
            "policy_version": policy_version,
            "subject_namespace": subject_namespace,
            "subject": normalized_subject,
        },
    )


def _derive_credential(secret: bytes, grant: SurveyEligibilityGrant) -> str:
    material = {
        "contract": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
        "key_version": grant.credential_key_version,
        "tenant_id": int(grant.tenant_id),
        "survey_id": int(grant.survey_id),
        "release_id": int(grant.release_id),
        "policy_version": grant.eligibility_policy_version,
        "eligibility_mode": grant.eligibility_mode,
        "subject_namespace": grant.subject_namespace,
        "grant_ref": grant.grant_ref,
        "generation": int(grant.generation),
        "issued_at": _as_utc(grant.issued_at).isoformat(),
        "expires_at": _as_utc(grant.expires_at).isoformat(),
        "authority_namespace": grant.authority_namespace,
        "authority_adapter_version": grant.authority_adapter_version,
        "review_reference_hmac": grant.review_reference_hmac,
        "review_key_version": grant.review_key_version,
    }
    digest = hmac.new(
        _derive_subkey(
            secret,
            purpose="credential-derive",
            tenant_id=grant.tenant_id,
            key_version=grant.credential_key_version,
        ),
        _canonical_json(material).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    credential = f"sec1_{_b64url(digest)}"
    if not _CREDENTIAL_RE.fullmatch(credential):
        raise SurveyEligibilityError(
            "No se pudo derivar la credencial opaca.",
            status_code=500,
            reason_code="survey_eligibility_credential_derivation_failed",
            action_hint="contact_support",
        )
    return credential


def _credential_digest(
    secret: bytes,
    *,
    tenant_id: int,
    survey_id: int,
    release_id: int,
    credential: str,
    key_version: str,
) -> str:
    return _hmac_hex(
        _derive_subkey(
            secret,
            purpose="credential-digest",
            tenant_id=tenant_id,
            key_version=key_version,
        ),
        {
            "contract": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
            "tenant_id": int(tenant_id),
            "survey_id": int(survey_id),
            "release_id": int(release_id),
            "credential": credential,
        },
    )


def _terminal_for(grant_id: int) -> SurveyEligibilityTerminal | None:
    return SurveyEligibilityTerminal.query.filter_by(grant_id=int(grant_id)).first()


def _grant_state(grant: SurveyEligibilityGrant, *, now: datetime | None = None) -> str:
    terminal = _terminal_for(grant.id)
    if terminal is not None:
        return terminal.disposition
    if _as_utc(grant.expires_at) <= _as_utc(now or _utc_now()):
        return "expired"
    return "active"


def _credential_for_issue_receipt(
    grant: SurveyEligibilityGrant,
    *,
    gate: SurveyEligibilityGate,
) -> str | None:
    if _grant_state(grant) != "active" or gate.secret is None:
        return None
    credential = _derive_credential(gate.secret, grant)
    expected = _credential_digest(
        gate.secret,
        tenant_id=grant.tenant_id,
        survey_id=grant.survey_id,
        release_id=grant.release_id,
        credential=credential,
        key_version=grant.credential_key_version,
    )
    if not hmac.compare_digest(expected, grant.credential_digest):
        raise SurveyEligibilityError(
            "La evidencia criptografica del grant no coincide.",
            status_code=500,
            reason_code="survey_eligibility_grant_integrity_failed",
            action_hint="contact_support",
        )
    return credential


def serialize_issue_receipt(
    grant: SurveyEligibilityGrant,
    *,
    gate: SurveyEligibilityGate,
    replayed: bool,
) -> dict[str, Any]:
    state = _grant_state(grant)
    credential = _credential_for_issue_receipt(grant, gate=gate)
    payload: dict[str, Any] = {
        "ok": True,
        "contract_version": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
        "tenant_id": int(grant.tenant_id),
        "survey_id": int(grant.survey_id),
        "release_id": int(grant.release_id),
        "grant_ref": grant.grant_ref,
        "state": state,
        "generation": int(grant.generation),
        "eligibility_mode": grant.eligibility_mode,
        "eligibility_policy_version": grant.eligibility_policy_version,
        "authority": {
            "namespace": grant.authority_namespace,
            "adapter_version": grant.authority_adapter_version,
        },
        "expires_at": _as_utc(grant.expires_at).isoformat(),
        "credential_header": ELIGIBILITY_CREDENTIAL_HEADER,
        "credential": credential,
        "idempotency": {
            "persisted": True,
            "replayed": bool(replayed),
            "disposition": "replayed" if replayed else "accepted",
        },
        "assurance": {
            "privacy": ELIGIBILITY_PRIVACY_ASSURANCE,
            "assurance_level": "human_reviewed_opaque_grant",
            "authority_binding": "operator_attested_v1",
            "raw_subject_persisted": False,
            "subject_identifier_exposed": False,
            "plaintext_credential_persisted": False,
            "ballot_secrecy_certified": False,
            "regulated_election_certified": False,
            "result_certified": False,
        },
    }
    return payload


def _parse_expiry(
    value: Any,
    *,
    release: SurveyGovernanceRelease,
    now: datetime,
) -> tuple[datetime, str | None]:
    requested_canonical: str | None = None
    if value in (None, ""):
        expiry = now + timedelta(days=30)
    elif isinstance(value, datetime):
        expiry = _as_utc(value)
        requested_canonical = expiry.isoformat()
    else:
        text = str(value).strip()
        try:
            expiry = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SurveyEligibilityError(
                "expires_at debe ser una fecha ISO-8601 valida.",
                status_code=422,
                reason_code="survey_eligibility_expiry_invalid",
                action_hint="provide_bounded_expiry",
            ) from exc
        expiry = _as_utc(expiry)
        requested_canonical = expiry.isoformat()
    if expiry <= now + timedelta(minutes=5) or expiry > now + timedelta(
        days=_MAX_EXPIRY_DAYS
    ):
        raise SurveyEligibilityError(
            "expires_at esta fuera del rango permitido.",
            status_code=422,
            reason_code="survey_eligibility_expiry_invalid",
            action_hint="provide_bounded_expiry",
        )
    try:
        snapshot = json.loads(release.snapshot_json)
        ends_at = snapshot.get("collection_rules", {}).get("ends_at")
        release_end = (
            _as_utc(datetime.fromisoformat(str(ends_at).replace("Z", "+00:00")))
            if ends_at
            else None
        )
    except (AttributeError, TypeError, ValueError):
        release_end = None
    if release_end is not None:
        if release_end <= now:
            raise SurveyEligibilityError(
                "La ventana del release ya finalizo.",
                status_code=409,
                reason_code="survey_eligibility_release_window_closed",
                action_hint="check_release_window",
            )
        expiry = min(expiry, release_end)
        if expiry <= now + timedelta(minutes=5):
            raise SurveyEligibilityError(
                "La ventana restante del release es demasiado corta para emitir una credencial.",
                status_code=409,
                reason_code="survey_eligibility_release_window_too_short",
                action_hint="extend_release_window_or_wait_for_next_release",
            )
    return expiry, requested_canonical


def _audit(
    *,
    tenant_id: int,
    actor_user_id: int | None,
    event_type: str,
    grant: SurveyEligibilityGrant,
    details: Mapping[str, Any],
    ip_address: str | None,
) -> None:
    db.session.add(
        AuditEvent(
            tenant_id=int(tenant_id),
            actor_user_id=actor_user_id,
            event_type=event_type,
            resource_type="survey_eligibility_grant",
            resource_id=grant.grant_ref,
            details={
                "contract_version": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
                "survey_id": int(grant.survey_id),
                "release_id": int(grant.release_id),
                "grant_ref": grant.grant_ref,
                "generation": int(grant.generation),
                "eligibility_mode": grant.eligibility_mode,
                "eligibility_policy_version": grant.eligibility_policy_version,
                "raw_subject_in_audit": False,
                "credential_in_audit": False,
                **dict(details),
            },
            ip_address=str(ip_address or "").strip()[:50] or None,
        )
    )


def _commit_or_error(*, operation: str) -> None:
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise SurveyEligibilityError(
            "Conflicto al persistir la elegibilidad opaca.",
            status_code=409,
            reason_code=f"survey_eligibility_{operation}_conflict",
            action_hint="retry_same_idempotency_key",
        ) from exc
    except Exception as exc:
        db.session.rollback()
        raise SurveyEligibilityError(
            "No se pudo confirmar atomicamente elegibilidad y auditoria.",
            status_code=500,
            reason_code="survey_eligibility_atomic_commit_failed",
            action_hint="retry_same_idempotency_key",
            retryable=True,
        ) from exc


def issue_eligibility_grant(
    *,
    tenant_id: int,
    survey_id: int,
    release_id: int,
    actor_user_id: int,
    subject_namespace: Any,
    subject_ref: Any,
    authority_namespace: Any,
    authority_adapter_version: Any,
    review_reference: Any,
    expires_at: Any,
    idempotency_key: Any,
    config: Mapping[str, Any] | Any | None = None,
    ip_address: str | None = None,
) -> tuple[SurveyEligibilityGrant, str | None, bool]:
    """Issue or exactly replay one grant without persisting the raw subject."""

    gate = require_eligibility_gate(
        config if config is not None else current_app.config,
        tenant_id=tenant_id,
    )
    assert gate.secret is not None
    key = _validate_idempotency_key(idempotency_key)
    normalized_namespace = _normalize_namespace(
        subject_namespace, field_name="subject_namespace"
    )
    normalized_subject = _normalize_subject(subject_ref)
    normalized_authority = _normalize_namespace(
        authority_namespace, field_name="authority_namespace"
    )
    if normalized_namespace != normalized_authority:
        raise SurveyEligibilityError(
            "subject_namespace debe provenir del mismo adapter de autoridad.",
            status_code=422,
            reason_code="survey_eligibility_authority_binding_invalid",
            action_hint="use_server_controlled_authority_adapter",
        )
    normalized_adapter = _normalize_version(
        authority_adapter_version, field_name="authority_adapter_version"
    )
    normalized_review_reference = _normalize_review_reference(review_reference)

    release = SurveyGovernanceRelease.query.filter_by(
        tenant_id=int(tenant_id), survey_id=int(survey_id), id=int(release_id)
    ).first()
    if release is None:
        raise SurveyEligibilityError(
            "Release no encontrado.",
            status_code=404,
            reason_code="survey_governance_release_not_found",
            action_hint="check_release_scope",
        )
    if release.status != "published":
        raise SurveyEligibilityError(
            "Los grants solo pueden emitirse para un release publicado.",
            status_code=409,
            reason_code="survey_eligibility_release_not_published",
            action_hint="publish_release_first",
        )
    policy = _release_eligibility_policy(release)
    if policy["mode"] not in _RESTRICTED_MODES:
        raise SurveyEligibilityError(
            "Este release no requiere credenciales de elegibilidad.",
            status_code=409,
            reason_code="survey_eligibility_credential_not_applicable",
            action_hint="use_attestation_only_flow",
        )
    subject_hmac = _subject_hmac(
        gate.secret,
        tenant_id=tenant_id,
        survey_id=survey_id,
        release_id=release_id,
        policy_version=policy["policy_version"],
        subject_namespace=normalized_namespace,
        normalized_subject=normalized_subject,
    )
    review_hmac = _review_reference_hmac(
        gate.secret,
        tenant_id=tenant_id,
        survey_id=survey_id,
        release_id=release_id,
        policy_version=policy["policy_version"],
        authority_namespace=normalized_authority,
        authority_adapter_version=normalized_adapter,
        review_reference=normalized_review_reference,
    )
    now = _utc_now()
    resolved_expiry, requested_expiry = _parse_expiry(
        expires_at, release=release, now=now
    )
    request_hash = _canonical_hash(
        {
            "operation": "issue",
            "contract": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
            "tenant_id": int(tenant_id),
            "survey_id": int(survey_id),
            "release_id": int(release_id),
            "policy_version": policy["policy_version"],
            "subject_hmac": subject_hmac,
            "authority_namespace": normalized_authority,
            "authority_adapter_version": normalized_adapter,
            "review_reference_hmac": review_hmac,
            "review_key_version": SURVEY_ELIGIBILITY_KEY_VERSION,
            "requested_expires_at": requested_expiry,
        }
    )

    replay = (
        SurveyEligibilityGrant.query.filter_by(
            tenant_id=int(tenant_id), issue_idempotency_key=key
        )
        .with_for_update()
        .first()
    )
    if replay is not None:
        if not hmac.compare_digest(replay.issue_request_hash, request_hash):
            raise SurveyEligibilityError(
                "Idempotency-Key ya fue usado con otra emision.",
                status_code=409,
                reason_code="survey_eligibility_idempotency_conflict",
                action_hint="reuse_original_payload_or_new_key",
            )
        return replay, _credential_for_issue_receipt(replay, gate=gate), True

    locked_release = (
        SurveyGovernanceRelease.query.filter_by(
            tenant_id=int(tenant_id), survey_id=int(survey_id), id=int(release_id)
        )
        .with_for_update()
        .first()
    )
    if locked_release is None or locked_release.status != "published":
        raise SurveyEligibilityError(
            "El release ya no esta disponible para emitir grants.",
            status_code=409,
            reason_code="survey_eligibility_release_not_published",
            action_hint="reload_release_state",
        )

    replay = (
        SurveyEligibilityGrant.query.filter_by(
            tenant_id=int(tenant_id), issue_idempotency_key=key
        )
        .with_for_update()
        .first()
    )
    if replay is not None:
        if not hmac.compare_digest(replay.issue_request_hash, request_hash):
            raise SurveyEligibilityError(
                "Idempotency-Key ya fue usado con otra emision.",
                status_code=409,
                reason_code="survey_eligibility_idempotency_conflict",
                action_hint="reuse_original_payload_or_new_key",
            )
        return replay, _credential_for_issue_receipt(replay, gate=gate), True

    previous = (
        SurveyEligibilityGrant.query.filter_by(
            tenant_id=int(tenant_id),
            release_id=int(release_id),
            subject_hmac=subject_hmac,
        )
        .order_by(SurveyEligibilityGrant.generation.desc())
        .first()
    )
    generation = 1
    if previous is not None:
        previous_state = _grant_state(previous, now=now)
        if previous_state == "active":
            raise SurveyEligibilityError(
                "El sujeto ya tiene un grant activo.",
                status_code=409,
                reason_code="survey_eligibility_subject_grant_active",
                action_hint="use_existing_issue_receipt",
            )
        if previous_state == "redeemed":
            raise SurveyEligibilityError(
                "El sujeto ya redimio su participacion para este release.",
                status_code=409,
                reason_code="survey_eligibility_subject_already_redeemed",
                action_hint="do_not_reissue",
            )
        if previous_state == "revoked":
            previous_terminal = _terminal_for(previous.id)
            if (
                previous_terminal is None
                or previous_terminal.reason_code
                not in _REISSUABLE_REVOCATION_REASONS
            ):
                raise SurveyEligibilityError(
                    "La revocacion previa no admite una nueva emision.",
                    status_code=409,
                    reason_code="survey_eligibility_subject_reissue_blocked",
                    action_hint="do_not_reissue",
                )
        if hmac.compare_digest(previous.review_reference_hmac, review_hmac):
            raise SurveyEligibilityError(
                "Una nueva emision requiere evidencia de revision renovada.",
                status_code=409,
                reason_code="survey_eligibility_reissue_review_required",
                action_hint="perform_new_human_review",
            )
        generation = int(previous.generation) + 1

    grant_ref = f"seg1_{secrets.token_urlsafe(32)}"
    if not _GRANT_REF_RE.fullmatch(grant_ref):
        raise SurveyEligibilityError(
            "No se pudo generar una referencia opaca.",
            status_code=500,
            reason_code="survey_eligibility_grant_generation_failed",
            action_hint="retry_same_idempotency_key",
            retryable=True,
        )
    grant = SurveyEligibilityGrant(
        tenant_id=int(tenant_id),
        survey_id=int(survey_id),
        release_id=int(release_id),
        grant_ref=grant_ref,
        eligibility_policy_version=policy["policy_version"],
        eligibility_mode=policy["mode"],
        subject_namespace=normalized_namespace,
        subject_hmac=subject_hmac,
        generation=generation,
        subject_key_version=SURVEY_ELIGIBILITY_KEY_VERSION,
        authority_namespace=normalized_authority,
        authority_adapter_version=normalized_adapter,
        review_reference_hmac=review_hmac,
        review_key_version=SURVEY_ELIGIBILITY_KEY_VERSION,
        credential_digest="0" * 64,
        credential_key_version=SURVEY_ELIGIBILITY_KEY_VERSION,
        issued_by_user_id=int(actor_user_id),
        issue_idempotency_key=key,
        issue_request_hash=request_hash,
        issued_at=now,
        expires_at=resolved_expiry,
    )
    credential = _derive_credential(gate.secret, grant)
    grant.credential_digest = _credential_digest(
        gate.secret,
        tenant_id=tenant_id,
        survey_id=survey_id,
        release_id=release_id,
        credential=credential,
        key_version=grant.credential_key_version,
    )
    db.session.add(grant)
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise SurveyEligibilityError(
            "Conflicto al reservar el grant de elegibilidad.",
            status_code=409,
            reason_code="survey_eligibility_issue_conflict",
            action_hint="retry_same_idempotency_key",
        ) from exc
    _audit(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type="survey.eligibility_grant.issued",
        grant=grant,
        details={
            "state": "active",
            "authority_namespace": normalized_authority,
            "authority_adapter_version": normalized_adapter,
            "review_reference_evidence_present": True,
            "raw_review_reference_in_audit": False,
        },
        ip_address=ip_address,
    )
    _commit_or_error(operation="issue")
    return grant, credential, False


def serialize_revocation_receipt(
    grant: SurveyEligibilityGrant,
    terminal: SurveyEligibilityTerminal,
    *,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "contract_version": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
        "tenant_id": int(grant.tenant_id),
        "survey_id": int(grant.survey_id),
        "release_id": int(grant.release_id),
        "grant_ref": grant.grant_ref,
        "state": terminal.disposition,
        "reason_code": terminal.reason_code,
        "eligibility_policy_version": grant.eligibility_policy_version,
        "idempotency": {
            "persisted": True,
            "replayed": bool(replayed),
            "disposition": "replayed" if replayed else "accepted",
        },
        "regulated_election_certified": False,
        "result_certified": False,
    }


def revoke_eligibility_grant(
    *,
    tenant_id: int,
    survey_id: int,
    release_id: int,
    grant_ref: str,
    actor_user_id: int,
    reason_code: Any,
    idempotency_key: Any,
    config: Mapping[str, Any] | Any | None = None,
    ip_address: str | None = None,
) -> tuple[SurveyEligibilityGrant, SurveyEligibilityTerminal, bool]:
    require_eligibility_gate(
        config if config is not None else current_app.config,
        tenant_id=tenant_id,
    )
    key = _validate_idempotency_key(idempotency_key)
    normalized_ref = str(grant_ref or "").strip()
    if not _GRANT_REF_RE.fullmatch(normalized_ref):
        raise SurveyEligibilityError(
            "Grant no encontrado.",
            status_code=404,
            reason_code="survey_eligibility_grant_not_found",
            action_hint="check_grant_ref",
        )
    normalized_reason = str(reason_code or "").strip().lower()
    if normalized_reason not in SURVEY_ELIGIBILITY_REVOCATION_REASONS:
        raise SurveyEligibilityError(
            "reason_code de revocacion invalido.",
            status_code=422,
            reason_code="survey_eligibility_revocation_reason_invalid",
            action_hint="choose_supported_revocation_reason",
        )
    request_hash = _canonical_hash(
        {
            "operation": "revoke",
            "contract": SURVEY_ELIGIBILITY_GRANT_CONTRACT_VERSION,
            "tenant_id": int(tenant_id),
            "survey_id": int(survey_id),
            "release_id": int(release_id),
            "grant_ref": normalized_ref,
            "reason_code": normalized_reason,
        }
    )
    replay = SurveyEligibilityTerminal.query.filter_by(
        tenant_id=int(tenant_id), idempotency_key=key
    ).first()
    if replay is not None:
        if replay.disposition != "revoked" or not hmac.compare_digest(
            replay.request_hash, request_hash
        ):
            raise SurveyEligibilityError(
                "Idempotency-Key ya fue usado con otra revocacion.",
                status_code=409,
                reason_code="survey_eligibility_idempotency_conflict",
                action_hint="reuse_original_payload_or_new_key",
            )
        grant = db.session.get(SurveyEligibilityGrant, replay.grant_id)
        if grant is None:
            raise SurveyEligibilityError(
                "El recibo de revocacion esta corrupto.",
                status_code=500,
                reason_code="survey_eligibility_terminal_corrupt",
                action_hint="contact_support",
            )
        return grant, replay, True

    grant = (
        SurveyEligibilityGrant.query.filter_by(
            tenant_id=int(tenant_id),
            survey_id=int(survey_id),
            release_id=int(release_id),
            grant_ref=normalized_ref,
        )
        .with_for_update()
        .first()
    )
    if grant is None:
        raise SurveyEligibilityError(
            "Grant no encontrado.",
            status_code=404,
            reason_code="survey_eligibility_grant_not_found",
            action_hint="check_grant_ref",
        )
    replay = SurveyEligibilityTerminal.query.filter_by(
        tenant_id=int(tenant_id), idempotency_key=key
    ).first()
    if replay is not None:
        if replay.grant_id == grant.id and replay.disposition == "revoked" and hmac.compare_digest(
            replay.request_hash, request_hash
        ):
            return grant, replay, True
        raise SurveyEligibilityError(
            "Idempotency-Key ya fue usado con otra revocacion.",
            status_code=409,
            reason_code="survey_eligibility_idempotency_conflict",
            action_hint="reuse_original_payload_or_new_key",
        )
    terminal = _terminal_for(grant.id)
    if terminal is not None:
        raise SurveyEligibilityError(
            "El grant ya tiene un estado terminal.",
            status_code=409,
            reason_code=(
                "survey_eligibility_credential_consumed"
                if terminal.disposition == "redeemed"
                else "survey_eligibility_grant_revoked"
            ),
            action_hint="do_not_revoke_terminal_grant",
        )
    if _as_utc(grant.expires_at) <= _utc_now():
        raise SurveyEligibilityError(
            "El grant ya vencio.",
            status_code=409,
            reason_code="survey_eligibility_grant_expired",
            action_hint="issue_next_generation_if_reviewed",
        )
    terminal = SurveyEligibilityTerminal(
        tenant_id=int(tenant_id),
        survey_id=int(survey_id),
        release_id=int(release_id),
        grant_id=grant.id,
        disposition="revoked",
        actor_user_id=int(actor_user_id),
        reason_code=normalized_reason,
        idempotency_key=key,
        response_id=None,
        eligibility_policy_version=grant.eligibility_policy_version,
        submission_payload_hash=None,
        request_hash=request_hash,
    )
    db.session.add(terminal)
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise SurveyEligibilityError(
            "El grant cambio mientras se revocaba.",
            status_code=409,
            reason_code="survey_eligibility_terminal_conflict",
            action_hint="reload_grant_state",
        ) from exc
    _audit(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type="survey.eligibility_grant.revoked",
        grant=grant,
        details={"state": "revoked", "reason_code": normalized_reason},
        ip_address=ip_address,
    )
    _commit_or_error(operation="revoke")
    return grant, terminal, False


def eligibility_aggregate(
    *,
    tenant_id: int,
    survey_id: int,
    release_id: int,
    config: Mapping[str, Any] | Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    require_eligibility_gate(
        config if config is not None else current_app.config,
        tenant_id=tenant_id,
    )
    release = SurveyGovernanceRelease.query.filter_by(
        tenant_id=int(tenant_id), survey_id=int(survey_id), id=int(release_id)
    ).first()
    if release is None:
        raise SurveyEligibilityError(
            "Release no encontrado.",
            status_code=404,
            reason_code="survey_governance_release_not_found",
            action_hint="check_release_scope",
        )
    policy = _release_eligibility_policy(release)
    if policy["mode"] not in _RESTRICTED_MODES:
        raise SurveyEligibilityError(
            "Este release no utiliza grants de elegibilidad.",
            status_code=409,
            reason_code="survey_eligibility_credential_not_applicable",
            action_hint="use_attestation_only_flow",
        )
    grants = SurveyEligibilityGrant.query.filter_by(
        tenant_id=int(tenant_id), survey_id=int(survey_id), release_id=int(release_id)
    ).all()
    terminals = SurveyEligibilityTerminal.query.filter_by(
        tenant_id=int(tenant_id), survey_id=int(survey_id), release_id=int(release_id)
    ).all()
    terminal_by_grant = {int(item.grant_id): item for item in terminals}
    reference = _as_utc(now or _utc_now())
    counts = {"issued": len(grants), "active": 0, "expired": 0, "revoked": 0, "redeemed": 0}
    for grant in grants:
        terminal = terminal_by_grant.get(int(grant.id))
        if terminal is not None:
            counts[terminal.disposition] += 1
        elif _as_utc(grant.expires_at) <= reference:
            counts["expired"] += 1
        else:
            counts["active"] += 1
    return {
        "ok": True,
        "contract_version": "surveys.eligibility_aggregate.v1",
        "survey_id": int(survey_id),
        "release_id": int(release_id),
        "policy_version": policy["policy_version"],
        "counts": counts,
        "eligible_population": None,
        "participation_rate": None,
        "abstentions": None,
        "denominator_status": {
            "available": False,
            "reason_code": "survey_eligible_population_not_sealed",
        },
        "subjects_exposed": False,
        "regulated_election_certified": False,
        "result_certified": False,
    }


def lock_eligibility_grant_for_submission(
    *,
    encuesta: EncEncuesta,
    release: SurveyGovernanceRelease,
    credential: Any,
    submission_id: str | None,
    commit: bool,
    transport: str,
    config: Mapping[str, Any] | Any | None = None,
) -> SurveyEligibilityGrant | None:
    """Resolve and lock a grant after both durable replay checks have missed.

    The caller must finish validating privacy, answers and duplicate
    participation before invoking this function.  It intentionally does not
    create a terminal row; :func:`stage_eligibility_redemption` owns that step.
    """

    policy = _release_eligibility_policy(release)
    if policy["mode"] in _ATTESTATION_ONLY_MODES:
        return None
    if not commit or str(transport or "").strip().lower() != "http":
        raise SurveyEligibilityError(
            "Este transporte no admite la credencial opaca requerida.",
            status_code=409,
            reason_code="survey_eligibility_transport_unsupported",
            action_hint="use_supported_http_submission",
        )
    if not submission_id:
        raise SurveyEligibilityError(
            "La elegibilidad opaca requiere submission_id durable.",
            status_code=400,
            reason_code="survey_eligibility_submission_id_required",
            action_hint="send_stable_submission_id",
        )
    if release.status != "published" or encuesta.estado != "publicada":
        raise SurveyEligibilityError(
            "El release no esta abierto para participacion.",
            status_code=409,
            reason_code="survey_eligibility_release_not_active",
            action_hint="reload_public_survey_contract",
        )
    gate = require_eligibility_gate(
        config if config is not None else current_app.config,
        tenant_id=encuesta.tenant_id,
    )
    assert gate.secret is not None
    normalized_credential = str(credential or "").strip()
    if not normalized_credential:
        raise SurveyEligibilityError(
            "Se requiere la credencial de elegibilidad.",
            status_code=428,
            reason_code="survey_eligibility_credential_required",
            action_hint="provide_eligibility_credential_header",
        )
    if not _CREDENTIAL_RE.fullmatch(normalized_credential):
        raise SurveyEligibilityError(
            "La credencial no es valida para este release.",
            status_code=403,
            reason_code="survey_eligibility_credential_invalid",
            action_hint="request_valid_eligibility_credential",
        )
    digest = _credential_digest(
        gate.secret,
        tenant_id=encuesta.tenant_id,
        survey_id=encuesta.id,
        release_id=release.id,
        credential=normalized_credential,
        key_version=SURVEY_ELIGIBILITY_KEY_VERSION,
    )
    grant = (
        SurveyEligibilityGrant.query.filter_by(
            tenant_id=encuesta.tenant_id,
            survey_id=encuesta.id,
            release_id=release.id,
            credential_digest=digest,
        )
        .with_for_update()
        .first()
    )
    if grant is None or not hmac.compare_digest(grant.credential_digest, digest):
        raise SurveyEligibilityError(
            "La credencial no es valida para este release.",
            status_code=403,
            reason_code="survey_eligibility_credential_invalid",
            action_hint="request_valid_eligibility_credential",
        )
    if (
        grant.eligibility_mode != policy["mode"]
        or grant.eligibility_policy_version != policy["policy_version"]
    ):
        raise SurveyEligibilityError(
            "La credencial no coincide con la politica vigente.",
            status_code=409,
            reason_code="survey_eligibility_policy_mismatch",
            action_hint="reload_public_survey_contract",
        )
    derived_credential = _derive_credential(gate.secret, grant)
    if not hmac.compare_digest(derived_credential, normalized_credential):
        raise SurveyEligibilityError(
            "La credencial no es valida para este release.",
            status_code=403,
            reason_code="survey_eligibility_credential_invalid",
            action_hint="request_valid_eligibility_credential",
        )
    assert_eligibility_grant_active(grant)
    return grant


def assert_eligibility_grant_active(
    grant: SurveyEligibilityGrant,
    *,
    now: datetime | None = None,
) -> None:
    terminal = _terminal_for(grant.id)
    if terminal is not None:
        if terminal.disposition == "revoked":
            status_code = 403
            reason_code = "survey_eligibility_grant_revoked"
        else:
            status_code = 409
            reason_code = "survey_eligibility_credential_consumed"
        raise SurveyEligibilityError(
            "La credencial ya no esta disponible.",
            status_code=status_code,
            reason_code=reason_code,
            action_hint="do_not_retry_credential",
        )
    if _as_utc(grant.expires_at) <= _as_utc(now or _utc_now()):
        raise SurveyEligibilityError(
            "La credencial vencio.",
            status_code=410,
            reason_code="survey_eligibility_grant_expired",
            action_hint="request_reviewed_reissue",
        )


def stage_eligibility_redemption(
    *,
    grant: SurveyEligibilityGrant,
    respuesta: EncRespuesta,
    submission_payload_hash: str,
    redeemed_at: datetime | None = None,
) -> SurveyEligibilityTerminal:
    """Stage one terminal redemption in the response transaction; never commit."""

    if not _HASH_RE.fullmatch(str(submission_payload_hash or "")):
        raise SurveyEligibilityError(
            "La redencion requiere el hash canonico del submission receipt.",
            status_code=500,
            reason_code="survey_eligibility_payload_hash_required",
            action_hint="contact_support",
        )
    if respuesta.id is None:
        raise SurveyEligibilityError(
            "La respuesta debe estar persistida antes de redimir.",
            status_code=500,
            reason_code="survey_eligibility_response_not_flushed",
            action_hint="contact_support",
        )
    assert_eligibility_grant_active(grant, now=redeemed_at)
    scope = (
        int(respuesta.tenant_id),
        int(respuesta.encuesta_id),
        int(respuesta.governance_release_id or 0),
    )
    expected_scope = (int(grant.tenant_id), int(grant.survey_id), int(grant.release_id))
    if scope != expected_scope:
        raise SurveyEligibilityError(
            "La respuesta no coincide con el scope del grant.",
            status_code=409,
            reason_code="survey_eligibility_response_scope_mismatch",
            action_hint="reload_public_survey_contract",
        )
    if not hmac.compare_digest(
        str(respuesta.governance_eligibility_policy_version or ""),
        grant.eligibility_policy_version,
    ):
        raise SurveyEligibilityError(
            "La respuesta no coincide con la politica del grant.",
            status_code=409,
            reason_code="survey_eligibility_response_policy_mismatch",
            action_hint="reload_public_survey_contract",
        )
    verified_at = _as_utc(redeemed_at or _utc_now())
    request_hash = _canonical_hash(
        {
            "operation": "redeem",
            "contract": SURVEY_ELIGIBILITY_REDEMPTION_CONTRACT_VERSION,
            "tenant_id": scope[0],
            "survey_id": scope[1],
            "release_id": scope[2],
            "grant_ref": grant.grant_ref,
            "response_id": int(respuesta.id),
            "submission_payload_hash": submission_payload_hash,
        }
    )
    terminal = SurveyEligibilityTerminal(
        tenant_id=scope[0],
        survey_id=scope[1],
        release_id=scope[2],
        grant_id=int(grant.id),
        disposition="redeemed",
        actor_user_id=None,
        reason_code=None,
        idempotency_key=None,
        response_id=int(respuesta.id),
        eligibility_policy_version=grant.eligibility_policy_version,
        submission_payload_hash=submission_payload_hash,
        request_hash=request_hash,
        created_at=verified_at,
    )
    respuesta.eligibility_contract_version = SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION
    respuesta.eligibility_decision = ELIGIBILITY_DECISION_VERIFIED
    respuesta.eligibility_verified_at = verified_at
    # The DB terminal-scope trigger verifies these persisted markers.  Flush
    # the dirty response first; a later terminal failure still rolls both back
    # because this function never commits.
    db.session.flush([respuesta])
    db.session.add(terminal)
    try:
        db.session.flush()
    except IntegrityError as exc:
        raise SurveyEligibilityError(
            "La credencial cambio mientras se redimia.",
            status_code=409,
            reason_code="survey_eligibility_terminal_conflict",
            action_hint="retry_same_submission_id",
        ) from exc
    return terminal


def response_eligibility_contract(respuesta: EncRespuesta) -> dict[str, Any]:
    terminal = None
    if getattr(respuesta, "id", None) is not None:
        terminal = SurveyEligibilityTerminal.query.filter_by(
            response_id=int(respuesta.id)
        ).first()
    verified = (
        getattr(respuesta, "eligibility_decision", None)
        == ELIGIBILITY_DECISION_VERIFIED
    )
    if verified or terminal is not None:
        terminal_grant = (
            db.session.get(SurveyEligibilityGrant, int(terminal.grant_id))
            if terminal is not None
            else None
        )
        valid_terminal = (
            verified
            and terminal is not None
            and terminal_grant is not None
            and terminal.disposition == "redeemed"
            and int(terminal.tenant_id) == int(respuesta.tenant_id)
            and int(terminal.survey_id) == int(respuesta.encuesta_id)
            and int(terminal.release_id)
            == int(respuesta.governance_release_id or 0)
            and hmac.compare_digest(
                terminal.eligibility_policy_version,
                str(respuesta.governance_eligibility_policy_version or ""),
            )
            and int(terminal_grant.tenant_id) == int(terminal.tenant_id)
            and int(terminal_grant.survey_id) == int(terminal.survey_id)
            and int(terminal_grant.release_id) == int(terminal.release_id)
            and hmac.compare_digest(
                terminal_grant.eligibility_policy_version,
                terminal.eligibility_policy_version,
            )
            and getattr(respuesta, "eligibility_contract_version", None)
            == SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION
            and getattr(respuesta, "eligibility_verified_at", None) is not None
            and _as_utc(terminal.created_at)
            == _as_utc(respuesta.eligibility_verified_at)
        )
        if not valid_terminal:
            raise SurveyEligibilityError(
                "El recibo de elegibilidad de la respuesta es inconsistente.",
                status_code=500,
                reason_code="survey_eligibility_response_receipt_corrupt",
                action_hint="contact_support",
            )
        return {
            "contract_version": SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION,
            "credential_required": True,
            "gate_status": "verified",
            "decision": ELIGIBILITY_DECISION_VERIFIED,
            "privacy_assurance": ELIGIBILITY_PRIVACY_ASSURANCE,
            "assurance_level": "human_reviewed_opaque_grant",
            "authority_binding": "operator_attested_v1",
            "subject_identifier_exposed": False,
            "plaintext_credential_persisted": False,
            "persist_client_side": False,
            "ballot_secrecy_certified": False,
            "redemption": {
                "contract_version": SURVEY_ELIGIBILITY_REDEMPTION_CONTRACT_VERSION,
                "state": "committed",
                "persisted": True,
                "response_id": int(respuesta.id),
                "release_id": int(terminal.release_id),
                "policy_version": terminal.eligibility_policy_version,
                "redeemed_at": _as_utc(terminal.created_at).isoformat(),
            },
            "eligible_population": None,
            "participation_rate": None,
            "abstentions": None,
            "denominator_status": {
                "available": False,
                "reason_code": "survey_eligible_population_not_sealed",
            },
            "regulated_election_certified": False,
            "result_certified": False,
        }
    release = getattr(respuesta, "governance_release", None)
    if release is None and getattr(respuesta, "governance_release_id", None):
        release = db.session.get(
            SurveyGovernanceRelease, int(respuesta.governance_release_id)
        )
    if release is None:
        if getattr(respuesta, "governance_release_id", None):
            raise SurveyEligibilityError(
                "El release de la respuesta no esta disponible.",
                status_code=500,
                reason_code="survey_eligibility_response_release_corrupt",
                action_hint="contact_support",
            )
        return {
            "contract_version": SURVEY_PUBLIC_ELIGIBILITY_CONTRACT_VERSION,
            "credential_required": False,
            "gate_status": "legacy",
            "decision": "not_evaluated",
            "privacy_assurance": "legacy",
            "subject_identifier_exposed": False,
            "plaintext_credential_persisted": False,
            "persist_client_side": False,
            "ballot_secrecy_certified": False,
            "regulated_election_certified": False,
            "result_certified": False,
        }
    policy = _release_eligibility_policy(release)
    if policy["mode"] in _RESTRICTED_MODES:
        raise SurveyEligibilityError(
            "La respuesta restringida no tiene una redencion verificable.",
            status_code=500,
            reason_code="survey_eligibility_response_receipt_corrupt",
            action_hint="contact_support",
        )
    return public_eligibility_contract(release)


__all__ = [
    "ELIGIBILITY_CREDENTIAL_HEADER",
    "ELIGIBILITY_DECISION_VERIFIED",
    "ELIGIBILITY_PRIVACY_ASSURANCE",
    "SurveyEligibilityError",
    "SurveyEligibilityGate",
    "SURVEY_ELIGIBILITY_CREDENTIAL_HEADER",
    "assert_eligibility_grant_active",
    "assert_restricted_release_gate_ready",
    "eligibility_aggregate",
    "issue_eligibility_grant",
    "lock_eligibility_grant_for_submission",
    "public_eligibility_contract",
    "require_eligibility_gate",
    "resolve_eligibility_gate",
    "response_eligibility_contract",
    "revoke_eligibility_grant",
    "serialize_issue_receipt",
    "serialize_revocation_receipt",
    "stage_eligibility_redemption",
]
