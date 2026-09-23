"""Durable, truth-bounded participation for deterministic demo surveys.

The public demo instruments in :mod:`services.demo_surveys` are synthetic
fixtures and intentionally do not exist in ``enc_encuesta``.  This module
provides an isolated append-only receipt ledger for Preview QA without ever
promoting an interaction to municipal/citizen response truth.

The rollout is fail-closed and default-off.  It can run only when all of these
conditions are true:

* ``ENABLE_PREVIEW_DURABLE_DEMO_SURVEY_VOTES_V1`` is explicitly enabled;
* the runtime is an exact Vercel Preview runtime (never Render/Production);
* the configured SQLAlchemy database is PostgreSQL hosted by Neon.

No raw idempotency key, IP address, user agent, Turnstile token, contact data or
untrusted metadata is persisted.  A single minimized row is both response and
receipt, making exactly-once acknowledgement atomic.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from typing import Any

from flask import current_app, g, has_app_context
from sqlalchemy import func, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from database import db
from models import DemoSurveyParticipation
from services.demo_surveys import (
    DEMO_SURVEY_RESPONSE_COUNT,
    build_demo_live_results_payload,
    build_demo_public_survey_payload,
    build_demo_survey_response_ack,
)
from services.encuestas_service import (
    EncuestaError,
    resolve_survey_submission_id,
)
from services.survey_response_provenance import build_survey_response_provenance


FEATURE_FLAG = "ENABLE_PREVIEW_DURABLE_DEMO_SURVEY_VOTES_V1"
EXPECTED_BRANCH_ID_CONFIG = "PREVIEW_DURABLE_DEMO_NEON_BRANCH_ID"
MAX_INTERACTIONS_CONFIG = "PREVIEW_DURABLE_DEMO_SURVEY_MAX_INTERACTIONS"
PARTICIPATION_CONTRACT_VERSION = "demo.survey_participation.v1"
AGGREGATE_CONTRACT_VERSION = "demo.survey_participation_aggregate.v1"
COMPOSITION_CONTRACT_VERSION = "demo.survey_data_composition.v1"
PERSISTENCE_CONTRACT_VERSION = "demo.survey_persistence.v1"
PUBLIC_RESPONSE_CONTRACT_VERSION = "surveys.public_response.v2"
RECEIPT_CONTRACT_VERSION = "surveys.response_receipt.v1"
CANONICAL_VERSION = "survey-response.v1"
INSTRUMENT_REVISION = 1
MAX_PUBLIC_PAYLOAD_BYTES = 32 * 1024
DEFAULT_MAX_INTERACTIONS_PER_SURVEY = 50
ABSOLUTE_MAX_INTERACTIONS_PER_SURVEY = 500

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off"})


@dataclass(frozen=True)
class DemoSurveyInstrument:
    slug: str
    tenant_slug: str
    sector: str
    question_id: str
    option_ids: tuple[str, ...]
    option_labels: tuple[str, ...]
    instrument_sha256: str
    instrument_revision: int = INSTRUMENT_REVISION


@dataclass(frozen=True)
class PreparedDemoSurveyParticipation:
    instrument: DemoSurveyInstrument
    submission_id: str
    submission_id_hash: str
    payload_hash: str
    question_id: str
    option_id: str


@dataclass(frozen=True)
class DemoSurveyParticipationReceipt:
    response_id: int
    survey_slug: str
    tenant_slug: str
    sector: str
    question_id: str
    option_id: str
    instrument_sha256: str
    instrument_revision: int
    submission_id: str
    replayed: bool


def _strict_opt_in(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if value is False or value is None or value == 0:
        return False
    normalized = str(value).strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return False


def _runtime_config() -> Mapping[str, Any]:
    return current_app.config if has_app_context() else {}


def _config_or_env(
    config: Mapping[str, Any],
    environ: Mapping[str, str],
    name: str,
) -> Any:
    return config.get(name) if name in config else environ.get(name)


def _expected_neon_branch_id(
    config: Mapping[str, Any],
    environ: Mapping[str, str],
) -> str:
    return str(_config_or_env(config, environ, EXPECTED_BRANCH_ID_CONFIG) or "").strip()


def _max_interactions_per_survey(
    config: Mapping[str, Any],
    environ: Mapping[str, str],
) -> int:
    raw_value = _config_or_env(config, environ, MAX_INTERACTIONS_CONFIG)
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError, OverflowError):
        parsed = DEFAULT_MAX_INTERACTIONS_PER_SURVEY
    return max(1, min(parsed, ABSOLUTE_MAX_INTERACTIONS_PER_SURVEY))


def _database_uri(
    config: Mapping[str, Any],
    environ: Mapping[str, str],
    explicit_uri: Any = None,
) -> Any:
    if explicit_uri not in (None, ""):
        return explicit_uri
    return (
        config.get("SQLALCHEMY_DATABASE_URI")
        or environ.get("SQLALCHEMY_DATABASE_URI")
        or environ.get("DATABASE_URL")
    )


def _is_neon_postgres_uri(raw_uri: Any) -> bool:
    """Recognize Neon PostgreSQL without returning or logging credentials."""

    if raw_uri in (None, ""):
        return False
    try:
        parsed = make_url(str(raw_uri))
        backend = str(parsed.get_backend_name() or "").strip().lower()
        hostname = str(parsed.host or "").strip().lower().rstrip(".")
    except Exception:
        return False
    return backend in {"postgres", "postgresql"} and (
        hostname == "neon.tech" or hostname.endswith(".neon.tech")
    )


def demo_survey_participation_gate(
    *,
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    database_uri: Any = None,
) -> dict[str, Any]:
    """Return a sanitized rollout decision; never includes a URL or secret."""

    runtime_config = _runtime_config() if config is None else config
    runtime_env = os.environ if environ is None else environ
    raw_flag = _config_or_env(runtime_config, runtime_env, FEATURE_FLAG)
    flag_enabled = _strict_opt_in(raw_flag)
    expected_branch_id = _expected_neon_branch_id(runtime_config, runtime_env)
    branch_guard_configured = (
        expected_branch_id.startswith("br-")
        and 6 <= len(expected_branch_id) <= 128
        and all(character.isalnum() or character == "-" for character in expected_branch_id)
    )
    max_interactions = _max_interactions_per_survey(runtime_config, runtime_env)
    vercel_env = str(runtime_env.get("VERCEL_ENV") or "").strip().lower()
    vercel_runtime = _strict_opt_in(runtime_env.get("VERCEL"))
    render_runtime = _strict_opt_in(runtime_env.get("RENDER")) or bool(
        str(runtime_env.get("RENDER_EXTERNAL_URL") or "").strip()
    )
    neon_database = _is_neon_postgres_uri(
        _database_uri(runtime_config, runtime_env, database_uri)
    )

    if not flag_enabled:
        reason = "flag_disabled"
    elif render_runtime:
        reason = "render_runtime_forbidden"
    elif not vercel_runtime or vercel_env != "preview":
        reason = "vercel_preview_required"
    elif not neon_database:
        reason = "neon_postgres_required"
    elif not branch_guard_configured:
        reason = "expected_neon_branch_required"
    else:
        reason = "enabled"

    return {
        "contract_version": "demo.survey_participation_gate.v1",
        "enabled": reason == "enabled",
        "reason": reason,
        "flag_enabled": flag_enabled,
        "runtime": "vercel_preview"
        if vercel_runtime and vercel_env == "preview" and not render_runtime
        else "unsupported",
        "database": "neon_postgres" if neon_database else "unsupported",
        "branch_guard_configured": branch_guard_configured,
        "max_interactions_per_survey": max_interactions,
        "production_allowed": False,
        "render_allowed": False,
    }


def durable_demo_survey_participation_enabled() -> bool:
    gate = demo_survey_participation_gate()
    if gate.get("enabled"):
        return True
    # An absent/default-off flag deliberately preserves the immutable demo.
    # Once an operator opts in, however, silently degrading to an ephemeral
    # acknowledgement would violate the advertised durability contract.
    if gate.get("flag_enabled"):
        raise _unavailable_error(str(gate.get("reason") or "unsupported_runtime"))
    return False


def _unavailable_error(reason: str, *, retryable: bool = False) -> EncuestaError:
    return EncuestaError(
        "La persistencia de participaciones demo no esta disponible en este entorno.",
        status_code=503,
        payload={
            "contract_version": PARTICIPATION_CONTRACT_VERSION,
            "reason_code": "demo_survey_participation_unavailable",
            "gate_reason": reason,
            "retryable": retryable,
            "action_hint": "retry_later" if retryable else "contact_support",
        },
    )


def _require_enabled() -> dict[str, Any]:
    gate = demo_survey_participation_gate()
    if not gate["enabled"]:
        raise _unavailable_error(str(gate["reason"]))
    expected_branch_id = _expected_neon_branch_id(_runtime_config(), os.environ)
    # Unit tests may replace the complete gate with an in-memory implementation.
    # Every real enabled gate requires an expected branch id above.
    if not expected_branch_id:
        return gate
    if has_app_context() and getattr(
        g,
        "_demo_survey_verified_neon_branch_id",
        None,
    ) == expected_branch_id:
        return gate
    try:
        actual_branch_id = str(
            db.session.execute(
                text("SELECT current_setting('neon.branch_id', true)")
            ).scalar()
            or ""
        ).strip()
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise _unavailable_error("neon_branch_verification_failed", retryable=True) from exc
    if actual_branch_id != expected_branch_id:
        db.session.rollback()
        raise _unavailable_error("neon_branch_mismatch")
    if has_app_context():
        g._demo_survey_verified_neon_branch_id = expected_branch_id
    return gate


def _survey_not_found_error() -> EncuestaError:
    return EncuestaError(
        "Encuesta no encontrada",
        status_code=404,
        payload={
            "contract_version": "surveys.public.v2",
            "reason_code": "survey_not_found",
            "retryable": False,
            "action_hint": "check_survey_link",
        },
    )


def _invalid_participation_error(message: str) -> EncuestaError:
    return EncuestaError(
        message,
        status_code=400,
        payload={
            "contract_version": PARTICIPATION_CONTRACT_VERSION,
            "reason_code": "demo_survey_participation_invalid",
            "retryable": False,
            "action_hint": "reload_survey",
        },
    )


def _submission_conflict_error() -> EncuestaError:
    return EncuestaError(
        "submission_id ya fue usado con otra participacion demo",
        status_code=409,
        payload={
            "contract_version": RECEIPT_CONTRACT_VERSION,
            "reason_code": "survey_submission_id_conflict",
            "retryable": False,
            "action_hint": "use_original_payload_or_new_submission_id",
        },
    )


def _receipt_corrupt_error() -> EncuestaError:
    return EncuestaError(
        "El recibo durable de la participacion demo es inconsistente.",
        status_code=500,
        payload={
            "contract_version": RECEIPT_CONTRACT_VERSION,
            "reason_code": "demo_survey_participation_receipt_corrupt",
            "retryable": False,
            "action_hint": "contact_support",
        },
    )


def _storage_error() -> EncuestaError:
    return _unavailable_error("storage_unavailable", retryable=True)


def _capacity_error(limit: int) -> EncuestaError:
    return EncuestaError(
        "La capacidad segura de esta encuesta demo ya fue alcanzada.",
        status_code=429,
        payload={
            "contract_version": PARTICIPATION_CONTRACT_VERSION,
            "reason_code": "demo_survey_participation_capacity_reached",
            "retryable": False,
            "action_hint": "use_another_demo_survey",
            "capacity": {
                "scope": "survey_slug",
                "limit": int(limit),
                "bounded": True,
            },
        },
    )


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_instrument(slug: str) -> DemoSurveyInstrument:
    normalized_slug = str(slug or "").strip().lower()
    public_payload = build_demo_public_survey_payload(normalized_slug)
    if not public_payload:
        raise _survey_not_found_error()

    questions = public_payload.get("preguntas") or []
    if not isinstance(questions, list) or len(questions) != 1:
        raise _invalid_participation_error(
            "El instrumento demo no tiene una pregunta unica verificable."
        )
    question = questions[0]
    if not isinstance(question, Mapping):
        raise _invalid_participation_error("La pregunta demo no es valida.")
    question_id = str(question.get("id") or "").strip()
    options = question.get("opciones") or []
    if not question_id or not isinstance(options, list) or not options:
        raise _invalid_participation_error("La pregunta demo no tiene opciones validas.")

    option_ids: list[str] = []
    option_labels: list[str] = []
    for raw_option in options:
        if not isinstance(raw_option, Mapping):
            raise _invalid_participation_error("La opcion demo no es valida.")
        option_id = str(raw_option.get("id") or "").strip()
        option_label = str(
            raw_option.get("texto") or raw_option.get("label") or ""
        ).strip()
        if not option_id or not option_label or option_id in option_ids:
            raise _invalid_participation_error("La opcion demo no es verificable.")
        option_ids.append(option_id)
        option_labels.append(option_label)

    instrument_document = {
        "contract_version": PARTICIPATION_CONTRACT_VERSION,
        "instrument_revision": INSTRUMENT_REVISION,
        "slug": normalized_slug,
        "tenant_slug": str(public_payload.get("tenant_slug") or "").strip().lower(),
        "sector": str(public_payload.get("sector") or "").strip().lower(),
        "question": {
            "id": question_id,
            "text": str(question.get("texto") or question.get("titulo") or "").strip(),
            "options": [
                {"id": option_id, "label": option_label}
                for option_id, option_label in zip(option_ids, option_labels)
            ],
        },
    }
    return DemoSurveyInstrument(
        slug=normalized_slug,
        tenant_slug=instrument_document["tenant_slug"],
        sector=instrument_document["sector"],
        question_id=question_id,
        option_ids=tuple(option_ids),
        option_labels=tuple(option_labels),
        instrument_sha256=_canonical_sha256(instrument_document),
    )


def _bounded_public_payload(payload: Mapping[str, Any]) -> None:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_participation_error(
            "La participacion demo contiene datos no validos."
        ) from exc
    if len(encoded) > MAX_PUBLIC_PAYLOAD_BYTES:
        raise _invalid_participation_error(
            "La participacion demo supera el tamano permitido."
        )


def prepare_demo_survey_participation(
    slug: str,
    payload: Mapping[str, Any],
    *,
    submission_id: str | None = None,
) -> PreparedDemoSurveyParticipation:
    """Validate and canonicalize one single-choice demo interaction."""

    if not isinstance(payload, Mapping):
        raise _invalid_participation_error("Debe enviar una participacion valida.")
    _bounded_public_payload(payload)
    try:
        resolved_submission_id = resolve_survey_submission_id(
            payload,
            header_value=submission_id,
            required=True,
        )
    except EncuestaError:
        raise
    if resolved_submission_id is None:  # pragma: no cover - required=True
        raise _invalid_participation_error("submission_id es obligatorio.")

    instrument = _resolve_instrument(slug)
    normalized_ack = build_demo_survey_response_ack(instrument.slug, dict(payload))
    answers = normalized_ack.get("answers") if isinstance(normalized_ack, Mapping) else None
    if not isinstance(answers, list) or len(answers) != 1:
        raise _invalid_participation_error(
            "Debe elegir exactamente una opcion valida."
        )
    answer = answers[0]
    if not isinstance(answer, Mapping):
        raise _invalid_participation_error("La respuesta demo no es valida.")
    question_id = str(answer.get("question_id") or "").strip()
    option_id = str(answer.get("option_id") or "").strip()
    if (
        question_id != instrument.question_id
        or option_id not in instrument.option_ids
    ):
        raise _invalid_participation_error(
            "La opcion elegida no pertenece al instrumento demo actual."
        )

    canonical = {
        "canonical_version": CANONICAL_VERSION,
        "instrument_revision": instrument.instrument_revision,
        "instrument_sha256": instrument.instrument_sha256,
        "survey_slug": instrument.slug,
        "question_id": question_id,
        "option_id": option_id,
        "response_origin": DemoSurveyParticipation.RESPONSE_ORIGIN,
    }
    submission_scope = (
        f"{RECEIPT_CONTRACT_VERSION}:{instrument.slug}:{resolved_submission_id}"
    )
    return PreparedDemoSurveyParticipation(
        instrument=instrument,
        submission_id=resolved_submission_id,
        submission_id_hash=hashlib.sha256(
            submission_scope.encode("utf-8")
        ).hexdigest(),
        payload_hash=_canonical_sha256(canonical),
        question_id=question_id,
        option_id=option_id,
    )


def _receipt_from_row(
    row: DemoSurveyParticipation,
    prepared: PreparedDemoSurveyParticipation,
    *,
    replayed: bool,
) -> DemoSurveyParticipationReceipt:
    return DemoSurveyParticipationReceipt(
        response_id=int(row.id),
        survey_slug=str(row.survey_slug),
        tenant_slug=str(row.tenant_slug),
        sector=str(row.sector),
        question_id=str(row.question_id),
        option_id=str(row.option_id),
        instrument_sha256=str(row.instrument_sha256),
        instrument_revision=int(row.instrument_revision),
        submission_id=prepared.submission_id,
        replayed=bool(replayed),
    )


def _validated_replay(
    row: DemoSurveyParticipation,
    prepared: PreparedDemoSurveyParticipation,
) -> DemoSurveyParticipationReceipt:
    if (
        row.payload_hash != prepared.payload_hash
        or row.instrument_sha256 != prepared.instrument.instrument_sha256
        or int(row.instrument_revision) != prepared.instrument.instrument_revision
        or row.question_id != prepared.question_id
        or row.option_id != prepared.option_id
    ):
        raise _submission_conflict_error()
    if (
        row.response_origin != DemoSurveyParticipation.RESPONSE_ORIGIN
        or row.tenant_slug != prepared.instrument.tenant_slug
        or row.sector != prepared.instrument.sector
    ):
        raise _receipt_corrupt_error()
    return _receipt_from_row(row, prepared, replayed=True)


def _lookup_row(
    prepared: PreparedDemoSurveyParticipation,
) -> DemoSurveyParticipation | None:
    return DemoSurveyParticipation.query.filter_by(
        survey_slug=prepared.instrument.slug,
        submission_id_hash=prepared.submission_id_hash,
    ).first()


def find_demo_survey_participation_replay(
    slug: str,
    payload: Mapping[str, Any],
    *,
    submission_id: str | None = None,
) -> DemoSurveyParticipationReceipt | None:
    """Resolve a committed replay before one-shot intake controls run."""

    _require_enabled()
    prepared = prepare_demo_survey_participation(
        slug,
        payload,
        submission_id=submission_id,
    )
    try:
        row = _lookup_row(prepared)
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise _storage_error() from exc
    if row is None:
        return None
    return _validated_replay(row, prepared)


def persist_demo_survey_participation(
    slug: str,
    payload: Mapping[str, Any],
    *,
    submission_id: str | None = None,
) -> DemoSurveyParticipationReceipt:
    """Insert one minimized receipt or return its exact committed replay."""

    gate = _require_enabled()
    prepared = prepare_demo_survey_participation(
        slug,
        payload,
        submission_id=submission_id,
    )
    try:
        existing = _lookup_row(prepared)
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise _storage_error() from exc
    if existing is not None:
        return _validated_replay(existing, prepared)

    try:
        if db.session.get_bind().dialect.name == "postgresql":
            # Serialize quota checks per demo slug. This is a transaction-level
            # lock only; it does not block unrelated surveys or real responses.
            db.session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:survey_slug, 0))"
                ),
                {"survey_slug": prepared.instrument.slug},
            )
            concurrent_existing = _lookup_row(prepared)
            if concurrent_existing is not None:
                db.session.commit()
                return _validated_replay(concurrent_existing, prepared)
        interaction_count = DemoSurveyParticipation.query.filter_by(
            survey_slug=prepared.instrument.slug
        ).count()
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise _storage_error() from exc
    interaction_limit = int(
        gate.get("max_interactions_per_survey")
        or DEFAULT_MAX_INTERACTIONS_PER_SURVEY
    )
    if interaction_count >= interaction_limit:
        db.session.rollback()
        raise _capacity_error(interaction_limit)

    row = DemoSurveyParticipation(
        survey_slug=prepared.instrument.slug,
        tenant_slug=prepared.instrument.tenant_slug,
        sector=prepared.instrument.sector,
        question_id=prepared.question_id,
        option_id=prepared.option_id,
        submission_id_hash=prepared.submission_id_hash,
        payload_hash=prepared.payload_hash,
        instrument_sha256=prepared.instrument.instrument_sha256,
        instrument_revision=prepared.instrument.instrument_revision,
        response_origin=DemoSurveyParticipation.RESPONSE_ORIGIN,
    )
    try:
        db.session.add(row)
        db.session.flush()
        receipt = _receipt_from_row(row, prepared, replayed=False)
        db.session.commit()
        return receipt
    except IntegrityError as exc:
        # A concurrent request may have committed the same unique receipt after
        # our initial lookup.  Re-read and compare the canonical payload.
        db.session.rollback()
        try:
            existing = _lookup_row(prepared)
        except SQLAlchemyError as lookup_exc:
            db.session.rollback()
            raise _storage_error() from lookup_exc
        if existing is not None:
            return _validated_replay(existing, prepared)
        raise _storage_error() from exc
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise _storage_error() from exc


def get_demo_survey_participation_aggregate(
    slug: str,
) -> dict[str, Any]:
    """Aggregate only the current immutable demo instrument revision."""

    _require_enabled()
    instrument = _resolve_instrument(slug)
    try:
        grouped = (
            db.session.query(
                DemoSurveyParticipation.instrument_sha256,
                DemoSurveyParticipation.option_id,
                func.count(DemoSurveyParticipation.id),
                func.max(DemoSurveyParticipation.id),
            )
            .filter(DemoSurveyParticipation.survey_slug == instrument.slug)
            .group_by(
                DemoSurveyParticipation.instrument_sha256,
                DemoSurveyParticipation.option_id,
            )
            .all()
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise _storage_error() from exc

    option_counts = {option_id: 0 for option_id in instrument.option_ids}
    stale_count = 0
    max_response_id = 0
    for row_instrument, raw_option_id, raw_count, raw_max_id in grouped:
        count = max(0, int(raw_count or 0))
        if row_instrument != instrument.instrument_sha256:
            stale_count += count
            continue
        option_id = str(raw_option_id or "")
        if option_id not in option_counts:
            # Current-revision rows with an unknown option violate the receipt
            # invariant. Do not silently add them to a visible choice.
            raise _receipt_corrupt_error()
        option_counts[option_id] += count
        max_response_id = max(max_response_id, int(raw_max_id or 0))

    interactive_count = sum(option_counts.values())
    return {
        "contract_version": AGGREGATE_CONTRACT_VERSION,
        "survey_slug": instrument.slug,
        "tenant_slug": instrument.tenant_slug,
        "sector": instrument.sector,
        "instrument_revision": instrument.instrument_revision,
        "instrument_sha256": instrument.instrument_sha256,
        "response_origin": DemoSurveyParticipation.RESPONSE_ORIGIN,
        "option_counts": option_counts,
        "interactive_demo_responses": interactive_count,
        "stale_instrument_responses_excluded": stale_count,
        "max_response_id": max_response_id,
        "institutional_truth": False,
        "verified_citizen_responses": 0,
    }


def _percentages_for_counts(counts: list[int]) -> list[float]:
    """Allocate exact basis points so rendered percentages sum to 100.00."""

    total = sum(max(0, int(count or 0)) for count in counts)
    if total <= 0:
        return [0.0 for _ in counts]
    raw_numerators = [max(0, int(count or 0)) * 10_000 for count in counts]
    basis_points = [value // total for value in raw_numerators]
    remainder = 10_000 - sum(basis_points)
    ranked = sorted(
        range(len(counts)),
        key=lambda index: (raw_numerators[index] % total, -index),
        reverse=True,
    )
    for index in ranked[:remainder]:
        basis_points[index] += 1
    return [round(value / 100, 2) for value in basis_points]


def _durable_demo_realtime_contract(
    slug: str,
    tenant_slug: str,
    *,
    current: Mapping[str, Any] | None = None,
    result_version: Any = None,
    snapshot_version: Any = None,
) -> dict[str, Any]:
    """Advertise the one tenant-scoped room owned by a durable demo fixture."""

    normalized_slug = str(slug or "").strip().lower()
    normalized_tenant = str(tenant_slug or "").strip().lower()
    if not normalized_slug or not normalized_tenant:
        raise ValueError("durable demo realtime scope is incomplete")
    room = f"encuesta:{normalized_tenant}:{normalized_slug}"
    payload = deepcopy(dict(current or {}))
    polling = (
        dict(payload.get("polling"))
        if isinstance(payload.get("polling"), Mapping)
        else {}
    )
    polling.update(
        {
            "enabled": True,
            "interval_ms": int(polling.get("interval_ms") or 8000),
            "fallback_after_ms": int(polling.get("fallback_after_ms") or 15000),
        }
    )
    versioning = (
        dict(payload.get("versioning"))
        if isinstance(payload.get("versioning"), Mapping)
        else {}
    )
    versioning.update(
        {
            "result_version": result_version,
            "snapshot_version": snapshot_version,
            "result_version_field": "result_version",
            "snapshot_version_field": "snapshot_version",
        }
    )
    payload.update(
        {
            "contract_version": "surveys.realtime.v2",
            "enabled": True,
            "demo_mode": True,
            "transports": ["socket.io", "polling"],
            "room": room,
            "primary_room": room,
            "rooms": [room],
            "socket": {
                "enabled": True,
                "path": "/api/socket.io",
                "join_event": "join",
                "join_payload": {"room": room},
                "join_payloads": [{"room": room}],
                "events": [
                    {
                        "name": "survey_update_v2",
                        "contract_version": "surveys.live_results.v2",
                    },
                    {
                        "name": "survey.vote.created",
                        "contract_version": "surveys.live_results.v2",
                    },
                    {"name": "survey_update", "contract_version": "legacy"},
                ],
            },
            "polling": polling,
            "versioning": versioning,
        }
    )
    return payload


def merge_demo_participation_into_live_results(
    live_results: Mapping[str, Any],
    aggregate: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay durable demo interactions onto the immutable synthetic seed."""

    payload = deepcopy(dict(live_results))
    slug = str(payload.get("slug") or "").strip().lower()
    aggregate_slug = str(aggregate.get("survey_slug") or "").strip().lower()
    if not slug or slug != aggregate_slug:
        raise ValueError("demo survey aggregate does not match live-results slug")
    tenant_slug = str(payload.get("tenant_slug") or "").strip().lower()
    aggregate_tenant = str(aggregate.get("tenant_slug") or "").strip().lower()
    if not tenant_slug or tenant_slug != aggregate_tenant:
        raise ValueError("demo survey aggregate does not match live-results tenant")

    raw_seeded = payload.get("seeded_responses", DEMO_SURVEY_RESPONSE_COUNT)
    try:
        seeded_count = max(0, int(raw_seeded))
    except (TypeError, ValueError, OverflowError):
        seeded_count = DEMO_SURVEY_RESPONSE_COUNT
    raw_option_counts = aggregate.get("option_counts")
    option_counts = raw_option_counts if isinstance(raw_option_counts, Mapping) else {}

    questions = payload.get("preguntas")
    if not isinstance(questions, Mapping) or len(questions) != 1:
        raise ValueError("demo live-results requires exactly one question")
    question_id, raw_question = next(iter(questions.items()))
    if not isinstance(raw_question, Mapping):
        raise ValueError("demo live-results question is invalid")
    question = dict(raw_question)
    raw_options = question.get("opciones")
    if not isinstance(raw_options, list) or not raw_options:
        raise ValueError("demo live-results options are invalid")

    merged_options: list[dict[str, Any]] = []
    merged_counts: list[int] = []
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping):
            raise ValueError("demo live-results option is invalid")
        option = dict(raw_option)
        option_id = str(option.get("id") or "").strip()
        try:
            seeded_votes = max(0, int(option.get("votos") or 0))
            durable_votes = max(0, int(option_counts.get(option_id, 0) or 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("demo participation count is invalid") from exc
        merged_counts.append(seeded_votes + durable_votes)
        merged_options.append(option)

    percentages = _percentages_for_counts(merged_counts)
    for option, votes, percentage in zip(
        merged_options,
        merged_counts,
        percentages,
    ):
        option["votos"] = votes
        option["porcentaje"] = percentage

    interactive_count = max(
        0,
        int(aggregate.get("interactive_demo_responses") or 0),
    )
    total_responses = seeded_count + interactive_count
    max_response_id = max(0, int(aggregate.get("max_response_id") or 0))
    snapshot_version = (
        f"demo:{slug}:seed:{seeded_count}:interactive:{interactive_count}:"
        f"receipt:{max_response_id}"
    )

    question["opciones"] = merged_options
    question["total_votos"] = sum(merged_counts)
    payload["preguntas"] = {str(question_id): question}
    payload["total_respuestas"] = total_responses
    payload["seeded_responses"] = seeded_count
    payload["interactive_demo_responses"] = interactive_count
    payload["result_version"] = total_responses
    payload["snapshot_version"] = snapshot_version
    payload["durable_demo_participation"] = True
    payload["municipal_truth"] = False

    composition = {
        "contract_version": COMPOSITION_CONTRACT_VERSION,
        "mode": "synthetic_demo_with_interactive_qa",
        "seeded_synthetic_responses": seeded_count,
        "interactive_demo_responses": interactive_count,
        "verified_citizen_responses": 0,
        "institutional_truth": False,
        "suitable_for_product_demonstration": True,
        "suitable_for_government_decisions": False,
    }
    provenance = build_survey_response_provenance(
        real_count=0,
        synthetic_count=total_responses,
        mode="synthetic",
        synthetic_excluded=0,
    )
    provenance["composition"] = composition
    payload["data_provenance"] = provenance
    payload["response_provenance"] = deepcopy(provenance)
    payload["demo_data_composition"] = composition
    payload["persistence"] = {
        "contract_version": PERSISTENCE_CONTRACT_VERSION,
        "state": "durable_preview",
        "durable": True,
        "database_write": True,
        "scope": "interactive_demo_only",
        "municipal_truth": False,
    }

    heatmap = payload.get("heatmap")
    if isinstance(heatmap, Mapping):
        heatmap_payload = dict(heatmap)
        metadata = (
            dict(heatmap_payload.get("metadata"))
            if isinstance(heatmap_payload.get("metadata"), Mapping)
            else {}
        )
        metadata.update(
            {
                "mapped_seeded_responses": seeded_count,
                "unmapped_interactive_demo_responses": interactive_count,
                "aggregate_total_responses": total_responses,
            }
        )
        heatmap_payload["metadata"] = metadata
        payload["heatmap"] = heatmap_payload

    payload["realtime"] = _durable_demo_realtime_contract(
        slug,
        tenant_slug,
        current=payload.get("realtime") if isinstance(payload.get("realtime"), Mapping) else None,
        result_version=total_responses,
        snapshot_version=snapshot_version,
    )

    return payload


def build_durable_demo_live_results_payload(
    slug: str,
    *,
    public_base_url: str = "https://www.chatboc.ar",
) -> dict[str, Any] | None:
    """Build the static demo payload plus its isolated durable Preview delta."""

    _require_enabled()
    base_payload = build_demo_live_results_payload(
        slug,
        public_base_url=public_base_url,
    )
    if base_payload is None:
        return None
    aggregate = get_demo_survey_participation_aggregate(slug)
    return merge_demo_participation_into_live_results(base_payload, aggregate)


def _non_negative_contract_count(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"demo survey {field} is invalid") from exc
    if parsed < 0:
        raise ValueError(f"demo survey {field} is invalid")
    return parsed


def _merge_durable_live_results_into_demo_item(
    item: Mapping[str, Any],
    live_results: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a durable live-results summary into one admin demo item.

    The immutable seed and the Preview-only interaction ledger remain explicit
    partitions.  Demographic and geographic segments continue to describe only
    the deterministic seed because the minimized durable receipt intentionally
    stores neither demographics nor location.
    """

    merged_item = deepcopy(dict(item))
    slug = str(merged_item.get("slug") or "").strip().lower()
    live_slug = str(live_results.get("slug") or "").strip().lower()
    if not slug or slug != live_slug:
        raise ValueError("demo survey item does not match live-results slug")
    if live_results.get("durable_demo_participation") is not True:
        raise ValueError("demo survey live-results are not durable")

    seeded_count = _non_negative_contract_count(
        live_results.get("seeded_responses"),
        field="seeded_responses",
    )
    interactive_count = _non_negative_contract_count(
        live_results.get("interactive_demo_responses"),
        field="interactive_demo_responses",
    )
    total_count = _non_negative_contract_count(
        live_results.get("total_respuestas"),
        field="total_respuestas",
    )
    if total_count != seeded_count + interactive_count:
        raise ValueError("demo survey response partitions do not equal total")

    questions = live_results.get("preguntas")
    if not isinstance(questions, Mapping) or len(questions) != 1:
        raise ValueError("demo survey live-results require one question")
    raw_question = next(iter(questions.values()))
    if not isinstance(raw_question, Mapping):
        raise ValueError("demo survey live-results question is invalid")
    raw_live_options = raw_question.get("opciones")
    if not isinstance(raw_live_options, list) or not raw_live_options:
        raise ValueError("demo survey live-results options are invalid")

    results = (
        deepcopy(dict(merged_item.get("results") or {}))
        if isinstance(merged_item.get("results"), Mapping)
        else {}
    )
    raw_result_options = results.get("options")
    if not isinstance(raw_result_options, list) or len(raw_result_options) != len(raw_live_options):
        raise ValueError("demo survey item options do not match live-results")

    merged_options: list[dict[str, Any]] = []
    for raw_result_option, raw_live_option in zip(raw_result_options, raw_live_options):
        if not isinstance(raw_result_option, Mapping) or not isinstance(raw_live_option, Mapping):
            raise ValueError("demo survey result option is invalid")
        option = deepcopy(dict(raw_result_option))
        result_label = str(option.get("label") or option.get("texto") or "").strip()
        live_label = str(raw_live_option.get("label") or raw_live_option.get("texto") or "").strip()
        if result_label and live_label and result_label != live_label:
            raise ValueError("demo survey option ordering does not match live-results")
        votes = _non_negative_contract_count(
            raw_live_option.get("votos"),
            field="option votes",
        )
        try:
            raw_percentage = float(raw_live_option.get("porcentaje") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("demo survey option percentage is invalid") from exc
        if not math.isfinite(raw_percentage):
            raise ValueError("demo survey option percentage is invalid")
        percentage = max(0.0, min(100.0, raw_percentage))
        option.update(
            {
                "label": live_label or result_label,
                "count": votes,
                "votos": votes,
                "porcentaje": percentage,
            }
        )
        merged_options.append(option)

    composition = deepcopy(dict(live_results.get("demo_data_composition") or {}))
    if _non_negative_contract_count(
        composition.get("verified_citizen_responses", 0),
        field="verified_citizen_responses",
    ) != 0:
        raise ValueError("demo survey cannot claim verified citizen responses")

    results.update(
        {
            "seeded_responses": seeded_count,
            "interactive_demo_responses": interactive_count,
            "total_respuestas": total_count,
            "verified_citizen_responses": 0,
            "options": merged_options,
            "data_provenance": deepcopy(live_results.get("data_provenance") or {}),
            "response_provenance": deepcopy(live_results.get("response_provenance") or {}),
            "demo_data_composition": composition,
            "persistence": deepcopy(live_results.get("persistence") or {}),
            "segment_scope": "seeded_synthetic_responses_only",
            "unsegmented_interactive_demo_responses": interactive_count,
        }
    )
    merged_item.update(
        {
            "results": results,
            "seeded_responses": seeded_count,
            "interactive_demo_responses": interactive_count,
            "total_respuestas": total_count,
            "verified_citizen_responses": 0,
            "durable_demo_participation": True,
            "municipal_truth": False,
            "data_provenance": deepcopy(live_results.get("data_provenance") or {}),
            "response_provenance": deepcopy(live_results.get("response_provenance") or {}),
            "demo_data_composition": composition,
            "persistence": deepcopy(live_results.get("persistence") or {}),
        }
    )

    analytics = (
        deepcopy(dict(merged_item.get("analytics_summary") or {}))
        if isinstance(merged_item.get("analytics_summary"), Mapping)
        else {}
    )
    analytics["responses"] = total_count
    analytics["seeded_responses"] = seeded_count
    analytics["interactive_demo_responses"] = interactive_count
    analytics["verified_citizen_responses"] = 0
    analytics["top_option"] = deepcopy(
        max(merged_options, key=lambda option: int(option.get("count") or 0))
    )
    merged_item["analytics_summary"] = analytics
    return merged_item


def enrich_demo_survey_voting_with_durable_participation(
    survey_voting: Mapping[str, Any],
    *,
    public_base_url: str = "https://www.chatboc.ar",
) -> dict[str, Any]:
    """Return an atomic, read-only durable projection for an admin contract.

    The caller owns fallback behavior.  This function either enriches every
    listed demo item from the same durable source used by ``live-results`` or
    raises without mutating the supplied baseline contract.
    """

    _require_enabled()
    enriched = deepcopy(dict(survey_voting))
    live_by_slug: dict[str, dict[str, Any]] = {}

    def _enrich_item(raw_item: Any) -> dict[str, Any]:
        if not isinstance(raw_item, Mapping):
            raise ValueError("demo survey contract item is invalid")
        slug = str(raw_item.get("slug") or "").strip().lower()
        if not slug:
            raise ValueError("demo survey contract item has no slug")
        if slug not in live_by_slug:
            live = build_durable_demo_live_results_payload(
                slug,
                public_base_url=public_base_url,
            )
            if live is None:
                raise ValueError("demo survey live-results are unavailable")
            live_by_slug[slug] = live
        return _merge_durable_live_results_into_demo_item(raw_item, live_by_slug[slug])

    for collection_name in ("items", "all_items"):
        raw_collection = enriched.get(collection_name)
        if raw_collection is None:
            continue
        if not isinstance(raw_collection, list):
            raise ValueError("demo survey item collection is invalid")
        enriched[collection_name] = [_enrich_item(item) for item in raw_collection]

    if not live_by_slug:
        raise ValueError("demo survey contract has no items")
    enriched["durable_demo_participation"] = True
    enriched["municipal_truth"] = False
    enriched["composition_scope"] = "per_item_partitioned"
    enriched["verified_citizen_responses"] = 0
    return enriched


def build_durable_demo_public_survey_payload(
    slug: str,
    *,
    public_base_url: str = "https://www.chatboc.ar",
) -> dict[str, Any] | None:
    """Build one public demo instrument with its durable Preview-only delta.

    The instrument remains a deterministic synthetic fixture.  Only the
    isolated QA participation ledger is mutable, and its provenance and
    persistence scope are copied to the public payload so clients cannot
    mistake the combined count for verified municipal evidence.
    """

    _require_enabled()
    payload = build_demo_public_survey_payload(
        slug,
        public_base_url=public_base_url,
    )
    if payload is None:
        return None
    live_results = build_durable_demo_live_results_payload(
        slug,
        public_base_url=public_base_url,
    )
    if live_results is None:  # pragma: no cover - same instrument resolver
        return None

    payload["resultados_envivo"] = live_results
    payload["results"] = live_results
    for key in (
        "data_provenance",
        "response_provenance",
        "demo_data_composition",
        "persistence",
        "seeded_responses",
        "interactive_demo_responses",
        "municipal_truth",
        "result_version",
        "snapshot_version",
    ):
        if key in live_results:
            payload[key] = deepcopy(live_results[key])
    payload["realtime"] = deepcopy(live_results["realtime"])
    payload["durable_demo_participation"] = True
    return payload


def publish_durable_demo_survey_participation_update(
    receipt: DemoSurveyParticipationReceipt,
    aggregate: Mapping[str, Any],
) -> bool:
    """Publish one committed demo vote to its exact tenant-scoped room.

    Replays deliberately do not republish.  Polling remains the durable
    fallback if Socket.IO publication is unavailable after the receipt commit.
    """

    if receipt.replayed:
        return False
    try:
        _require_enabled()
        aggregate_slug = str(aggregate.get("survey_slug") or "").strip().lower()
        aggregate_tenant = str(aggregate.get("tenant_slug") or "").strip().lower()
        if aggregate_slug != receipt.survey_slug or aggregate_tenant != receipt.tenant_slug:
            raise ValueError("demo survey realtime aggregate scope mismatch")

        base_payload = build_demo_live_results_payload(receipt.survey_slug)
        if base_payload is None:
            return False
        modern_payload = merge_demo_participation_into_live_results(base_payload, aggregate)
        legacy_payload = deepcopy(modern_payload)
        modern_payload["legacy_contract_version"] = modern_payload.get("contract_version")
        modern_payload["contract_version"] = "surveys.live_results.v2"
        modern_payload["legacy_results"] = legacy_payload
        modern_payload["event"] = {
            "contract_version": "surveys.realtime_effect.v2",
            "event_id": hashlib.sha256(
                f"demo-survey-response:{receipt.survey_slug}:{receipt.response_id}".encode("utf-8")
            ).hexdigest(),
            "event_name": "survey.response.committed",
            "response_id": int(receipt.response_id),
            "slug": receipt.survey_slug,
            "tenant_slug": receipt.tenant_slug,
        }

        from socket_service import emit_survey_update

        return bool(
            emit_survey_update(
                receipt.survey_slug,
                modern_payload,
                tenant_slug=receipt.tenant_slug,
            )
        )
    except Exception:
        if has_app_context():
            current_app.logger.exception(
                "Durable demo survey realtime publish failed slug=%s response_id=%s",
                receipt.survey_slug,
                receipt.response_id,
            )
        return False


def build_demo_survey_participation_ack(
    receipt: DemoSurveyParticipationReceipt,
    *,
    aggregate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the exact durable receipt contract expected by first-party UI."""

    interactive_after = None
    total_after = None
    if aggregate is not None:
        aggregate_slug = str(aggregate.get("survey_slug") or "").strip().lower()
        if aggregate_slug != receipt.survey_slug:
            raise ValueError("demo survey aggregate does not match receipt slug")
        interactive_after = max(
            0,
            int(aggregate.get("interactive_demo_responses") or 0),
        )
        total_after = DEMO_SURVEY_RESPONSE_COUNT + interactive_after

    idempotency = {
        "contract_version": RECEIPT_CONTRACT_VERSION,
        "canonical_version": CANONICAL_VERSION,
        "receipt_id": receipt.response_id,
        "submission_id": receipt.submission_id,
        "response_id": receipt.response_id,
        "instrument_revision": receipt.instrument_revision,
        "state": "committed",
        "disposition": "replayed" if receipt.replayed else "accepted",
        "persisted": True,
        "replayed": receipt.replayed,
    }
    persistence = {
        "contract_version": PERSISTENCE_CONTRACT_VERSION,
        "state": "durable_preview",
        "durable": True,
        "database_write": True,
        "scope": "interactive_demo_only",
        "municipal_truth": False,
    }
    max_response_id = (
        max(0, int(aggregate.get("max_response_id") or 0))
        if aggregate is not None
        else 0
    )
    snapshot_version = (
        f"demo:{receipt.survey_slug}:seed:{DEMO_SURVEY_RESPONSE_COUNT}:"
        f"interactive:{interactive_after}:receipt:{max_response_id}"
        if interactive_after is not None
        else None
    )
    realtime = _durable_demo_realtime_contract(
        receipt.survey_slug,
        receipt.tenant_slug,
        result_version=total_after,
        snapshot_version=snapshot_version,
    )
    payload: dict[str, Any] = {
        "contract_version": PUBLIC_RESPONSE_CONTRACT_VERSION,
        "participation_contract_version": PARTICIPATION_CONTRACT_VERSION,
        "ok": True,
        "success": True,
        "accepted": True,
        "persisted": True,
        "durable": True,
        "replayed": receipt.replayed,
        "duplicate": False,
        "demo_mode": True,
        "municipal_truth": False,
        "response_origin": DemoSurveyParticipation.RESPONSE_ORIGIN,
        "slug": receipt.survey_slug,
        "tenant_slug": receipt.tenant_slug,
        "sector": receipt.sector,
        "respuesta_id": receipt.response_id,
        "response_id": receipt.response_id,
        "instrument_revision": receipt.instrument_revision,
        "question_id": receipt.question_id,
        "option_id": receipt.option_id,
        "idempotency": idempotency,
        "persistence": persistence,
        "realtime": realtime,
        "seeded_responses_before": DEMO_SURVEY_RESPONSE_COUNT,
        "seeded_responses_after": DEMO_SURVEY_RESPONSE_COUNT,
        "interactive_demo_responses_after": interactive_after,
        "total_responses_after": total_after,
        "message": (
            "La participacion demo ya estaba guardada con el mismo recibo."
            if receipt.replayed
            else "Participacion demo guardada de forma durable en el entorno Preview."
        ),
        "data_provenance": {
            "contract_version": COMPOSITION_CONTRACT_VERSION,
            "mode": "interactive_demo",
            "verified_citizen_responses": 0,
            "institutional_truth": False,
        },
    }
    return payload


__all__ = [
    "AGGREGATE_CONTRACT_VERSION",
    "ABSOLUTE_MAX_INTERACTIONS_PER_SURVEY",
    "CANONICAL_VERSION",
    "COMPOSITION_CONTRACT_VERSION",
    "DEFAULT_MAX_INTERACTIONS_PER_SURVEY",
    "DemoSurveyInstrument",
    "DemoSurveyParticipationReceipt",
    "FEATURE_FLAG",
    "EXPECTED_BRANCH_ID_CONFIG",
    "INSTRUMENT_REVISION",
    "PARTICIPATION_CONTRACT_VERSION",
    "MAX_INTERACTIONS_CONFIG",
    "PERSISTENCE_CONTRACT_VERSION",
    "PreparedDemoSurveyParticipation",
    "RECEIPT_CONTRACT_VERSION",
    "build_demo_survey_participation_ack",
    "build_durable_demo_live_results_payload",
    "build_durable_demo_public_survey_payload",
    "demo_survey_participation_gate",
    "durable_demo_survey_participation_enabled",
    "enrich_demo_survey_voting_with_durable_participation",
    "find_demo_survey_participation_replay",
    "get_demo_survey_participation_aggregate",
    "merge_demo_participation_into_live_results",
    "persist_demo_survey_participation",
    "publish_durable_demo_survey_participation_update",
    "prepare_demo_survey_participation",
]
