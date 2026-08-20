"""Versioned survey/vote releases with deterministic, human-governed policy.

This module proves what instrument and declarative policy were published. It
does not certify an election, determine eligibility, adjudicate a challenge or
declare a legally binding result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from config import TIMEZONE_OFFSET as _CONFIG_TIMEZONE_OFFSET
from database import db
from models import AuditEvent, EncEncuesta, EncLink, EncRespuesta
from models_survey_governance import (
    SURVEY_RELEASE_CONTRACT_VERSION,
    SurveyGovernanceRelease,
)


GOVERNANCE_PUBLIC_CONTRACT_VERSION = "surveys.public_governance.v1"
ELIGIBILITY_POLICY_CONTRACT_VERSION = "surveys.eligibility_policy.v1"
CONSENT_POLICY_CONTRACT_VERSION = "surveys.consent_policy.v1"
DECISION_RULES_CONTRACT_VERSION = "surveys.decision_rules.v1"
CLOSURE_MANIFEST_CONTRACT_VERSION = "surveys.closure_manifest.v1"
CONSENT_TEXT_NORMALIZATION = "unicode_nfc_lf_trim_v1"
CONSENT_TEXT_CONTENT_FORMAT = "plain_text"
CONSENT_TEXT_MIN_CODEPOINTS = 1
CONSENT_TEXT_MAX_CODEPOINTS = 4000
RELEASE_SNAPSHOT_SCHEMA_V1 = "surveys.release_snapshot.v1"
RELEASE_SNAPSHOT_SCHEMA_V2 = "surveys.release_snapshot.v2"
RELEASE_SNAPSHOT_SCHEMA_V3 = "surveys.release_snapshot.v3"
_SUPPORTED_RELEASE_SNAPSHOT_SCHEMAS = {
    RELEASE_SNAPSHOT_SCHEMA_V1,
    RELEASE_SNAPSHOT_SCHEMA_V2,
    RELEASE_SNAPSHOT_SCHEMA_V3,
}

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_DECLARATION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,63}$")
_OPAQUE_REVIEW_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}:[A-Za-z][A-Za-z0-9_.:-]{7,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

_FORBIDDEN_POLICY_KEYS = {
    "dni",
    "document",
    "documento",
    "email",
    "phone",
    "telefono",
    "name",
    "nombre",
    "address",
    "direccion",
    "voter_roll",
    "padron",
    "person_ids",
    "eligible_people",
    "admitted",
    "rejected",
    "winner",
    "certified_result",
}


class SurveyGovernanceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        reason_code: str = "survey_governance_invalid",
        action_hint: str = "review_request",
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason_code = reason_code
        self.action_hint = action_hint
        self.extra = dict(extra or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.message,
            "message": self.message,
            "contract_version": SURVEY_RELEASE_CONTRACT_VERSION,
            "reason_code": self.reason_code,
            "action_hint": self.action_hint,
            "retryable": False,
            **self.extra,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_public_consent_text(value: Any) -> str:
    """Return the exact portable bytes represented by a public consent text.

    The algorithm intentionally has a small cross-runtime surface so the
    browser can reproduce it before enabling consent:
    CRLF/CR -> LF, Unicode NFC, trim ASCII spaces/LF at the document edges,
    reject C0/DEL controls except LF, and count Unicode code points.
    """

    if not isinstance(value, str):
        raise SurveyGovernanceError(
            "consent_policy.public_text es obligatorio",
            status_code=422,
            reason_code="survey_consent_public_text_required",
            action_hint="provide_public_plain_text_consent",
        )
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    ).strip(" \n")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in normalized):
        raise SurveyGovernanceError(
            "consent_policy.public_text contiene Unicode sustituto inválido",
            status_code=422,
            reason_code="survey_consent_public_text_unicode_invalid",
            action_hint="remove_lone_unicode_surrogates",
        )
    if any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in normalized):
        raise SurveyGovernanceError(
            "consent_policy.public_text contiene caracteres de control no permitidos",
            status_code=422,
            reason_code="survey_consent_public_text_control_invalid",
            action_hint="use_plain_text_without_control_characters",
        )
    if not (
        CONSENT_TEXT_MIN_CODEPOINTS
        <= len(normalized)
        <= CONSENT_TEXT_MAX_CODEPOINTS
    ):
        raise SurveyGovernanceError(
            "consent_policy.public_text debe tener entre 1 y 4000 caracteres",
            status_code=422,
            reason_code="survey_consent_public_text_length_invalid",
            action_hint="provide_bounded_public_consent_text",
        )
    return normalized


def _canonical_hash(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _survey_schedule_timezone() -> timezone:
    try:
        offset_hours = int(
            current_app.config.get("TIMEZONE_OFFSET", _CONFIG_TIMEZONE_OFFSET)
        )
    except (RuntimeError, TypeError, ValueError):
        offset_hours = int(_CONFIG_TIMEZONE_OFFSET)
    return timezone(timedelta(hours=max(-12, min(14, offset_hours))))


def _survey_schedule_iso_v1(value: Any) -> str | None:
    """Return the legacy wall-time representation used by snapshot v1."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        return str(value)
    return value.replace(tzinfo=None).isoformat()


def _survey_schedule_iso_v1_local(value: Any) -> str | None:
    """Rebuild the v1 auto-start wall time after a PostgreSQL UTC reload."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        return value.isoformat()
    return (
        value.astimezone(_survey_schedule_timezone())
        .replace(tzinfo=None)
        .isoformat()
    )


def _survey_schedule_iso(value: Any) -> str | None:
    """Canonicalize schedule instants for snapshot v2.

    PostgreSQL returns ``TIMESTAMP WITH TIME ZONE`` values as aware datetimes,
    while SQLite drops the offset and the survey runtime interprets those wall
    values in ``TIMEZONE_OFFSET``. Normalizing both representations to UTC
    keeps the immutable snapshot stable across a commit/reload.
    """

    if value is None:
        return None
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_survey_schedule_timezone())
    return value.astimezone(timezone.utc).isoformat()


def _require_exact_keys(
    value: Mapping[str, Any], allowed: set[str], *, field: str
) -> None:
    extras = sorted(str(key) for key in value.keys() if key not in allowed)
    if extras:
        raise SurveyGovernanceError(
            f"{field} contiene campos no permitidos",
            status_code=422,
            reason_code="survey_governance_policy_field_not_allowed",
            action_hint="remove_unapproved_policy_fields",
            extra={"field": field, "fields": extras},
        )


def _reject_forbidden_policy_keys(value: Any, *, path: str = "policy") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if key in _FORBIDDEN_POLICY_KEYS:
                raise SurveyGovernanceError(
                    "La politica no puede contener padrones, PII ni decisiones",
                    status_code=422,
                    reason_code="survey_governance_sensitive_policy_rejected",
                    action_hint="use_minimized_declarative_policy",
                    extra={"field": f"{path}.{key}"},
                )
            _reject_forbidden_policy_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_policy_keys(child, path=f"{path}[{index}]")


def _policy_version(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not _VERSION_RE.fullmatch(normalized):
        raise SurveyGovernanceError(
            f"{field} debe ser una version opaca valida",
            status_code=422,
            reason_code="survey_governance_policy_version_invalid",
            action_hint="provide_versioned_policy",
            extra={"field": field},
        )
    return normalized


def _normalize_eligibility_policy(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SurveyGovernanceError(
            "eligibility_policy es obligatorio",
            status_code=422,
            reason_code="survey_eligibility_policy_required",
            action_hint="provide_minimized_eligibility_policy",
        )
    _reject_forbidden_policy_keys(raw, path="eligibility_policy")
    _require_exact_keys(
        raw,
        {
            "policy_version",
            "mode",
            "declarations",
            "human_review_required",
            "automated_decision",
        },
        field="eligibility_policy",
    )
    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in {"open", "self_attested", "institution_attested", "manual_review"}:
        raise SurveyGovernanceError(
            "Modo de elegibilidad no soportado",
            status_code=422,
            reason_code="survey_eligibility_mode_invalid",
            action_hint="choose_supported_eligibility_mode",
        )
    declarations_raw = raw.get("declarations") or []
    if not isinstance(declarations_raw, list) or len(declarations_raw) > 20:
        raise SurveyGovernanceError(
            "declarations debe ser una lista acotada",
            status_code=422,
            reason_code="survey_eligibility_declarations_invalid",
            action_hint="use_declaration_codes_only",
        )
    declarations: list[str] = []
    for item in declarations_raw:
        declaration = str(item or "").strip().lower()
        if not _DECLARATION_RE.fullmatch(declaration):
            raise SurveyGovernanceError(
                "Cada declaracion debe ser un codigo no identificatorio",
                status_code=422,
                reason_code="survey_eligibility_declaration_invalid",
                action_hint="use_declaration_codes_only",
            )
        if declaration not in declarations:
            declarations.append(declaration)
    if raw.get("automated_decision") not in (None, False):
        raise SurveyGovernanceError(
            "La elegibilidad no puede decidirse automaticamente",
            status_code=422,
            reason_code="survey_automated_eligibility_forbidden",
            action_hint="route_to_human_review",
        )
    if raw.get("human_review_required") is not True:
        raise SurveyGovernanceError(
            "La politica debe preservar revision humana",
            status_code=422,
            reason_code="survey_human_review_required",
            action_hint="set_human_review_required_true",
        )
    return {
        "contract_version": ELIGIBILITY_POLICY_CONTRACT_VERSION,
        "policy_version": _policy_version(
            raw.get("policy_version"), field="eligibility_policy.policy_version"
        ),
        "mode": mode,
        "declarations": declarations,
        "human_review_required": True,
        "automated_decision": False,
        "stores_roster_or_pii": False,
        "decision_state": "not_evaluated",
    }


def _normalize_consent_policy(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SurveyGovernanceError(
            "consent_policy es obligatorio",
            status_code=422,
            reason_code="survey_consent_policy_required",
            action_hint="provide_versioned_consent_policy",
        )
    _reject_forbidden_policy_keys(raw, path="consent_policy")
    _require_exact_keys(
        raw,
        {"policy_version", "public_text", "text_sha256", "required"},
        field="consent_policy",
    )
    public_text = normalize_public_consent_text(raw.get("public_text"))
    text_sha256 = str(raw.get("text_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(text_sha256):
        raise SurveyGovernanceError(
            "consent_policy.text_sha256 debe ser SHA-256",
            status_code=422,
            reason_code="survey_consent_text_hash_invalid",
            action_hint="provide_consent_text_sha256",
        )
    expected_sha256 = _sha256_text(public_text)
    if text_sha256 != expected_sha256:
        raise SurveyGovernanceError(
            "consent_policy.text_sha256 no coincide con el texto público normalizado",
            status_code=422,
            reason_code="survey_consent_text_hash_mismatch",
            action_hint="hash_normalized_public_consent_text",
        )
    if raw.get("required") is not True:
        raise SurveyGovernanceError(
            "El release gobernado exige consentimiento explicito",
            status_code=422,
            reason_code="survey_governance_consent_required",
            action_hint="set_consent_required_true",
        )
    return {
        "contract_version": CONSENT_POLICY_CONTRACT_VERSION,
        "policy_version": _policy_version(
            raw.get("policy_version"), field="consent_policy.policy_version"
        ),
        "public_text": public_text,
        "text_sha256": text_sha256,
        "content_format": CONSENT_TEXT_CONTENT_FORMAT,
        "normalization": CONSENT_TEXT_NORMALIZATION,
        "required": True,
        "stores_public_text": True,
        "records_participant_input": False,
    }


def _normalize_decision_rules(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SurveyGovernanceError(
            "decision_rules es obligatorio",
            status_code=422,
            reason_code="survey_decision_rules_required",
            action_hint="provide_declarative_decision_rules",
        )
    _reject_forbidden_policy_keys(raw, path="decision_rules")
    _require_exact_keys(
        raw,
        {"quorum", "tie", "challenge", "human_review_required", "declarative_only"},
        field="decision_rules",
    )
    if raw.get("human_review_required") is not True or raw.get("declarative_only") is not True:
        raise SurveyGovernanceError(
            "Quorum, empate y challenge deben ser declarativos y revisados por personas",
            status_code=422,
            reason_code="survey_decision_rules_human_review_required",
            action_hint="set_declarative_human_review_flags",
        )

    quorum_raw = raw.get("quorum") or {}
    if not isinstance(quorum_raw, Mapping):
        raise SurveyGovernanceError("quorum invalido", status_code=422)
    _require_exact_keys(quorum_raw, {"type", "value"}, field="decision_rules.quorum")
    quorum_type = str(quorum_raw.get("type") or "none").strip().lower()
    if quorum_type not in {"none", "minimum_responses", "minimum_percentage"}:
        raise SurveyGovernanceError(
            "Tipo de quorum invalido",
            status_code=422,
            reason_code="survey_quorum_rule_invalid",
        )
    quorum_value = quorum_raw.get("value")
    if quorum_type == "none":
        quorum_value = None
    elif isinstance(quorum_value, bool) or not isinstance(quorum_value, (int, float)):
        raise SurveyGovernanceError(
            "El quorum requiere un valor numerico declarativo",
            status_code=422,
            reason_code="survey_quorum_value_invalid",
        )
    elif quorum_value <= 0 or (
        quorum_type == "minimum_percentage" and quorum_value > 100
    ):
        raise SurveyGovernanceError(
            "Valor de quorum fuera de rango",
            status_code=422,
            reason_code="survey_quorum_value_invalid",
        )

    tie_raw = raw.get("tie") or {}
    if not isinstance(tie_raw, Mapping):
        raise SurveyGovernanceError("tie invalido", status_code=422)
    _require_exact_keys(tie_raw, {"procedure"}, field="decision_rules.tie")
    tie_procedure = str(tie_raw.get("procedure") or "human_review").strip().lower()
    if tie_procedure not in {"human_review", "runoff", "declared_tie"}:
        raise SurveyGovernanceError(
            "Procedimiento de empate invalido",
            status_code=422,
            reason_code="survey_tie_rule_invalid",
        )

    challenge_raw = raw.get("challenge") or {}
    if not isinstance(challenge_raw, Mapping):
        raise SurveyGovernanceError("challenge invalido", status_code=422)
    _require_exact_keys(
        challenge_raw,
        {"enabled", "window_hours", "procedure"},
        field="decision_rules.challenge",
    )
    challenge_enabled = challenge_raw.get("enabled") is True
    challenge_window = challenge_raw.get("window_hours")
    if challenge_enabled:
        if isinstance(challenge_window, bool) or not isinstance(challenge_window, int):
            raise SurveyGovernanceError(
                "challenge.window_hours debe ser entero",
                status_code=422,
                reason_code="survey_challenge_window_invalid",
            )
        if challenge_window < 1 or challenge_window > 720:
            raise SurveyGovernanceError(
                "challenge.window_hours fuera de rango",
                status_code=422,
                reason_code="survey_challenge_window_invalid",
            )
    else:
        challenge_window = None
    challenge_procedure = str(
        challenge_raw.get("procedure") or "human_review"
    ).strip().lower()
    if challenge_procedure != "human_review":
        raise SurveyGovernanceError(
            "Los challenges requieren revision humana",
            status_code=422,
            reason_code="survey_challenge_human_review_required",
        )

    return {
        "contract_version": DECISION_RULES_CONTRACT_VERSION,
        "quorum": {"type": quorum_type, "value": quorum_value},
        "tie": {"procedure": tie_procedure},
        "challenge": {
            "enabled": challenge_enabled,
            "window_hours": challenge_window,
            "procedure": "human_review",
        },
        "human_review_required": True,
        "declarative_only": True,
        "computed_outcome": None,
    }


def normalize_governance_policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SurveyGovernanceError("El payload debe ser un objeto JSON")
    _require_exact_keys(
        payload,
        {"eligibility_policy", "consent_policy", "decision_rules"},
        field="release",
    )
    normalized = {
        "eligibility": _normalize_eligibility_policy(payload.get("eligibility_policy")),
        "consent": _normalize_consent_policy(payload.get("consent_policy")),
        "decision_rules": _normalize_decision_rules(payload.get("decision_rules")),
    }
    if len(_canonical_json(normalized).encode("utf-8")) > 32768:
        raise SurveyGovernanceError(
            "La politica excede el limite permitido",
            status_code=413,
            reason_code="survey_governance_policy_too_large",
        )
    return normalized


def build_release_snapshot(
    encuesta: EncEncuesta,
    governance_policy: Mapping[str, Any],
    *,
    schema_version: str = RELEASE_SNAPSHOT_SCHEMA_V3,
) -> dict[str, Any]:
    if schema_version not in _SUPPORTED_RELEASE_SNAPSHOT_SCHEMAS:
        raise SurveyGovernanceError(
            "La version del snapshot de gobernanza no esta soportada",
            status_code=500,
            reason_code="survey_governance_snapshot_schema_unsupported",
            action_hint="contact_support",
        )
    schedule_serializer = (
        _survey_schedule_iso_v1
        if schema_version == RELEASE_SNAPSHOT_SCHEMA_V1
        else _survey_schedule_iso
    )
    questions: list[dict[str, Any]] = []
    for question in sorted(
        list(encuesta.preguntas or []), key=lambda item: (int(item.orden), int(item.id or 0))
    ):
        questions.append(
            {
                "id": int(question.id),
                "question_ref": question.logical_ref,
                "order": int(question.orden),
                "type": question.tipo,
                "text": question.texto,
                "required": bool(question.obligatoria),
                "min_selections": question.min_selecciones,
                "max_selections": question.max_selecciones,
                "conditional_logic": question.logica_condicional,
                "options": [
                    {
                        "id": int(option.id),
                        "option_ref": option.logical_ref,
                        "order": int(option.orden),
                        "text": option.texto,
                        "value": option.valor,
                    }
                    for option in sorted(
                        list(question.opciones or []),
                        key=lambda item: (int(item.orden), int(item.id or 0)),
                    )
                ],
            }
        )
    snapshot = {
        "schema_version": schema_version,
        "instrument": {
            "survey_id": int(encuesta.id),
            "tenant_id": int(encuesta.tenant_id),
            "document_ref": encuesta.document_ref,
            "structure_revision": int(encuesta.structure_revision or 1),
            "title": encuesta.titulo,
            "description": encuesta.descripcion,
            "type": encuesta.tipo,
            "questions": questions,
        },
        "collection_rules": {
            "starts_at": schedule_serializer(encuesta.inicio_at),
            "ends_at": schedule_serializer(encuesta.fin_at),
            "requires_identity": bool(encuesta.requiere_identidad),
            "uniqueness_policy": encuesta.politica_unicidad,
            "anonymous_allowed": bool(encuesta.anonimo_permitido),
            "live_vote": bool(encuesta.es_votacion_envivo),
            "live_results": bool(encuesta.mostrar_resultados_envivo),
            "comments_allowed": bool(encuesta.permitir_comentarios),
        },
        "privacy": {
            "mode": encuesta.privacy_mode or "legacy",
            "policy_version": encuesta.privacy_policy_version,
            "policy_url": encuesta.privacy_policy_url,
            "consent_required": bool(encuesta.privacy_consent_required),
            "retention_days": encuesta.response_retention_days,
        },
        "governance": dict(governance_policy),
        "assurance": {
            "scope": "instrument_and_policy_integrity",
            "regulated_election_certified": False,
            "result_certified": False,
            "human_review_required": True,
        },
    }
    if schema_version == RELEASE_SNAPSHOT_SCHEMA_V3:
        from services.survey_jurisdiction import survey_content_sha256

        snapshot["content_integrity"] = {
            "contract_version": "surveys.jurisdiction_guard.v1",
            "jurisdiction_ref": encuesta.jurisdiction_ref,
            "content_origin": encuesta.content_origin or "legacy_unverified",
            "content_origin_ref": encuesta.content_origin_ref,
            "content_sha256": survey_content_sha256(encuesta),
        }
    return snapshot


def _validate_idempotency_key(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(normalized):
        raise SurveyGovernanceError(
            "Idempotency-Key es obligatorio y debe tener entre 8 y 128 caracteres seguros",
            status_code=400,
            reason_code="survey_governance_idempotency_key_invalid",
            action_hint="send_valid_idempotency_key",
        )
    return normalized


def _operation_hash(operation: str, payload: Mapping[str, Any]) -> str:
    return _canonical_hash(
        {
            "contract_version": SURVEY_RELEASE_CONTRACT_VERSION,
            "operation": operation,
            "payload": dict(payload),
        }
    )


def _audit_event(
    *,
    tenant_id: int,
    actor_user_id: int,
    event_type: str,
    release: SurveyGovernanceRelease,
    details: Mapping[str, Any],
    ip_address: str | None,
) -> None:
    safe_ip = str(ip_address or "").strip()[:50] or None
    db.session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            resource_type="survey_governance_release",
            resource_id=str(release.id),
            details={
                "contract_version": SURVEY_RELEASE_CONTRACT_VERSION,
                "survey_id": int(release.survey_id),
                "release_id": int(release.id),
                "version_number": int(release.version_number),
                "snapshot_sha256": release.snapshot_sha256,
                **dict(details),
            },
            ip_address=safe_ip,
        )
    )


def _with_locked_survey(tenant_id: int, survey_id: int) -> EncEncuesta:
    survey = (
        EncEncuesta.query.filter_by(tenant_id=tenant_id, id=survey_id)
        .with_for_update()
        .first()
    )
    if survey is None:
        raise SurveyGovernanceError(
            "Encuesta no encontrada",
            status_code=404,
            reason_code="survey_not_found",
            action_hint="check_survey_id",
        )
    return survey


def _commit_or_governance_error(reason_code: str) -> None:
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise SurveyGovernanceError(
            "Conflicto al guardar el release gobernado",
            status_code=409,
            reason_code=reason_code,
            action_hint="reload_release_state",
        ) from exc
    except Exception as exc:
        db.session.rollback()
        raise SurveyGovernanceError(
            "No se pudo confirmar atomicamente el release y su auditoria",
            status_code=500,
            reason_code="survey_governance_atomic_commit_failed",
            action_hint="retry_same_idempotency_key",
        ) from exc


def create_release(
    *,
    tenant_id: int,
    survey_id: int,
    actor_user_id: int,
    payload: Mapping[str, Any],
    idempotency_key: str,
    ip_address: str | None = None,
) -> tuple[SurveyGovernanceRelease, bool]:
    key = _validate_idempotency_key(idempotency_key)
    policy = normalize_governance_policy(payload)
    request_hash = _operation_hash(
        "create", {"tenant_id": tenant_id, "survey_id": survey_id, "policy": policy}
    )
    replay = SurveyGovernanceRelease.query.filter_by(
        tenant_id=tenant_id, create_idempotency_key=key
    ).first()
    if replay is not None:
        if replay.create_request_hash != request_hash:
            raise SurveyGovernanceError(
                "Idempotency-Key ya fue usado con otro release",
                status_code=409,
                reason_code="survey_governance_idempotency_conflict",
                action_hint="reuse_original_payload_or_new_key",
            )
        return replay, True

    survey = _with_locked_survey(tenant_id, survey_id)
    if survey.estado != "borrador" or survey.respuestas.count() != 0:
        raise SurveyGovernanceError(
            "El release gobernado sólo puede crearse sobre un borrador sin respuestas",
            status_code=409,
            reason_code="survey_governance_requires_pristine_draft",
            action_hint="duplicate_as_new_draft",
        )
    existing = SurveyGovernanceRelease.query.filter_by(
        tenant_id=tenant_id, survey_id=survey_id
    ).first()
    if existing is not None:
        raise SurveyGovernanceError(
            "La encuesta ya tiene un release de gobernanza",
            status_code=409,
            reason_code="survey_governance_release_exists",
            action_hint="use_existing_release_or_duplicate_survey",
            extra={"release_id": existing.id, "status": existing.status},
        )
    if not survey.preguntas:
        raise SurveyGovernanceError(
            "La encuesta debe tener preguntas",
            status_code=422,
            reason_code="survey_governance_questions_required",
        )

    # Pin an explicit start so publication cannot silently change the snapshot.
    if survey.inicio_at is None:
        from services.encuestas_service import _public_schedule_now

        survey.inicio_at = _public_schedule_now()
    snapshot = build_release_snapshot(survey, policy)
    snapshot_json = _canonical_json(snapshot)
    release = SurveyGovernanceRelease(
        tenant_id=tenant_id,
        survey_id=survey_id,
        version_number=1,
        status="draft",
        snapshot_json=snapshot_json,
        snapshot_sha256=_sha256_text(snapshot_json),
        policy_sha256=_canonical_hash(policy),
        eligibility_policy_version=policy["eligibility"]["policy_version"],
        consent_policy_version=policy["consent"]["policy_version"],
        created_by_user_id=actor_user_id,
        create_idempotency_key=key,
        create_request_hash=request_hash,
    )
    db.session.add(release)
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise SurveyGovernanceError(
            "Conflicto al reservar el release gobernado",
            status_code=409,
            reason_code="survey_governance_create_conflict",
            action_hint="reload_release_state",
        ) from exc
    _audit_event(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type="survey.governance_release.created",
        release=release,
        details={"status": "draft", "policy_sha256": release.policy_sha256},
        ip_address=ip_address,
    )
    _commit_or_governance_error("survey_governance_create_conflict")
    return release, False


def _legacy_v1_auto_start_matches(
    survey: EncEncuesta,
    governance: Mapping[str, Any],
    stored: Mapping[str, Any],
    stored_json: str,
) -> bool:
    """Accept only the known v1 PostgreSQL timezone round-trip.

    Snapshot v1 removed the timezone offset from an auto-generated local
    ``inicio_at`` before the transaction committed. PostgreSQL later returns
    that same instant in UTC. The compatibility path substitutes only the
    legacy local representation of ``starts_at``; every other byte of the
    canonical snapshot still has to match.
    """

    stored_collection = stored.get("collection_rules")
    if not isinstance(stored_collection, Mapping):
        return False
    legacy_local_start = _survey_schedule_iso_v1_local(survey.inicio_at)
    if stored_collection.get("starts_at") != legacy_local_start:
        return False

    candidate = build_release_snapshot(
        survey,
        governance,
        schema_version=RELEASE_SNAPSHOT_SCHEMA_V1,
    )
    candidate["collection_rules"]["starts_at"] = legacy_local_start
    return _canonical_json(candidate) == stored_json


def _assert_release_snapshot_integrity(
    survey: EncEncuesta, release: SurveyGovernanceRelease
) -> None:
    try:
        stored = json.loads(release.snapshot_json)
    except (TypeError, ValueError) as exc:
        raise SurveyGovernanceError(
            "El snapshot de gobernanza esta corrupto",
            status_code=500,
            reason_code="survey_governance_snapshot_corrupt",
            action_hint="contact_support",
        ) from exc
    governance = stored.get("governance") if isinstance(stored, dict) else None
    if not isinstance(governance, dict):
        raise SurveyGovernanceError(
            "El snapshot de gobernanza está corrupto",
            status_code=500,
            reason_code="survey_governance_snapshot_corrupt",
            action_hint="contact_support",
        )
    schema_version = stored.get("schema_version")
    if schema_version not in _SUPPORTED_RELEASE_SNAPSHOT_SCHEMAS:
        raise SurveyGovernanceError(
            "La version del snapshot de gobernanza no esta soportada",
            status_code=500,
            reason_code="survey_governance_snapshot_schema_unsupported",
            action_hint="contact_support",
            extra={"release_id": release.id},
        )

    digest_matches = _sha256_text(release.snapshot_json) == release.snapshot_sha256
    current_json = _canonical_json(
        build_release_snapshot(
            survey,
            governance,
            schema_version=schema_version,
        )
    )
    snapshot_matches = current_json == release.snapshot_json
    if (
        digest_matches
        and not snapshot_matches
        and schema_version == RELEASE_SNAPSHOT_SCHEMA_V1
    ):
        snapshot_matches = _legacy_v1_auto_start_matches(
            survey,
            governance,
            stored,
            release.snapshot_json,
        )

    if not digest_matches or not snapshot_matches:
        raise SurveyGovernanceError(
            "El instrumento ya no coincide con su release inmutable",
            status_code=409,
            reason_code="survey_governance_snapshot_drift",
            action_hint="duplicate_as_new_draft",
            extra={"release_id": release.id},
        )


def publish_release(
    *,
    tenant_id: int,
    survey_id: int,
    release_id: int,
    actor_user_id: int,
    idempotency_key: str,
    expected_snapshot_sha256: str | None = None,
    ip_address: str | None = None,
) -> tuple[SurveyGovernanceRelease, bool]:
    key = _validate_idempotency_key(idempotency_key)
    request_hash = _operation_hash(
        "publish",
        {
            "tenant_id": tenant_id,
            "survey_id": survey_id,
            "release_id": release_id,
            "expected_snapshot_sha256": expected_snapshot_sha256,
        },
    )
    replay = SurveyGovernanceRelease.query.filter_by(
        tenant_id=tenant_id, publish_idempotency_key=key
    ).first()
    if replay is not None:
        if replay.publish_request_hash != request_hash:
            raise SurveyGovernanceError(
                "Idempotency-Key de publicación en conflicto",
                status_code=409,
                reason_code="survey_governance_idempotency_conflict",
                action_hint="reuse_original_payload_or_new_key",
            )
        return replay, True

    survey = _with_locked_survey(tenant_id, survey_id)
    release = (
        SurveyGovernanceRelease.query.filter_by(
            tenant_id=tenant_id, survey_id=survey_id, id=release_id
        )
        .with_for_update()
        .first()
    )
    if release is None:
        raise SurveyGovernanceError(
            "Release no encontrado",
            status_code=404,
            reason_code="survey_governance_release_not_found",
        )
    if release.status != "draft" or survey.estado != "borrador":
        raise SurveyGovernanceError(
            "El release no está en estado publicable",
            status_code=409,
            reason_code="survey_governance_release_not_publishable",
            action_hint="reload_release_state",
        )
    if expected_snapshot_sha256 and expected_snapshot_sha256 != release.snapshot_sha256:
        raise SurveyGovernanceError(
            "El snapshot esperado no coincide",
            status_code=409,
            reason_code="survey_governance_snapshot_conflict",
            action_hint="reload_release_state",
        )
    _assert_release_snapshot_integrity(survey, release)
    _assert_release_public_consent_integrity(release)
    from services.survey_eligibility import (
        SurveyEligibilityError,
        assert_restricted_release_gate_ready,
    )

    try:
        assert_restricted_release_gate_ready(release)
    except SurveyEligibilityError as exc:
        raise SurveyGovernanceError(
            exc.message,
            status_code=exc.status_code,
            reason_code=exc.reason_code,
            action_hint=exc.action_hint,
            extra=exc.extra,
        ) from exc

    # Reuse the battle-tested validation rules without its commit boundary.
    from services.encuestas_service import (
        _ensure_privacy_publication_ready,
        _ensure_publication_window,
        _resolve_current_public_link,
        _validate_persisted_instrument,
    )

    _validate_persisted_instrument(survey)
    _ensure_publication_window(survey)
    _ensure_privacy_publication_ready(survey)

    from services.survey_jurisdiction import (
        SurveyJurisdictionError,
        assert_publication_allowed,
        record_content_receipt,
    )

    try:
        assert_publication_allowed(survey)
    except SurveyJurisdictionError as exc:
        raise SurveyGovernanceError(
            exc.message,
            status_code=exc.status_code,
            reason_code=exc.reason_code,
            action_hint=exc.action_hint,
            extra=exc.extra,
        ) from exc

    now = _utc_now()
    release.status = "published"
    release.published_at = now
    release.published_by_user_id = actor_user_id
    release.publish_idempotency_key = key
    release.publish_request_hash = request_hash
    survey.estado = "publicada"
    link = _resolve_current_public_link(survey)
    if link is None:
        db.session.add(EncLink(encuesta_id=survey.id, slug_publico=survey.slug, canal="web"))
    _audit_event(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type="survey.governance_release.published",
        release=release,
        details={"status": "published", "policy_sha256": release.policy_sha256},
        ip_address=ip_address,
    )
    record_content_receipt(
        survey,
        event_type="published",
        decision="published",
        actor_user_id=actor_user_id,
        reason_code="survey_governance_release_published",
        idempotency_key=(
            "governance-publish:"
            + _sha256_text(f"{tenant_id}:{survey_id}:{release.id}:{key}")
        ),
    )
    _commit_or_governance_error("survey_governance_publish_conflict")
    return release, False


def _response_set_manifest(release: SurveyGovernanceRelease) -> tuple[int, str]:
    rows = (
        EncRespuesta.query.filter_by(
            tenant_id=release.tenant_id,
            encuesta_id=release.survey_id,
            governance_release_id=release.id,
        )
        .order_by(EncRespuesta.id.asc())
        .all()
    )
    # Mirror fields on enc_respuesta are useful for reads, but the append-only
    # terminal ledger is authoritative for restricted eligibility. A
    # privileged or future writer must not be able to close a governed release
    # by populating only the mirror columns.
    from services.survey_eligibility import (
        SurveyEligibilityError,
        response_eligibility_contract,
    )

    for row in rows:
        try:
            response_eligibility_contract(row)
        except SurveyEligibilityError as exc:
            raise SurveyGovernanceError(
                "Una respuesta no tiene un recibo de elegibilidad autoritativo; el cierre falla de forma segura",
                status_code=409,
                reason_code="survey_governance_eligibility_receipt_incomplete",
                action_hint="investigate_eligibility_receipt_integrity",
                extra={"eligibility_reason_code": exc.reason_code},
            ) from exc
    canonical_rows = [
        {
            "response_id": int(row.id),
            "content_sha256": row.content_hash,
            "submitted_at": _iso(row.submitted_at),
        }
        for row in rows
    ]
    return len(canonical_rows), _canonical_hash(canonical_rows)


def close_release(
    *,
    tenant_id: int,
    survey_id: int,
    release_id: int,
    actor_user_id: int,
    idempotency_key: str,
    human_review_reference: str,
    ip_address: str | None = None,
) -> tuple[SurveyGovernanceRelease, bool]:
    key = _validate_idempotency_key(idempotency_key)
    review_ref = str(human_review_reference or "").strip()
    if not _OPAQUE_REVIEW_RE.fullmatch(review_ref):
        raise SurveyGovernanceError(
            "human_review_reference debe ser una referencia opaca namespaced",
            status_code=422,
            reason_code="survey_human_review_reference_invalid",
            action_hint="provide_opaque_human_review_reference",
        )
    review_ref_sha256 = _sha256_text(review_ref)
    request_hash = _operation_hash(
        "close",
        {
            "tenant_id": tenant_id,
            "survey_id": survey_id,
            "release_id": release_id,
            "human_review_reference_sha256": review_ref_sha256,
        },
    )
    replay = SurveyGovernanceRelease.query.filter_by(
        tenant_id=tenant_id, close_idempotency_key=key
    ).first()
    if replay is not None:
        if replay.close_request_hash != request_hash:
            raise SurveyGovernanceError(
                "Idempotency-Key de cierre en conflicto",
                status_code=409,
                reason_code="survey_governance_idempotency_conflict",
                action_hint="reuse_original_payload_or_new_key",
            )
        return replay, True

    survey = _with_locked_survey(tenant_id, survey_id)
    release = (
        SurveyGovernanceRelease.query.filter_by(
            tenant_id=tenant_id, survey_id=survey_id, id=release_id
        )
        .with_for_update()
        .first()
    )
    if release is None:
        raise SurveyGovernanceError(
            "Release no encontrado",
            status_code=404,
            reason_code="survey_governance_release_not_found",
        )
    if release.status != "published" or survey.estado != "publicada":
        raise SurveyGovernanceError(
            "El release no está abierto para cierre",
            status_code=409,
            reason_code="survey_governance_release_not_closable",
            action_hint="reload_release_state",
        )
    _assert_release_snapshot_integrity(survey, release)
    total_responses = EncRespuesta.query.filter_by(
        tenant_id=tenant_id, encuesta_id=survey_id
    ).count()
    response_count, response_set_sha256 = _response_set_manifest(release)
    if total_responses != response_count:
        raise SurveyGovernanceError(
            "Existen respuestas no vinculadas al release; el cierre falla de forma segura",
            status_code=409,
            reason_code="survey_governance_unpinned_responses",
            action_hint="investigate_response_integrity",
        )

    closed_at = _utc_now()
    manifest = {
        "contract_version": CLOSURE_MANIFEST_CONTRACT_VERSION,
        "tenant_id": tenant_id,
        "survey_id": survey_id,
        "release_id": release_id,
        "release_version": release.version_number,
        "snapshot_sha256": release.snapshot_sha256,
        "policy_sha256": release.policy_sha256,
        "response_count": response_count,
        "response_set_sha256": response_set_sha256,
        "human_review_reference_sha256": review_ref_sha256,
        "closed_at": _iso(closed_at),
        "assurance": {
            "scope": "local_database_closure_integrity",
            "regulated_election_certified": False,
            "result_certified": False,
            "external_anchor_verified": False,
        },
    }
    manifest_json = _canonical_json(manifest)
    release.status = "closed"
    release.closed_at = closed_at
    release.closed_by_user_id = actor_user_id
    release.close_idempotency_key = key
    release.close_request_hash = request_hash
    release.closure_manifest_json = manifest_json
    release.closure_manifest_sha256 = _sha256_text(manifest_json)
    release.closed_response_count = response_count
    survey.estado = "cerrada"
    survey.fin_at = survey.fin_at or closed_at
    _audit_event(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type="survey.governance_release.closed",
        release=release,
        details={
            "status": "closed",
            "closure_manifest_sha256": release.closure_manifest_sha256,
            "response_count": response_count,
        },
        ip_address=ip_address,
    )
    _commit_or_governance_error("survey_governance_close_conflict")
    return release, False


def _release_governance_payload(release: SurveyGovernanceRelease) -> dict[str, Any]:
    try:
        snapshot = json.loads(release.snapshot_json)
    except (TypeError, ValueError):
        snapshot = {}
    governance = snapshot.get("governance") if isinstance(snapshot, dict) else {}
    return governance if isinstance(governance, dict) else {}


def release_public_consent_status(
    release: SurveyGovernanceRelease,
) -> dict[str, Any]:
    """Describe whether an immutable release carries verifiable public consent.

    This is deliberately non-throwing so legacy rows can still be listed and
    audited. Missing or non-canonical legacy content is marked incomplete and
    is never treated as implicit consent.
    """

    governance = _release_governance_payload(release)
    consent = governance.get("consent") if isinstance(governance, Mapping) else None
    if not isinstance(consent, Mapping):
        return {
            "complete": False,
            "reason_code": "survey_consent_public_text_required",
            "content_format": CONSENT_TEXT_CONTENT_FORMAT,
            "normalization": CONSENT_TEXT_NORMALIZATION,
        }
    public_text = consent.get("public_text")
    try:
        normalized = normalize_public_consent_text(public_text)
    except SurveyGovernanceError as exc:
        return {
            "complete": False,
            "reason_code": exc.reason_code,
            "content_format": CONSENT_TEXT_CONTENT_FORMAT,
            "normalization": CONSENT_TEXT_NORMALIZATION,
        }
    if public_text != normalized:
        return {
            "complete": False,
            "reason_code": "survey_consent_public_text_not_canonical",
            "content_format": CONSENT_TEXT_CONTENT_FORMAT,
            "normalization": CONSENT_TEXT_NORMALIZATION,
        }
    text_sha256 = str(consent.get("text_sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(text_sha256) or text_sha256 != _sha256_text(normalized):
        return {
            "complete": False,
            "reason_code": "survey_consent_text_hash_mismatch",
            "content_format": CONSENT_TEXT_CONTENT_FORMAT,
            "normalization": CONSENT_TEXT_NORMALIZATION,
        }
    if (
        consent.get("content_format") != CONSENT_TEXT_CONTENT_FORMAT
        or consent.get("normalization") != CONSENT_TEXT_NORMALIZATION
        or consent.get("required") is not True
        or consent.get("stores_public_text") is not True
        or consent.get("records_participant_input") is not False
    ):
        return {
            "complete": False,
            "reason_code": "survey_consent_public_contract_incomplete",
            "content_format": CONSENT_TEXT_CONTENT_FORMAT,
            "normalization": CONSENT_TEXT_NORMALIZATION,
        }
    return {
        "complete": True,
        "reason_code": None,
        "content_format": CONSENT_TEXT_CONTENT_FORMAT,
        "normalization": CONSENT_TEXT_NORMALIZATION,
    }


def _assert_release_public_consent_integrity(
    release: SurveyGovernanceRelease,
) -> None:
    status = release_public_consent_status(release)
    if status["complete"] is not True:
        raise SurveyGovernanceError(
            "El release no contiene un consentimiento público inmutable y verificable",
            status_code=409,
            reason_code=str(status["reason_code"]),
            action_hint="duplicate_as_new_draft_with_public_consent",
            extra={"release_id": release.id, "public_consent": status},
        )


def serialize_release(
    release: SurveyGovernanceRelease, *, replayed: bool = False
) -> dict[str, Any]:
    manifest = None
    if release.closure_manifest_json:
        try:
            manifest = json.loads(release.closure_manifest_json)
        except (TypeError, ValueError):
            manifest = None
    public_consent = release_public_consent_status(release)
    return {
        "ok": True,
        "contract_version": SURVEY_RELEASE_CONTRACT_VERSION,
        "release_id": int(release.id),
        "survey_id": int(release.survey_id),
        "version_number": int(release.version_number),
        "status": release.status,
        "snapshot_sha256": release.snapshot_sha256,
        "policy_sha256": release.policy_sha256,
        "governance": _release_governance_payload(release),
        "completeness": {"public_consent": public_consent},
        "published_at": _iso(release.published_at),
        "closed_at": _iso(release.closed_at),
        "closure": {
            "manifest_sha256": release.closure_manifest_sha256,
            "manifest": manifest,
        }
        if release.status == "closed"
        else None,
        "idempotency": {
            "persisted": True,
            "replayed": replayed,
            "disposition": "replayed" if replayed else "accepted",
        },
        "assurance": {
            "scope": "instrument_and_policy_integrity",
            "regulated_election_certified": False,
            "result_certified": False,
            "external_verification": "not_performed",
        },
    }


def _public_release_projection(
    release: SurveyGovernanceRelease,
    encuesta: EncEncuesta,
) -> dict[str, Any]:
    """Project a release without exposing a reconstructable small cohort.

    The durable closure manifest remains the audit source of truth.  Public
    source-anonymous contracts expose that manifest only once the final real
    response cohort reaches the configured disclosure threshold.  Synthetic
    and quarantined historical rows never satisfy that threshold.
    """

    payload = serialize_release(release)
    privacy_mode = str(getattr(encuesta, "privacy_mode", "legacy") or "legacy")
    if (
        release.status != "closed"
        or privacy_mode.strip().lower() != "source_anonymous"
    ):
        return payload

    origin_counts = {
        str(origin or "legacy_unverified"): int(count or 0)
        for origin, count in (
            db.session.query(
                EncRespuesta.response_origin,
                func.count(EncRespuesta.id),
            )
            .filter(
                EncRespuesta.tenant_id == release.tenant_id,
                EncRespuesta.encuesta_id == release.survey_id,
                EncRespuesta.governance_release_id == release.id,
            )
            .group_by(EncRespuesta.response_origin)
            .all()
        )
    }
    real_response_count = int(origin_counts.get("real", 0))
    excluded_counts = {
        "synthetic_demo": int(origin_counts.get("synthetic_demo", 0)),
        "legacy_unverified": int(origin_counts.get("legacy_unverified", 0)),
    }
    excluded_total = sum(excluded_counts.values())
    # Import locally to keep the governance persistence layer independent
    # while sharing the canonical public k-anonymity contract and bounds.
    from services.encuestas_service import public_survey_response_count_contract

    privacy = public_survey_response_count_contract(encuesta, real_response_count)
    if privacy.get("suppressed") is not True and excluded_total == 0:
        return payload

    closure = payload.get("closure")
    manifest_sha256 = (
        closure.get("manifest_sha256") if isinstance(closure, Mapping) else None
    )
    payload["closure"] = {
        "manifest_sha256": manifest_sha256,
        "manifest": None,
        "redacted": True,
        "privacy": privacy,
        "public_summary": {
            "contract_version": "surveys.public_release_summary.v1",
            "response_origin": "real",
            "response_count": privacy.get("count"),
            "response_count_bucket": privacy.get("bucket"),
            "excluded_non_real": {
                **excluded_counts,
                "total": excluded_total,
            },
            "truth_label": "citizen_responses_real_only",
        },
    }
    return payload


def _survey_releases(encuesta: EncEncuesta) -> list[SurveyGovernanceRelease]:
    return (
        SurveyGovernanceRelease.query.filter_by(
            tenant_id=encuesta.tenant_id, survey_id=encuesta.id
        )
        .order_by(SurveyGovernanceRelease.version_number.desc())
        .all()
    )


def survey_governance_contract(
    encuesta: EncEncuesta, *, validate_integrity: bool = True
) -> dict[str, Any]:
    releases = _survey_releases(encuesta)
    if not releases:
        return {
            "contract_version": GOVERNANCE_PUBLIC_CONTRACT_VERSION,
            "mode": "legacy",
            "release_required": False,
            "active_release": None,
            "latest_release": None,
            "regulated_election_certified": False,
            "result_certified": False,
        }
    active = next((item for item in releases if item.status == "published"), None)
    latest = active or releases[0]
    if active is not None and validate_integrity:
        _assert_release_snapshot_integrity(encuesta, active)
    active_consent = (
        release_public_consent_status(active)
        if active is not None
        else {
            "complete": False,
            "reason_code": "survey_governance_release_not_active",
            "content_format": CONSENT_TEXT_CONTENT_FORMAT,
            "normalization": CONSENT_TEXT_NORMALIZATION,
        }
    )
    active_eligibility = None
    if active is not None:
        # Import locally to keep the governance snapshot layer independent
        # from the runtime redemption model while exposing one authoritative
        # public gate contract.
        from services.survey_eligibility import public_eligibility_contract

        active_eligibility = public_eligibility_contract(active)
    eligibility_ready = bool(
        active_eligibility is not None
        and active_eligibility.get("intake_available") is True
    )
    blocked_reason_code = None
    if active_consent["complete"] is not True:
        blocked_reason_code = active_consent["reason_code"]
    elif not eligibility_ready:
        blocked_reason_code = (
            active_eligibility.get("blocked_reason_code")
            if isinstance(active_eligibility, Mapping)
            else "survey_governance_release_not_active"
        )
    return {
        "contract_version": GOVERNANCE_PUBLIC_CONTRACT_VERSION,
        "mode": "governed_release",
        "release_required": True,
        "active_release": (
            _public_release_projection(active, encuesta)
            if active is not None
            else None
        ),
        "latest_release": _public_release_projection(latest, encuesta),
        "eligibility": active_eligibility,
        "accepting_responses": bool(
            active is not None
            and encuesta.estado == "publicada"
            and active_consent["complete"] is True
            and eligibility_ready
        ),
        "blocked_reason_code": blocked_reason_code,
        "regulated_election_certified": False,
        "result_certified": False,
    }


def _governance_submission(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = payload.get("governance")
    return raw if isinstance(raw, Mapping) else {}


def governed_response_context(
    encuesta: EncEncuesta, payload: Mapping[str, Any]
) -> SurveyGovernanceRelease | None:
    releases = _survey_releases(encuesta)
    if not releases:
        return None
    release = next((item for item in releases if item.status == "published"), None)
    if release is None:
        raise SurveyGovernanceError(
            "La encuesta gobernada no tiene un release publicado activo",
            status_code=409,
            reason_code="survey_governance_release_not_active",
            action_hint="wait_for_published_release",
        )
    _assert_release_snapshot_integrity(encuesta, release)
    _assert_release_public_consent_integrity(release)
    submitted = _governance_submission(payload)
    expected = {
        "release_id": int(release.id),
        "snapshot_sha256": release.snapshot_sha256,
        "eligibility_policy_version": release.eligibility_policy_version,
        "consent_policy_version": release.consent_policy_version,
    }
    for field, expected_value in expected.items():
        actual = submitted.get(field)
        if field == "release_id":
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                actual = None
        if actual != expected_value:
            raise SurveyGovernanceError(
                "La respuesta no reconoce el release y las políticas vigentes",
                status_code=409,
                reason_code="survey_governance_ack_mismatch",
                action_hint="reload_public_survey_contract",
                extra={"field": field},
            )
    if submitted.get("consent_accepted") is not True:
        raise SurveyGovernanceError(
            "Se requiere consentimiento explícito para este release",
            status_code=422,
            reason_code="survey_governance_consent_missing",
            action_hint="request_explicit_consent",
        )
    if submitted.get("eligibility_acknowledged") is not True:
        raise SurveyGovernanceError(
            "Se requiere reconocer la política de elegibilidad",
            status_code=422,
            reason_code="survey_governance_eligibility_ack_missing",
            action_hint="request_eligibility_policy_ack",
        )
    return release


def bind_governed_response(
    respuesta: EncRespuesta,
    release: SurveyGovernanceRelease | None,
    *,
    acknowledged_at: datetime,
) -> None:
    if release is None:
        return
    respuesta.governance_release_id = release.id
    respuesta.governance_eligibility_policy_version = (
        release.eligibility_policy_version
    )
    respuesta.governance_consent_policy_version = release.consent_policy_version
    respuesta.governance_acknowledged_at = acknowledged_at


def response_governance_contract(respuesta: EncRespuesta) -> dict[str, Any]:
    from services.survey_eligibility import response_eligibility_contract

    eligibility = response_eligibility_contract(respuesta)
    release_id = getattr(respuesta, "governance_release_id", None)
    if release_id is None:
        return {
            "contract_version": GOVERNANCE_PUBLIC_CONTRACT_VERSION,
            "mode": "legacy",
            "release_id": None,
            "eligibility": eligibility,
        }
    release = db.session.get(SurveyGovernanceRelease, int(release_id))
    return {
        "contract_version": GOVERNANCE_PUBLIC_CONTRACT_VERSION,
        "mode": "governed_release",
        "release_id": int(release_id),
        "snapshot_sha256": getattr(release, "snapshot_sha256", None),
        "eligibility_policy_version": getattr(
            respuesta, "governance_eligibility_policy_version", None
        ),
        "consent_policy_version": getattr(
            respuesta, "governance_consent_policy_version", None
        ),
        "acknowledged_at": _iso(
            getattr(respuesta, "governance_acknowledged_at", None)
        ),
        "eligibility": eligibility,
        "eligibility_decision": eligibility.get("decision", "not_evaluated"),
        "human_review_required": True,
        "regulated_election_certified": False,
        "result_certified": False,
    }


def has_governance_release(encuesta: EncEncuesta) -> bool:
    return (
        db.session.query(func.count(SurveyGovernanceRelease.id))
        .filter_by(tenant_id=encuesta.tenant_id, survey_id=encuesta.id)
        .scalar()
        or 0
    ) > 0


def has_published_governance_release(encuesta: EncEncuesta) -> bool:
    return (
        db.session.query(func.count(SurveyGovernanceRelease.id))
        .filter(
            SurveyGovernanceRelease.tenant_id == encuesta.tenant_id,
            SurveyGovernanceRelease.survey_id == encuesta.id,
            SurveyGovernanceRelease.status.in_(["published", "closed"]),
        )
        .scalar()
        or 0
    ) > 0
