"""Deterministic assessment/interview domain service.

The service validates and stages mutations but never commits them. Routes commit
the domain mutation and its AuditEvent atomically. No function in this module
can finalize an admission, rejection, hiring, eligibility, or other decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from typing import Any, Mapping

from extensions import db
from models import (
    AuditEvent,
    ChannelSessionIdentityBinding,
    TenantProfile,
    User,
    WhatsAppInboundTurn,
    WhatsAppOutboundAttempt,
)
from models_interviews import (
    ASSESSMENT_SUBJECT_TYPES,
    INTERVIEW_CHANNELS,
    INTERVIEW_CONSENT_ATTESTATION_KINDS,
    INTERVIEW_CONSENT_SOURCES,
    INTERVIEW_EVIDENCE_TYPES,
    PROGRAM_TYPES,
    AssessmentCase,
    AssessmentProgram,
    AssessmentProgramVersion,
    InterviewConsentChallenge,
    InterviewConsentPresentation,
    InterviewEvidence,
    InterviewSession,
)


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_OPAQUE_REF_RE = re.compile(
    r"^[a-z][a-z0-9._-]{1,31}:[A-Za-z0-9][A-Za-z0-9._-]{5,466}$"
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_MIME_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_POLICY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_MAX_DEFINITION_BYTES = 64 * 1024
_MAX_EVIDENCE_BYTES = 10 * 1024 * 1024 * 1024
INTERVIEW_CONSENT_TEXT_NORMALIZATION = "unicode_nfc_lf_trim_v1"
INTERVIEW_CONSENT_TEXT_FORMAT = "plain_text"
_MAX_CONSENT_TEXT_CODEPOINTS = 8000
_CONSENT_CHALLENGE_TTL_SECONDS = 10 * 60
_CONSENT_ACTION = "grant_consent"
_CONSENT_ACTION_RE = re.compile(
    r"^interview_consent_v3:([A-Za-z0-9_-]{43}):([0-9a-f]{64}):grant_consent$"
)
_CONSENT_PRESENTATION_OUTBOUND_CONTRACT = (
    "interview.consent_presentation_outbound.v1"
)
_FORBIDDEN_DECISION_KEYS = frozenset(
    {
        "decision",
        "final_decision",
        "automated_decision",
        "auto_decision",
        "outcome",
        "admission_status",
        "admission_outcome",
        "employment_outcome",
        "eligibility_outcome",
        "approved",
        "rejected",
    }
)
_ALLOWED_PROVENANCE_KEYS = frozenset(
    {
        "provider",
        "source_channel",
        "source_message_ref",
        "captured_at",
        "mime_type",
        "transformation",
        "model_version",
    }
)
_RAW_EVIDENCE_KEYS = frozenset(
    {"body", "bytes", "content", "data", "payload", "raw", "text", "transcript"}
)


class InterviewDomainError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 422,
        reason_code: str = "interview_validation_failed",
        action_hint: str = "check_request",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason_code = reason_code
        self.action_hint = action_hint
        self.retryable = retryable


@dataclass(frozen=True)
class InterviewMutation:
    value: Any
    replayed: bool = False


def validate_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise InterviewDomainError(
            "Idempotency-Key is required and must be 8-128 opaque characters",
            status_code=400,
            reason_code="interview_idempotency_key_required",
            action_hint="send_idempotency_key",
        )
    return key


def _ensure_allowed_fields(payload: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in payload.keys() if str(key) not in allowed)
    if unknown:
        if any(key.lower() in _FORBIDDEN_DECISION_KEYS for key in unknown):
            raise InterviewDomainError(
                "Automated or final decisions are not supported by this API",
                reason_code="interview_decision_not_supported",
                action_hint="send_for_human_review",
            )
        raise InterviewDomainError(
            f"Unsupported fields: {', '.join(unknown)}",
            reason_code="interview_unknown_fields",
            action_hint="remove_unsupported_fields",
        )


def _reject_forbidden_definition_keys(value: Any, *, path: str = "definition") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_DECISION_KEYS:
                raise InterviewDomainError(
                    f"{path}.{key} cannot define an automated or final decision",
                    reason_code="interview_decision_not_supported",
                    action_hint="remove_automated_decision_rule",
                )
            _reject_forbidden_definition_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden_definition_keys(nested, path=f"{path}[{index}]")


def _required_text(value: Any, field: str, *, max_length: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length:
        raise InterviewDomainError(
            f"{field} is required and must be at most {max_length} characters",
            reason_code="interview_field_invalid",
            action_hint=f"provide_valid_{field}",
        )
    return text


def _opaque_reference(value: Any, field: str, *, max_length: int) -> str:
    """Validate a namespaced reference, never a URL, secret, or embedded object."""

    if not isinstance(value, str):
        raise InterviewDomainError(
            f"{field} must be an opaque namespaced reference",
            reason_code=f"interview_{field}_invalid",
            action_hint=f"provide_opaque_{field}",
        )
    reference = value.strip()
    if len(reference) > max_length or not _OPAQUE_REF_RE.fullmatch(reference):
        raise InterviewDomainError(
            f"{field} must use namespace:opaque-token and cannot be a URL or raw object",
            reason_code=f"interview_{field}_invalid",
            action_hint=f"provide_opaque_{field}",
        )
    token = reference.split(":", 1)[1]
    if not any(character.isalpha() for character in token):
        raise InterviewDomainError(
            f"{field} cannot be a phone number or numeric personal identifier",
            reason_code=f"interview_{field}_invalid",
            action_hint=f"provide_opaque_{field}",
        )
    return reference


def _optional_text(value: Any, field: str, *, max_length: int) -> str | None:
    if value in (None, ""):
        return None
    return _required_text(value, field, max_length=max_length)


def _enum_value(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise InterviewDomainError(
            f"Unsupported {field}",
            reason_code=f"interview_{field}_invalid",
            action_hint=f"choose_supported_{field}",
        )
    return normalized


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InterviewDomainError(
            "Payload must be valid JSON",
            reason_code="interview_json_invalid",
        ) from exc


def _operation_hash(operation: str, payload: Mapping[str, Any]) -> str:
    serialized = _canonical_json({"operation": operation, "payload": payload})
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_definition(value: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict) or not value:
        raise InterviewDomainError(
            "definition must be a non-empty JSON object",
            reason_code="interview_definition_invalid",
            action_hint="provide_versioned_definition",
        )
    _reject_forbidden_definition_keys(value)
    serialized = _canonical_json(value)
    if len(serialized.encode("utf-8")) > _MAX_DEFINITION_BYTES:
        raise InterviewDomainError(
            "definition exceeds 64 KiB",
            status_code=413,
            reason_code="interview_definition_too_large",
            action_hint="reduce_definition_size",
        )
    return dict(value), hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sha256(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise InterviewDomainError(
            f"{field} must be a lowercase SHA-256 digest",
            reason_code="interview_hash_invalid",
            action_hint=f"provide_valid_{field}",
        )
    return normalized


def _normalize_consent_text(value: Any) -> tuple[str, str]:
    """Normalize the exact public consent snapshot and hash it server-side."""

    if not isinstance(value, str):
        raise InterviewDomainError(
            "consent_text is required",
            reason_code="interview_consent_text_required",
            action_hint="provide_public_plain_text_consent",
        )
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip(" \n")
    if not normalized or len(normalized) > _MAX_CONSENT_TEXT_CODEPOINTS:
        raise InterviewDomainError(
            "consent_text must contain 1-8000 Unicode code points",
            reason_code="interview_consent_text_invalid",
            action_hint="provide_public_plain_text_consent",
        )
    if any(0xD800 <= ord(char) <= 0xDFFF for char in normalized):
        raise InterviewDomainError(
            "consent_text contains an invalid Unicode surrogate",
            reason_code="interview_consent_text_unicode_invalid",
            action_hint="remove_lone_unicode_surrogates",
        )
    if any(
        (ord(char) < 32 and char != "\n") or ord(char) == 127
        for char in normalized
    ):
        raise InterviewDomainError(
            "consent_text contains unsupported control characters",
            reason_code="interview_consent_text_control_invalid",
            action_hint="use_plain_text_without_control_characters",
        )
    return normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _consent_text_snapshot(
    payload: Mapping[str, Any],
    *,
    fallback_text: str | None = None,
) -> tuple[str, str]:
    raw_text = payload.get("consent_text")
    if raw_text in (None, ""):
        raw_text = fallback_text
    text, server_hash = _normalize_consent_text(raw_text)
    claimed_hash = payload.get("consent_text_sha256")
    if claimed_hash not in (None, ""):
        if _sha256(claimed_hash, "consent_text_sha256") != server_hash:
            raise InterviewDomainError(
                "consent_text_sha256 does not match the normalized consent_text",
                status_code=409,
                reason_code="interview_consent_text_hash_mismatch",
                action_hint="hash_the_normalized_public_consent_text",
            )
    return text, server_hash


def _submitted_consent_text_hash(value: Any) -> str:
    if value in (None, ""):
        raise InterviewDomainError(
            "The exact consent text hash is required",
            reason_code="interview_consent_text_hash_required",
            action_hint="submit_the_presented_consent_text_hash",
        )
    return _sha256(value, "consent_text_sha256")


def _version_consent_snapshot_is_valid(version: AssessmentProgramVersion) -> bool:
    if (
        not version.consent_text
        or version.consent_text_format != INTERVIEW_CONSENT_TEXT_FORMAT
        or version.consent_text_normalization
        != INTERVIEW_CONSENT_TEXT_NORMALIZATION
    ):
        return False
    try:
        normalized, digest = _normalize_consent_text(version.consent_text)
    except InterviewDomainError:
        return False
    return (
        normalized == version.consent_text
        and digest == version.consent_text_sha256
    )


def _active_whatsapp_subject_binding(
    *,
    tenant: TenantProfile,
    binding_id: Any,
) -> ChannelSessionIdentityBinding:
    try:
        resolved_binding_id = int(binding_id or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InterviewDomainError(
            "A valid WhatsApp subject channel identity binding is required",
            reason_code="interview_subject_identity_binding_required",
            action_hint="pin_subject_channel_identity_before_scheduling",
        ) from exc
    if resolved_binding_id < 1:
        raise InterviewDomainError(
            "A valid WhatsApp subject channel identity binding is required",
            reason_code="interview_subject_identity_binding_required",
            action_hint="pin_subject_channel_identity_before_scheduling",
        )
    binding = ChannelSessionIdentityBinding.query.filter_by(
        tenant_id=tenant.id,
        id=resolved_binding_id,
        channel="whatsapp",
        provider="twilio",
        status=ChannelSessionIdentityBinding.STATUS_ACTIVE,
    ).first()
    if binding is None:
        raise InterviewDomainError(
            "Active tenant-scoped WhatsApp subject channel identity was not found",
            status_code=409,
            reason_code="interview_subject_identity_binding_invalid",
            action_hint="pin_active_subject_channel_identity",
        )
    if not (
        _SHA256_RE.fullmatch(str(binding.identity_hmac or ""))
        and str(binding.identity_version or "").strip()
        and str(binding.chat_session_id or "").strip()
    ):
        raise InterviewDomainError(
            "WhatsApp subject channel identity has an incomplete durable snapshot",
            status_code=409,
            reason_code="interview_subject_identity_binding_invalid",
            action_hint="repair_subject_channel_identity",
        )
    return binding


def _binding_matches_subject_snapshot(
    binding: ChannelSessionIdentityBinding,
    *,
    binding_id: Any,
    identity_version: Any,
    identity_hmac: Any,
    chat_session_id: Any,
) -> bool:
    return bool(
        binding.id == binding_id
        and str(binding.identity_version or "") == str(identity_version or "")
        and hmac.compare_digest(
            str(binding.identity_hmac or ""), str(identity_hmac or "")
        )
        and hmac.compare_digest(
            str(binding.chat_session_id or ""), str(chat_session_id or "")
        )
    )


def _consent_action_id(nonce: str, consent_text_sha256: str) -> str:
    return (
        f"interview_consent_v3:{nonce}:{consent_text_sha256}:{_CONSENT_ACTION}"
    )


def _parse_consent_action_id(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = _CONSENT_ACTION_RE.fullmatch(value)
    if match is None:
        return None
    return match.group(1), match.group(2)


def _outbound_payload_digest(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    return hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def _consent_attestation(value: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InterviewDomainError(
            "A durable participant event or explicit in-person attestation is required",
            reason_code="interview_consent_attestation_required",
            action_hint="provide_consent_attestation",
        )
    _ensure_allowed_fields(
        value,
        {
            "kind",
            "provider",
            "evidence_ref",
            "evidence_sha256",
            "captured_at",
            "attested",
        },
    )
    kind = _enum_value(
        value.get("kind"),
        "consent_attestation_kind",
        tuple(
            item
            for item in INTERVIEW_CONSENT_ATTESTATION_KINDS
            if item != "legacy_unverified"
        ),
    )
    now = datetime.now(timezone.utc)
    if kind == "operator_attestation":
        if source != "in_person" or value.get("attested") is not True:
            raise InterviewDomainError(
                "Operator attestation is allowed only for explicit in-person consent",
                reason_code="interview_consent_attestation_invalid",
                action_hint="capture_participant_event_or_confirm_in_person",
            )
        forbidden = ("provider", "evidence_ref", "evidence_sha256", "captured_at")
        if any(value.get(field) not in (None, "") for field in forbidden):
            raise InterviewDomainError(
                "Operator attestation cannot impersonate participant evidence",
                reason_code="interview_consent_attestation_invalid",
                action_hint="remove_participant_evidence_fields",
            )
        return {
            "kind": kind,
            "provider": None,
            "evidence_ref": None,
            "evidence_sha256": None,
            "captured_at": now,
        }

    provider = str(value.get("provider") or "").strip().lower()
    if not _SAFE_IDENTIFIER_RE.fullmatch(provider):
        raise InterviewDomainError(
            "Consent evidence provider is invalid",
            reason_code="interview_consent_attestation_invalid",
            action_hint="provide_safe_provider_identifier",
        )
    evidence_ref = _opaque_reference(
        value.get("evidence_ref"), "consent_evidence_ref", max_length=500
    )
    evidence_sha256 = _sha256(
        value.get("evidence_sha256"), "consent_evidence_sha256"
    )
    captured_at = _parse_datetime(value.get("captured_at"), "captured_at")
    if captured_at is None or (captured_at - now).total_seconds() > 300:
        raise InterviewDomainError(
            "Consent evidence capture time is invalid",
            reason_code="interview_consent_attestation_invalid",
            action_hint="use_server_synchronized_capture_time",
        )
    return {
        "kind": kind,
        "provider": provider,
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_sha256,
        "captured_at": captured_at,
    }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _presentation_attempt_scope(
    *,
    tenant: TenantProfile,
    session: InterviewSession,
    challenge: InterviewConsentChallenge,
    version: AssessmentProgramVersion,
    attempt: WhatsAppOutboundAttempt,
) -> tuple[WhatsAppInboundTurn, str]:
    if not (
        attempt.tenant_id == tenant.id
        and attempt.provider == "twilio"
        and attempt.status == WhatsAppOutboundAttempt.STATUS_ACCEPTED
        and str(attempt.provider_status or "").lower()
        in InterviewConsentPresentation.VERIFIED_PROVIDER_STATUSES
        and attempt.provider_message_sid
        and attempt.completed_at is not None
        and attempt.message_kind == "interactive"
    ):
        raise InterviewDomainError(
            "Consent delivery has no delivered/read provider evidence",
            status_code=409,
            reason_code="interview_consent_presentation_not_delivered",
            action_hint="wait_for_signed_delivered_or_read_callback",
        )

    source_turn = WhatsAppInboundTurn.query.filter_by(
        id=attempt.inbound_turn_id,
        tenant_id=tenant.id,
        status=WhatsAppInboundTurn.STATUS_COMPLETED,
    ).first()
    if source_turn is None or not (
        source_turn.session_identity_binding_id
        == session.subject_identity_binding_id
        == challenge.expected_identity_binding_id
        and str(source_turn.session_identity_version or "")
        == str(session.subject_identity_version or "")
        == str(challenge.expected_identity_version or "")
        and hmac.compare_digest(
            str(source_turn.session_identity_hmac or ""),
            str(session.subject_identity_hmac or ""),
        )
        and hmac.compare_digest(
            str(source_turn.session_identity_hmac or ""),
            str(challenge.expected_identity_hmac or ""),
        )
        and hmac.compare_digest(
            str(source_turn.chat_session_id or ""),
            str(session.subject_chat_session_id or ""),
        )
        and hmac.compare_digest(
            str(source_turn.chat_session_id or ""),
            str(challenge.expected_chat_session_id or ""),
        )
    ):
        raise InterviewDomainError(
            "Consent delivery targeted a different channel identity or chat session",
            status_code=409,
            reason_code="interview_consent_presentation_identity_mismatch",
            action_hint="deliver_to_pinned_subject_channel_identity",
        )

    payload = attempt.payload_json if isinstance(attempt.payload_json, Mapping) else {}
    payload_digest = _outbound_payload_digest(payload)
    if not payload_digest or not hmac.compare_digest(
        payload_digest, str(attempt.payload_digest or "")
    ):
        raise InterviewDomainError(
            "Consent outbound payload digest is invalid",
            status_code=409,
            reason_code="interview_consent_presentation_payload_mismatch",
            action_hint="use_immutable_durable_outbound_payload",
        )

    policy = payload.get("_chatboc_policy_metadata")
    metadata = (
        policy.get("interview_consent_presentation")
        if isinstance(policy, Mapping)
        else None
    )
    expected_metadata = {
        "contract_version": _CONSENT_PRESENTATION_OUTBOUND_CONTRACT,
        "challenge_id": challenge.id,
        "challenge_nonce_sha256": challenge.nonce_sha256,
        "consent_text_sha256": challenge.consent_text_sha256,
        "action": _CONSENT_ACTION,
        "expected_identity_binding_id": challenge.expected_identity_binding_id,
        "expected_identity_version": challenge.expected_identity_version,
        "expected_chat_session_id": challenge.expected_chat_session_id,
        "template_contract": "interview_consent_exact_text_action.v1",
    }
    if not isinstance(metadata, Mapping) or dict(metadata) != expected_metadata:
        raise InterviewDomainError(
            "Consent outbound policy metadata does not match the challenge",
            status_code=409,
            reason_code="interview_consent_presentation_payload_mismatch",
            action_hint="render_exact_challenge_presentation_contract",
        )

    content_sid = str(payload.get("content_sid") or "").strip()
    variables = payload.get("content_variables")
    if not content_sid or not isinstance(variables, Mapping) or set(variables) != {
        "1",
        "2",
    }:
        raise InterviewDomainError(
            "Consent outbound template variables are incomplete",
            status_code=409,
            reason_code="interview_consent_presentation_payload_mismatch",
            action_hint="render_exact_consent_text_and_action",
        )
    try:
        presented_text, presented_text_sha256 = _normalize_consent_text(
            variables.get("1")
        )
    except InterviewDomainError as exc:
        raise InterviewDomainError(
            "Consent outbound text is not the immutable program snapshot",
            status_code=409,
            reason_code="interview_consent_presentation_text_mismatch",
            action_hint="render_pinned_consent_text",
        ) from exc
    parsed_action = _parse_consent_action_id(variables.get("2"))
    if parsed_action is None:
        raise InterviewDomainError(
            "Consent outbound action is invalid",
            status_code=409,
            reason_code="interview_consent_presentation_action_mismatch",
            action_hint="render_exact_consent_action",
        )
    nonce, action_text_sha256 = parsed_action
    if not (
        presented_text == version.consent_text
        and hmac.compare_digest(
            presented_text_sha256, str(challenge.consent_text_sha256 or "")
        )
        and hmac.compare_digest(
            action_text_sha256, str(challenge.consent_text_sha256 or "")
        )
        and hmac.compare_digest(
            hashlib.sha256(nonce.encode("ascii")).hexdigest(),
            str(challenge.nonce_sha256 or ""),
        )
    ):
        raise InterviewDomainError(
            "Consent outbound text or action does not match the challenge",
            status_code=409,
            reason_code="interview_consent_presentation_payload_mismatch",
            action_hint="render_exact_consent_text_and_action",
        )
    return source_turn, payload_digest


def _verify_registered_consent_presentation(
    *,
    tenant: TenantProfile,
    session: InterviewSession,
    challenge: InterviewConsentChallenge,
    version: AssessmentProgramVersion,
    click_turn: WhatsAppInboundTurn,
) -> InterviewConsentPresentation:
    presentation = InterviewConsentPresentation.query.filter_by(
        tenant_id=tenant.id,
        interview_session_id=session.id,
        consent_challenge_id=challenge.id,
    ).first()
    if presentation is None:
        raise InterviewDomainError(
            "Exact consent-text delivery has not been durably verified",
            status_code=409,
            reason_code="interview_consent_presentation_required",
            action_hint="register_delivered_consent_presentation_before_click",
        )
    attempt = WhatsAppOutboundAttempt.query.filter_by(
        tenant_id=tenant.id,
        attempt_id=presentation.outbound_attempt_id,
    ).first()
    if attempt is None:
        raise InterviewDomainError(
            "Durable consent outbound attempt is unavailable",
            status_code=409,
            reason_code="interview_consent_presentation_invalid",
            action_hint="repair_provider_bound_presentation_evidence",
        )
    _presentation_attempt_scope(
        tenant=tenant,
        session=session,
        challenge=challenge,
        version=version,
        attempt=attempt,
    )
    if not (
        presentation.contract_version == InterviewConsentPresentation.CONTRACT_VERSION
        and presentation.outbound_attempt_id == attempt.attempt_id
        and presentation.outbound_provider == attempt.provider
        and hmac.compare_digest(
            str(presentation.outbound_provider_message_sid or ""),
            str(attempt.provider_message_sid or ""),
        )
        and hmac.compare_digest(
            str(presentation.outbound_payload_sha256 or ""),
            str(attempt.payload_digest or ""),
        )
        and presentation.expected_identity_binding_id
        == challenge.expected_identity_binding_id
        == session.subject_identity_binding_id
        and str(presentation.expected_identity_version or "")
        == str(challenge.expected_identity_version or "")
        == str(session.subject_identity_version or "")
        and hmac.compare_digest(
            str(presentation.expected_identity_hmac or ""),
            str(challenge.expected_identity_hmac or ""),
        )
        and hmac.compare_digest(
            str(presentation.expected_chat_session_id or ""),
            str(challenge.expected_chat_session_id or ""),
        )
        and hmac.compare_digest(
            str(presentation.consent_text_sha256 or ""),
            str(challenge.consent_text_sha256 or ""),
        )
        and hmac.compare_digest(
            str(presentation.challenge_nonce_sha256 or ""),
            str(challenge.nonce_sha256 or ""),
        )
        and presentation.action == _CONSENT_ACTION
        and presentation.outbound_provider_status
        in InterviewConsentPresentation.VERIFIED_PROVIDER_STATUSES
    ):
        raise InterviewDomainError(
            "Consent presentation ledger does not match the immutable challenge",
            status_code=409,
            reason_code="interview_consent_presentation_invalid",
            action_hint="repair_provider_bound_presentation_evidence",
        )
    provider_status_at = _as_utc(presentation.outbound_provider_status_at)
    registered_at = _as_utc(presentation.registered_at)
    click_received_at = _as_utc(click_turn.received_at)
    issued_at = _as_utc(challenge.issued_at)
    expires_at = _as_utc(challenge.expires_at)
    if not all(
        (provider_status_at, registered_at, click_received_at, issued_at, expires_at)
    ) or not (
        issued_at <= provider_status_at <= registered_at <= click_received_at <= expires_at
    ):
        raise InterviewDomainError(
            "Consent delivery evidence must predate the participant channel action",
            status_code=409,
            reason_code="interview_consent_presentation_order_invalid",
            action_hint="deliver_and_register_before_acceptance",
        )
    return presentation


def _verify_participant_event_attestation(
    *,
    tenant: TenantProfile,
    session: InterviewSession,
    version: AssessmentProgramVersion,
    source: str,
    attestation: Mapping[str, Any],
    require_consumed: bool = False,
) -> tuple[
    dict[str, Any],
    InterviewConsentChallenge | None,
    WhatsAppInboundTurn | None,
    InterviewConsentPresentation | None,
]:
    if attestation.get("kind") != "participant_event":
        return dict(attestation), None, None, None
    if source != "whatsapp":
        raise InterviewDomainError(
            "This channel has no verified participant-consent receipt integration",
            status_code=409,
            reason_code="interview_consent_provider_receipt_unavailable",
            action_hint="use_verified_whatsapp_receipt_or_in_person_attestation",
        )

    evidence_ref = str(attestation.get("evidence_ref") or "")
    namespace, _, provider_message_sid = evidence_ref.partition(":")
    if namespace != "whatsapp" or not provider_message_sid:
        raise InterviewDomainError(
            "WhatsApp consent evidence must reference its durable provider event",
            reason_code="interview_consent_provider_receipt_invalid",
            action_hint="provide_whatsapp_provider_message_reference",
        )
    turn = WhatsAppInboundTurn.query.filter_by(
        tenant_id=tenant.id,
        provider=str(attestation.get("provider") or ""),
        provider_message_sid=provider_message_sid,
        status=WhatsAppInboundTurn.STATUS_COMPLETED,
    ).first()
    if turn is None:
        raise InterviewDomainError(
            "Verified WhatsApp consent event was not found",
            status_code=409,
            reason_code="interview_consent_provider_receipt_not_found",
            action_hint="wait_for_signed_provider_event_processing",
        )
    if not hmac.compare_digest(
        str(attestation.get("evidence_sha256") or ""),
        str(turn.payload_digest or ""),
    ):
        raise InterviewDomainError(
            "Consent evidence digest does not match the durable provider event",
            status_code=409,
            reason_code="interview_consent_provider_receipt_mismatch",
            action_hint="use_durable_provider_event_digest",
        )
    result = turn.result_json if isinstance(turn.result_json, Mapping) else {}
    receipt = result.get("interview_consent_receipt")
    if not isinstance(receipt, Mapping) or not (
        receipt.get("contract_version")
        == "interview.consent_provider_receipt.v3"
        and receipt.get("granted") is True
        and receipt.get("action") == _CONSENT_ACTION
    ):
        raise InterviewDomainError(
            "Provider event has no supported one-time consent receipt",
            status_code=409,
            reason_code="interview_consent_provider_receipt_mismatch",
            action_hint="issue_and_use_v2_consent_challenge",
        )
    nonce_sha256 = str(receipt.get("challenge_nonce_sha256") or "").strip().lower()
    receipt_text_sha256 = str(receipt.get("consent_text_sha256") or "").strip().lower()
    if not (
        _SHA256_RE.fullmatch(nonce_sha256)
        and _SHA256_RE.fullmatch(receipt_text_sha256)
    ):
        raise InterviewDomainError(
            "Provider consent receipt has an invalid challenge digest",
            status_code=409,
            reason_code="interview_consent_provider_receipt_mismatch",
            action_hint="issue_and_use_v2_consent_challenge",
        )
    challenge = InterviewConsentChallenge.query.filter_by(
        tenant_id=tenant.id,
        nonce_sha256=nonce_sha256,
    ).first()
    if challenge is None:
        raise InterviewDomainError(
            "The one-time consent challenge was not found",
            status_code=409,
            reason_code="interview_consent_challenge_not_found",
            action_hint="issue_new_consent_challenge",
        )
    if not (
        challenge.contract_version == InterviewConsentChallenge.CONTRACT_VERSION
        and challenge.interview_session_id == session.id
        and challenge.program_version_id == version.id
        and hmac.compare_digest(
            str(challenge.consent_text_sha256 or ""),
            str(version.consent_text_sha256 or ""),
        )
        and hmac.compare_digest(
            receipt_text_sha256,
            str(challenge.consent_text_sha256 or ""),
        )
    ):
        raise InterviewDomainError(
            "Consent challenge does not match this session and program version",
            status_code=409,
            reason_code="interview_consent_challenge_scope_mismatch",
            action_hint="issue_session_specific_consent_challenge",
        )
    binding = ChannelSessionIdentityBinding.query.filter_by(
        tenant_id=tenant.id,
        id=challenge.expected_identity_binding_id,
        status=ChannelSessionIdentityBinding.STATUS_ACTIVE,
        channel="whatsapp",
        provider=str(turn.provider or ""),
    ).first()
    if binding is None or not (
        hmac.compare_digest(
            str(binding.identity_hmac or ""),
            str(challenge.expected_identity_hmac or ""),
        )
        and turn.session_identity_binding_id == binding.id
        and hmac.compare_digest(
            str(turn.session_identity_hmac or ""),
            str(challenge.expected_identity_hmac or ""),
        )
        and str(turn.session_identity_version or "") == str(binding.identity_version or "")
        and hmac.compare_digest(
            str(turn.chat_session_id or ""), str(binding.chat_session_id or "")
        )
        and challenge.expected_identity_binding_id
        == session.subject_identity_binding_id
        and str(challenge.expected_identity_version or "")
        == str(session.subject_identity_version or "")
        == str(binding.identity_version or "")
        and hmac.compare_digest(
            str(challenge.expected_identity_hmac or ""),
            str(session.subject_identity_hmac or ""),
        )
        and hmac.compare_digest(
            str(challenge.expected_chat_session_id or ""),
            str(session.subject_chat_session_id or ""),
        )
        and hmac.compare_digest(
            str(challenge.expected_chat_session_id or ""),
            str(binding.chat_session_id or ""),
        )
    ):
        raise InterviewDomainError(
            "Consent came from a different channel participant identity",
            status_code=409,
            reason_code="interview_consent_participant_identity_mismatch",
            action_hint="use_expected_participant_whatsapp_identity",
        )
    received_at = turn.received_at
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    else:
        received_at = received_at.astimezone(timezone.utc)
    submitted_at = attestation.get("captured_at")
    if isinstance(submitted_at, datetime):
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        else:
            submitted_at = submitted_at.astimezone(timezone.utc)
    if not isinstance(submitted_at, datetime) or abs(
        (submitted_at - received_at).total_seconds()
    ) > 300:
        raise InterviewDomainError(
            "Consent capture time does not match the durable provider event",
            status_code=409,
            reason_code="interview_consent_provider_receipt_mismatch",
            action_hint="use_provider_event_received_at",
        )
    issued_at = challenge.issued_at
    expires_at = challenge.expires_at
    for field_name, value in (("issued_at", issued_at), ("expires_at", expires_at)):
        if value is None:
            raise InterviewDomainError(
                f"Consent challenge {field_name} is invalid",
                status_code=409,
                reason_code="interview_consent_challenge_invalid",
                action_hint="issue_new_consent_challenge",
            )
    issued_at = issued_at.replace(tzinfo=timezone.utc) if issued_at.tzinfo is None else issued_at.astimezone(timezone.utc)
    expires_at = expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at.astimezone(timezone.utc)
    if received_at < issued_at or received_at > expires_at:
        raise InterviewDomainError(
            "Consent challenge was used outside its validity window",
            status_code=409,
            reason_code="interview_consent_challenge_expired",
            action_hint="issue_new_consent_challenge",
        )
    if require_consumed:
        if not (
            challenge.consumed_at is not None
            and challenge.consumed_turn_id == turn.id
            and hmac.compare_digest(
                str(challenge.consumed_provider_message_sid or ""),
                str(turn.provider_message_sid or ""),
            )
        ):
            raise InterviewDomainError(
                "Consent challenge has no matching atomic consumption receipt",
                status_code=409,
                reason_code="interview_consent_challenge_unconsumed",
                action_hint="consume_challenge_atomically",
            )
    elif challenge.consumed_at is not None:
        raise InterviewDomainError(
            "Consent challenge was already consumed",
            status_code=409,
            reason_code="interview_consent_challenge_reused",
            action_hint="issue_new_consent_challenge",
        )
    presentation = _verify_registered_consent_presentation(
        tenant=tenant,
        session=session,
        challenge=challenge,
        version=version,
        click_turn=turn,
    )
    return (
        {**dict(attestation), "captured_at": received_at},
        challenge,
        turn,
        presentation,
    )


def _policy_version(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _POLICY_VERSION_RE.fullmatch(normalized):
        raise InterviewDomainError(
            "consent_policy_version is required",
            reason_code="interview_consent_policy_invalid",
            action_hint="provide_versioned_consent_policy",
        )
    return normalized


def _parse_datetime(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise InterviewDomainError(
            f"{field} must be an ISO-8601 timestamp",
            reason_code="interview_datetime_invalid",
        )
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InterviewDomainError(
            f"{field} must be an ISO-8601 timestamp",
            reason_code="interview_datetime_invalid",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _idempotency_conflict() -> InterviewDomainError:
    return InterviewDomainError(
        "Idempotency-Key was already used with a different request",
        status_code=409,
        reason_code="interview_idempotency_conflict",
        action_hint="reuse_original_payload_or_new_key",
    )


def _audit(
    *,
    tenant: TenantProfile,
    actor: User,
    event_type: str,
    resource_type: str,
    resource_id: int,
    details: dict[str, Any],
) -> None:
    db.session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=actor.id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=str(resource_id),
            details=details,
            ip_address=None,
        )
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_program_version(version: AssessmentProgramVersion) -> dict[str, Any]:
    snapshot_verified = _version_consent_snapshot_is_valid(version)
    payload = {
        "id": version.id,
        "program_id": version.program_id,
        "version_number": version.version_number,
        "status": version.status,
        "definition_hash": version.definition_hash,
        "consent_policy_version": version.consent_policy_version,
        "consent_text": version.consent_text,
        "consent_text_sha256": version.consent_text_sha256,
        "consent_text_format": version.consent_text_format,
        "consent_text_normalization": version.consent_text_normalization,
        "consent_snapshot_complete": snapshot_verified,
        "published_at": _iso(version.published_at),
        "created_at": _iso(version.created_at),
        "immutable": version.published_at is not None,
    }
    if version.status == "draft":
        payload["definition"] = version.definition_json
    return payload


def serialize_program(
    program: AssessmentProgram, version: AssessmentProgramVersion | None = None
) -> dict[str, Any]:
    payload = {
        "contract_version": "assessment.program.v1",
        "id": program.id,
        "tenant_id": program.tenant_id,
        "name": program.name,
        "description": program.description,
        "program_type": program.program_type,
        "status": program.status,
        "published_version_number": program.published_version_number,
        "created_at": _iso(program.created_at),
        "updated_at": _iso(program.updated_at),
    }
    if version is not None:
        payload["version"] = serialize_program_version(version)
    return payload


def serialize_interview_session(session: InterviewSession) -> dict[str, Any]:
    evidence_count = InterviewEvidence.query.filter_by(
        tenant_id=session.tenant_id,
        interview_session_id=session.id,
    ).count()
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=session.tenant_id,
        id=session.program_version_id,
    ).first()
    text_snapshot_verified = bool(
        session.consent_granted
        and version
        and _version_consent_snapshot_is_valid(version)
        and session.consent_text_sha256 == version.consent_text_sha256
    )
    participant_evidence_bound = bool(
        session.consent_attestation_kind == "participant_event"
        and session.consent_evidence_provider
        and session.consent_evidence_ref
        and session.consent_evidence_sha256
        and session.consent_evidence_captured_at
        and session.consent_attested_by_user_id
    )
    participant_evidence_verified = False
    tenant = db.session.get(TenantProfile, session.tenant_id)
    if participant_evidence_bound and version and tenant is not None:
        try:
            _verify_participant_event_attestation(
                tenant=tenant,
                session=session,
                version=version,
                source=str(session.consent_source or ""),
                attestation={
                    "kind": session.consent_attestation_kind,
                    "provider": session.consent_evidence_provider,
                    "evidence_ref": session.consent_evidence_ref,
                    "evidence_sha256": session.consent_evidence_sha256,
                    "captured_at": session.consent_evidence_captured_at,
                },
                require_consumed=True,
            )
            participant_evidence_verified = True
        except InterviewDomainError:
            participant_evidence_verified = False
    return {
        "id": session.id,
        "tenant_id": session.tenant_id,
        "assessment_case_id": session.assessment_case_id,
        "program_version_id": session.program_version_id,
        "status": session.status,
        "channel": session.channel,
        "interviewer_user_id": session.interviewer_user_id,
        "subject_channel_identity": {
            "binding_id": session.subject_identity_binding_id,
            "identity_version": session.subject_identity_version,
            "chat_session_id": session.subject_chat_session_id,
            "channel_identity_pinned": bool(session.subject_identity_binding_id),
            "civil_identity_verified": False,
        },
        "scheduled_for": _iso(session.scheduled_for),
        "consent": {
            "granted": bool(session.consent_granted),
            "policy_version": session.consent_policy_version,
            "text_sha256": session.consent_text_sha256,
            "recorded_at": _iso(session.consent_recorded_at),
            "source": session.consent_source,
            "attestation_kind": session.consent_attestation_kind,
            "evidence_provider": session.consent_evidence_provider,
            "evidence_ref": session.consent_evidence_ref,
            "evidence_sha256": session.consent_evidence_sha256,
            "evidence_captured_at": _iso(session.consent_evidence_captured_at),
            "attested_by_user_id": session.consent_attested_by_user_id,
            "verified_against_program_version": text_snapshot_verified,
            "text_snapshot_verified": text_snapshot_verified,
            "participant_evidence_bound": participant_evidence_bound,
            "participant_evidence_verified": participant_evidence_verified,
            "channel_identity_evidence_verified": participant_evidence_verified,
            "consent_text_delivery_evidence_verified": participant_evidence_verified,
            "civil_identity_verified": False,
            "proof_status": (
                "expected_channel_delivery_and_action_verified"
                if participant_evidence_verified and text_snapshot_verified
                else "operator_attested"
                if session.consent_attestation_kind == "operator_attestation"
                and text_snapshot_verified
                else "legacy_unverified"
                if session.consent_granted
                else "not_granted"
            ),
        },
        "started_at": _iso(session.started_at),
        "completed_at": _iso(session.completed_at),
        "evidence_count": evidence_count,
    }


def serialize_consent_challenge(
    value: tuple[InterviewConsentChallenge, str],
) -> dict[str, Any]:
    challenge, action_id = value
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=challenge.tenant_id,
        id=challenge.program_version_id,
    ).first()
    consent_text = version.consent_text if version is not None else None
    return {
        "contract_version": InterviewConsentChallenge.CONTRACT_VERSION,
        "id": challenge.id,
        "tenant_id": challenge.tenant_id,
        "interview_session_id": challenge.interview_session_id,
        "program_version_id": challenge.program_version_id,
        "consent_text_sha256": challenge.consent_text_sha256,
        "expected_identity_binding_id": challenge.expected_identity_binding_id,
        "expected_identity_version": challenge.expected_identity_version,
        "expected_chat_session_id": challenge.expected_chat_session_id,
        "expires_at": _iso(challenge.expires_at),
        "action_id": action_id,
        "one_time": True,
        "civil_identity_verified": False,
        "presentation_outbound_contract": {
            "contract_version": _CONSENT_PRESENTATION_OUTBOUND_CONTRACT,
            "message_kind": "interactive",
            "required_provider_statuses": list(
                InterviewConsentPresentation.VERIFIED_PROVIDER_STATUSES
            ),
            "content_variables": {"1": consent_text, "2": action_id},
            "policy_metadata": {
                "interview_consent_presentation": {
                    "contract_version": _CONSENT_PRESENTATION_OUTBOUND_CONTRACT,
                    "challenge_id": challenge.id,
                    "challenge_nonce_sha256": challenge.nonce_sha256,
                    "consent_text_sha256": challenge.consent_text_sha256,
                    "action": _CONSENT_ACTION,
                    "expected_identity_binding_id": (
                        challenge.expected_identity_binding_id
                    ),
                    "expected_identity_version": challenge.expected_identity_version,
                    "expected_chat_session_id": challenge.expected_chat_session_id,
                    "template_contract": "interview_consent_exact_text_action.v1",
                }
            },
        },
    }


def serialize_consent_presentation(
    presentation: InterviewConsentPresentation,
) -> dict[str, Any]:
    return {
        "contract_version": presentation.contract_version,
        "id": presentation.id,
        "tenant_id": presentation.tenant_id,
        "interview_session_id": presentation.interview_session_id,
        "consent_challenge_id": presentation.consent_challenge_id,
        "outbound_attempt_id": presentation.outbound_attempt_id,
        "outbound_provider": presentation.outbound_provider,
        "outbound_provider_message_sid": presentation.outbound_provider_message_sid,
        "outbound_provider_status": presentation.outbound_provider_status,
        "outbound_provider_status_at": _iso(
            presentation.outbound_provider_status_at
        ),
        "outbound_payload_sha256": presentation.outbound_payload_sha256,
        "expected_identity_binding_id": presentation.expected_identity_binding_id,
        "consent_text_sha256": presentation.consent_text_sha256,
        "action": presentation.action,
        "registered_at": _iso(presentation.registered_at),
        "delivery_evidence_verified": True,
        "read_receipt_verified": presentation.outbound_provider_status == "read",
        "civil_identity_verified": False,
    }


def serialize_assessment_case(
    case: AssessmentCase, *, include_sessions: bool = True
) -> dict[str, Any]:
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=case.tenant_id,
        id=case.program_version_id,
        program_id=case.program_id,
    ).first()
    payload = {
        "contract_version": "assessment.case.v1",
        "id": case.id,
        "tenant_id": case.tenant_id,
        "program_id": case.program_id,
        "program_version_id": case.program_version_id,
        "subject_type": case.subject_type,
        "subject_ref": case.subject_ref,
        "status": case.status,
        "source_channel": case.source_channel,
        "subject_channel_identity": {
            "binding_id": case.subject_identity_binding_id,
            "identity_version": case.subject_identity_version,
            "chat_session_id": case.subject_chat_session_id,
            "channel_identity_pinned": bool(case.subject_identity_binding_id),
            "civil_identity_verified": False,
        },
        "created_at": _iso(case.created_at),
        "updated_at": _iso(case.updated_at),
        "program_version": (
            {
                "id": version.id,
                "version_number": version.version_number,
                "definition_hash": version.definition_hash,
                "consent_policy_version": version.consent_policy_version,
                "consent_text": version.consent_text,
                "consent_text_sha256": version.consent_text_sha256,
                "consent_text_format": version.consent_text_format,
                "consent_text_normalization": version.consent_text_normalization,
                "immutable": version.published_at is not None,
            }
            if version
            else None
        ),
        "next_action": (
            "human_review"
            if case.status == "awaiting_human_review"
            else "continue_interview"
        ),
    }
    if include_sessions:
        sessions = InterviewSession.query.filter_by(
            tenant_id=case.tenant_id,
            assessment_case_id=case.id,
        ).order_by(InterviewSession.id.asc()).all()
        payload["sessions"] = [serialize_interview_session(item) for item in sessions]
    return payload


def serialize_evidence(evidence: InterviewEvidence) -> dict[str, Any]:
    return {
        "contract_version": "interview.evidence.v1",
        "id": evidence.id,
        "tenant_id": evidence.tenant_id,
        "interview_session_id": evidence.interview_session_id,
        "evidence_type": evidence.evidence_type,
        "source_channel": evidence.source_channel,
        "storage_ref": evidence.storage_ref,
        "content_sha256": evidence.content_sha256,
        "provenance": evidence.provenance_json,
        "size_bytes": evidence.size_bytes,
        "created_at": _iso(evidence.created_at),
    }


def create_program(
    tenant: TenantProfile,
    actor: User,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    _ensure_allowed_fields(
        payload,
        {
            "name",
            "description",
            "program_type",
            "definition",
            "consent_policy_version",
            "consent_text",
            "consent_text_sha256",
        },
    )
    name = _required_text(payload.get("name"), "name", max_length=200)
    description = _optional_text(
        payload.get("description"), "description", max_length=1000
    )
    program_type = _enum_value(
        payload.get("program_type"), "program_type", PROGRAM_TYPES
    )
    definition, definition_hash = _json_definition(payload.get("definition"))
    consent_policy_version = _policy_version(payload.get("consent_policy_version"))
    consent_text, consent_text_sha256 = _consent_text_snapshot(payload)
    normalized = {
        "name": name,
        "description": description,
        "program_type": program_type,
        "definition": definition,
        "consent_policy_version": consent_policy_version,
        "consent_text": consent_text,
        "consent_text_sha256": consent_text_sha256,
    }
    request_hash = _operation_hash("assessment.program.create.v1", normalized)
    existing = AssessmentProgram.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key=idempotency_key,
    ).first()
    if existing:
        if existing.request_hash != request_hash:
            raise _idempotency_conflict()
        version = AssessmentProgramVersion.query.filter_by(
            tenant_id=tenant.id,
            program_id=existing.id,
            version_number=1,
        ).first()
        return InterviewMutation((existing, version), replayed=True)

    program = AssessmentProgram(
        tenant_id=tenant.id,
        name=name,
        description=description,
        program_type=program_type,
        status="draft",
        created_by_user_id=actor.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    db.session.add(program)
    db.session.flush()
    version = AssessmentProgramVersion(
        tenant_id=tenant.id,
        program_id=program.id,
        version_number=1,
        status="draft",
        definition_json=definition,
        definition_hash=definition_hash,
        consent_policy_version=consent_policy_version,
        consent_text=consent_text,
        consent_text_sha256=consent_text_sha256,
        consent_text_format=INTERVIEW_CONSENT_TEXT_FORMAT,
        consent_text_normalization=INTERVIEW_CONSENT_TEXT_NORMALIZATION,
        created_by_user_id=actor.id,
        create_idempotency_key=idempotency_key,
        create_request_hash=request_hash,
    )
    db.session.add(version)
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="assessment.program.created",
        resource_type="assessment_program",
        resource_id=program.id,
        details={
            "contract_version": "assessment.program.v1",
            "program_type": program_type,
            "version_number": 1,
            "definition_hash": definition_hash,
            "consent_policy_version": consent_policy_version,
            "consent_text_sha256": consent_text_sha256,
            "definition_persisted_in_audit": False,
            "consent_text_persisted_in_audit": False,
        },
    )
    return InterviewMutation((program, version))


def create_program_version(
    tenant: TenantProfile,
    actor: User,
    program_id: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    """Create the one next draft from an explicit definition or published copy."""

    _ensure_allowed_fields(
        payload,
        {
            "definition",
            "copy_from_version_number",
            "consent_policy_version",
            "consent_text",
            "consent_text_sha256",
        },
    )
    has_definition = payload.get("definition") is not None
    has_copy_source = payload.get("copy_from_version_number") is not None
    if has_definition == has_copy_source:
        raise InterviewDomainError(
            "Provide exactly one of definition or copy_from_version_number",
            reason_code="interview_version_source_invalid",
            action_hint="provide_definition_or_published_copy",
        )

    source_version = None
    if has_copy_source:
        try:
            copy_from = int(payload.get("copy_from_version_number"))
        except (TypeError, ValueError) as exc:
            raise InterviewDomainError(
                "copy_from_version_number must be a positive integer",
                reason_code="interview_version_source_invalid",
            ) from exc
        if copy_from <= 0:
            raise InterviewDomainError(
                "copy_from_version_number must be a positive integer",
                reason_code="interview_version_source_invalid",
            )
        source_version = AssessmentProgramVersion.query.filter_by(
            tenant_id=tenant.id,
            program_id=program_id,
            version_number=copy_from,
            status="published",
        ).first()
        if not source_version or source_version.published_at is None:
            raise InterviewDomainError(
                "Published source program version not found",
                status_code=404,
                reason_code="assessment_program_version_not_found",
                action_hint="choose_published_program_version",
            )
        definition = json.loads(_canonical_json(source_version.definition_json))
        definition_hash = source_version.definition_hash
        consent_policy_version = (
            _policy_version(payload.get("consent_policy_version"))
            if payload.get("consent_policy_version") not in (None, "")
            else source_version.consent_policy_version
        )
        consent_text, consent_text_sha256 = _consent_text_snapshot(
            payload,
            fallback_text=source_version.consent_text,
        )
        source_descriptor: dict[str, Any] = {
            "kind": "published_copy",
            "version_number": source_version.version_number,
            "definition_hash": source_version.definition_hash,
        }
    else:
        definition, definition_hash = _json_definition(payload.get("definition"))
        consent_policy_version = _policy_version(payload.get("consent_policy_version"))
        consent_text, consent_text_sha256 = _consent_text_snapshot(payload)
        source_descriptor = {
            "kind": "explicit_definition",
            "definition_hash": definition_hash,
        }

    normalized = {
        "program_id": program_id,
        "definition": definition,
        "definition_hash": definition_hash,
        "consent_policy_version": consent_policy_version,
        "consent_text": consent_text,
        "consent_text_sha256": consent_text_sha256,
        "source": source_descriptor,
    }
    request_hash = _operation_hash("assessment.program_version.create.v1", normalized)
    existing = AssessmentProgramVersion.query.filter_by(
        tenant_id=tenant.id,
        create_idempotency_key=idempotency_key,
    ).first()
    if existing:
        if existing.program_id != program_id or existing.create_request_hash != request_hash:
            raise _idempotency_conflict()
        program = AssessmentProgram.query.filter_by(
            tenant_id=tenant.id,
            id=program_id,
        ).first()
        return InterviewMutation((program, existing), replayed=True)

    program = AssessmentProgram.query.filter_by(
        tenant_id=tenant.id,
        id=program_id,
        status="active",
    ).first()
    if not program or not program.published_version_number:
        raise InterviewDomainError(
            "Publish the current program version before creating the next draft",
            status_code=409,
            reason_code="assessment_program_not_published",
            action_hint="publish_program_version",
        )
    existing_draft = AssessmentProgramVersion.query.filter_by(
        tenant_id=tenant.id,
        program_id=program.id,
        status="draft",
    ).first()
    if existing_draft:
        raise InterviewDomainError(
            "A draft program version already exists",
            status_code=409,
            reason_code="assessment_program_draft_exists",
            action_hint="publish_or_discard_existing_draft",
        )

    latest_number = (
        db.session.query(db.func.max(AssessmentProgramVersion.version_number))
        .filter_by(tenant_id=tenant.id, program_id=program.id)
        .scalar()
        or 0
    )
    next_number = int(latest_number) + 1
    version = AssessmentProgramVersion(
        tenant_id=tenant.id,
        program_id=program.id,
        version_number=next_number,
        status="draft",
        definition_json=definition,
        definition_hash=definition_hash,
        consent_policy_version=consent_policy_version,
        consent_text=consent_text,
        consent_text_sha256=consent_text_sha256,
        consent_text_format=INTERVIEW_CONSENT_TEXT_FORMAT,
        consent_text_normalization=INTERVIEW_CONSENT_TEXT_NORMALIZATION,
        created_by_user_id=actor.id,
        create_idempotency_key=idempotency_key,
        create_request_hash=request_hash,
    )
    db.session.add(version)
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="assessment.program.version_created",
        resource_type="assessment_program_version",
        resource_id=version.id,
        details={
            "contract_version": "assessment.program_version.v1",
            "program_id": program.id,
            "version_number": next_number,
            "definition_hash": definition_hash,
            "consent_policy_version": consent_policy_version,
            "consent_text_sha256": consent_text_sha256,
            "source": source_descriptor,
            "definition_persisted_in_audit": False,
            "consent_text_persisted_in_audit": False,
        },
    )
    return InterviewMutation((program, version))


def publish_program_version(
    tenant: TenantProfile,
    actor: User,
    program_id: int,
    version_number: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    _ensure_allowed_fields(payload, set())
    normalized = {"program_id": program_id, "version_number": version_number}
    request_hash = _operation_hash("assessment.program.publish.v1", normalized)

    prior_key = AssessmentProgramVersion.query.filter_by(
        tenant_id=tenant.id,
        publish_idempotency_key=idempotency_key,
    ).first()
    if prior_key:
        if (
            prior_key.program_id != program_id
            or prior_key.version_number != version_number
            or prior_key.publish_request_hash != request_hash
        ):
            raise _idempotency_conflict()
        program = AssessmentProgram.query.filter_by(
            tenant_id=tenant.id, id=program_id
        ).first()
        return InterviewMutation((program, prior_key), replayed=True)

    program = AssessmentProgram.query.filter_by(
        tenant_id=tenant.id, id=program_id
    ).first()
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=tenant.id,
        program_id=program_id,
        version_number=version_number,
    ).first()
    if not program or not version:
        raise InterviewDomainError(
            "Assessment program version not found",
            status_code=404,
            reason_code="assessment_program_version_not_found",
            action_hint="choose_tenant_program_version",
        )
    if version.status == "published" or version.published_at is not None:
        raise InterviewDomainError(
            "Published assessment program versions are immutable",
            status_code=409,
            reason_code="assessment_program_version_immutable",
            action_hint="create_new_program_version",
        )
    if not _version_consent_snapshot_is_valid(version):
        raise InterviewDomainError(
            "Program version has no verifiable public consent snapshot",
            status_code=409,
            reason_code="interview_consent_snapshot_incomplete",
            action_hint="create_new_program_version_with_consent_text",
        )

    version.status = "published"
    version.published_at = datetime.now(timezone.utc)
    version.published_by_user_id = actor.id
    version.publish_idempotency_key = idempotency_key
    version.publish_request_hash = request_hash
    program.status = "active"
    program.published_version_number = version.version_number
    db.session.add_all([program, version])
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="assessment.program.version_published",
        resource_type="assessment_program_version",
        resource_id=version.id,
        details={
            "contract_version": "assessment.program_version.v1",
            "program_id": program.id,
            "version_number": version.version_number,
            "definition_hash": version.definition_hash,
            "consent_policy_version": version.consent_policy_version,
            "consent_text_sha256": version.consent_text_sha256,
            "immutable": True,
            "definition_persisted_in_audit": False,
            "consent_text_persisted_in_audit": False,
        },
    )
    return InterviewMutation((program, version))


def create_assessment_case(
    tenant: TenantProfile,
    actor: User,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    _ensure_allowed_fields(
        payload,
        {
            "program_id",
            "subject_type",
            "subject_ref",
            "source_channel",
            "subject_identity_binding_id",
        },
    )
    try:
        program_id = int(payload.get("program_id"))
    except (TypeError, ValueError) as exc:
        raise InterviewDomainError(
            "program_id must be an integer",
            reason_code="assessment_program_id_invalid",
        ) from exc
    subject_type = _enum_value(
        payload.get("subject_type"), "subject_type", ASSESSMENT_SUBJECT_TYPES
    )
    subject_ref = _opaque_reference(
        payload.get("subject_ref"), "subject_ref", max_length=160
    )
    source_channel = _enum_value(
        payload.get("source_channel"), "source_channel", INTERVIEW_CHANNELS
    )
    subject_binding = None
    if source_channel == "whatsapp":
        subject_binding = _active_whatsapp_subject_binding(
            tenant=tenant,
            binding_id=payload.get("subject_identity_binding_id"),
        )
    elif payload.get("subject_identity_binding_id") not in (None, ""):
        raise InterviewDomainError(
            "A subject channel identity binding is supported only for WhatsApp cases",
            reason_code="interview_subject_identity_binding_not_allowed",
            action_hint="remove_subject_identity_binding",
        )
    normalized = {
        "program_id": program_id,
        "subject_type": subject_type,
        "subject_ref": subject_ref,
        "source_channel": source_channel,
        "subject_identity_binding_id": (
            subject_binding.id if subject_binding is not None else None
        ),
        "subject_identity_version": (
            subject_binding.identity_version if subject_binding is not None else None
        ),
        "subject_identity_hmac": (
            subject_binding.identity_hmac if subject_binding is not None else None
        ),
        "subject_chat_session_id": (
            subject_binding.chat_session_id if subject_binding is not None else None
        ),
    }
    request_hash = _operation_hash("assessment.case.create.v1", normalized)
    existing = AssessmentCase.query.filter_by(
        tenant_id=tenant.id, idempotency_key=idempotency_key
    ).first()
    if existing:
        if existing.request_hash != request_hash:
            raise _idempotency_conflict()
        return InterviewMutation(existing, replayed=True)

    program = AssessmentProgram.query.filter_by(
        tenant_id=tenant.id, id=program_id, status="active"
    ).first()
    if not program or not program.published_version_number:
        raise InterviewDomainError(
            "A published assessment program version is required",
            status_code=409,
            reason_code="assessment_program_not_published",
            action_hint="publish_program_version",
        )
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=tenant.id,
        program_id=program.id,
        version_number=program.published_version_number,
        status="published",
    ).first()
    if not version or version.published_at is None:
        raise InterviewDomainError(
            "Published assessment program version is unavailable",
            status_code=409,
            reason_code="assessment_program_version_unavailable",
            action_hint="repair_program_publication",
        )

    case = AssessmentCase(
        tenant_id=tenant.id,
        program_id=program.id,
        program_version_id=version.id,
        subject_type=subject_type,
        subject_ref=subject_ref,
        status="consent_pending",
        source_channel=source_channel,
        subject_identity_binding_id=(
            subject_binding.id if subject_binding is not None else None
        ),
        subject_identity_version=(
            subject_binding.identity_version if subject_binding is not None else None
        ),
        subject_identity_hmac=(
            subject_binding.identity_hmac if subject_binding is not None else None
        ),
        subject_chat_session_id=(
            subject_binding.chat_session_id if subject_binding is not None else None
        ),
        created_by_user_id=actor.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    db.session.add(case)
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="assessment.case.created",
        resource_type="assessment_case",
        resource_id=case.id,
        details={
            "contract_version": "assessment.case.v1",
            "program_id": program.id,
            "program_version_id": version.id,
            "program_version_number": version.version_number,
            "subject_type": subject_type,
            "source_channel": source_channel,
            "subject_identity_binding_id": (
                subject_binding.id if subject_binding is not None else None
            ),
            "civil_identity_verified": False,
            "subject_ref_persisted_in_audit": False,
        },
    )
    return InterviewMutation(case)


def get_assessment_case(tenant: TenantProfile, case_id: int) -> AssessmentCase:
    case = AssessmentCase.query.filter_by(tenant_id=tenant.id, id=case_id).first()
    if not case:
        raise InterviewDomainError(
            "Assessment case not found",
            status_code=404,
            reason_code="assessment_case_not_found",
            action_hint="choose_tenant_case",
        )
    return case


def create_interview_session(
    tenant: TenantProfile,
    actor: User,
    case_id: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    _ensure_allowed_fields(payload, {"channel", "scheduled_for"})
    channel = _enum_value(payload.get("channel"), "channel", INTERVIEW_CHANNELS)
    scheduled_for = _parse_datetime(payload.get("scheduled_for"), "scheduled_for")
    normalized = {
        "case_id": case_id,
        "channel": channel,
        "scheduled_for": _iso(scheduled_for),
    }
    request_hash = _operation_hash("interview.session.create.v1", normalized)
    existing = InterviewSession.query.filter_by(
        tenant_id=tenant.id, create_idempotency_key=idempotency_key
    ).first()
    if existing:
        if existing.create_request_hash != request_hash:
            raise _idempotency_conflict()
        return InterviewMutation(existing, replayed=True)

    case = get_assessment_case(tenant, case_id)
    if case.status != "consent_pending":
        raise InterviewDomainError(
            f"Cannot schedule interview from case status {case.status}",
            status_code=409,
            reason_code="assessment_case_transition_invalid",
            action_hint="reload_case",
        )
    subject_binding = None
    if channel == "whatsapp":
        if case.source_channel != "whatsapp" or not case.subject_identity_binding_id:
            raise InterviewDomainError(
                "WhatsApp interviews require a subject identity pinned on the case",
                status_code=409,
                reason_code="interview_subject_identity_not_pinned",
                action_hint="create_case_with_subject_channel_identity",
            )
        subject_binding = _active_whatsapp_subject_binding(
            tenant=tenant,
            binding_id=case.subject_identity_binding_id,
        )
        if not _binding_matches_subject_snapshot(
            subject_binding,
            binding_id=case.subject_identity_binding_id,
            identity_version=case.subject_identity_version,
            identity_hmac=case.subject_identity_hmac,
            chat_session_id=case.subject_chat_session_id,
        ):
            raise InterviewDomainError(
                "The case subject channel identity changed after it was pinned",
                status_code=409,
                reason_code="interview_subject_identity_snapshot_mismatch",
                action_hint="quarantine_and_review_case_identity",
            )
    session = InterviewSession(
        tenant_id=tenant.id,
        assessment_case_id=case.id,
        program_version_id=case.program_version_id,
        status="scheduled",
        channel=channel,
        subject_identity_binding_id=(
            subject_binding.id if subject_binding is not None else None
        ),
        subject_identity_version=(
            subject_binding.identity_version if subject_binding is not None else None
        ),
        subject_identity_hmac=(
            subject_binding.identity_hmac if subject_binding is not None else None
        ),
        subject_chat_session_id=(
            subject_binding.chat_session_id if subject_binding is not None else None
        ),
        interviewer_user_id=actor.id,
        scheduled_for=scheduled_for,
        consent_granted=False,
        create_idempotency_key=idempotency_key,
        create_request_hash=request_hash,
    )
    case.status = "scheduled"
    db.session.add_all([case, session])
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="interview.session.created",
        resource_type="interview_session",
        resource_id=session.id,
        details={
            "contract_version": "interview.session.v1",
            "assessment_case_id": case.id,
            "program_version_id": case.program_version_id,
            "channel": channel,
            "subject_identity_binding_id": (
                subject_binding.id if subject_binding is not None else None
            ),
            "civil_identity_verified": False,
            "consent_granted": False,
        },
    )
    return InterviewMutation(session)


def issue_interview_consent_challenge(
    tenant: TenantProfile,
    actor: User,
    session_id: int,
    payload: Mapping[str, Any],
) -> InterviewMutation:
    """Issue a short-lived opaque control for one expected WhatsApp identity."""

    _ensure_allowed_fields(payload, set())

    session = (
        InterviewSession.query.filter_by(tenant_id=tenant.id, id=session_id)
        .with_for_update()
        .first()
    )
    if session is None:
        raise InterviewDomainError(
            "Interview session not found",
            status_code=404,
            reason_code="interview_session_not_found",
            action_hint="choose_tenant_session",
        )
    case = get_assessment_case(tenant, session.assessment_case_id)
    if session.status != "scheduled" or case.status != "scheduled":
        raise InterviewDomainError(
            "Consent challenge requires a scheduled interview session",
            status_code=409,
            reason_code="interview_session_transition_invalid",
            action_hint="reload_session",
        )
    if session.channel != "whatsapp":
        raise InterviewDomainError(
            "One-time provider consent challenges are available only on WhatsApp",
            status_code=409,
            reason_code="interview_consent_provider_receipt_unavailable",
            action_hint="use_in_person_attestation_for_in_person_sessions",
        )
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=tenant.id,
        id=session.program_version_id,
        status="published",
    ).first()
    if version is None or not _version_consent_snapshot_is_valid(version):
        raise InterviewDomainError(
            "Pinned program version has no verifiable public consent snapshot",
            status_code=409,
            reason_code="interview_consent_snapshot_incomplete",
            action_hint="repair_program_version",
        )
    if not (
        case.subject_identity_binding_id
        and session.subject_identity_binding_id
        and case.subject_identity_binding_id == session.subject_identity_binding_id
        and str(case.subject_identity_version or "")
        == str(session.subject_identity_version or "")
        and hmac.compare_digest(
            str(case.subject_identity_hmac or ""),
            str(session.subject_identity_hmac or ""),
        )
        and hmac.compare_digest(
            str(case.subject_chat_session_id or ""),
            str(session.subject_chat_session_id or ""),
        )
    ):
        raise InterviewDomainError(
            "Session subject identity does not match the case snapshot",
            status_code=409,
            reason_code="interview_subject_identity_snapshot_mismatch",
            action_hint="quarantine_and_review_case_identity",
        )
    binding = _active_whatsapp_subject_binding(
        tenant=tenant,
        binding_id=session.subject_identity_binding_id,
    )
    if not _binding_matches_subject_snapshot(
        binding,
        binding_id=session.subject_identity_binding_id,
        identity_version=session.subject_identity_version,
        identity_hmac=session.subject_identity_hmac,
        chat_session_id=session.subject_chat_session_id,
    ):
        raise InterviewDomainError(
            "Pinned subject channel identity changed before challenge issuance",
            status_code=409,
            reason_code="interview_subject_identity_snapshot_mismatch",
            action_hint="quarantine_and_review_case_identity",
        )

    now = datetime.now(timezone.utc)
    # A retry after an ambiguous HTTP response must not leave two usable
    # controls. The session row is locked above, so the newest issuance
    # deterministically supersedes every still-active unconsumed challenge.
    InterviewConsentChallenge.query.filter(
        InterviewConsentChallenge.tenant_id == tenant.id,
        InterviewConsentChallenge.interview_session_id == session.id,
        InterviewConsentChallenge.consumed_at.is_(None),
        InterviewConsentChallenge.expires_at > now,
    ).update(
        {InterviewConsentChallenge.expires_at: now},
        synchronize_session=False,
    )
    nonce = secrets.token_urlsafe(32)
    nonce_sha256 = hashlib.sha256(nonce.encode("ascii")).hexdigest()
    challenge = InterviewConsentChallenge(
        tenant_id=tenant.id,
        interview_session_id=session.id,
        program_version_id=version.id,
        consent_text_sha256=version.consent_text_sha256,
        expected_identity_binding_id=binding.id,
        expected_identity_version=binding.identity_version,
        expected_identity_hmac=binding.identity_hmac,
        expected_chat_session_id=binding.chat_session_id,
        nonce_sha256=nonce_sha256,
        expires_at=now + timedelta(seconds=_CONSENT_CHALLENGE_TTL_SECONDS),
        issued_by_user_id=actor.id,
        issued_at=now,
        contract_version=InterviewConsentChallenge.CONTRACT_VERSION,
    )
    db.session.add(challenge)
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="interview.consent_challenge.issued",
        resource_type="interview_consent_challenge",
        resource_id=challenge.id,
        details={
            "contract_version": InterviewConsentChallenge.CONTRACT_VERSION,
            "interview_session_id": session.id,
            "program_version_id": version.id,
            "consent_text_sha256": version.consent_text_sha256,
            "expected_identity_binding_id": binding.id,
            "expires_at": _iso(challenge.expires_at),
            "raw_nonce_persisted": False,
        },
    )
    return InterviewMutation(
        (challenge, _consent_action_id(nonce, version.consent_text_sha256))
    )


def register_interview_consent_presentation(
    tenant: TenantProfile,
    actor: User,
    session_id: int,
    challenge_id: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    """Pin signed provider delivery evidence for one exact consent payload."""

    _ensure_allowed_fields(payload, {"outbound_attempt_id"})
    attempt_id = _required_text(
        payload.get("outbound_attempt_id"),
        "outbound_attempt_id",
        max_length=36,
    )
    if not _SAFE_IDENTIFIER_RE.fullmatch(attempt_id):
        raise InterviewDomainError(
            "outbound_attempt_id is invalid",
            reason_code="interview_consent_presentation_attempt_invalid",
            action_hint="provide_durable_outbound_attempt_id",
        )
    normalized = {
        "session_id": session_id,
        "challenge_id": challenge_id,
        "outbound_attempt_id": attempt_id,
    }
    request_hash = _operation_hash(
        "interview.consent_presentation.register.v1", normalized
    )
    prior_key = InterviewConsentPresentation.query.filter_by(
        tenant_id=tenant.id,
        registration_idempotency_key=idempotency_key,
    ).first()
    if prior_key is not None:
        if not (
            prior_key.interview_session_id == session_id
            and prior_key.consent_challenge_id == challenge_id
            and prior_key.registration_request_hash == request_hash
        ):
            raise _idempotency_conflict()
        return InterviewMutation(prior_key, replayed=True)

    session = (
        InterviewSession.query.filter_by(tenant_id=tenant.id, id=session_id)
        .with_for_update()
        .first()
    )
    if session is None:
        raise InterviewDomainError(
            "Interview session not found",
            status_code=404,
            reason_code="interview_session_not_found",
            action_hint="choose_tenant_session",
        )
    if session.status != "scheduled" or session.channel != "whatsapp":
        raise InterviewDomainError(
            "Consent presentation requires a scheduled WhatsApp interview",
            status_code=409,
            reason_code="interview_session_transition_invalid",
            action_hint="reload_session",
        )
    challenge = (
        InterviewConsentChallenge.query.filter_by(
            tenant_id=tenant.id,
            id=challenge_id,
            interview_session_id=session.id,
        )
        .with_for_update()
        .first()
    )
    if challenge is None:
        raise InterviewDomainError(
            "Consent challenge not found",
            status_code=404,
            reason_code="interview_consent_challenge_not_found",
            action_hint="issue_session_specific_consent_challenge",
        )
    now = datetime.now(timezone.utc)
    issued_at = _as_utc(challenge.issued_at)
    expires_at = _as_utc(challenge.expires_at)
    if not (
        challenge.contract_version == InterviewConsentChallenge.CONTRACT_VERSION
        and challenge.consumed_at is None
        and issued_at is not None
        and expires_at is not None
        and issued_at <= now <= expires_at
    ):
        raise InterviewDomainError(
            "Consent challenge is expired, consumed, or uses an unsupported contract",
            status_code=409,
            reason_code="interview_consent_challenge_expired",
            action_hint="issue_new_consent_challenge",
        )
    existing = InterviewConsentPresentation.query.filter_by(
        tenant_id=tenant.id,
        consent_challenge_id=challenge.id,
    ).first()
    if existing is not None:
        raise InterviewDomainError(
            "Consent challenge already has immutable delivery evidence",
            status_code=409,
            reason_code="interview_consent_presentation_already_registered",
            action_hint="reuse_original_idempotency_key",
        )
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=tenant.id,
        id=session.program_version_id,
        status="published",
    ).first()
    if version is None or not _version_consent_snapshot_is_valid(version):
        raise InterviewDomainError(
            "Pinned published program version is unavailable",
            status_code=409,
            reason_code="assessment_program_version_unavailable",
            action_hint="repair_program_version",
        )
    attempt = WhatsAppOutboundAttempt.query.filter_by(
        tenant_id=tenant.id,
        attempt_id=attempt_id,
    ).first()
    if attempt is None:
        raise InterviewDomainError(
            "Durable WhatsApp outbound attempt was not found",
            status_code=409,
            reason_code="interview_consent_presentation_attempt_not_found",
            action_hint="use_tenant_scoped_outbound_attempt",
        )
    _, payload_digest = _presentation_attempt_scope(
        tenant=tenant,
        session=session,
        challenge=challenge,
        version=version,
        attempt=attempt,
    )
    provider_status_at = _as_utc(attempt.completed_at)
    if provider_status_at is None or not (
        issued_at <= provider_status_at <= now <= expires_at
    ):
        raise InterviewDomainError(
            "Provider delivery evidence is outside the challenge validity window",
            status_code=409,
            reason_code="interview_consent_presentation_order_invalid",
            action_hint="deliver_and_register_current_challenge",
        )
    payload_json = attempt.payload_json if isinstance(attempt.payload_json, Mapping) else {}
    presentation = InterviewConsentPresentation(
        tenant_id=tenant.id,
        interview_session_id=session.id,
        consent_challenge_id=challenge.id,
        outbound_attempt_id=attempt.attempt_id,
        outbound_provider=attempt.provider,
        outbound_provider_message_sid=attempt.provider_message_sid,
        outbound_content_sid=str(payload_json.get("content_sid") or ""),
        outbound_provider_status=str(attempt.provider_status or "").lower(),
        outbound_provider_status_at=provider_status_at,
        outbound_payload_sha256=payload_digest,
        expected_identity_binding_id=challenge.expected_identity_binding_id,
        expected_identity_version=challenge.expected_identity_version,
        expected_identity_hmac=challenge.expected_identity_hmac,
        expected_chat_session_id=challenge.expected_chat_session_id,
        consent_text_sha256=challenge.consent_text_sha256,
        challenge_nonce_sha256=challenge.nonce_sha256,
        action=_CONSENT_ACTION,
        registered_by_user_id=actor.id,
        registration_idempotency_key=idempotency_key,
        registration_request_hash=request_hash,
        registered_at=now,
        contract_version=InterviewConsentPresentation.CONTRACT_VERSION,
    )
    db.session.add(presentation)
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="interview.consent_presentation.registered",
        resource_type="interview_consent_presentation",
        resource_id=presentation.id,
        details={
            "contract_version": presentation.contract_version,
            "interview_session_id": session.id,
            "consent_challenge_id": challenge.id,
            "outbound_provider": presentation.outbound_provider,
            "outbound_provider_status": presentation.outbound_provider_status,
            "outbound_provider_status_at": _iso(provider_status_at),
            "outbound_payload_sha256": payload_digest,
            "expected_identity_binding_id": challenge.expected_identity_binding_id,
            "consent_text_sha256": challenge.consent_text_sha256,
            "action": _CONSENT_ACTION,
            "provider_message_sid_persisted_in_audit": False,
            "raw_nonce_persisted": False,
            "civil_identity_verified": False,
        },
    )
    return InterviewMutation(presentation)


def start_interview_session(
    tenant: TenantProfile,
    actor: User,
    session_id: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    _ensure_allowed_fields(payload, {"consent"})
    consent = payload.get("consent")
    if not isinstance(consent, dict):
        raise InterviewDomainError(
            "Explicit versioned consent is required",
            reason_code="interview_consent_required",
            action_hint="record_explicit_consent",
        )
    _ensure_allowed_fields(
        consent,
        {"granted", "policy_version", "text_sha256", "source", "attestation"},
    )
    granted = consent.get("granted") is True
    if not granted:
        raise InterviewDomainError(
            "Explicit consent must be granted before starting",
            reason_code="interview_consent_required",
            action_hint="record_explicit_consent",
        )
    policy_version = _policy_version(consent.get("policy_version"))
    consent_text_sha256 = _submitted_consent_text_hash(
        consent.get("text_sha256")
    )
    source = _enum_value(
        consent.get("source"), "consent_source", INTERVIEW_CONSENT_SOURCES
    )
    attestation = _consent_attestation(consent.get("attestation"), source=source)
    normalized = {
        "session_id": session_id,
        "consent": {
            "granted": granted,
            "policy_version": policy_version,
            "text_sha256": consent_text_sha256,
            "source": source,
            "attestation": {
                "kind": attestation["kind"],
                "provider": attestation["provider"],
                "evidence_ref": attestation["evidence_ref"],
                "evidence_sha256": attestation["evidence_sha256"],
                "captured_at": _iso(attestation["captured_at"]),
            },
        },
    }
    request_hash = _operation_hash("interview.session.start.v1", normalized)
    prior_key = InterviewSession.query.filter_by(
        tenant_id=tenant.id, start_idempotency_key=idempotency_key
    ).first()
    if prior_key:
        if prior_key.id != session_id or prior_key.start_request_hash != request_hash:
            raise _idempotency_conflict()
        return InterviewMutation(prior_key, replayed=True)

    session = (
        InterviewSession.query.filter_by(tenant_id=tenant.id, id=session_id)
        .with_for_update()
        .first()
    )
    if not session:
        raise InterviewDomainError(
            "Interview session not found",
            status_code=404,
            reason_code="interview_session_not_found",
            action_hint="choose_tenant_session",
        )
    case = get_assessment_case(tenant, session.assessment_case_id)
    if session.status != "scheduled" or case.status != "scheduled":
        raise InterviewDomainError(
            "Interview session cannot be started from its current state",
            status_code=409,
            reason_code="interview_session_transition_invalid",
            action_hint="reload_session",
        )
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=tenant.id, id=session.program_version_id, status="published"
    ).first()
    if not version:
        raise InterviewDomainError(
            "Pinned published program version is unavailable",
            status_code=409,
            reason_code="assessment_program_version_unavailable",
            action_hint="repair_program_version",
        )
    if policy_version != version.consent_policy_version:
        raise InterviewDomainError(
            "Consent policy version does not match the pinned program version",
            status_code=409,
            reason_code="interview_consent_version_mismatch",
            action_hint="present_pinned_consent_policy",
        )
    if consent_text_sha256 != version.consent_text_sha256:
        raise InterviewDomainError(
            "Consent text hash does not match the pinned program version",
            status_code=409,
            reason_code="interview_consent_text_mismatch",
            action_hint="present_pinned_consent_text",
        )
    if not _version_consent_snapshot_is_valid(version):
        raise InterviewDomainError(
            "Pinned program version has no verifiable public consent snapshot",
            status_code=409,
            reason_code="interview_consent_snapshot_incomplete",
            action_hint="create_new_program_version_with_consent_text",
        )
    if source != session.channel:
        raise InterviewDomainError(
            "Consent source must match the scheduled interview channel",
            status_code=409,
            reason_code="interview_consent_channel_mismatch",
            action_hint="capture_consent_in_the_pinned_session_channel",
        )
    (
        attestation,
        consent_challenge,
        consent_turn,
        consent_presentation,
    ) = _verify_participant_event_attestation(
        tenant=tenant,
        session=session,
        version=version,
        source=source,
        attestation=attestation,
    )
    captured_at = attestation["captured_at"]
    session_created_at = session.created_at
    if session_created_at is not None:
        if session_created_at.tzinfo is None:
            session_created_at = session_created_at.replace(tzinfo=timezone.utc)
        else:
            session_created_at = session_created_at.astimezone(timezone.utc)
        if captured_at < session_created_at - timedelta(minutes=5):
            raise InterviewDomainError(
                "Consent evidence predates this interview session",
                status_code=409,
                reason_code="interview_consent_attestation_stale",
                action_hint="capture_session_specific_consent",
            )
    evidence_ref = attestation["evidence_ref"]
    if evidence_ref:
        reused = InterviewSession.query.filter(
            InterviewSession.tenant_id == tenant.id,
            InterviewSession.consent_evidence_ref == evidence_ref,
            InterviewSession.id != session.id,
        ).first()
        if reused:
            raise InterviewDomainError(
                "Consent evidence is already bound to another interview session",
                status_code=409,
                reason_code="interview_consent_evidence_reused",
                action_hint="capture_session_specific_consent",
            )

    now = datetime.now(timezone.utc)
    if consent_challenge is not None:
        consumed = InterviewConsentChallenge.query.filter(
            InterviewConsentChallenge.id == consent_challenge.id,
            InterviewConsentChallenge.tenant_id == tenant.id,
            InterviewConsentChallenge.consumed_at.is_(None),
            InterviewConsentChallenge.expires_at >= now,
        ).update(
            {
                InterviewConsentChallenge.consumed_at: now,
                InterviewConsentChallenge.consumed_by_user_id: actor.id,
                InterviewConsentChallenge.consumed_turn_id: consent_turn.id,
                InterviewConsentChallenge.consumed_provider_message_sid: (
                    consent_turn.provider_message_sid
                ),
            },
            synchronize_session=False,
        )
        if consumed != 1:
            raise InterviewDomainError(
                "Consent challenge expired or was already consumed",
                status_code=409,
                reason_code="interview_consent_challenge_reused",
                action_hint="issue_new_consent_challenge",
            )
    activated = InterviewSession.query.filter(
        InterviewSession.id == session.id,
        InterviewSession.tenant_id == tenant.id,
        InterviewSession.status == "scheduled",
        InterviewSession.consent_granted.is_(False),
        InterviewSession.start_idempotency_key.is_(None),
    ).update(
        {
            InterviewSession.status: "active",
            InterviewSession.consent_granted: True,
            InterviewSession.consent_policy_version: policy_version,
            InterviewSession.consent_text_sha256: version.consent_text_sha256,
            InterviewSession.consent_recorded_at: now,
            InterviewSession.consent_source: source,
            InterviewSession.consent_attestation_kind: attestation["kind"],
            InterviewSession.consent_evidence_provider: attestation["provider"],
            InterviewSession.consent_evidence_ref: attestation["evidence_ref"],
            InterviewSession.consent_evidence_sha256: attestation["evidence_sha256"],
            InterviewSession.consent_evidence_captured_at: attestation["captured_at"],
            InterviewSession.consent_attested_by_user_id: actor.id,
            InterviewSession.start_idempotency_key: idempotency_key,
            InterviewSession.start_request_hash: request_hash,
            InterviewSession.started_at: now,
        },
        synchronize_session=False,
    )
    if activated != 1:
        raise InterviewDomainError(
            "Interview session was concurrently started",
            status_code=409,
            reason_code="interview_session_transition_invalid",
            action_hint="reload_session",
        )
    db.session.expire(session)
    case.status = "in_progress"
    db.session.add(case)
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="interview.session.started",
        resource_type="interview_session",
        resource_id=session.id,
        details={
            "contract_version": "interview.session.v1",
            "assessment_case_id": case.id,
            "program_version_id": version.id,
            "consent_granted": True,
            "consent_policy_version": policy_version,
            "consent_text_sha256": version.consent_text_sha256,
            "consent_source": source,
            "consent_attestation_kind": attestation["kind"],
            "consent_evidence_provider": attestation["provider"],
            "consent_evidence_sha256": attestation["evidence_sha256"],
            "consent_evidence_captured_at": _iso(attestation["captured_at"]),
            "consent_presentation_id": (
                consent_presentation.id if consent_presentation is not None else None
            ),
            "civil_identity_verified": False,
            "consent_evidence_ref_persisted_in_audit": False,
            "consent_attested_by_user_id": actor.id,
            "consent_text_persisted_in_audit": False,
        },
    )
    return InterviewMutation(session)


def complete_interview_session(
    tenant: TenantProfile,
    actor: User,
    session_id: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    _ensure_allowed_fields(payload, set())
    normalized = {"session_id": session_id}
    request_hash = _operation_hash("interview.session.complete.v1", normalized)
    prior_key = InterviewSession.query.filter_by(
        tenant_id=tenant.id, complete_idempotency_key=idempotency_key
    ).first()
    if prior_key:
        if prior_key.id != session_id or prior_key.complete_request_hash != request_hash:
            raise _idempotency_conflict()
        return InterviewMutation(prior_key, replayed=True)

    session = InterviewSession.query.filter_by(
        tenant_id=tenant.id, id=session_id
    ).first()
    if not session:
        raise InterviewDomainError(
            "Interview session not found",
            status_code=404,
            reason_code="interview_session_not_found",
            action_hint="choose_tenant_session",
        )
    case = get_assessment_case(tenant, session.assessment_case_id)
    if session.status != "active" or case.status != "in_progress":
        raise InterviewDomainError(
            "Interview session cannot be completed from its current state",
            status_code=409,
            reason_code="interview_session_transition_invalid",
            action_hint="reload_session",
        )

    now = datetime.now(timezone.utc)
    session.status = "completed"
    session.complete_idempotency_key = idempotency_key
    session.complete_request_hash = request_hash
    session.completed_at = now
    case.status = "awaiting_human_review"
    db.session.add_all([session, case])
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="interview.session.completed",
        resource_type="interview_session",
        resource_id=session.id,
        details={
            "contract_version": "interview.session.v1",
            "assessment_case_id": case.id,
            "session_status": "completed",
            "case_status": "awaiting_human_review",
            "review_required": True,
            "next_action": "human_review",
        },
    )
    return InterviewMutation(session)


def _provenance(value: Any, *, expected_channel: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise InterviewDomainError(
            "provenance must be a non-empty object",
            reason_code="interview_evidence_provenance_required",
            action_hint="provide_evidence_provenance",
        )
    unknown = sorted(str(key) for key in value if str(key) not in _ALLOWED_PROVENANCE_KEYS)
    if unknown:
        raise InterviewDomainError(
            f"Raw or unsupported provenance fields: {', '.join(unknown)}",
            reason_code="interview_evidence_raw_content_forbidden",
            action_hint="send_reference_metadata_only",
        )
    required = {"provider", "source_channel", "captured_at"}
    missing = sorted(key for key in required if value.get(key) in (None, ""))
    if missing:
        raise InterviewDomainError(
            f"Missing provenance fields: {', '.join(missing)}",
            reason_code="interview_evidence_provenance_required",
            action_hint="provide_evidence_provenance",
        )

    provider = str(value.get("provider") or "").strip().lower()
    if not _SAFE_IDENTIFIER_RE.fullmatch(provider):
        raise InterviewDomainError(
            "provenance.provider must be a safe provider identifier",
            reason_code="interview_evidence_provenance_invalid",
        )
    source_channel = _enum_value(
        value.get("source_channel"), "source_channel", INTERVIEW_CHANNELS
    )
    if source_channel != expected_channel:
        raise InterviewDomainError(
            "provenance.source_channel must match source_channel",
            reason_code="interview_evidence_provenance_invalid",
        )
    captured_at = _parse_datetime(value.get("captured_at"), "captured_at")
    if captured_at is None:
        raise InterviewDomainError(
            "provenance.captured_at is required",
            reason_code="interview_evidence_provenance_required",
        )
    if (captured_at - datetime.now(timezone.utc)).total_seconds() > 300:
        raise InterviewDomainError(
            "provenance.captured_at cannot be more than five minutes in the future",
            reason_code="interview_evidence_provenance_invalid",
            action_hint="use_server_synchronized_capture_time",
        )

    normalized: dict[str, Any] = {
        "provider": provider,
        "source_channel": source_channel,
        "captured_at": _iso(captured_at),
    }
    if "source_message_ref" in value:
        normalized["source_message_ref"] = _opaque_reference(
            value.get("source_message_ref"),
            "source_message_ref",
            max_length=160,
        )
    if "mime_type" in value:
        mime_type = str(value.get("mime_type") or "").strip().lower()
        if not _MIME_TYPE_RE.fullmatch(mime_type):
            raise InterviewDomainError(
                "provenance.mime_type must be a media type without parameters",
                reason_code="interview_evidence_provenance_invalid",
            )
        normalized["mime_type"] = mime_type
    for key in ("transformation", "model_version"):
        if key not in value:
            continue
        identifier = str(value.get(key) or "").strip().lower()
        if not _SAFE_IDENTIFIER_RE.fullmatch(identifier):
            raise InterviewDomainError(
                f"provenance.{key} must be a safe identifier",
                reason_code="interview_evidence_provenance_invalid",
            )
        normalized[key] = identifier
    return normalized


def create_interview_evidence(
    tenant: TenantProfile,
    actor: User,
    session_id: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    raw_fields = sorted(
        str(key) for key in payload if str(key).strip().lower() in _RAW_EVIDENCE_KEYS
    )
    if raw_fields:
        raise InterviewDomainError(
            f"Raw evidence fields are forbidden: {', '.join(raw_fields)}",
            reason_code="interview_evidence_raw_content_forbidden",
            action_hint="store_media_then_send_reference",
        )
    _ensure_allowed_fields(
        payload,
        {
            "evidence_type",
            "source_channel",
            "storage_ref",
            "content_sha256",
            "provenance",
            "size_bytes",
        },
    )
    evidence_type = _enum_value(
        payload.get("evidence_type"), "evidence_type", INTERVIEW_EVIDENCE_TYPES
    )
    source_channel = _enum_value(
        payload.get("source_channel"), "source_channel", INTERVIEW_CHANNELS
    )
    storage_ref = _opaque_reference(
        payload.get("storage_ref"), "storage_ref", max_length=500
    )
    content_sha256 = _sha256(payload.get("content_sha256"), "content_sha256")
    provenance = _provenance(
        payload.get("provenance"), expected_channel=source_channel
    )
    raw_size = payload.get("size_bytes")
    size_bytes = None
    if raw_size not in (None, ""):
        try:
            size_bytes = int(raw_size)
        except (TypeError, ValueError) as exc:
            raise InterviewDomainError(
                "size_bytes must be a non-negative integer",
                reason_code="interview_evidence_size_invalid",
            ) from exc
        if size_bytes < 0 or size_bytes > _MAX_EVIDENCE_BYTES:
            raise InterviewDomainError(
                "size_bytes must be between 0 and 10 GiB",
                reason_code="interview_evidence_size_invalid",
            )
    normalized = {
        "session_id": session_id,
        "evidence_type": evidence_type,
        "source_channel": source_channel,
        "storage_ref": storage_ref,
        "content_sha256": content_sha256,
        "provenance": provenance,
        "size_bytes": size_bytes,
    }
    request_hash = _operation_hash("interview.evidence.create.v1", normalized)
    existing = InterviewEvidence.query.filter_by(
        tenant_id=tenant.id, idempotency_key=idempotency_key
    ).first()
    if existing:
        if existing.request_hash != request_hash:
            raise _idempotency_conflict()
        return InterviewMutation(existing, replayed=True)

    session = InterviewSession.query.filter_by(
        tenant_id=tenant.id, id=session_id
    ).first()
    if not session:
        raise InterviewDomainError(
            "Interview session not found",
            status_code=404,
            reason_code="interview_session_not_found",
            action_hint="choose_tenant_session",
        )
    if session.status != "active":
        raise InterviewDomainError(
            "Evidence can only be attached to an active interview session",
            status_code=409,
            reason_code="interview_evidence_session_inactive",
            action_hint="start_session_first",
        )

    evidence = InterviewEvidence(
        tenant_id=tenant.id,
        interview_session_id=session.id,
        evidence_type=evidence_type,
        source_channel=source_channel,
        storage_ref=storage_ref,
        content_sha256=content_sha256,
        provenance_json=provenance,
        size_bytes=size_bytes,
        created_by_user_id=actor.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    db.session.add(evidence)
    db.session.flush()
    _audit(
        tenant=tenant,
        actor=actor,
        event_type="interview.evidence.created",
        resource_type="interview_evidence",
        resource_id=evidence.id,
        details={
            "contract_version": "interview.evidence.v1",
            "interview_session_id": session.id,
            "evidence_type": evidence_type,
            "source_channel": source_channel,
            "content_sha256": content_sha256,
            "provenance_keys": sorted(provenance.keys()),
            "storage_ref_persisted_in_audit": False,
            "raw_content_persisted_in_audit": False,
        },
    )
    return InterviewMutation(evidence)
