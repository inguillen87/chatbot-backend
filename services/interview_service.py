"""Deterministic assessment/interview domain service.

The service validates and stages mutations but never commits them. Routes commit
the domain mutation and its AuditEvent atomically. No function in this module
can finalize an admission, rejection, hiring, eligibility, or other decision.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from typing import Any, Mapping

from flask import current_app
from sqlalchemy import and_, func, or_

from extensions import db
from models import (
    AuditEvent,
    ChannelSessionIdentityBinding,
    MessageTemplateRegistry,
    MessagingEventLedger,
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
    INTERVIEW_ASSIGNMENT_REASON_CODES,
    INTERVIEW_SESSION_STATUSES,
    PROGRAM_TYPES,
    AssessmentCase,
    AssessmentProgram,
    AssessmentProgramVersion,
    InterviewConsentChallenge,
    InterviewConsentPresentation,
    InterviewAssignment,
    InterviewEvidence,
    InterviewSession,
)
from services.interview_access_policy import (
    INTERVIEW_SESSIONS_CONDUCT,
    missing_interview_capabilities,
)
from services.channel_session_identity import (
    ChannelSessionIdentityError,
    channel_session_identity_mode,
    derive_channel_session_identity_hmac,
    resolve_channel_session_identity_secret,
)
from utils.auth_decorators import _is_authorized_for_tenant
from utils.roles import canonical_role


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
_CONSENT_TEMPLATE_CONTRACT_VERSION = "interview.consent_template.v1"
_CONSENT_TEMPLATE_BODY = "{{1}}"
_CONSENT_TEMPLATE_ACTION_TITLE = "Acepto"
_CONSENT_TEMPLATE_ACTION_ID = "{{2}}"
_CONSENT_TEMPLATE_PROVIDER_EVIDENCE_MAX_AGE = timedelta(days=7)
_TWILIO_CONTENT_SID_RE = re.compile(r"^HX[0-9a-fA-F]{32}$")
_INTERVIEW_RUNTIME_DEFINITION_CONTRACT = "interview.definition.v1"
_INTERVIEW_RESUME_CONTRACT = "interview.session_resume.v1"
_INTERVIEW_INBOX_CONTRACT_V1 = "assessment.interviews.inbox.v1"
_INTERVIEW_INBOX_CONTRACT_V2 = "assessment.interviews.inbox.v2"
_INTERVIEW_ASSIGNMENT_MUTATION_CONTRACT = "assessment.interviews.assignment.v1"
_INTERVIEW_ASSIGNMENT_CANDIDATES_CONTRACT = (
    "assessment.interviews.assignment_candidates.v1"
)
_INTERVIEW_ASSIGNMENT_REASON_LABELS = {
    "initial_assignment": "Asignaci\u00f3n inicial",
    "workload_balance": "Balance de carga",
    "availability": "Disponibilidad",
    "specialty_match": "Especialidad requerida",
    "continuity": "Continuidad del caso",
    "supervisor_override": "Reasignaci\u00f3n supervisada",
}
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
        "step_ref",
    }
)
_RAW_EVIDENCE_KEYS = frozenset(
    {"body", "bytes", "content", "data", "payload", "raw", "text", "transcript"}
)

_INTERVIEW_SESSION_LABELS = {
    "scheduled": "Programada",
    "active": "En curso",
    "completed": "Pendiente de revisión humana",
    "interrupted": "Interrumpida",
    "no_show": "Ausente",
    "void": "Anulada",
}
_INTERVIEW_CHANNEL_LABELS = {
    "api": "API",
    "web": "Web",
    "widget": "Widget",
    "whatsapp": "WhatsApp",
    "voice": "Voz",
    "in_person": "Presencial",
}
_INTERVIEW_NEXT_ACTION_LABELS = {
    "issue_consent_challenge": "Solicitar consentimiento",
    "record_consent_and_start": "Registrar consentimiento e iniciar",
    "capture_step": "Continuar captura de evidencia",
    "complete_interview": "Completar entrevista",
    "human_review": "Requiere revisión humana",
    "operator_reschedule_required": "Requiere reprogramación",
    "none": "Sin acción disponible",
}


class InterviewDomainError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 422,
        reason_code: str = "interview_validation_failed",
        action_hint: str = "check_request",
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason_code = reason_code
        self.action_hint = action_hint
        self.retryable = retryable
        self.details = dict(details or {})


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
    # The versioned runtime contract is optional for historical definitions,
    # but when selected it must be executable and checkpointable.  This keeps
    # legacy programs readable while making new conversational interviews
    # deterministic across reconnects and channels.
    _runtime_steps_from_definition(value)
    serialized = _canonical_json(value)
    if len(serialized.encode("utf-8")) > _MAX_DEFINITION_BYTES:
        raise InterviewDomainError(
            "definition exceeds 64 KiB",
            status_code=413,
            reason_code="interview_definition_too_large",
            action_hint="reduce_definition_size",
        )
    return dict(value), hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _runtime_steps_from_definition(
    definition: Any,
) -> tuple[str, list[dict[str, Any]]]:
    """Project an immutable definition into stable, channel-neutral steps.

    ``interview.definition.v1`` is strict: every section/question has a safe
    stable identifier and every question declares a prompt. Older definitions
    receive an advisory ordinal projection so an operator can resume them, but
    completion is not retroactively gated on data they never captured.
    """

    definition = definition if isinstance(definition, Mapping) else {}
    declared_contract = str(definition.get("contract_version") or "").strip()
    if (
        declared_contract.startswith("interview.definition.")
        and declared_contract != _INTERVIEW_RUNTIME_DEFINITION_CONTRACT
    ):
        raise InterviewDomainError(
            "Unsupported interview runtime definition contract",
            status_code=409,
            reason_code="interview_runtime_definition_unsupported",
            action_hint="use_supported_interview_definition_contract",
        )
    strict = declared_contract == _INTERVIEW_RUNTIME_DEFINITION_CONTRACT
    sections = definition.get("sections")
    if not isinstance(sections, list):
        if strict:
            raise InterviewDomainError(
                "interview.definition.v1 requires a non-empty sections array",
                reason_code="interview_runtime_definition_invalid",
                action_hint="provide_versioned_interview_sections",
            )
        return "evidence_only", []

    steps: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for section_index, section_value in enumerate(sections, start=1):
        section = section_value if isinstance(section_value, Mapping) else {}
        questions = section.get("questions")
        if not isinstance(questions, list):
            if strict:
                raise InterviewDomainError(
                    "Every interview.definition.v1 section requires questions",
                    reason_code="interview_runtime_definition_invalid",
                    action_hint="provide_versioned_interview_questions",
                )
            continue

        raw_section_id = str(section.get("id") or "").strip().lower()
        if strict:
            if not _SAFE_IDENTIFIER_RE.fullmatch(raw_section_id):
                raise InterviewDomainError(
                    "Every interview section requires a safe stable id",
                    reason_code="interview_runtime_definition_invalid",
                    action_hint="provide_safe_section_and_question_ids",
                )
            section_id = raw_section_id
        else:
            section_id = (
                raw_section_id
                if _SAFE_IDENTIFIER_RE.fullmatch(raw_section_id)
                else f"section_{section_index}"
            )

        for question_index, question_value in enumerate(questions, start=1):
            if isinstance(question_value, Mapping):
                question = question_value
                raw_question_id = str(question.get("id") or "").strip().lower()
                prompt_value = (
                    question.get("prompt")
                    if question.get("prompt") not in (None, "")
                    else question.get("text")
                    if question.get("text") not in (None, "")
                    else question.get("question")
                )
                required_value = question.get("required", True)
                raw_evidence_types = question.get("evidence_types")
            else:
                question = {}
                raw_question_id = ""
                prompt_value = question_value
                required_value = True
                raw_evidence_types = None

            if strict:
                if not isinstance(question_value, Mapping):
                    raise InterviewDomainError(
                        "interview.definition.v1 questions must be objects",
                        reason_code="interview_runtime_definition_invalid",
                        action_hint="provide_stable_question_ids_and_prompts",
                    )
                if not _SAFE_IDENTIFIER_RE.fullmatch(raw_question_id):
                    raise InterviewDomainError(
                        "Every interview question requires a safe stable id",
                        reason_code="interview_runtime_definition_invalid",
                        action_hint="provide_safe_section_and_question_ids",
                    )
                question_id = raw_question_id
            else:
                question_id = (
                    raw_question_id
                    if _SAFE_IDENTIFIER_RE.fullmatch(raw_question_id)
                    else f"question_{question_index}"
                )

            prompt = str(prompt_value or "").strip()
            if strict and (not prompt or len(prompt) > 4000):
                raise InterviewDomainError(
                    "Every interview question requires a prompt of at most 4000 characters",
                    reason_code="interview_runtime_definition_invalid",
                    action_hint="provide_stable_question_ids_and_prompts",
                )
            if not prompt:
                continue
            if len(prompt) > 4000:
                prompt = prompt[:4000]

            if strict and not isinstance(required_value, bool):
                raise InterviewDomainError(
                    "Interview question required must be boolean",
                    reason_code="interview_runtime_definition_invalid",
                    action_hint="use_boolean_question_required",
                )
            required = required_value if isinstance(required_value, bool) else True

            if raw_evidence_types is None:
                evidence_types = list(INTERVIEW_EVIDENCE_TYPES)
            elif isinstance(raw_evidence_types, list):
                evidence_types = []
                for raw_type in raw_evidence_types:
                    normalized_type = str(raw_type or "").strip().lower()
                    if normalized_type not in INTERVIEW_EVIDENCE_TYPES:
                        if strict:
                            raise InterviewDomainError(
                                "Interview question declares an unsupported evidence type",
                                reason_code="interview_runtime_definition_invalid",
                                action_hint="choose_supported_interview_evidence_types",
                            )
                        continue
                    if normalized_type not in evidence_types:
                        evidence_types.append(normalized_type)
                if strict and not evidence_types:
                    raise InterviewDomainError(
                        "Interview question evidence_types cannot be empty",
                        reason_code="interview_runtime_definition_invalid",
                        action_hint="choose_supported_interview_evidence_types",
                    )
                if not evidence_types:
                    evidence_types = list(INTERVIEW_EVIDENCE_TYPES)
            else:
                if strict:
                    raise InterviewDomainError(
                        "Interview question evidence_types must be an array",
                        reason_code="interview_runtime_definition_invalid",
                        action_hint="choose_supported_interview_evidence_types",
                    )
                evidence_types = list(INTERVIEW_EVIDENCE_TYPES)

            step_ref = f"{section_id}.{question_id}"
            if not _SAFE_IDENTIFIER_RE.fullmatch(step_ref) or step_ref in seen_refs:
                if strict:
                    raise InterviewDomainError(
                        "Interview step ids must be unique and at most 64 characters",
                        reason_code="interview_runtime_definition_invalid",
                        action_hint="provide_unique_short_section_and_question_ids",
                    )
                step_ref = f"section_{section_index}.question_{question_index}"
            if step_ref in seen_refs:
                continue
            seen_refs.add(step_ref)
            steps.append(
                {
                    "step_ref": step_ref,
                    "section_id": section_id,
                    "question_id": question_id,
                    "prompt": prompt,
                    "required": bool(required),
                    "evidence_types": evidence_types,
                    "ordinal": len(steps) + 1,
                }
            )

    if strict and not steps:
        raise InterviewDomainError(
            "interview.definition.v1 requires at least one question",
            reason_code="interview_runtime_definition_invalid",
            action_hint="provide_versioned_interview_questions",
        )
    return ("step_evidence_v1" if strict else "legacy_advisory"), steps


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


def _consent_template_definition() -> dict[str, Any]:
    return {
        "contract_version": _CONSENT_TEMPLATE_CONTRACT_VERSION,
        "types": {
            "twilio/text": {"body": _CONSENT_TEMPLATE_BODY},
            "twilio/quick-reply": {
                "body": _CONSENT_TEMPLATE_BODY,
                "actions": [
                    {
                        "type": "QUICK_REPLY",
                        "title": _CONSENT_TEMPLATE_ACTION_TITLE,
                        "id": _CONSENT_TEMPLATE_ACTION_ID,
                    }
                ],
            },
        },
    }


def _consent_template_definition_digest() -> str:
    return hashlib.sha256(
        _canonical_json(_consent_template_definition()).encode("utf-8")
    ).hexdigest()


def _verified_consent_template_digest(
    *,
    tenant: TenantProfile,
    attempt: WhatsAppOutboundAttempt,
    content_sid: str,
) -> str:
    """Verify the provider-created template renders only the pinned text/action.

    Delivery of a ContentSid proves transport, not what that remote template
    renders.  The local provider-sync registry is therefore part of the proof:
    it must be an approved, fresh definition created by Chatboc's Content API
    workflow before this attempt was staged.
    """

    if not _TWILIO_CONTENT_SID_RE.fullmatch(content_sid):
        raise InterviewDomainError(
            "Consent outbound ContentSid is invalid",
            status_code=409,
            reason_code="interview_consent_template_contract_invalid",
            action_hint="use_provider_synced_consent_template",
        )
    rows = (
        MessageTemplateRegistry.query.filter_by(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            content_sid=content_sid,
        )
        .order_by(MessageTemplateRegistry.id.asc())
        .limit(2)
        .all()
    )
    if len(rows) != 1:
        raise InterviewDomainError(
            "Consent outbound template is missing or ambiguous for this tenant",
            status_code=409,
            reason_code="interview_consent_template_contract_invalid",
            action_hint="sync_one_tenant_scoped_consent_template",
        )
    registry = rows[0]
    metadata = registry.metadata_json if isinstance(registry.metadata_json, Mapping) else {}
    components = registry.components if isinstance(registry.components, Mapping) else {}
    text_type = components.get("twilio/text")
    quick_reply_type = components.get("twilio/quick-reply")
    text_type = text_type if isinstance(text_type, Mapping) else {}
    quick_reply_type = quick_reply_type if isinstance(quick_reply_type, Mapping) else {}
    actions = quick_reply_type.get("actions")
    actions = actions if isinstance(actions, list) else []
    action = actions[0] if len(actions) == 1 and isinstance(actions[0], Mapping) else {}
    action_keys = set(action)
    definition_matches = bool(
        set(components) == {"twilio/text", "twilio/quick-reply"}
        and dict(text_type) == {"body": _CONSENT_TEMPLATE_BODY}
        and set(quick_reply_type) == {"body", "actions"}
        and quick_reply_type.get("body") == _CONSENT_TEMPLATE_BODY
        and action_keys in ({"title", "id"}, {"type", "title", "id"})
        and str(action.get("type") or "QUICK_REPLY").upper() == "QUICK_REPLY"
        and action.get("title") == _CONSENT_TEMPLATE_ACTION_TITLE
        and action.get("id") == _CONSENT_TEMPLATE_ACTION_ID
        and str(registry.body_preview or "") == _CONSENT_TEMPLATE_BODY
    )
    synced_at = _as_utc(registry.last_sync_at)
    registry_updated_at = _as_utc(registry.updated_at)
    attempt_created_at = _as_utc(attempt.created_at)
    now = datetime.now(timezone.utc)
    provider_sync_matches = bool(
        registry.status == "approved"
        and metadata.get("source") == "whatsapp_experience_creation_manifest"
        and metadata.get("sync_state") == "complete"
        and synced_at is not None
        and registry_updated_at is not None
        and attempt_created_at is not None
        and synced_at <= registry_updated_at <= attempt_created_at
        and attempt_created_at <= now + timedelta(minutes=5)
        and attempt_created_at - synced_at
        <= _CONSENT_TEMPLATE_PROVIDER_EVIDENCE_MAX_AGE
    )
    if not definition_matches or not provider_sync_matches:
        raise InterviewDomainError(
            "Consent outbound template has no fresh exact provider-sync contract",
            status_code=409,
            reason_code="interview_consent_template_contract_invalid",
            action_hint="sync_and_approve_exact_text_action_template",
        )
    return _consent_template_definition_digest()


def _delivery_callback_event_digest(event: MessagingEventLedger) -> str:
    occurred_at = _as_utc(event.occurred_at)
    created_at = _as_utc(event.created_at)
    return hashlib.sha256(
        _canonical_json(
            {
                "contract_version": "interview.consent_delivery_callback.v1",
                "event_id": event.id,
                "tenant_id": event.tenant_id,
                "provider_connection_id": event.provider_connection_id,
                "provider_sender_id": event.provider_sender_id,
                "channel": event.channel,
                "direction": event.direction,
                "event_type": event.event_type,
                "provider": event.provider,
                "provider_event_id": event.provider_event_id,
                "external_message_sid": event.external_message_sid,
                "external_status": str(event.external_status or "").lower(),
                "occurred_at": _iso(occurred_at),
                "created_at": _iso(created_at),
            }
        ).encode("utf-8")
    ).hexdigest()


def _verified_delivery_callback_event(
    *,
    tenant: TenantProfile,
    attempt: WhatsAppOutboundAttempt,
    required_status: Any = None,
    required_occurred_at: datetime | None = None,
) -> tuple[MessagingEventLedger, str]:
    """Bind consent delivery to the durable event created after signature checks."""

    current_status = str(attempt.provider_status or "").strip().lower()
    status = str(required_status or current_status).strip().lower()
    status_rank = {"delivered": 1, "read": 2}
    if (
        current_status not in status_rank
        or status not in status_rank
        or status_rank[current_status] < status_rank[status]
    ):
        raise InterviewDomainError(
            "Consent delivery has no delivered/read provider evidence",
            status_code=409,
            reason_code="interview_consent_presentation_not_delivered",
            action_hint="wait_for_signed_delivered_or_read_callback",
        )

    provider_message_sid = str(attempt.provider_message_sid or "").strip()
    query = MessagingEventLedger.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        direction="outbound",
        event_type="delivery_status",
        provider_event_id=f"{provider_message_sid}:{status}",
        external_message_sid=provider_message_sid,
        external_status=status,
    )
    if attempt.provider_sender_id is not None:
        query = query.filter_by(provider_sender_id=attempt.provider_sender_id)
    if attempt.provider_connection_id is not None:
        query = query.filter_by(
            provider_connection_id=attempt.provider_connection_id
        )
    rows = query.order_by(MessagingEventLedger.id.asc()).limit(2).all()
    if len(rows) != 1:
        raise InterviewDomainError(
            "Consent delivery has no unique signed provider callback event",
            status_code=409,
            reason_code="interview_consent_presentation_signed_callback_required",
            action_hint="wait_for_signed_delivered_or_read_callback",
        )
    event = rows[0]
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    message_refs = [
        str(payload[key]).strip()
        for key in ("MessageSid", "SmsMessageSid")
        if payload.get(key) not in (None, "")
    ]
    statuses = [
        str(payload[key]).strip().lower()
        for key in ("MessageStatus", "SmsStatus")
        if payload.get(key) not in (None, "")
    ]
    occurred_at = _as_utc(event.occurred_at)
    created_at = _as_utc(event.created_at)
    attempt_completed_at = _as_utc(attempt.completed_at)
    expected_occurred_at = _as_utc(required_occurred_at)
    callback_shape_valid = bool(
        message_refs
        and all(value == provider_message_sid for value in message_refs)
        and statuses
        and all(value == status for value in statuses)
        and occurred_at is not None
        and created_at is not None
        and attempt_completed_at is not None
        and occurred_at <= created_at <= attempt_completed_at
        and (
            expected_occurred_at is None
            or occurred_at == expected_occurred_at
        )
    )
    if not callback_shape_valid:
        raise InterviewDomainError(
            "Consent provider callback evidence is inconsistent with the outbound attempt",
            status_code=409,
            reason_code="interview_consent_presentation_signed_callback_invalid",
            action_hint="repair_signed_provider_callback_evidence",
        )
    return event, _delivery_callback_event_digest(event)


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
    required_provider_status: Any = None,
    required_provider_status_at: datetime | None = None,
    verify_current_template_registry: bool = True,
) -> tuple[WhatsAppInboundTurn, str, str, MessagingEventLedger, str]:
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

    status_event, status_event_digest = _verified_delivery_callback_event(
        tenant=tenant,
        attempt=attempt,
        required_status=required_provider_status,
        required_occurred_at=required_provider_status_at,
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

    try:
        identity_mode = channel_session_identity_mode(current_app.config)
        identity_secret = resolve_channel_session_identity_secret(
            current_app.config,
            mode=identity_mode,
        )
        recipient_hmac = derive_channel_session_identity_hmac(
            secret=identity_secret,
            tenant_id=tenant.id,
            provider=attempt.provider,
            identity_version=challenge.expected_identity_version,
            provider_identity=payload.get("to"),
        )
    except ChannelSessionIdentityError as exc:
        raise InterviewDomainError(
            "Consent outbound recipient cannot be verified against the pinned subject",
            status_code=409,
            reason_code="interview_consent_presentation_recipient_invalid",
            action_hint="send_to_pinned_subject_channel_identity",
        ) from exc
    if not hmac.compare_digest(
        recipient_hmac,
        str(challenge.expected_identity_hmac or ""),
    ):
        raise InterviewDomainError(
            "Consent outbound recipient is not the pinned subject channel identity",
            status_code=409,
            reason_code="interview_consent_presentation_recipient_mismatch",
            action_hint="send_to_pinned_subject_channel_identity",
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
        "template_definition_sha256": _consent_template_definition_digest(),
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
    template_digest = (
        _verified_consent_template_digest(
            tenant=tenant,
            attempt=attempt,
            content_sid=content_sid,
        )
        if verify_current_template_registry
        else _consent_template_definition_digest()
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
    return (
        source_turn,
        payload_digest,
        template_digest,
        status_event,
        status_event_digest,
    )


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
    (
        _,
        _,
        template_digest,
        status_event,
        status_event_digest,
    ) = _presentation_attempt_scope(
        tenant=tenant,
        session=session,
        challenge=challenge,
        version=version,
        attempt=attempt,
        required_provider_status=presentation.outbound_provider_status,
        required_provider_status_at=presentation.outbound_provider_status_at,
        # The immutable presentation already snapshots the exact approved
        # definition. Later provider-registry refreshes must not invalidate a
        # historical consent proof merely because ``updated_at`` advanced.
        verify_current_template_registry=False,
    )
    attempt_payload = (
        attempt.payload_json if isinstance(attempt.payload_json, Mapping) else {}
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
            str(presentation.outbound_content_sid or ""),
            str(attempt_payload.get("content_sid") or ""),
        )
        and presentation.outbound_status_event_id == status_event.id
        and hmac.compare_digest(
            str(presentation.outbound_status_event_sha256 or ""),
            status_event_digest,
        )
        and hmac.compare_digest(
            str(presentation.outbound_payload_sha256 or ""),
            str(attempt.payload_digest or ""),
        )
        and hmac.compare_digest(
            str(presentation.outbound_template_sha256 or ""),
            template_digest,
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
            str(presentation.expected_identity_hmac or ""),
            str(session.subject_identity_hmac or ""),
        )
        and hmac.compare_digest(
            str(presentation.expected_chat_session_id or ""),
            str(challenge.expected_chat_session_id or ""),
        )
        and hmac.compare_digest(
            str(presentation.expected_chat_session_id or ""),
            str(session.subject_chat_session_id or ""),
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
            action_hint="issue_and_use_v3_consent_challenge",
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
            action_hint="issue_and_use_v3_consent_challenge",
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
            "required_delivery_evidence": "signed_provider_status_callback",
            "content_variables": {"1": consent_text, "2": action_id},
            "template_contract": {
                "contract_version": _CONSENT_TEMPLATE_CONTRACT_VERSION,
                "provider": "twilio",
                "channel": "whatsapp",
                "status": "approved",
                "max_provider_evidence_age_seconds": int(
                    _CONSENT_TEMPLATE_PROVIDER_EVIDENCE_MAX_AGE.total_seconds()
                ),
                "types": {
                    "twilio/text": {"body": _CONSENT_TEMPLATE_BODY},
                    "twilio/quick-reply": {
                        "body": _CONSENT_TEMPLATE_BODY,
                        "actions": [
                            {
                                "type": "QUICK_REPLY",
                                "title": _CONSENT_TEMPLATE_ACTION_TITLE,
                                "id": _CONSENT_TEMPLATE_ACTION_ID,
                            }
                        ],
                    },
                },
            },
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
                    "template_definition_sha256": (
                        _consent_template_definition_digest()
                    ),
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
        "outbound_status_event_id": presentation.outbound_status_event_id,
        "outbound_status_event_sha256": (
            presentation.outbound_status_event_sha256
        ),
        "outbound_payload_sha256": presentation.outbound_payload_sha256,
        "outbound_template_sha256": presentation.outbound_template_sha256,
        "expected_identity_binding_id": presentation.expected_identity_binding_id,
        "consent_text_sha256": presentation.consent_text_sha256,
        "action": presentation.action,
        "registered_at": _iso(presentation.registered_at),
        "delivery_evidence_verified": True,
        "signed_provider_callback_verified": True,
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


def serialize_interview_assignment(
    assignment: InterviewAssignment,
) -> dict[str, Any]:
    """Serialize the stable append-only assignment receipt."""

    return {
        "contract_version": InterviewAssignment.CONTRACT_VERSION,
        "id": assignment.id,
        "tenant_id": assignment.tenant_id,
        "interview_session_id": assignment.interview_session_id,
        "version": assignment.version,
        "assignee_user_id": assignment.assignee_user_id,
        "previous_assignee_user_id": assignment.previous_assignee_user_id,
        "assigned_by_user_id": assignment.assigned_by_user_id,
        "reason_code": assignment.reason_code,
        "supersedes_assignment_id": assignment.supersedes_assignment_id,
        "created_at": _iso(assignment.created_at),
        "history_immutable": True,
    }


def get_interview_session(
    tenant: TenantProfile, session_id: int
) -> InterviewSession:
    session = InterviewSession.query.filter_by(
        tenant_id=tenant.id,
        id=session_id,
    ).first()
    if session is None:
        raise InterviewDomainError(
            "Interview session not found",
            status_code=404,
            reason_code="interview_session_not_found",
            action_hint="choose_tenant_session",
        )
    return session


def _current_interview_assignment(
    tenant_id: int,
    session_id: int,
) -> InterviewAssignment | None:
    return (
        InterviewAssignment.query.filter_by(
            tenant_id=tenant_id,
            interview_session_id=session_id,
        )
        .order_by(InterviewAssignment.version.desc(), InterviewAssignment.id.desc())
        .first()
    )


def _assignment_integer(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InterviewDomainError(
            f"{field} must be an integer greater than or equal to {minimum}",
            reason_code="interview_assignment_payload_invalid",
            action_hint="send_valid_assignment_payload",
            details={"field": field},
        )
    return value


def _eligible_assignment_user(
    tenant: TenantProfile,
    assignee_user_id: int,
) -> User:
    assignee = db.session.get(User, assignee_user_id)
    eligible = bool(assignee and _assignment_user_is_eligible(assignee, tenant))
    if not eligible:
        raise InterviewDomainError(
            "The selected interview assignee is not eligible for this tenant",
            reason_code="interview_assignment_assignee_ineligible",
            action_hint="choose_authorized_conductor",
        )
    return assignee


def _assignment_user_has_explicit_membership(user: User, tenant: TenantProfile) -> bool:
    owner_ids = {
        int(value)
        for value in (tenant.municipio_id, tenant.pyme_id)
        if isinstance(value, int) and value > 0
    }
    user_tenant_id = getattr(user, "tenant_id", None)
    user_tenant_slug = str(getattr(user, "tenant_slug", "") or "").strip().lower()
    tenant_slug = str(tenant.slug or "").strip().lower()
    return bool(
        user_tenant_id == tenant.id
        or (user_tenant_slug and user_tenant_slug == tenant_slug)
        or user.id in owner_ids
        or getattr(user, "empresa_id", None) in owner_ids
    )


def _assignment_user_is_eligible(user: User, tenant: TenantProfile) -> bool:
    return bool(
        str(getattr(user, "name", "") or "").strip()
        and _assignment_user_has_explicit_membership(user, tenant)
        and _is_authorized_for_tenant(
            user,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
        )
        and not missing_interview_capabilities(
            user,
            tenant,
            INTERVIEW_SESSIONS_CONDUCT,
        )
    )


def assign_interview_session(
    tenant: TenantProfile,
    actor: User,
    session_id: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> InterviewMutation:
    """Append one assignment revision and synchronize the legacy session pointer."""

    _ensure_allowed_fields(
        payload,
        {"assignee_user_id", "reason_code", "expected_assignment_version"},
    )
    assignee_user_id = _assignment_integer(
        payload.get("assignee_user_id"),
        "assignee_user_id",
        minimum=1,
    )
    expected_version = _assignment_integer(
        payload.get("expected_assignment_version"),
        "expected_assignment_version",
        minimum=0,
    )
    reason_code = _enum_value(
        payload.get("reason_code"),
        "reason_code",
        INTERVIEW_ASSIGNMENT_REASON_CODES,
    )
    normalized = {
        "session_id": session_id,
        "assignee_user_id": assignee_user_id,
        "reason_code": reason_code,
        "expected_assignment_version": expected_version,
    }
    request_hash = _operation_hash("interview.assignment.create.v1", normalized)
    existing = InterviewAssignment.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key=idempotency_key,
    ).first()
    if existing is not None:
        if not hmac.compare_digest(existing.request_hash, request_hash):
            raise _idempotency_conflict()
        return InterviewMutation(existing, replayed=True)

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
    # A second read after the session lock turns a concurrent retry with the
    # same operation identity into a replay instead of a stale-version error.
    existing_after_lock = InterviewAssignment.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key=idempotency_key,
    ).first()
    if existing_after_lock is not None:
        if not hmac.compare_digest(existing_after_lock.request_hash, request_hash):
            raise _idempotency_conflict()
        return InterviewMutation(existing_after_lock, replayed=True)
    if session.status in {"completed", "void"}:
        raise InterviewDomainError(
            "Closed interview sessions cannot be reassigned",
            status_code=409,
            reason_code="interview_assignment_session_closed",
            action_hint="choose_open_interview_session",
        )
    assignee = _eligible_assignment_user(tenant, assignee_user_id)
    current_assignment = _current_interview_assignment(tenant.id, session.id)
    current_version = current_assignment.version if current_assignment else 0
    if current_assignment and session.interviewer_user_id != current_assignment.assignee_user_id:
        raise InterviewDomainError(
            "The interview assignment ledger does not match the session owner",
            status_code=409,
            reason_code="interview_assignment_pointer_inconsistent",
            action_hint="repair_assignment_ledger",
        )
    if expected_version != current_version:
        raise InterviewDomainError(
            "The interview assignment changed before this request was applied",
            status_code=409,
            reason_code="interview_assignment_version_conflict",
            action_hint="refresh_assignment_and_retry",
            details={"current_assignment_version": current_version},
        )
    if current_assignment and current_assignment.assignee_user_id == assignee.id:
        raise InterviewDomainError(
            "The selected user is already assigned to this interview",
            status_code=409,
            reason_code="interview_assignment_unchanged",
            action_hint="choose_different_assignee",
            details={"current_assignment_version": current_version},
        )

    previous_assignee_user_id = session.interviewer_user_id
    baseline_materialized = bool(
        current_assignment is None
        and previous_assignee_user_id == assignee.id
    )
    change_kind = "baseline_materialized" if baseline_materialized else "assignee_changed"
    assignment = InterviewAssignment(
        tenant_id=tenant.id,
        interview_session_id=session.id,
        version=current_version + 1,
        assignee_user_id=assignee.id,
        previous_assignee_user_id=previous_assignee_user_id,
        assigned_by_user_id=actor.id,
        reason_code=reason_code,
        supersedes_assignment_id=(
            current_assignment.id if current_assignment is not None else None
        ),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    db.session.add(assignment)
    db.session.flush()
    session.interviewer_user_id = assignee.id
    _audit(
        tenant=tenant,
        actor=actor,
        event_type=(
            "interview.assignment.baselined"
            if baseline_materialized
            else "interview.assignment.changed"
        ),
        resource_type="interview_assignment",
        resource_id=assignment.id,
        details={
            "contract_version": InterviewAssignment.CONTRACT_VERSION,
            "interview_session_id": session.id,
            "assignment_version": assignment.version,
            "assignee_user_id": assignee.id,
            "previous_assignee_user_id": (
                assignment.previous_assignee_user_id
            ),
            "change_kind": change_kind,
            "reason_code": reason_code,
            "free_text_persisted": False,
        },
    )
    return InterviewMutation(assignment)


def build_interview_assignment_candidates(tenant: TenantProfile) -> dict[str, Any]:
    """Return the minimal tenant roster eligible to conduct an interview."""

    owner_ids = {
        int(value)
        for value in (tenant.municipio_id, tenant.pyme_id)
        if isinstance(value, int) and value > 0
    }
    normalized_tenant_slug = str(tenant.slug or "").strip().lower()
    clauses = [User.tenant_id == tenant.id]
    if normalized_tenant_slug:
        clauses.append(
            func.lower(func.trim(User.tenant_slug)) == normalized_tenant_slug
        )
    if owner_ids:
        clauses.extend([User.id.in_(owner_ids), User.empresa_id.in_(owner_ids)])
    candidates = []
    role_labels = {
        "tenant_admin": "Administrador",
        "employee": "Operador",
        "superadmin": "Administrador de plataforma",
    }
    for user in User.query.filter(or_(*clauses)).order_by(User.name.asc(), User.id.asc()).all():
        if not _assignment_user_is_eligible(user, tenant):
            continue
        normalized_role = canonical_role(getattr(user, "rol", None))
        candidates.append(
            {
                "user_id": user.id,
                "display_name": str(user.name or "").strip(),
                "role_label": role_labels.get(normalized_role, "Entrevistador"),
                "can_conduct": True,
            }
        )
    return {
        "contract_version": _INTERVIEW_ASSIGNMENT_CANDIDATES_CONTRACT,
        "tenant": {"id": tenant.id, "slug": tenant.slug},
        "candidates": candidates,
        "presentation": {
            "empty_title": "No hay entrevistadores disponibles",
            "empty_description": "Un administrador debe habilitar la capacidad de conducir entrevistas.",
        },
    }


def _verified_runtime_steps(
    case: AssessmentCase | None,
    version: AssessmentProgramVersion | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Validate one pinned immutable definition without performing new queries."""

    snapshot_valid = bool(
        case is not None
        and version is not None
        and version.program_id == case.program_id
        and version.status == "published"
        and version.published_at is not None
        and isinstance(version.definition_json, dict)
        and version.definition_json
    )
    if snapshot_valid:
        definition_digest = hashlib.sha256(
            _canonical_json(version.definition_json).encode("utf-8")
        ).hexdigest()
        snapshot_valid = hmac.compare_digest(
            definition_digest,
            str(version.definition_hash or ""),
        )
    if not snapshot_valid:
        raise InterviewDomainError(
            "The session's published interview definition cannot be verified",
            status_code=409,
            reason_code="interview_runtime_snapshot_invalid",
            action_hint="quarantine_session_and_repair_program_version",
        )
    progress_mode, steps = _runtime_steps_from_definition(version.definition_json)
    return progress_mode, steps


def _pinned_session_runtime(
    session: InterviewSession,
) -> tuple[AssessmentCase, AssessmentProgramVersion, str, list[dict[str, Any]]]:
    """Resolve and integrity-check the immutable runtime selected by a session."""

    case = AssessmentCase.query.filter_by(
        tenant_id=session.tenant_id,
        id=session.assessment_case_id,
        program_version_id=session.program_version_id,
    ).first()
    version = AssessmentProgramVersion.query.filter_by(
        tenant_id=session.tenant_id,
        id=session.program_version_id,
    ).first()
    progress_mode, steps = _verified_runtime_steps(case, version)
    return case, version, progress_mode, steps


def _session_progress(
    *,
    session: InterviewSession,
    progress_mode: str,
    steps: list[dict[str, Any]],
    evidence_rows: list[InterviewEvidence],
) -> dict[str, Any]:
    evidence_by_type = {item: 0 for item in INTERVIEW_EVIDENCE_TYPES}
    step_evidence_counts = {item["step_ref"]: 0 for item in steps}
    for evidence in evidence_rows:
        evidence_by_type[evidence.evidence_type] = (
            evidence_by_type.get(evidence.evidence_type, 0) + 1
        )
        provenance = (
            evidence.provenance_json
            if isinstance(evidence.provenance_json, Mapping)
            else {}
        )
        step_ref = str(provenance.get("step_ref") or "").strip().lower()
        if step_ref in step_evidence_counts:
            step_evidence_counts[step_ref] += 1

    completed_refs = [
        item["step_ref"]
        for item in steps
        if step_evidence_counts.get(item["step_ref"], 0) > 0
    ]
    required_refs = [item["step_ref"] for item in steps if item["required"]]
    completed_required_refs = [
        step_ref for step_ref in required_refs if step_ref in completed_refs
    ]
    pending_required_refs = [
        step_ref for step_ref in required_refs if step_ref not in completed_refs
    ]
    candidate_refs = pending_required_refs or [
        item["step_ref"]
        for item in steps
        if item["step_ref"] not in completed_refs
    ]
    next_step = next(
        (item for item in steps if item["step_ref"] in candidate_refs),
        None,
    )
    if progress_mode == "evidence_only":
        percent = None
        required_satisfied = None
    else:
        required_satisfied = not pending_required_refs
        percent = (
            100
            if not required_refs
            else int(round(100 * len(completed_required_refs) / len(required_refs)))
        )
    if evidence_rows:
        last_checkpoint = evidence_rows[-1].created_at
        checkpoint_source = "evidence"
    elif session.started_at is not None:
        last_checkpoint = session.started_at
        checkpoint_source = "session_started"
    else:
        last_checkpoint = session.created_at
        checkpoint_source = "session_created"
    return {
        "contract_version": "interview.progress.v1",
        "mode": progress_mode,
        "completion_gate_enforced": progress_mode == "step_evidence_v1",
        "total_steps": len(steps),
        "required_steps": len(required_refs),
        "completed_steps": len(completed_refs),
        "completed_required_steps": len(completed_required_refs),
        "pending_required_steps": len(pending_required_refs),
        "percent": percent,
        "required_steps_satisfied": required_satisfied,
        "completed_step_refs": completed_refs,
        "pending_required_step_refs": pending_required_refs,
        "step_evidence_counts": step_evidence_counts,
        "evidence_count": len(evidence_rows),
        "evidence_by_type": evidence_by_type,
        "last_checkpoint_at": _iso(last_checkpoint),
        "last_checkpoint_source": checkpoint_source,
        "current_step": dict(next_step) if next_step is not None else None,
        "session_status": session.status,
    }


def _interview_resume_actions(
    session: InterviewSession,
    progress: Mapping[str, Any],
    progress_mode: str,
) -> tuple[str, list[str]]:
    if session.status == "scheduled":
        next_action = (
            "issue_consent_challenge"
            if session.channel == "whatsapp"
            else "record_consent_and_start"
        )
        available_actions = (
            ["issue_consent_challenge", "start_session"]
            if session.channel == "whatsapp"
            else ["start_session"]
        )
    elif session.status == "active":
        next_action = (
            "capture_step"
            if progress.get("current_step") is not None
            else "complete_interview"
        )
        available_actions = ["add_evidence"]
        if (
            progress_mode != "step_evidence_v1"
            or progress.get("required_steps_satisfied") is True
        ):
            available_actions.append("complete_session")
    elif session.status == "completed":
        next_action = "human_review"
        available_actions = []
    elif session.status in {"interrupted", "no_show"}:
        next_action = "operator_reschedule_required"
        available_actions = []
    else:
        next_action = "none"
        available_actions = []
    return next_action, available_actions


def serialize_interview_resume(session: InterviewSession) -> dict[str, Any]:
    """Return the no-store runtime/checkpoint needed to resume one session."""

    case, version, progress_mode, steps = _pinned_session_runtime(session)
    evidence_rows = (
        InterviewEvidence.query.filter_by(
            tenant_id=session.tenant_id,
            interview_session_id=session.id,
        )
        .order_by(InterviewEvidence.id.asc())
        .all()
    )
    progress = _session_progress(
        session=session,
        progress_mode=progress_mode,
        steps=steps,
        evidence_rows=evidence_rows,
    )
    next_action, available_actions = _interview_resume_actions(
        session,
        progress,
        progress_mode,
    )

    progress["can_complete"] = "complete_session" in available_actions
    return {
        "contract_version": _INTERVIEW_RESUME_CONTRACT,
        "tenant_id": session.tenant_id,
        "session": serialize_interview_session(session),
        "case": {
            "id": case.id,
            "status": case.status,
            "program_id": case.program_id,
            "program_version_id": case.program_version_id,
            "source_channel": case.source_channel,
        },
        "program_snapshot": {
            "id": version.id,
            "program_id": version.program_id,
            "version_number": version.version_number,
            "definition_hash": version.definition_hash,
            "definition": version.definition_json,
            "runtime_contract": (
                _INTERVIEW_RUNTIME_DEFINITION_CONTRACT
                if progress_mode == "step_evidence_v1"
                else None
            ),
            "immutable": True,
        },
        "steps": steps,
        "progress": progress,
        "evidence": [serialize_evidence(item) for item in evidence_rows],
        "supported_evidence_types": list(INTERVIEW_EVIDENCE_TYPES),
        "next_action": next_action,
        "available_actions": available_actions,
        "resumable": session.status in {"scheduled", "active"},
        "updated_at": _iso(session.updated_at),
    }


def _inbox_action(
    *,
    action_id: str,
    label: str,
    enabled: bool,
    method: str | None,
    endpoint: str | None,
    disabled_reason_code: str | None,
    candidates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "action_id": action_id,
        "label": label,
        "enabled": enabled,
        "method": method,
        "endpoint": endpoint,
        "disabled_reason_code": disabled_reason_code,
    }
    if candidates is not None:
        payload["candidates"] = candidates
    return payload


def build_interview_inbox(
    tenant: TenantProfile,
    *,
    can_view_resume: bool,
    assignment_feature_enabled: bool = False,
    can_assign: bool = False,
    limit: int = 50,
    status: str | None = None,
) -> dict[str, Any]:
    """Build a bounded, tenant-scoped operational inbox from persisted facts.

    Managed assignment is exposed only when its separate fail-closed feature
    gate and caller capability are both enabled. Human-review completion and
    follow-up remain disabled because they have no durable records yet.
    """

    can_assign = bool(assignment_feature_enabled and can_assign)

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise InterviewDomainError(
            "Interview inbox limit must be an integer between 1 and 100",
            status_code=400,
            reason_code="interview_inbox_limit_invalid",
            action_hint="send_valid_limit",
        )
    normalized_status = str(status or "").strip().lower() or None
    if normalized_status is not None and normalized_status not in INTERVIEW_SESSION_STATUSES:
        raise InterviewDomainError(
            "Interview inbox status filter is invalid",
            status_code=400,
            reason_code="interview_inbox_status_invalid",
            action_hint="choose_supported_interview_status",
            details={"supported_statuses": list(INTERVIEW_SESSION_STATUSES)},
        )

    query = (
        db.session.query(
            InterviewSession,
            AssessmentCase,
            AssessmentProgramVersion,
            AssessmentProgram,
        )
        .join(
            AssessmentCase,
            and_(
                AssessmentCase.tenant_id == InterviewSession.tenant_id,
                AssessmentCase.id == InterviewSession.assessment_case_id,
                AssessmentCase.program_version_id == InterviewSession.program_version_id,
            ),
        )
        .join(
            AssessmentProgramVersion,
            and_(
                AssessmentProgramVersion.tenant_id == InterviewSession.tenant_id,
                AssessmentProgramVersion.id == InterviewSession.program_version_id,
                AssessmentProgramVersion.program_id == AssessmentCase.program_id,
            ),
        )
        .join(
            AssessmentProgram,
            and_(
                AssessmentProgram.tenant_id == InterviewSession.tenant_id,
                AssessmentProgram.id == AssessmentCase.program_id,
            ),
        )
        .filter(
            InterviewSession.tenant_id == tenant.id,
            AssessmentCase.tenant_id == tenant.id,
            AssessmentProgramVersion.tenant_id == tenant.id,
            AssessmentProgram.tenant_id == tenant.id,
        )
    )
    if normalized_status is not None:
        query = query.filter(InterviewSession.status == normalized_status)
    rows = (
        query.order_by(InterviewSession.updated_at.desc(), InterviewSession.id.desc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    session_ids = [int(session.id) for session, _case, _version, _program in rows]

    evidence_rows: list[InterviewEvidence] = []
    if session_ids:
        evidence_rows = (
            InterviewEvidence.query.filter(
                InterviewEvidence.tenant_id == tenant.id,
                InterviewEvidence.interview_session_id.in_(session_ids),
            )
            .order_by(
                InterviewEvidence.interview_session_id.asc(),
                InterviewEvidence.created_at.asc(),
                InterviewEvidence.id.asc(),
            )
            .all()
        )
    evidence_by_session: dict[int, list[InterviewEvidence]] = defaultdict(list)
    evidence_session_by_id: dict[str, int] = {}
    for evidence in evidence_rows:
        evidence_by_session[int(evidence.interview_session_id)].append(evidence)
        evidence_session_by_id[str(evidence.id)] = int(evidence.interview_session_id)

    assignment_rows: list[InterviewAssignment] = []
    if assignment_feature_enabled and session_ids:
        assignment_rows = (
            InterviewAssignment.query.filter(
                InterviewAssignment.tenant_id == tenant.id,
                InterviewAssignment.interview_session_id.in_(session_ids),
            )
            .order_by(
                InterviewAssignment.interview_session_id.asc(),
                InterviewAssignment.version.desc(),
                InterviewAssignment.id.desc(),
            )
            .all()
        )
    current_assignment_by_session: dict[int, InterviewAssignment] = {}
    assignment_session_by_id: dict[str, int] = {}
    for assignment in assignment_rows:
        session_key = int(assignment.interview_session_id)
        current_assignment_by_session.setdefault(session_key, assignment)
        assignment_session_by_id[str(assignment.id)] = session_key

    assigned_user_ids = {
        int(session.interviewer_user_id)
        for session, _case, _version, _program in rows
        if session.interviewer_user_id
    }
    assigned_user_labels = {}
    for user in (
        User.query.filter(User.id.in_(assigned_user_ids)).all()
        if assigned_user_ids
        else []
    ):
        if _assignment_user_is_eligible(user, tenant):
            assigned_user_labels[int(user.id)] = str(user.name or "").strip()

    audit_events: list[AuditEvent] = []
    if session_ids:
        audit_clauses = [
            and_(
                AuditEvent.resource_type == "interview_session",
                AuditEvent.resource_id.in_([str(item) for item in session_ids]),
            )
        ]
        if evidence_session_by_id:
            audit_clauses.append(
                and_(
                    AuditEvent.resource_type == "interview_evidence",
                    AuditEvent.resource_id.in_(list(evidence_session_by_id)),
                )
            )
        if assignment_session_by_id:
            audit_clauses.append(
                and_(
                    AuditEvent.resource_type == "interview_assignment",
                    AuditEvent.resource_id.in_(list(assignment_session_by_id)),
                )
            )
        audit_events = (
            AuditEvent.query.filter(AuditEvent.tenant_id == tenant.id)
            .filter(or_(*audit_clauses))
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .all()
        )
    audit_by_session: dict[int, list[AuditEvent]] = defaultdict(list)
    for event in audit_events:
        if event.resource_type == "interview_session":
            try:
                session_id = int(str(event.resource_id or ""))
            except (TypeError, ValueError):
                continue
        else:
            resource_id = str(event.resource_id or "")
            session_id = evidence_session_by_id.get(resource_id)
            if session_id is None:
                session_id = assignment_session_by_id.get(resource_id)
        if session_id in session_ids:
            audit_by_session[int(session_id)].append(event)

    items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    total_evidence = 0
    total_audit_events = 0
    assignment_disabled_reason = (
        None
        if can_assign
        else (
            "interview_assignment_domain_not_implemented"
            if not assignment_feature_enabled
            else "interview_assignment_capability_required"
        )
    )
    for session, case, version, program in rows:
        item_can_assign = bool(
            can_assign and session.status not in {"completed", "void"}
        )
        item_assignment_disabled_reason = (
            None
            if item_can_assign
            else (
                "interview_assignment_session_closed"
                if can_assign and session.status in {"completed", "void"}
                else assignment_disabled_reason
            )
        )
        current_assignment = current_assignment_by_session.get(int(session.id))
        if (
            current_assignment is not None
            and current_assignment.assignee_user_id != session.interviewer_user_id
        ):
            raise InterviewDomainError(
                "The interview assignment ledger does not match the session owner",
                status_code=409,
                reason_code="interview_assignment_pointer_inconsistent",
                action_hint="repair_assignment_ledger",
            )
        progress_mode, steps = _verified_runtime_steps(case, version)
        session_evidence = evidence_by_session.get(int(session.id), [])
        progress = _session_progress(
            session=session,
            progress_mode=progress_mode,
            steps=steps,
            evidence_rows=session_evidence,
        )
        next_action, available_actions = _interview_resume_actions(
            session,
            progress,
            progress_mode,
        )
        progress["can_complete"] = "complete_session" in available_actions
        current_step = progress.get("current_step")
        progress_summary = {
            "contract_version": "interview.progress.v1",
            "mode": progress["mode"],
            "completion_gate_enforced": progress["completion_gate_enforced"],
            "total_steps": progress["total_steps"],
            "required_steps": progress["required_steps"],
            "completed_steps": progress["completed_steps"],
            "completed_required_steps": progress["completed_required_steps"],
            "pending_required_steps": progress["pending_required_steps"],
            "percent": progress["percent"],
            "evidence_count": progress["evidence_count"],
            "last_checkpoint_at": progress["last_checkpoint_at"],
            "current_step_ref": (
                current_step.get("step_ref") if isinstance(current_step, Mapping) else None
            ),
            "can_complete": progress["can_complete"],
        }
        by_type = {
            evidence_type: int(progress["evidence_by_type"].get(evidence_type, 0))
            for evidence_type in INTERVIEW_EVIDENCE_TYPES
        }
        evidence_summary = {
            "total": len(session_evidence),
            "by_type": by_type,
            "last_captured_at": (
                _iso(session_evidence[-1].created_at) if session_evidence else None
            ),
            "content_hashes_present": sum(
                1 for item in session_evidence if bool(item.content_sha256)
            ),
            "references_exposed": False,
        }
        session_audit = audit_by_session.get(int(session.id), [])
        last_event = session_audit[0] if session_audit else None
        audit_summary = {
            "source": "audit_event",
            "events_recorded": len(session_audit),
            "last_event": (
                {
                    "event_type": last_event.event_type,
                    "actor_user_id": last_event.actor_user_id,
                    "created_at": _iso(last_event.created_at),
                }
                if last_event is not None
                else None
            ),
            "sensitive_details_exposed": False,
        }
        actions = {
            "view_resume": _inbox_action(
                action_id="view_resume",
                label="Abrir checkpoint",
                enabled=bool(can_view_resume),
                method="GET",
                endpoint=f"/api/v2/interviews/sessions/{session.id}",
                disabled_reason_code=(
                    None if can_view_resume else "interview_conduct_capability_required"
                ),
            ),
            "assign": _inbox_action(
                action_id="assign",
                label="Asignar responsable",
                enabled=item_can_assign,
                method="POST" if item_can_assign else None,
                endpoint=(
                    f"/api/v2/interviews/sessions/{session.id}/assignment"
                    if item_can_assign
                    else None
                ),
                disabled_reason_code=item_assignment_disabled_reason,
                candidates=(
                    {
                        "method": "GET",
                        "endpoint": "/api/v2/interviews/assignment-candidates",
                    }
                    if item_can_assign
                    else None
                ),
            ),
            "review": _inbox_action(
                action_id="review",
                label="Registrar revisión",
                enabled=False,
                method=None,
                endpoint=None,
                disabled_reason_code="interview_human_review_domain_not_implemented",
            ),
            "follow_up": _inbox_action(
                action_id="follow_up",
                label="Marcar seguimiento",
                enabled=False,
                method=None,
                endpoint=None,
                disabled_reason_code="interview_follow_up_domain_not_implemented",
            ),
        }
        items.append(
            {
                "id": session.id,
                "tenant_id": session.tenant_id,
                "session": {
                    "id": session.id,
                    "status": session.status,
                    "status_label": _INTERVIEW_SESSION_LABELS[session.status],
                    "channel": session.channel,
                    "channel_label": _INTERVIEW_CHANNEL_LABELS[session.channel],
                    "interviewer_user_id": session.interviewer_user_id,
                    "scheduled_for": _iso(session.scheduled_for),
                    "started_at": _iso(session.started_at),
                    "completed_at": _iso(session.completed_at),
                    "updated_at": _iso(session.updated_at),
                    "consent_granted": bool(session.consent_granted),
                },
                "case": {
                    "id": case.id,
                    "status": case.status,
                    "subject_type": case.subject_type,
                    "subject_reference_exposed": False,
                    "source_channel": case.source_channel,
                },
                "program": {
                    "id": program.id,
                    "name": program.name,
                    "program_type": program.program_type,
                    "version_id": version.id,
                    "version_number": version.version_number,
                    "immutable": True,
                },
                "progress": progress_summary,
                "evidence": evidence_summary,
                "review": {
                    "required": case.status == "awaiting_human_review",
                    "state": (
                        "pending" if case.status == "awaiting_human_review" else "not_ready"
                    ),
                    "decision_available": False,
                },
                "assignment": (
                    {
                        "interviewer_user_id": session.interviewer_user_id,
                        "assignment_id": (
                            current_assignment.id
                            if current_assignment is not None
                            else None
                        ),
                        "version": (
                            current_assignment.version
                            if current_assignment is not None
                            else 0
                        ),
                        "managed_assignment_available": True,
                        "assigned_user_label": (
                            assigned_user_labels.get(int(session.interviewer_user_id))
                            if session.interviewer_user_id is not None
                            else None
                        ),
                    }
                    if assignment_feature_enabled
                    else {
                        "interviewer_user_id": session.interviewer_user_id,
                        "managed_assignment_available": False,
                    }
                ),
                "audit": audit_summary,
                "next_action": next_action,
                "next_action_label": _INTERVIEW_NEXT_ACTION_LABELS[next_action],
                "actions": actions,
            }
        )
        status_counts[session.status] += 1
        total_evidence += len(session_evidence)
        total_audit_events += len(session_audit)

    presentation = {
        "title": "Entrevistas y evaluaciones",
        "description": "Bandeja operativa con progreso, evidencia y trazabilidad verificables.",
        "empty_title": "No hay entrevistas en esta bandeja",
        "empty_description": "Las sesiones aparecer\u00e1n cuando exista un caso con entrevista programada.",
    }
    if assignment_feature_enabled:
        presentation["assignment_dialog"] = {
            "title": "Asignar responsable",
            "description": "Seleccion\u00e1 una persona autorizada para conducir esta entrevista.",
            "assignee_label": "Responsable",
            "assignee_placeholder": "Seleccionar responsable",
            "reason_label": "Motivo de la asignaci\u00f3n",
            "submit_label": "Confirmar asignaci\u00f3n",
            "retry_label": "Reintentar",
            "cancel_label": "Cancelar",
            "loading_candidates_label": "Cargando responsables autorizados",
            "candidates_error_label": "No pudimos cargar los responsables autorizados.",
            "submit_error_label": "No pudimos registrar la asignaci\u00f3n.",
            "reasons": [
                {"reason_code": value, "label": _INTERVIEW_ASSIGNMENT_REASON_LABELS[value]}
                for value in INTERVIEW_ASSIGNMENT_REASON_CODES
            ],
        }

    return {
        "contract_version": (
            _INTERVIEW_INBOX_CONTRACT_V2
            if assignment_feature_enabled
            else _INTERVIEW_INBOX_CONTRACT_V1
        ),
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.nombre,
        },
        "presentation": presentation,
        "freshness": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": (
                "assessment_case_interview_session_evidence_assignment_audit_event"
                if assignment_feature_enabled
                else "assessment_case_interview_session_evidence_audit_event"
            ),
            "synthetic": False,
        },
        "summary": {
            "scope": "current_page",
            "sessions": len(items),
            "scheduled": int(status_counts.get("scheduled", 0)),
            "active": int(status_counts.get("active", 0)),
            "awaiting_human_review": sum(
                1 for item in items if item["review"]["required"] is True
            ),
            "with_evidence": sum(1 for item in items if item["evidence"]["total"] > 0),
            "evidence_records": total_evidence,
            "audit_events": total_audit_events,
        },
        "capabilities": {
            "read_only": not can_assign,
            "can_view_inbox": True,
            "can_view_resume": bool(can_view_resume),
            "can_assign": can_assign,
            "can_review": False,
            "can_mark_follow_up": False,
        },
        "governance": {
            "assignment_workflow_persisted": bool(assignment_feature_enabled),
            "human_review_workflow_persisted": False,
            "follow_up_workflow_persisted": False,
            "automated_decisions_allowed": False,
            "disabled_reason_codes": {
                "assign": assignment_disabled_reason,
                "review": "interview_human_review_domain_not_implemented",
                "follow_up": "interview_follow_up_domain_not_implemented",
            },
        },
        "filters": {"status": normalized_status},
        "page": {
            "limit": limit,
            "returned": len(items),
            "has_more": has_more,
            "continuation_available": False,
            "continuation_disabled_reason_code": (
                "interview_inbox_cursor_not_implemented" if has_more else None
            ),
        },
        "items": items,
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
            "raw_nonce_persisted_in_audit": False,
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
    (
        _,
        payload_digest,
        template_digest,
        status_event,
        status_event_digest,
    ) = _presentation_attempt_scope(
        tenant=tenant,
        session=session,
        challenge=challenge,
        version=version,
        attempt=attempt,
    )
    provider_status_at = _as_utc(status_event.occurred_at)
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
        outbound_provider_status=str(status_event.external_status or "").lower(),
        outbound_provider_status_at=provider_status_at,
        outbound_status_event_id=status_event.id,
        outbound_status_event_sha256=status_event_digest,
        outbound_payload_sha256=payload_digest,
        outbound_template_sha256=template_digest,
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
            "outbound_status_event_id": status_event.id,
            "outbound_status_event_sha256": status_event_digest,
            "outbound_payload_sha256": payload_digest,
            "outbound_template_sha256": template_digest,
            "expected_identity_binding_id": challenge.expected_identity_binding_id,
            "consent_text_sha256": challenge.consent_text_sha256,
            "action": _CONSENT_ACTION,
            "provider_message_sid_persisted_in_audit": False,
            "raw_nonce_persisted_in_audit": False,
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

    runtime_case, _version, progress_mode, steps = _pinned_session_runtime(session)
    if runtime_case.id != case.id:
        raise InterviewDomainError(
            "Interview session case scope is inconsistent",
            status_code=409,
            reason_code="interview_runtime_snapshot_invalid",
            action_hint="quarantine_session_and_repair_program_version",
        )
    evidence_rows = (
        InterviewEvidence.query.filter_by(
            tenant_id=tenant.id,
            interview_session_id=session.id,
        )
        .order_by(InterviewEvidence.id.asc())
        .all()
    )
    progress = _session_progress(
        session=session,
        progress_mode=progress_mode,
        steps=steps,
        evidence_rows=evidence_rows,
    )
    if (
        progress_mode == "step_evidence_v1"
        and progress["required_steps_satisfied"] is not True
    ):
        raise InterviewDomainError(
            "Required interview steps are missing durable evidence",
            status_code=409,
            reason_code="interview_required_steps_incomplete",
            action_hint="resume_interview",
            details={"progress": progress},
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
            "progress_mode": progress_mode,
            "required_steps": progress["required_steps"],
            "completed_required_steps": progress["completed_required_steps"],
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
    for key in ("transformation", "model_version", "step_ref"):
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
    _case, _version, progress_mode, steps = _pinned_session_runtime(session)
    step_ref = str(provenance.get("step_ref") or "").strip().lower()
    step_by_ref = {item["step_ref"]: item for item in steps}
    if progress_mode == "step_evidence_v1" and not step_ref:
        raise InterviewDomainError(
            "Evidence for interview.definition.v1 requires provenance.step_ref",
            reason_code="interview_evidence_step_required",
            action_hint="resume_session_and_use_current_step_ref",
            details={"expected_step_refs": list(step_by_ref)},
        )
    if step_ref and step_ref not in step_by_ref:
        raise InterviewDomainError(
            "provenance.step_ref does not belong to the pinned interview definition",
            reason_code="interview_evidence_step_unknown",
            action_hint="resume_session_and_use_current_step_ref",
            details={"expected_step_refs": list(step_by_ref)},
        )
    if step_ref and evidence_type not in step_by_ref[step_ref]["evidence_types"]:
        raise InterviewDomainError(
            "Evidence type is not accepted by the referenced interview step",
            reason_code="interview_evidence_type_not_accepted",
            action_hint="use_step_supported_evidence_type",
            details={
                "step_ref": step_ref,
                "accepted_evidence_types": step_by_ref[step_ref]["evidence_types"],
            },
        )
    if progress_mode == "step_evidence_v1" and source_channel != session.channel:
        raise InterviewDomainError(
            "Evidence source channel must match the pinned interview channel",
            status_code=409,
            reason_code="interview_evidence_channel_mismatch",
            action_hint="capture_evidence_in_pinned_session_channel",
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
