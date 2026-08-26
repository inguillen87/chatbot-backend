"""Single fail-closed boundary for survey jurisdiction and content review.

The service never interprets municipality names or survey copy.  It compares
only server-owned opaque jurisdiction references and immutable review receipts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from flask import current_app
from sqlalchemy.exc import IntegrityError

from database import db
from models import EncEncuesta, TenantProfile
from models_survey_jurisdiction import (
    SURVEY_CONTENT_ORIGINS,
    SURVEY_CONTENT_RECEIPT_CONTRACT_VERSION,
    SurveyContentReceipt,
)


SURVEY_JURISDICTION_CONTRACT_VERSION = "surveys.jurisdiction_guard.v1"
SURVEY_JURISDICTION_MODES = frozenset(
    {"observe", "enforce_publish", "enforce_visibility"}
)
SURVEY_JURISDICTION_RESERVED_FIELDS = frozenset(
    {"jurisdiction_ref", "content_origin", "content_origin_ref"}
)
_MUTATION_EVENTS = frozenset({"created", "updated", "rebound"})
_REVIEW_EVENTS = frozenset({"review_approved", "review_blocked"})
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{2,159}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class SurveyJurisdictionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        reason_code: str = "survey_jurisdiction_guard_blocked",
        action_hint: str = "review_survey_jurisdiction",
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
            "contract_version": SURVEY_JURISDICTION_CONTRACT_VERSION,
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


def _normalized_ref(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _validated_opaque_ref(value: Any, *, field: str) -> str:
    normalized = _normalized_ref(value)
    if normalized is None or not _OPAQUE_REF_RE.fullmatch(normalized):
        raise SurveyJurisdictionError(
            f"{field} no tiene un formato opaco válido",
            status_code=422,
            reason_code="survey_jurisdiction_reference_invalid",
            action_hint="configure_verified_tenant_jurisdiction",
        )
    return normalized


def _configured_mode() -> str:
    return str(
        current_app.config.get("SURVEY_JURISDICTION_GATE_MODE", "observe")
        or "observe"
    ).strip().lower()


def _configured_tenant_ids() -> tuple[set[int], bool]:
    raw = str(
        current_app.config.get("SURVEY_JURISDICTION_GATE_TENANT_IDS", "") or ""
    ).strip()
    if not raw:
        return set(), False
    result: set[int] = set()
    for item in raw.split(","):
        normalized = item.strip()
        if not re.fullmatch(r"[1-9][0-9]*", normalized):
            return set(), False
        result.add(int(normalized))
    return result, bool(result)


def jurisdiction_gate_configuration() -> dict[str, Any]:
    """Return parsed rollout state without reading or writing survey rows."""

    mode = _configured_mode()
    tenant_ids, allowlist_valid = _configured_tenant_ids()
    valid = mode in SURVEY_JURISDICTION_MODES and (
        mode == "observe" or allowlist_valid
    )
    return {
        "mode": mode,
        "tenant_ids": tenant_ids,
        "allowlist_valid": allowlist_valid,
        "valid": valid,
    }


def _tenant_is_enforced(tenant_id: int, *, visibility: bool) -> tuple[bool, bool]:
    config = jurisdiction_gate_configuration()
    mode = config["mode"]
    if mode == "observe":
        return False, bool(config["valid"])
    if not config["valid"]:
        # An invalid enforcement configuration cannot silently become observe.
        return True, False
    if visibility and mode != "enforce_visibility":
        return False, True
    return int(tenant_id) in config["tenant_ids"], True


def tenant_verified_jurisdiction(tenant: TenantProfile) -> str | None:
    if str(getattr(tenant, "jurisdiction_status", "") or "").strip().lower() != "verified":
        return None
    evidence = _normalized_ref(getattr(tenant, "jurisdiction_evidence_ref", None))
    reviewer = getattr(tenant, "jurisdiction_verified_by_user_id", None)
    verified_at = getattr(tenant, "jurisdiction_verified_at", None)
    if evidence is None or reviewer is None or verified_at is None:
        return None
    reference = _normalized_ref(getattr(tenant, "jurisdiction_ref", None))
    if reference is None or not _OPAQUE_REF_RE.fullmatch(reference):
        return None
    return reference


def bind_verified_tenant_jurisdiction(
    encuesta: EncEncuesta,
    *,
    tenant: TenantProfile | None = None,
) -> str | None:
    """Bind only a verified server-side tenant reference; never infer one."""

    tenant = tenant or db.session.get(TenantProfile, int(encuesta.tenant_id))
    if tenant is None:
        return None
    verified_ref = tenant_verified_jurisdiction(tenant)
    if verified_ref is None:
        return None
    current_ref = _normalized_ref(getattr(encuesta, "jurisdiction_ref", None))
    if current_ref is not None and current_ref != verified_ref:
        raise SurveyJurisdictionError(
            "La jurisdicción vinculada a la encuesta contradice al tenant verificado",
            reason_code="survey_jurisdiction_binding_conflict",
            action_hint="duplicate_and_review_for_verified_jurisdiction",
        )
    encuesta.jurisdiction_ref = verified_ref
    return verified_ref


def survey_content_document(encuesta: EncEncuesta) -> dict[str, Any]:
    """Build the server-owned content document covered by human review."""

    questions: list[dict[str, Any]] = []
    for question in sorted(
        list(encuesta.preguntas or []),
        key=lambda item: (int(item.orden or 0), int(item.id or 0)),
    ):
        questions.append(
            {
                "question_ref": question.logical_ref,
                "order": int(question.orden or 0),
                "type": question.tipo,
                "text": question.texto,
                "required": bool(question.obligatoria),
                "min_selections": question.min_selecciones,
                "max_selections": question.max_selecciones,
                "conditional_logic": question.logica_condicional,
                "options": [
                    {
                        "option_ref": option.logical_ref,
                        "order": int(option.orden or 0),
                        "text": option.texto,
                        "value": option.valor,
                    }
                    for option in sorted(
                        list(question.opciones or []),
                        key=lambda item: (int(item.orden or 0), int(item.id or 0)),
                    )
                ],
            }
        )
    segments = [
        {"key": segment.clave, "value": segment.valor}
        for segment in sorted(
            list(encuesta.segmentos or []),
            key=lambda item: (str(item.clave), str(item.valor), int(item.id or 0)),
        )
    ]
    return {
        "contract_version": "surveys.content_document.v1",
        "tenant_id": int(encuesta.tenant_id),
        "survey_id": int(encuesta.id),
        "jurisdiction_ref": _normalized_ref(encuesta.jurisdiction_ref),
        "content_origin": str(
            encuesta.content_origin or "legacy_unverified"
        ).strip().lower(),
        "content_origin_ref": _normalized_ref(encuesta.content_origin_ref),
        "document_ref": _normalized_ref(encuesta.document_ref),
        "slug": encuesta.slug,
        "title": encuesta.titulo,
        "description": encuesta.descripcion,
        "type": encuesta.tipo,
        "collection": {
            "requires_identity": bool(encuesta.requiere_identidad),
            "uniqueness_policy": encuesta.politica_unicidad,
            "anonymous_allowed": bool(encuesta.anonimo_permitido),
            "live_vote": bool(encuesta.es_votacion_envivo),
            "live_results": bool(encuesta.mostrar_resultados_envivo),
            "comments_allowed": bool(encuesta.permitir_comentarios),
            "reward_points": int(encuesta.puntos_recompensa or 0),
        },
        "privacy": {
            "mode": encuesta.privacy_mode or "legacy",
            "policy_version": encuesta.privacy_policy_version,
            "policy_url": encuesta.privacy_policy_url,
            "consent_required": bool(encuesta.privacy_consent_required),
            "retention_days": encuesta.response_retention_days,
        },
        "questions": questions,
        "segments": segments,
    }


def survey_content_sha256(encuesta: EncEncuesta) -> str:
    return _sha256_text(_canonical_json(survey_content_document(encuesta)))


def _receipt_rows(encuesta: EncEncuesta) -> list[SurveyContentReceipt]:
    return (
        SurveyContentReceipt.query.filter_by(
            tenant_id=int(encuesta.tenant_id), survey_id=int(encuesta.id)
        )
        .order_by(SurveyContentReceipt.id.asc())
        .all()
    )


def _receipt_chain_is_valid(rows: list[SurveyContentReceipt]) -> bool:
    previous_sha256: str | None = None
    for row in rows:
        if row.previous_receipt_sha256 != previous_sha256:
            return False
        if _sha256_text(row.receipt_json) != row.receipt_sha256:
            return False
        try:
            body = json.loads(row.receipt_json)
        except (TypeError, ValueError):
            return False
        if not isinstance(body, dict):
            return False
        expected = {
            "contract_version": row.contract_version,
            "tenant_id": int(row.tenant_id),
            "survey_id": int(row.survey_id),
            "event_type": row.event_type,
            "decision": row.decision,
            "content_sha256": row.content_sha256,
            "request_content_sha256": row.request_content_sha256,
            "jurisdiction_ref": _normalized_ref(row.jurisdiction_ref),
            "content_origin": row.content_origin,
            "content_origin_ref": _normalized_ref(row.content_origin_ref),
            "actor_user_id": row.actor_user_id,
            "evidence_ref": _normalized_ref(row.evidence_ref),
            "reason_code": _normalized_ref(row.reason_code),
            "idempotency_key": row.idempotency_key,
            "previous_receipt_sha256": previous_sha256,
        }
        if any(body.get(key) != value for key, value in expected.items()):
            return False
        previous_sha256 = row.receipt_sha256
    return True


def _readiness(encuesta: EncEncuesta) -> dict[str, Any]:
    content_sha256 = survey_content_sha256(encuesta)
    tenant = db.session.get(TenantProfile, int(encuesta.tenant_id))
    verified_ref = tenant_verified_jurisdiction(tenant) if tenant is not None else None
    survey_ref = _normalized_ref(encuesta.jurisdiction_ref)
    origin = str(encuesta.content_origin or "legacy_unverified").strip().lower()
    rows = _receipt_rows(encuesta)
    receipt_chain_valid = _receipt_chain_is_valid(rows)
    latest_mutation = next(
        (item for item in reversed(rows) if item.event_type in _MUTATION_EVENTS),
        None,
    )
    review_rows = [
        item
        for item in rows
        if item.event_type in _REVIEW_EVENTS
        and (latest_mutation is None or int(item.id) > int(latest_mutation.id))
    ]
    latest_review = review_rows[-1] if review_rows else None

    reason_code = "survey_jurisdiction_ready"
    ready = True
    if tenant is None:
        ready = False
        reason_code = "survey_tenant_not_found"
    elif verified_ref is None:
        ready = False
        reason_code = "survey_tenant_jurisdiction_unverified"
    elif survey_ref is None:
        ready = False
        reason_code = "survey_jurisdiction_unbound"
    elif survey_ref != verified_ref:
        ready = False
        reason_code = "survey_jurisdiction_binding_conflict"
    elif origin not in SURVEY_CONTENT_ORIGINS:
        ready = False
        reason_code = "survey_content_origin_invalid"
    elif not receipt_chain_valid:
        ready = False
        reason_code = "survey_content_receipt_integrity_failed"
    elif latest_review is None:
        ready = False
        reason_code = "survey_content_review_required"
    elif latest_review.event_type == "review_blocked":
        ready = False
        reason_code = "survey_content_review_blocked"
    elif (
        latest_review.content_sha256 != content_sha256
        or _normalized_ref(latest_review.jurisdiction_ref) != verified_ref
        or str(latest_review.content_origin or "").strip().lower() != origin
        or _normalized_ref(latest_review.content_origin_ref)
        != _normalized_ref(encuesta.content_origin_ref)
    ):
        ready = False
        reason_code = "survey_content_review_stale"

    return {
        "ready": ready,
        "reason_code": reason_code,
        "content_sha256": content_sha256,
        "tenant_jurisdiction_ref": verified_ref,
        "survey_jurisdiction_ref": survey_ref,
        "content_origin": origin,
        "content_origin_ref": _normalized_ref(encuesta.content_origin_ref),
        "latest_mutation_receipt_id": latest_mutation.id if latest_mutation else None,
        "latest_review_receipt_id": latest_review.id if latest_review else None,
        "latest_review_receipt_sha256": (
            latest_review.receipt_sha256 if latest_review else None
        ),
        "receipt_chain_valid": receipt_chain_valid,
    }


def jurisdiction_contract(encuesta: EncEncuesta) -> dict[str, Any]:
    """Read-only admin contract; never creates, binds or refreshes rows."""

    readiness = _readiness(encuesta)
    config = jurisdiction_gate_configuration()
    publish_enforced, config_valid = _tenant_is_enforced(
        int(encuesta.tenant_id), visibility=False
    )
    visibility_enforced, _ = _tenant_is_enforced(
        int(encuesta.tenant_id), visibility=True
    )
    return {
        "contract_version": SURVEY_JURISDICTION_CONTRACT_VERSION,
        "mode": config["mode"],
        "configuration_valid": config_valid,
        "publish_enforced": publish_enforced,
        "visibility_enforced": visibility_enforced,
        "allowed_to_publish": bool(
            not publish_enforced or (config_valid and readiness["ready"])
        ),
        **readiness,
    }


def assert_publication_allowed(encuesta: EncEncuesta) -> dict[str, Any]:
    contract = jurisdiction_contract(encuesta)
    if contract["allowed_to_publish"]:
        return contract
    reason = contract["reason_code"]
    if not contract["configuration_valid"]:
        reason = "survey_jurisdiction_gate_configuration_invalid"
    action_hint = {
        "survey_jurisdiction_gate_configuration_invalid": (
            "fix_survey_jurisdiction_gate_configuration"
        ),
        "survey_tenant_jurisdiction_unverified": (
            "configure_verified_tenant_jurisdiction"
        ),
        "survey_jurisdiction_unbound": (
            "bind_verified_tenant_jurisdiction_then_review"
        ),
        "survey_jurisdiction_binding_conflict": (
            "duplicate_and_review_for_verified_jurisdiction"
        ),
        "survey_content_receipt_integrity_failed": "contact_support",
    }.get(reason, "review_exact_survey_content")
    raise SurveyJurisdictionError(
        "La publicación está bloqueada por el control institucional de jurisdicción",
        reason_code=reason,
        action_hint=action_hint,
        extra={
            "survey_id": int(encuesta.id),
            "current_state": str(encuesta.estado or "unknown"),
            "jurisdiction": contract,
        },
    )


def assert_content_mutation_allowed(encuesta: EncEncuesta) -> None:
    """Prevent an enforced published instrument from bypassing re-publication.

    Observe mode retains the legacy editable-public behavior. Once a tenant is
    canaried for either enforcement stage, published content is immutable and
    changes must follow duplicate -> review -> publish.
    """

    enforced, config_valid = _tenant_is_enforced(
        int(encuesta.tenant_id), visibility=False
    )
    if str(encuesta.estado or "").strip().lower() != "publicada" or not enforced:
        return
    reason_code = (
        "survey_jurisdiction_gate_configuration_invalid"
        if not config_valid
        else "survey_published_content_mutation_requires_duplicate"
    )
    raise SurveyJurisdictionError(
        "Una encuesta publicada y gobernada no puede modificarse en el lugar",
        reason_code=reason_code,
        action_hint=(
            "fix_survey_jurisdiction_gate_configuration"
            if not config_valid
            else "duplicate_as_new_draft_review_and_publish"
        ),
    )


def survey_is_publicly_visible(encuesta: EncEncuesta) -> bool:
    enforced, config_valid = _tenant_is_enforced(
        int(encuesta.tenant_id), visibility=True
    )
    if not enforced:
        return True
    if not config_valid:
        return False
    return bool(_readiness(encuesta)["ready"])


def _validate_receipt_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise SurveyJurisdictionError(
            "Idempotency-Key debe tener entre 8 y 128 caracteres seguros",
            status_code=400,
            reason_code="survey_content_review_idempotency_invalid",
            action_hint="send_valid_idempotency_key",
        )
    return key


def record_content_receipt(
    encuesta: EncEncuesta,
    *,
    event_type: str,
    decision: str,
    actor_user_id: int | None,
    evidence_ref: str | None = None,
    reason_code: str | None = None,
    idempotency_key: str | None = None,
    request_content_sha256: str | None = None,
    created_at: datetime | None = None,
) -> tuple[SurveyContentReceipt | None, bool]:
    """Append one receipt inside the caller's transaction."""

    db.session.flush()
    tenant = db.session.get(TenantProfile, int(encuesta.tenant_id))
    if tenant is None:
        enforced, config_valid = _tenant_is_enforced(
            int(encuesta.tenant_id), visibility=False
        )
        if enforced or not config_valid:
            raise SurveyJurisdictionError(
                "No existe un tenant canónico para el recibo institucional",
                reason_code="survey_tenant_not_found",
                action_hint="migrate_survey_to_canonical_tenant_scope",
            )
        # Observe mode preserves non-canonical legacy callers without
        # manufacturing an audit row that falsely asserts tenant ownership.
        return None, False
    if idempotency_key is not None:
        existing = SurveyContentReceipt.query.filter_by(
            tenant_id=int(encuesta.tenant_id), idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            expected_hash = survey_content_sha256(encuesta)
            if (
                int(existing.survey_id) != int(encuesta.id)
                or existing.event_type != event_type
                or existing.decision != decision
                or existing.content_sha256 != expected_hash
                or existing.request_content_sha256 != request_content_sha256
            ):
                raise SurveyJurisdictionError(
                    "Idempotency-Key ya fue usado con otro recibo",
                    reason_code="survey_content_review_idempotency_conflict",
                    action_hint="reuse_original_request_or_new_key",
                )
            return existing, True

    previous = (
        SurveyContentReceipt.query.filter_by(
            tenant_id=int(encuesta.tenant_id), survey_id=int(encuesta.id)
        )
        .order_by(SurveyContentReceipt.id.desc())
        .first()
    )
    timestamp = created_at or _utc_now()
    content_sha256 = survey_content_sha256(encuesta)
    body = {
        "contract_version": SURVEY_CONTENT_RECEIPT_CONTRACT_VERSION,
        "tenant_id": int(encuesta.tenant_id),
        "survey_id": int(encuesta.id),
        "event_type": event_type,
        "decision": decision,
        "content_sha256": content_sha256,
        "request_content_sha256": request_content_sha256,
        "jurisdiction_ref": _normalized_ref(encuesta.jurisdiction_ref),
        "content_origin": str(
            encuesta.content_origin or "legacy_unverified"
        ).strip().lower(),
        "content_origin_ref": _normalized_ref(encuesta.content_origin_ref),
        "actor_user_id": int(actor_user_id) if actor_user_id is not None else None,
        "evidence_ref": _normalized_ref(evidence_ref),
        "reason_code": _normalized_ref(reason_code),
        "idempotency_key": idempotency_key,
        "previous_receipt_sha256": (
            previous.receipt_sha256 if previous is not None else None
        ),
        "created_at": timestamp.astimezone(timezone.utc).isoformat(),
    }
    receipt_json = _canonical_json(body)
    receipt = SurveyContentReceipt(
        tenant_id=int(encuesta.tenant_id),
        survey_id=int(encuesta.id),
        event_type=event_type,
        decision=decision,
        content_sha256=content_sha256,
        request_content_sha256=request_content_sha256,
        jurisdiction_ref=body["jurisdiction_ref"],
        content_origin=body["content_origin"],
        content_origin_ref=body["content_origin_ref"],
        actor_user_id=body["actor_user_id"],
        evidence_ref=body["evidence_ref"],
        reason_code=body["reason_code"],
        idempotency_key=idempotency_key,
        previous_receipt_sha256=body["previous_receipt_sha256"],
        receipt_json=receipt_json,
        receipt_sha256=_sha256_text(receipt_json),
        created_at=timestamp,
    )
    db.session.add(receipt)
    db.session.flush()
    return receipt, False


def review_survey_content(
    *,
    tenant_id: int,
    survey_id: int,
    actor_user_id: int,
    decision: str,
    expected_content_sha256: str,
    evidence_ref: str,
    idempotency_key: str,
) -> tuple[SurveyContentReceipt, bool]:
    """Bind or review one exact server-side content document.

    Binding is deliberately a separate request from approval.  A reviewer must
    reload the post-bind document and explicitly submit that new hash before an
    approval receipt can be appended.
    """

    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"bind", "approve", "block"}:
        raise SurveyJurisdictionError(
            "decision debe ser bind, approve o block",
            status_code=400,
            reason_code="survey_content_review_decision_invalid",
            action_hint="send_bind_approve_or_block",
        )
    expected_hash = str(expected_content_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(expected_hash):
        raise SurveyJurisdictionError(
            "expected_content_sha256 es obligatorio",
            status_code=400,
            reason_code="survey_content_review_hash_invalid",
            action_hint="reload_jurisdiction_readiness",
        )
    evidence = _validated_opaque_ref(evidence_ref, field="evidence_ref")
    key = _validate_receipt_idempotency_key(idempotency_key)

    survey = (
        EncEncuesta.query.filter_by(tenant_id=int(tenant_id), id=int(survey_id))
        .with_for_update()
        .first()
    )
    tenant = (
        TenantProfile.query.filter_by(id=int(tenant_id)).with_for_update().first()
    )
    if survey is None or tenant is None:
        raise SurveyJurisdictionError(
            "Encuesta no encontrada",
            status_code=404,
            reason_code="survey_not_found",
            action_hint="check_survey_id",
        )

    replay = SurveyContentReceipt.query.filter_by(
        tenant_id=int(tenant_id), idempotency_key=key
    ).first()
    if replay is not None:
        expected_event = {
            "bind": "rebound",
            "approve": "review_approved",
            "block": "review_blocked",
        }[normalized_decision]
        if (
            int(replay.survey_id) == int(survey_id)
            and replay.event_type == expected_event
            and replay.request_content_sha256 == expected_hash
        ):
            return replay, True
        raise SurveyJurisdictionError(
            "Idempotency-Key ya fue usado con otra revisión",
            reason_code="survey_content_review_idempotency_conflict",
            action_hint="reuse_original_request_or_new_key",
        )

    current_hash = survey_content_sha256(survey)
    if current_hash != expected_hash:
        raise SurveyJurisdictionError(
            "El contenido cambió desde que fue cargado para revisión",
            reason_code="survey_content_review_hash_conflict",
            action_hint="reload_jurisdiction_readiness",
            extra={"current_content_sha256": current_hash},
        )

    if normalized_decision in {"bind", "approve"}:
        verified_ref = tenant_verified_jurisdiction(tenant)
        if verified_ref is None:
            raise SurveyJurisdictionError(
                "El tenant no tiene una jurisdicción verificada",
                reason_code="survey_tenant_jurisdiction_unverified",
                action_hint="verify_tenant_jurisdiction_with_evidence",
            )
        current_ref = _normalized_ref(survey.jurisdiction_ref)
        if current_ref is not None and current_ref != verified_ref:
            raise SurveyJurisdictionError(
                "La jurisdicción vinculada a la encuesta contradice al tenant verificado",
                reason_code="survey_jurisdiction_binding_conflict",
                action_hint="duplicate_and_review_for_verified_jurisdiction",
            )

    if normalized_decision == "bind":
        if _normalized_ref(survey.jurisdiction_ref) is not None:
            raise SurveyJurisdictionError(
                "La encuesta ya tiene una jurisdicción verificada vinculada",
                reason_code="survey_jurisdiction_already_bound",
                action_hint="reload_jurisdiction_readiness_then_review",
            )
        bind_verified_tenant_jurisdiction(survey, tenant=tenant)
        event_type = "rebound"
        receipt_decision = "recorded"
        receipt_reason_code = "survey_jurisdiction_bound_before_review"
    elif normalized_decision == "approve":
        if _normalized_ref(survey.jurisdiction_ref) is None:
            raise SurveyJurisdictionError(
                "La jurisdicción verificada debe vincularse antes de aprobar",
                reason_code="survey_jurisdiction_binding_required",
                action_hint="bind_verified_jurisdiction_then_reload",
            )
        event_type = "review_approved"
        receipt_decision = "approved"
        receipt_reason_code = "survey_content_human_review"
    else:
        event_type = "review_blocked"
        receipt_decision = "blocked"
        receipt_reason_code = "survey_content_human_review"

    receipt, replayed = record_content_receipt(
        survey,
        event_type=event_type,
        decision=receipt_decision,
        actor_user_id=actor_user_id,
        evidence_ref=evidence,
        reason_code=receipt_reason_code,
        idempotency_key=key,
        request_content_sha256=expected_hash,
    )
    if receipt is None:  # Review always has a canonical locked tenant.
        raise SurveyJurisdictionError(
            "No se pudo crear el recibo institucional",
            status_code=500,
            reason_code="survey_content_review_receipt_missing",
            action_hint="contact_support",
        )
    if event_type in _REVIEW_EVENTS and (
        receipt.content_sha256 != expected_hash
        or receipt.request_content_sha256 != expected_hash
    ):
        db.session.rollback()
        raise SurveyJurisdictionError(
            "La revisión no coincide con el hash exacto solicitado",
            status_code=500,
            reason_code="survey_content_review_hash_invariant_failed",
            action_hint="reload_jurisdiction_readiness",
        )
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise SurveyJurisdictionError(
            "Conflicto al guardar la revisión inmutable",
            reason_code="survey_content_review_commit_conflict",
            action_hint="retry_same_idempotency_key",
        ) from exc
    except Exception as exc:
        db.session.rollback()
        raise SurveyJurisdictionError(
            "No se pudo confirmar atómicamente la revisión",
            status_code=500,
            reason_code="survey_content_review_atomic_commit_failed",
            action_hint="retry_same_idempotency_key",
        ) from exc
    return receipt, replayed


def serialize_content_receipt(receipt: SurveyContentReceipt) -> dict[str, Any]:
    return {
        "id": int(receipt.id),
        "contract_version": receipt.contract_version,
        "tenant_id": int(receipt.tenant_id),
        "survey_id": int(receipt.survey_id),
        "event_type": receipt.event_type,
        "decision": receipt.decision,
        "content_sha256": receipt.content_sha256,
        "request_content_sha256": receipt.request_content_sha256,
        "jurisdiction_ref": receipt.jurisdiction_ref,
        "content_origin": receipt.content_origin,
        "content_origin_ref": receipt.content_origin_ref,
        "evidence_ref": receipt.evidence_ref,
        "reason_code": receipt.reason_code,
        "previous_receipt_sha256": receipt.previous_receipt_sha256,
        "receipt_sha256": receipt.receipt_sha256,
        "created_at": receipt.created_at.isoformat(),
    }
