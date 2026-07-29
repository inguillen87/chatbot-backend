"""Business logic for survey creation, publishing and response handling."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import secrets
import unicodedata
from collections import Counter
from datetime import datetime, timezone, timedelta
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote_plus

from flask import current_app, g, has_request_context, request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import func, or_, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload, load_only

from config import TIMEZONE_OFFSET as _CONFIG_TIMEZONE_OFFSET
from database import db
from utils.auth_helpers import user_from_token
from utils.db_utils import ensure_enc_encuesta_schema
from utils.roles import is_authorized_superadmin_user
from models import (
    EncEncuesta,
    EncPregunta,
    EncOpcion,
    EncRespuesta,
    EncRespuestaDetalle,
    EncLink,
    EncSegmento,
    EncComentario,
    PointsTransaction,
    SurveyDraftMaterialization,
    SurveyResponseReceipt,
    TenantProfile,
    User,
)
from services.user_service import get_user_profile_identity
from services.survey_refs import is_canonical_survey_logical_ref
try:
    from socket_service import emit_survey_update, emit_survey_comment
except ImportError:
    # Fallback to avoid circular import if running in restricted context,
    # though typical usage is safe.
    emit_survey_update = None
    emit_survey_comment = None

try:
    from services.analytics.ingestor import analytics_ingestor
except Exception:  # pragma: no cover - analytics optional in some contexts
    analytics_ingestor = None


_BOOTSTRAP_TENANT_ID: Optional[int] = None
_ENC_COMENTARIO_HAS_REPORT_COUNT: Optional[bool] = None
SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION = "surveys.response_receipt.v1"
SURVEY_RESPONSE_CANONICAL_VERSION = "survey-response.v1"
_SURVEY_SUBMISSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_SURVEY_SUBMISSION_ID_FIELDS = (
    "submission_id",
    "submissionId",
    "idempotency_key",
    "idempotencyKey",
)
_SURVEY_RECEIPT_EXCLUDED_FIELDS = frozenset(
    {
        *_SURVEY_SUBMISSION_ID_FIELDS,
        "turnstile_token",
        "turnstileToken",
        "cloudflare_turnstile_token",
        "cf-turnstile-response",
        "cf_turnstile_response",
        "request_id",
        "requestId",
    }
)


def _public_schedule_now() -> datetime:
    """Return now in the timezone used by public survey windows.

    SQLite strips timezone data from DateTime columns in tests and local demos.
    Storing survey windows in the same local timezone that models use for
    naive values keeps newly published surveys immediately active.
    """

    try:
        offset_hours = int(current_app.config.get("TIMEZONE_OFFSET", _CONFIG_TIMEZONE_OFFSET))
    except (RuntimeError, TypeError, ValueError):
        offset_hours = int(_CONFIG_TIMEZONE_OFFSET)
    offset_hours = max(-12, min(14, offset_hours))
    return datetime.now(timezone(timedelta(hours=offset_hours)))


def _social_comment_serializer() -> URLSafeTimedSerializer:
    secret = (
        current_app.config.get("SURVEY_SOCIAL_TOKEN_SECRET")
        or current_app.config.get("SECRET_KEY")
        or "chatboc-social-comment-secret"
    )
    salt = current_app.config.get("SURVEY_SOCIAL_TOKEN_SALT", "survey-social-comment")
    return URLSafeTimedSerializer(secret_key=secret, salt=salt)


def issue_social_comment_token(claims: Dict[str, Any]) -> str:
    payload = {
        "provider": str(claims.get("provider") or "").strip().lower(),
        "auth_user_id": str(claims.get("auth_user_id") or "").strip(),
        "auth_email": str(claims.get("auth_email") or "").strip() or None,
        "auth_first_name": str(claims.get("auth_first_name") or "").strip() or None,
        "auth_last_name": str(claims.get("auth_last_name") or "").strip() or None,
    }
    return _social_comment_serializer().dumps(payload)


def verify_social_comment_token(token: str, *, max_age_seconds: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if not token:
        return None

    ttl = max_age_seconds
    if ttl is None:
        raw_ttl = current_app.config.get("SURVEY_SOCIAL_TOKEN_TTL_SECONDS", 900)
        try:
            ttl = int(raw_ttl or 900)
        except (TypeError, ValueError):
            ttl = 900

    try:
        decoded = _social_comment_serializer().loads(token, max_age=max(60, ttl))
        return decoded if isinstance(decoded, dict) else None
    except SignatureExpired:
        return None
    except BadSignature:
        return None


def _enc_comentario_has_report_count() -> bool:
    """Return whether DB schema includes enc_comentario.report_count.

    Some deployments may run app code before the migration lands. Keep
    public survey comments endpoint resilient in that window.
    """

    global _ENC_COMENTARIO_HAS_REPORT_COUNT
    if _ENC_COMENTARIO_HAS_REPORT_COUNT is not None:
        return _ENC_COMENTARIO_HAS_REPORT_COUNT

    try:
        inspector = inspect(db.engine)
        columns = {col.get("name") for col in inspector.get_columns("enc_comentario")}
        _ENC_COMENTARIO_HAS_REPORT_COUNT = "report_count" in columns
    except Exception:
        _ENC_COMENTARIO_HAS_REPORT_COUNT = True

    return bool(_ENC_COMENTARIO_HAS_REPORT_COUNT)


class EncuestaError(Exception):
    """Base exception for survey service errors."""

    def __init__(self, message: str, status_code: int = 400, payload: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}

    def to_dict(self) -> Dict[str, Any]:
        data = {"error": self.message}
        data.update(self.payload)
        return data


def _survey_concurrency_error(
    message: str = "La encuesta esta siendo actualizada. Intenta nuevamente.",
    *,
    reason_code: str = "survey_concurrent_update",
) -> EncuestaError:
    return EncuestaError(
        message,
        status_code=409,
        payload={
            "contract_version": "surveys.structure_guard.v1",
            "reason_code": reason_code,
            "retryable": True,
            "action_hint": "reload_survey",
        },
    )


def _survey_duplicate_response_error() -> EncuestaError:
    return EncuestaError(
        "Ya registramos tu participacion",
        status_code=409,
        payload={
            "contract_version": "surveys.public_response.v2",
            "reason_code": "survey_response_duplicate",
            "retryable": False,
            "action_hint": "show_existing_participation",
        },
    )


def _survey_submission_error(
    message: str,
    *,
    reason_code: str,
    status_code: int = 400,
    action_hint: str = "check_submission_id",
) -> EncuestaError:
    return EncuestaError(
        message,
        status_code=status_code,
        payload={
            "contract_version": SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION,
            "reason_code": reason_code,
            "retryable": False,
            "action_hint": action_hint,
        },
    )


def resolve_survey_submission_id(
    payload: Optional[Mapping[str, Any]],
    *,
    header_value: Optional[Any] = None,
    required: bool = False,
) -> Optional[str]:
    """Resolve one caller-supplied idempotency key without inventing identity."""

    supplied: List[Tuple[str, str]] = []
    if header_value not in (None, ""):
        supplied.append(("Idempotency-Key", str(header_value).strip()))
    if isinstance(payload, Mapping):
        for field in _SURVEY_SUBMISSION_ID_FIELDS:
            raw_value = payload.get(field)
            if raw_value in (None, ""):
                continue
            supplied.append((field, str(raw_value).strip()))

    values = {value for _, value in supplied if value}
    if len(values) > 1:
        raise _survey_submission_error(
            "Idempotency-Key y submission_id deben coincidir",
            reason_code="survey_submission_id_mismatch",
        )
    submission_id = next(iter(values), None)
    if submission_id is None:
        if required:
            raise _survey_submission_error(
                "submission_id es obligatorio para garantizar el registro",
                reason_code="survey_submission_id_required",
            )
        return None
    if not _SURVEY_SUBMISSION_ID_PATTERN.fullmatch(submission_id):
        raise _survey_submission_error(
            "submission_id debe tener entre 8 y 128 caracteres seguros",
            reason_code="survey_submission_id_invalid",
        )
    return submission_id


def _survey_submission_conflict_error() -> EncuestaError:
    return _survey_submission_error(
        "submission_id ya fue usado con otra respuesta",
        reason_code="survey_submission_id_conflict",
        status_code=409,
        action_hint="use_original_payload_or_new_submission_id",
    )


def _is_retryable_survey_lock_error(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    sqlstate = str(
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or ""
    ).upper()
    if sqlstate in {"55P03", "40001", "40P01", "57014"}:
        return True

    sqlite_error_code = getattr(original, "sqlite_errorcode", None)
    if sqlite_error_code in {5, 6}:  # SQLITE_BUSY / SQLITE_LOCKED
        return True
    message = str(original or exc).strip().lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database is busy",
        )
    )


def _acquire_encuesta_write_guard(encuesta_id: int) -> EncEncuesta:
    """Serialize survey structure/state changes and response validation.

    ``SELECT .. FOR UPDATE`` is a no-op on SQLite.  A no-op ``UPDATE`` is not:
    PostgreSQL takes a row write lock and SQLite takes its database write lock.
    Both therefore serialize the first response with structural edits.  The
    statement deliberately uses raw SQL so SQLAlchemy does not apply the
    ``updated_at`` Python on-update default for a lock-only operation.

    Response writes use :func:`_acquire_encuesta_response_guard` instead.  That
    helper keeps this exclusive path for the first response and for SQLite,
    while already-frozen PostgreSQL instruments use a shared row lock.
    """

    try:
        result = db.session.execute(
            text(
                "UPDATE enc_encuesta "
                "SET structure_revision = structure_revision "
                "WHERE id = :encuesta_id"
            ),
            {"encuesta_id": int(encuesta_id)},
        )
    except OperationalError as exc:
        if _is_retryable_survey_lock_error(exc):
            raise _survey_concurrency_error() from exc
        # Schema drift, missing columns and unrelated database failures are
        # server errors.  Mislabeling them as a retryable 409 hides a broken
        # deployment and encourages pointless client retries.
        raise

    if result.rowcount == 0:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    return _load_encuesta_after_guard(encuesta_id)


def _load_encuesta_after_guard(encuesta_id: int) -> EncEncuesta:
    """Refresh the ORM instrument after a database lock statement wins."""

    encuesta = db.session.get(EncEncuesta, int(encuesta_id))
    if encuesta is None:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    # The identity map may contain the pre-lock lookup performed for tenant
    # authorization.  Refresh scalar state and expire the instrument so every
    # validation below observes the version that won the write guard.
    db.session.refresh(encuesta)
    db.session.expire(encuesta, ["preguntas", "links", "segmentos"])
    return encuesta


def _acquire_encuesta_response_guard(encuesta_id: int) -> EncEncuesta:
    """Protect response validation without serializing frozen PG instruments.

    ``structure_locked_at`` is irreversible.  Once it is non-null, structural
    writers can only enter through the exclusive no-op ``UPDATE`` guard and
    must reject structural changes.  PostgreSQL responders therefore acquire
    ``FOR SHARE`` on that parent row: many responders may hold it together,
    while ``UPDATE``/``DELETE`` writers wait until every response transaction
    finishes.  The conditional lock query also waits for an in-flight writer
    and returns its committed row version before validation continues.

    A null marker still takes the exclusive guard, preserving the race between
    the first response and a structural edit.  SQLite has no compatible shared
    row-lock primitive and serializes all writers at database level, so it
    deliberately retains the exclusive guard for both correctness and clear
    ``SQLITE_BUSY`` handling.  Unknown dialects fail safe the same way.
    """

    bind = db.session.get_bind()
    dialect_name = str(getattr(getattr(bind, "dialect", None), "name", "")).lower()
    if dialect_name != "postgresql":
        return _acquire_encuesta_write_guard(encuesta_id)

    try:
        result = db.session.execute(
            text(
                "SELECT id FROM enc_encuesta "
                "WHERE id = :encuesta_id "
                "AND structure_locked_at IS NOT NULL "
                "FOR SHARE"
            ),
            {"encuesta_id": int(encuesta_id)},
        )
        locked_id = result.scalar_one_or_none()
    except OperationalError as exc:
        if _is_retryable_survey_lock_error(exc):
            raise _survey_concurrency_error() from exc
        raise

    if locked_id is None:
        # Missing rows and not-yet-frozen instruments are intentionally
        # distinguished by the exclusive guard's rowcount/refresh checks.
        return _acquire_encuesta_write_guard(encuesta_id)

    return _load_encuesta_after_guard(encuesta_id)


def _ensure_locked_public_encuesta(encuesta: EncEncuesta) -> None:
    """Re-check public eligibility after waiting for the write guard."""

    if encuesta.estado != "publicada":
        raise EncuestaError(
            "La encuesta no esta activa",
            status_code=403,
            payload={"reason_code": "survey_not_published"},
        )
    if current_app.config.get("ENABLE_DEMO_MODE"):
        _ensure_demo_public_window(encuesta)
    if not encuesta.esta_activa():
        raise EncuestaError(
            "La encuesta no esta en su ventana de participacion",
            status_code=403,
            payload={"reason_code": "survey_outside_active_window"},
        )


def _survey_structure_is_locked(encuesta: EncEncuesta) -> bool:
    """Return the durable lock state, with a legacy-row safety fallback."""

    if encuesta.structure_locked_at is not None:
        return True
    return (
        db.session.query(EncRespuesta.id)
        .filter(EncRespuesta.encuesta_id == encuesta.id)
        .first()
        is not None
    )


def _structure_locked_error(encuesta: EncEncuesta, detail: str) -> EncuestaError:
    return EncuestaError(
        "No se puede modificar la estructura de una encuesta con respuestas registradas",
        status_code=409,
        payload={
            "contract_version": "surveys.structure_guard.v1",
            "reason_code": "survey_structure_locked",
            "detail": detail,
            "encuesta_id": encuesta.id,
            "structure_locked_at": (
                encuesta.structure_locked_at.isoformat()
                if encuesta.structure_locked_at is not None
                else None
            ),
        },
    )


def _expected_structure_revision(data: Mapping[str, Any]) -> Optional[int]:
    raw_revision: Any = data.get("expected_structure_revision")
    if raw_revision is None and isinstance(data.get("structure_guard"), Mapping):
        raw_revision = data["structure_guard"].get("revision")
    if raw_revision is None:
        return None
    if isinstance(raw_revision, bool):
        raise EncuestaError(
            "expected_structure_revision debe ser un entero positivo",
            status_code=400,
            payload={"reason_code": "survey_structure_revision_invalid"},
        )
    try:
        revision = int(raw_revision)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EncuestaError(
            "expected_structure_revision debe ser un entero positivo",
            status_code=400,
            payload={"reason_code": "survey_structure_revision_invalid"},
        ) from exc
    if revision <= 0 or str(raw_revision).strip() != str(revision):
        raise EncuestaError(
            "expected_structure_revision debe ser un entero positivo",
            status_code=400,
            payload={"reason_code": "survey_structure_revision_invalid"},
        )
    return revision


def _structure_revision_conflict(
    encuesta: EncEncuesta,
    expected_revision: int,
) -> EncuestaError:
    return EncuestaError(
        "La encuesta cambio desde que abriste el editor. Recargala antes de guardar.",
        status_code=409,
        payload={
            "contract_version": "surveys.structure_guard.v1",
            "reason_code": "survey_structure_revision_conflict",
            "expected_revision": expected_revision,
            "current_revision": int(encuesta.structure_revision or 1),
            "retryable": False,
            "action_hint": "reload_survey",
        },
    )


def _submitted_instrument_revision(payload: Mapping[str, Any]) -> Optional[int]:
    if "instrument_revision" not in payload:
        return None
    raw_revision = payload.get("instrument_revision")
    if isinstance(raw_revision, bool):
        raise EncuestaError(
            "instrument_revision debe ser un entero positivo",
            status_code=400,
            payload={"reason_code": "survey_instrument_revision_invalid"},
        )
    try:
        revision = int(raw_revision)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EncuestaError(
            "instrument_revision debe ser un entero positivo",
            status_code=400,
            payload={"reason_code": "survey_instrument_revision_invalid"},
        ) from exc
    if revision <= 0 or str(raw_revision).strip() != str(revision):
        raise EncuestaError(
            "instrument_revision debe ser un entero positivo",
            status_code=400,
            payload={"reason_code": "survey_instrument_revision_invalid"},
        )
    return revision


def _stale_instrument_error(
    encuesta: EncEncuesta,
    submitted_revision: int,
) -> EncuestaError:
    return EncuestaError(
        "La encuesta cambio desde que abriste el formulario. Recargala antes de responder.",
        status_code=409,
        payload={
            "contract_version": "surveys.public_response.v2",
            "reason_code": "survey_structure_changed",
            "submitted_instrument_revision": submitted_revision,
            "current_instrument_revision": int(encuesta.structure_revision or 1),
            "retryable": False,
            "action_hint": "reload_survey",
        },
    )


def _env_flag(name: str, default: bool = True) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    return normalized not in {"0", "false", "no", "off", "disabled"}


def _parse_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_text_value(value: Any, *, fallback: str = "") -> str:
    """Normalize potentially nested/structured values into a safe string.

    Frontend components should never receive dict/list objects as direct React
    children. This helper keeps comments payloads render-safe.
    """

    if value is None:
        return fallback
    if isinstance(value, str):
        text = value.strip()
        return text or fallback
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        preferred_keys = ("texto", "text", "label", "nombre", "value", "pregunta", "lider")
        for key in preferred_keys:
            candidate = _safe_text_value(value.get(key), fallback="")
            if candidate:
                return candidate
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return fallback
    if isinstance(value, (list, tuple, set)):
        parts = [_safe_text_value(item, fallback="") for item in value]
        parts = [part for part in parts if part]
        return " · ".join(parts) if parts else fallback

    return _safe_text_value(str(value), fallback=fallback)


def _current_app_logger():
    try:
        return current_app.logger
    except RuntimeError:
        return None


_BOOTSTRAP_SAMPLE_ENABLED = _env_flag("ENCUESTAS_BOOTSTRAP_SAMPLE", default=False)


_AUTO_SEED_SEGMENT_KEY = "auto_seed_demo"
_AUTO_SEED_DEFAULT_LABEL = "Emular 100 respuestas demo"


_BOOTSTRAP_CONFIG_ENV_VAR = "ENCUESTAS_BOOTSTRAP_CONFIG_PATH"
_BOOTSTRAP_CONFIG_DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "encuestas_bootstrap"
    / "templates.json"
)


def _demo_seed_runtime_allowed() -> bool:
    try:
        if bool(current_app.config.get("ENABLE_DEMO_MODE", False)):
            return True
        if bool(current_app.config.get("ALLOW_SURVEY_DEMO_SEEDING", False)):
            return True
    except RuntimeError:
        pass
    return _env_flag("ALLOW_SURVEY_DEMO_SEEDING", default=False)


def _bootstrap_config_path() -> Path:
    env_override = os.getenv(_BOOTSTRAP_CONFIG_ENV_VAR)
    if env_override:
        return Path(env_override)

    try:
        config_override = current_app.config.get(_BOOTSTRAP_CONFIG_ENV_VAR)  # type: ignore[attr-defined]
    except RuntimeError:
        config_override = None

    if config_override:
        return Path(config_override)

    return _BOOTSTRAP_CONFIG_DEFAULT_PATH


@lru_cache(maxsize=1)
def _load_bootstrap_data(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        _log = _current_app_logger()
        if _log:
            _log.warning(
                "[encuestas] No se encontró el archivo de plantillas demo en %s", path
            )
        return {"templates": [], "profiles": []}
    except json.JSONDecodeError:
        _log = _current_app_logger()
        if _log:
            _log.exception(
                "[encuestas] Error al parsear el archivo de plantillas demo %s", path
            )
        return {"templates": [], "profiles": []}

    if not isinstance(data, dict):
        return {"templates": [], "profiles": []}

    data.setdefault("templates", [])
    data.setdefault("profiles", [])
    data.setdefault("geo_catalog", {})
    return data


def _get_bootstrap_data() -> Dict[str, Any]:
    path = _bootstrap_config_path()
    return _load_bootstrap_data(str(path))


def _bootstrap_templates() -> Sequence[Dict[str, Any]]:
    templates = _get_bootstrap_data().get("templates", [])
    if not isinstance(templates, list):
        return ()
    return tuple(templates)


def _geo_catalog() -> Dict[str, Any]:
    catalog = _get_bootstrap_data().get("geo_catalog", {})
    if isinstance(catalog, dict):
        return catalog
    return {}


def _resolve_geo_metadata(
    municipality: Optional[str] = None,
    profile_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    catalog = _geo_catalog()
    candidates: List[str] = []
    if profile_key:
        candidates.append(_slugify(profile_key))
    if municipality:
        candidates.append(_slugify(municipality))
    for candidate in candidates:
        entry = catalog.get(candidate)
        if isinstance(entry, dict):
            return entry
    # Fallback: return first catalog entry if available.
    if catalog:
        first_key = next(iter(catalog))
        entry = catalog.get(first_key)
        if isinstance(entry, dict):
            return entry
    return None


def _build_location_question(
    geo_metadata: Dict[str, Any],
    municipality: str,
) -> Optional[Dict[str, Any]]:
    barrios = list(geo_metadata.get("neighborhoods") or [])
    distritos = list(geo_metadata.get("districts") or [])
    options_labels: List[str] = []
    seen: set[str] = set()

    for label in distritos + barrios:
        normalized = (label or "").strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        options_labels.append(normalized)

    if not options_labels:
        return None

    opciones: List[Dict[str, Any]] = [
        {"orden": idx + 1, "texto": label}
        for idx, label in enumerate(options_labels)
    ]

    otros_label = geo_metadata.get("otros_label") or "Otros (especificar ubicación)"
    opciones.append(
        {
            "orden": len(opciones) + 1,
            "texto": str(otros_label),
            "valor": "geo_autocomplete",
        }
    )

    question_text = (
        "¿En qué distrito o barrio de {municipio} residís?"
    ).format(municipio=municipality)

    return {
        "orden": 1,
        "tipo": "opcion_unica",
        "texto": question_text,
        "obligatoria": True,
        "opciones": opciones,
    }


def _augment_payload_with_geo(
    payload: Dict[str, Any],
    geo_metadata: Optional[Dict[str, Any]],
    municipality: str,
) -> Dict[str, Any]:
    if not geo_metadata:
        return payload

    preguntas = list(payload.get("preguntas") or [])
    has_geo_option = any(
        any((opcion.get("valor") == "geo_autocomplete") for opcion in pregunta.get("opciones", []))
        for pregunta in preguntas
    )

    if not has_geo_option:
        location_question = _build_location_question(geo_metadata, municipality)
        if location_question:
            preguntas = [location_question] + preguntas

    for index, pregunta in enumerate(preguntas, start=1):
        pregunta["orden"] = index
    payload["preguntas"] = preguntas

    geo_payload = {
        "center": geo_metadata.get("center"),
        "bounds": geo_metadata.get("bounds"),
        "coordinates": geo_metadata.get("coordinates"),
        "districts": geo_metadata.get("districts"),
        "neighborhoods": geo_metadata.get("neighborhoods"),
    }
    payload.setdefault("metadata", {})["geo"] = geo_payload
    return payload


def _pick_geo_point(
    geo_metadata: Optional[Dict[str, Any]],
    rng: random.Random,
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not geo_metadata:
        return None, None, None

    coordinates = list(geo_metadata.get("coordinates") or [])
    if coordinates:
        sample = rng.choice(coordinates)
        return sample.get("lat"), sample.get("lng"), sample.get("barrio")

    bounds = geo_metadata.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        west, south, east, north = bounds
        lat = rng.uniform(min(south, north), max(south, north))
        lng = rng.uniform(min(west, east), max(west, east))
        return lat, lng, None

    center = geo_metadata.get("center")
    if isinstance(center, (list, tuple)) and len(center) == 2:
        lat = center[0] + rng.uniform(-0.01, 0.01)
        lng = center[1] + rng.uniform(-0.01, 0.01)
        return lat, lng, None

    return None, None, None


def _build_bootstrap_payloads(
    municipality: str,
    inicio: datetime,
    fin: datetime,
    templates: Optional[Sequence[Dict[str, Any]]] = None,
    geo_metadata: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    source_templates = templates or _bootstrap_templates()
    municipality_slug = _slugify(municipality)
    geo_info = geo_metadata or _resolve_geo_metadata(municipality)
    payloads: List[Dict[str, Any]] = []
    for template in source_templates:
        template_copy = deepcopy(template)
        payload: Dict[str, Any] = {
            "titulo": _render_municipality_placeholder(
                template_copy.get("titulo", ""), municipality
            ),
            "slug": f"{template_copy.get('slug', 'encuesta')}-{municipality_slug}",
            "descripcion": _render_municipality_placeholder(
                template_copy.get("descripcion", ""), municipality
            ),
            "tipo": template_copy.get("tipo", "opinion"),
            "anonimo_permitido": bool(template_copy.get("anonimato", True)),
            "requiere_identidad": bool(
                template_copy.get("requiere_datos_contacto", False)
            ),
            "es_votacion_envivo": bool(template_copy.get("es_votacion_envivo", False)),
            "mostrar_resultados_envivo": bool(
                template_copy.get("mostrar_resultados_envivo", False)
            ),
            "permitir_comentarios": bool(template_copy.get("permitir_comentarios", False)),
            "politica_unicidad": template_copy.get("politica_unicidad", "libre"),
            "tags": list(template_copy.get("tags") or []),
            "inicio_at": inicio.isoformat(),
            "fin_at": fin.isoformat(),
        }

        auto_seed_demo = template_copy.get("auto_seed_demo")
        if isinstance(auto_seed_demo, dict):
            payload["auto_seed_demo"] = deepcopy(auto_seed_demo)

        preguntas: List[Dict[str, Any]] = []
        for pregunta_tpl in template_copy.get("preguntas", []):
            pregunta_tipo = pregunta_tpl.get("tipo", "opcion_unica")
            if pregunta_tipo == "multiple":
                pregunta_tipo = "opcion_multiple"
            pregunta: Dict[str, Any] = {
                "orden": int(pregunta_tpl.get("orden", len(preguntas) + 1)),
                "tipo": pregunta_tipo,
                "texto": _render_municipality_placeholder(
                    pregunta_tpl.get("texto", ""), municipality
                ),
                "obligatoria": bool(pregunta_tpl.get("obligatoria", False)),
            }
            if "min_selecciones" in pregunta_tpl:
                pregunta["min_selecciones"] = pregunta_tpl["min_selecciones"]
            if "max_selecciones" in pregunta_tpl:
                pregunta["max_selecciones"] = pregunta_tpl["max_selecciones"]

            if pregunta_tipo in {"opcion_unica", "opcion_multiple"}:
                opciones_payload = pregunta_tpl.get("opciones") or []
                opciones: List[Dict[str, Any]] = []
                for opcion_tpl in opciones_payload:
                    opcion: Dict[str, Any] = {
                        "orden": int(opcion_tpl.get("orden", len(opciones) + 1)),
                        "texto": _render_municipality_placeholder(
                            opcion_tpl.get("texto", ""), municipality
                        ),
                    }
                    if "valor" in opcion_tpl:
                        opcion["valor"] = opcion_tpl["valor"]
                    opciones.append(opcion)
                pregunta["opciones"] = opciones
            preguntas.append(pregunta)

        payload["preguntas"] = preguntas
        payload = _augment_payload_with_geo(payload, geo_info, municipality)
        payloads.append(payload)

    return payloads


def _render_municipality_placeholder(value: Any, municipality: str) -> Any:
    if isinstance(value, str):
        return value.replace("{{municipality}}", municipality)
    return value


def _build_junin_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("Junín", inicio, fin)


def _build_san_martin_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("San Martín", inicio, fin)


def _build_rivadavia_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("Rivadavia", inicio, fin)


def _build_mendoza_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("Mendoza", inicio, fin)


def _build_godoy_cruz_bootstrap_payload(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
    return _build_bootstrap_payloads("Godoy Cruz", inicio, fin)


def _find_template_definition(slug: str) -> Optional[Dict[str, Any]]:
    if not slug:
        return None
    for template in _bootstrap_templates():
        if template.get("slug") == slug:
            return deepcopy(template)
    return None


def list_template_catalog(
    municipality: Optional[str] = None,
    template_slugs: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    templates = _bootstrap_templates()
    if template_slugs:
        templates = _select_templates_by_slugs(templates, template_slugs)

    rendered: List[Dict[str, Any]] = []
    geo_profile_key = _slugify(municipality) if municipality else None

    for template in templates:
        template_copy = deepcopy(template)
        slug = template_copy.get("slug")

        def _render(value: Any) -> Any:
            return _render_municipality_placeholder(value, municipality) if municipality else value

        preguntas_rendered: List[Dict[str, Any]] = []
        for pregunta in template_copy.get("preguntas", []):
            opciones_rendered: List[Dict[str, Any]] = []
            for opcion in pregunta.get("opciones", []):
                opciones_rendered.append(
                    {
                        "orden": opcion.get("orden"),
                        "texto": _render(opcion.get("texto")),
                        "texto_template": opcion.get("texto"),
                        "valor": opcion.get("valor"),
                    }
                )

            preguntas_rendered.append(
                {
                    "orden": pregunta.get("orden"),
                    "tipo": pregunta.get("tipo", "opcion_unica"),
                    "texto": _render(pregunta.get("texto")),
                    "texto_template": pregunta.get("texto"),
                    "obligatoria": bool(pregunta.get("obligatoria", False)),
                    "min_selecciones": pregunta.get("min_selecciones"),
                    "max_selecciones": pregunta.get("max_selecciones"),
                    "opciones": opciones_rendered,
                }
            )

        demo_seed = {
            "label": "Emular 100 respuestas demo",
            "cantidad": 100,
            "geo_profile_key": geo_profile_key,
            "municipality_label": municipality,
        }

        rendered.append(
            {
                "slug": slug,
                "titulo": _render(template_copy.get("titulo")),
                "titulo_template": template_copy.get("titulo"),
                "descripcion": _render(template_copy.get("descripcion")),
                "descripcion_template": template_copy.get("descripcion"),
                "tipo": template_copy.get("tipo", "opinion"),
                "politica_unicidad": template_copy.get("politica_unicidad", "libre"),
                "anonimato": bool(template_copy.get("anonimato", True)),
                "requiere_datos_contacto": bool(template_copy.get("requiere_datos_contacto", False)),
                "es_votacion_envivo": bool(template_copy.get("es_votacion_envivo", False)),
                "mostrar_resultados_envivo": bool(
                    template_copy.get("mostrar_resultados_envivo", False)
                ),
                "permitir_comentarios": bool(template_copy.get("permitir_comentarios", False)),
                "tags": list(template_copy.get("tags") or []),
                "preguntas": preguntas_rendered,
                "demo_seed": demo_seed,
                "quick_actions": [
                    {
                        "key": "demo_seed",
                        "label": demo_seed["label"],
                        "cantidad": demo_seed["cantidad"],
                        "geo_profile_key": demo_seed["geo_profile_key"],
                        "municipality_label": demo_seed["municipality_label"],
                    }
                ],
            }
        )

    return rendered


def build_template_draft_from_slug(
    slug: str,
    municipality: Optional[str],
    *,
    start: Optional[Any] = None,
    end: Optional[Any] = None,
) -> Dict[str, Any]:
    template = _find_template_definition(slug)
    if not template:
        raise EncuestaError("Plantilla no encontrada", status_code=404)

    if not municipality:
        raise EncuestaError("La localidad es requerida para generar la plantilla", status_code=400)

    def _ensure_datetime(value: Optional[Any]) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            return _parse_datetime(value)
        return None

    inicio = _ensure_datetime(start) or datetime.now(timezone.utc)
    fin = _ensure_datetime(end)
    if fin is None:
        fin = inicio + timedelta(days=30)

    payloads = _build_bootstrap_payloads(municipality, inicio, fin, templates=[template])
    if not payloads:
        raise EncuestaError("No se pudo generar la plantilla solicitada", status_code=500)

    payload = payloads[0]
    geo_profile_key = _slugify(municipality)
    auto_seed_demo = {
        "enabled": True,
        "cantidad": 100,
        "label": "Emular 100 respuestas demo",
        "geo_profile_key": geo_profile_key,
        "municipality_label": municipality,
    }
    requiere_identidad = bool(payload.get("requiere_identidad", False))
    anonimato_habilitado = bool(payload.get("anonimo_permitido", True)) and not requiere_identidad

    preguntas = []
    for pregunta in payload.get("preguntas", []):
        preguntas.append(
            {
                "orden": pregunta.get("orden"),
                "tipo": "multiple" if pregunta.get("tipo") == "opcion_multiple" else pregunta.get("tipo"),
                "texto": pregunta.get("texto"),
                "obligatoria": bool(pregunta.get("obligatoria", False)),
                "min_selecciones": pregunta.get("min_selecciones"),
                "max_selecciones": pregunta.get("max_selecciones"),
                "opciones": [
                    {
                        "orden": opcion.get("orden"),
                        "texto": opcion.get("texto"),
                        "valor": opcion.get("valor"),
                    }
                    for opcion in pregunta.get("opciones", [])
                ],
            }
        )

    return {
        "slug": payload.get("slug"),
        "municipality": municipality,
        "titulo": payload.get("titulo"),
        "descripcion": payload.get("descripcion"),
        "tipo": payload.get("tipo", "opinion"),
        "inicio_at": payload.get("inicio_at"),
        "fin_at": payload.get("fin_at"),
        "politica_unicidad": payload.get("politica_unicidad", "libre"),
        "anonimato": anonimato_habilitado,
        "requiere_datos_contacto": requiere_identidad,
        "tags": payload.get("tags") or [],
        "preguntas": preguntas,
        "auto_seed_demo": auto_seed_demo,
        "quick_actions": [
            {
                "key": "demo_seed",
                "label": auto_seed_demo["label"],
                "cantidad": auto_seed_demo["cantidad"],
                "geo_profile_key": auto_seed_demo["geo_profile_key"],
                "municipality_label": auto_seed_demo["municipality_label"],
            }
        ],
    }


def _select_templates_by_slugs(
    templates: Sequence[Dict[str, Any]],
    template_slugs: Sequence[str],
) -> List[Dict[str, Any]]:
    slug_set = {slug for slug in template_slugs if slug}
    if not slug_set:
        return list(templates)
    return [template for template in templates if template.get("slug") in slug_set]


def _make_profile_builder(
    municipality: str, template_slugs: Optional[Sequence[str]] = None
) -> Callable[[datetime, datetime], List[Dict[str, Any]]]:
    def _builder(inicio: datetime, fin: datetime) -> List[Dict[str, Any]]:
        templates = _bootstrap_templates()
        if template_slugs:
            payload_templates = _select_templates_by_slugs(templates, template_slugs)
        else:
            payload_templates = list(templates)
        return _build_bootstrap_payloads(
            municipality,
            inicio,
            fin,
            templates=payload_templates,
        )

    return _builder


def _load_bootstrap_profiles() -> List[Dict[str, Any]]:
    data = _get_bootstrap_data()
    profiles: List[Dict[str, Any]] = []

    default_builders = {
        "junin": _build_junin_bootstrap_payload,
        "san_martin": _build_san_martin_bootstrap_payload,
        "rivadavia": _build_rivadavia_bootstrap_payload,
        "mendoza": _build_mendoza_bootstrap_payload,
        "godoy_cruz": _build_godoy_cruz_bootstrap_payload,
    }

    for raw_profile in data.get("profiles", []):
        if not isinstance(raw_profile, dict):
            continue

        key = raw_profile.get("key")
        municipality = raw_profile.get("municipality")
        if not key and municipality:
            key = _slugify(municipality)
        if not key:
            continue

        template_slugs = raw_profile.get("template_slugs") or []

        builder = None
        if template_slugs and municipality:
            builder = _make_profile_builder(municipality, template_slugs)
        elif template_slugs:
            builder = _make_profile_builder(key, template_slugs)
        elif key in default_builders:
            builder = default_builders[key]
        elif municipality:
            builder = _make_profile_builder(municipality)

        if builder is None:
            fallback_label = municipality or key
            builder = _make_profile_builder(fallback_label)

        profile: Dict[str, Any] = {
            "key": key,
            "tenant_env": raw_profile.get("tenant_env"),
            "fallback_tenant_id": raw_profile.get("fallback_tenant_id"),
            "keywords": tuple(raw_profile.get("keywords", [])),
            "template_slugs": tuple(template_slugs),
            "payload_builder": builder,
            "auto_publish": bool(raw_profile.get("auto_publish", True)),
            "tenant_id": raw_profile.get("tenant_id"),
            "municipality_label": municipality or key.replace("_", " ") if key else None,
            "geo_key": raw_profile.get("geo_key") or key,
        }

        profiles.append(profile)

    if not profiles:
        profiles = [
            {
                "key": "junin",
                "tenant_env": "JUNIN_ENCUESTAS_TENANT_ID",
                "fallback_tenant_id": 4,
                "keywords": ("junin",),
                "payload_builder": _build_junin_bootstrap_payload,
                "auto_publish": True,
                "tenant_id": None,
                "municipality_label": "Junín",
                "geo_key": "junin",
            }
        ]

    return profiles

_BOOTSTRAP_PROFILES: List[Dict[str, Any]] = _load_bootstrap_profiles()


def _bootstrap_skip_registry() -> set:
    """Return the in-memory registry of tenants where bootstrap must be skipped."""

    skip_registry = current_app.config.setdefault("ENCUESTAS_BOOTSTRAP_SKIP_TENANTS", set())
    return skip_registry

_PUBLIC_SLUG_ALIAS_RE = re.compile(r"^(?P<base>.+)-(?P<token>[0-9a-f]{6,})$")


def _slugify(value: str, fallback: Optional[str] = None) -> str:
    text = (value or "").strip().lower()
    if not text and fallback:
        text = fallback
    normalized = unicodedata.normalize("NFKD", text)
    cleaned = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    cleaned = cleaned.replace("/", " ")
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in cleaned)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    cleaned = cleaned.strip("-")
    return cleaned or (fallback or secrets.token_hex(6))


def _slug_exists(slug: str) -> bool:
    if not slug:
        return False
    return (
        db.session.query(EncEncuesta.id)
        .filter(EncEncuesta.slug == slug)
        .first()
        is not None
    )


def _generate_unique_slug(initial_slug: str) -> str:
    """Return a slug that is unique in ``EncEncuesta`` by appending a counter."""

    slug = initial_slug or secrets.token_hex(6)
    if not _slug_exists(slug):
        return slug

    match = re.match(r"^(?P<stem>.+?)(?:-(?P<num>\d+))?$", slug)
    stem = match.group("stem") if match else slug
    counter = int(match.group("num")) + 1 if match and match.group("num") else 2

    while True:
        candidate = f"{stem}-{counter}"
        if not _slug_exists(candidate):
            return candidate
        counter += 1


def _determine_tenant_id(user: Any) -> int:
    tenant_profile = getattr(g, "tenant_profile", None)
    if tenant_profile is not None:
        try:
            resolved_tenant_id = int(getattr(tenant_profile, "id", 0) or 0)
        except (TypeError, ValueError):
            resolved_tenant_id = 0
        if not resolved_tenant_id:
            raise EncuestaError("No se pudo determinar el tenant solicitado", status_code=403)
        if is_authorized_superadmin_user(user):
            return resolved_tenant_id

        try:
            authoritative_user_tenant_id = int(getattr(user, "tenant_id", 0) or 0)
        except (TypeError, ValueError):
            authoritative_user_tenant_id = 0
        if authoritative_user_tenant_id == resolved_tenant_id:
            return resolved_tenant_id

        # Transitional owner fallback is accepted only when no authoritative
        # user.tenant_id exists and this resolved TenantProfile points back to
        # the principal. If the legacy principal also has a tenant_slug, it
        # must agree. Legacy User.municipio_id/pyme_id values are deliberately
        # never compared with TenantProfile.id.
        user_slug = str(getattr(user, "tenant_slug", "") or "").strip().lower()
        profile_slug = str(getattr(tenant_profile, "slug", "") or "").strip().lower()
        try:
            user_id = int(getattr(user, "id", 0) or 0)
        except (TypeError, ValueError):
            user_id = 0
        owner_ids = set()
        for value in (
            getattr(tenant_profile, "municipio_id", None),
            getattr(tenant_profile, "pyme_id", None),
        ):
            try:
                owner_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        if (
            not authoritative_user_tenant_id
            and user_id
            and user_id in owner_ids
            and (not user_slug or user_slug == profile_slug)
        ):
            return resolved_tenant_id

        raise EncuestaError("No tenés permiso para este tenant", status_code=403)

    tenant_candidate = (
        getattr(user, "tenant_id", None)
        or getattr(user, "municipio_id", None)
        or getattr(user, "empresa_id", None)
        or getattr(user, "pyme_id", None)
        or getattr(user, "id", None)
    )
    if not tenant_candidate:
        raise EncuestaError("No se pudo determinar el tenant del usuario", status_code=403)
    return int(tenant_candidate)


def determine_tenant_id_for_user(user: Any) -> int:
    """Public helper used by routes to resolve tenant consistently.

    Keeping this indirection avoids route-level drift where list/create endpoints
    accidentally resolve different tenant IDs for the same authenticated user.
    """

    return _determine_tenant_id(user)


def _ensure_tenant_access(encuesta: EncEncuesta, user: Any) -> None:
    tenant_id = _determine_tenant_id(user)
    if encuesta.tenant_id == tenant_id:
        return

    raise EncuestaError("No tenés permiso para esta encuesta", status_code=403)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EncuestaError(f"Fecha inválida: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_pregunta_tipo(raw_tipo: Any) -> str:
    if raw_tipo is None:
        return "opcion_unica"

    text = str(raw_tipo).strip().lower()
    if not text:
        return "opcion_unica"

    mapping = {
        "multiple": "opcion_multiple",
        "opcion multiple": "opcion_multiple",
        "opcion_multiple": "opcion_multiple",
        "multiple_choice": "opcion_multiple",
        "multiple-choice": "opcion_multiple",
        "multiplechoice": "opcion_multiple",
        "multi_choice": "opcion_multiple",
        "multi-choice": "opcion_multiple",
        "multichoice": "opcion_multiple",
        "multi_select": "opcion_multiple",
        "multi-select": "opcion_multiple",
        "multiselect": "opcion_multiple",
        "checkbox": "opcion_multiple",
        "check": "opcion_multiple",
        "multiple answers": "opcion_multiple",
        "multi": "opcion_multiple",
        "single": "opcion_unica",
        "single_choice": "opcion_unica",
        "single-choice": "opcion_unica",
        "singlechoice": "opcion_unica",
        "radio": "opcion_unica",
        "opcion_unica": "opcion_unica",
        "texto": "abierta",
        "text": "abierta",
        "free_text": "abierta",
        "open": "abierta",
        "open_text": "abierta",
        "open-text": "abierta",
        "rating_emoji": "rating_emoji",
        "emoji_rating": "rating_emoji",
        "emoji": "rating_emoji",
    }

    return mapping.get(text, text)


def _map_pregunta_tipo_for_response(tipo: Optional[str]) -> Tuple[str, Optional[str]]:
    """Return a frontend-friendly question type along with the original value."""

    mapping = {
        "opcion_unica": "single_choice",
        "opcion_multiple": "multiple_choice",
        "abierta": "text",
    }

    original = tipo
    external = mapping.get(tipo, tipo)
    return external, original


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _normalize_option_entries(opciones: Sequence[Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for opt in opciones or []:
        if isinstance(opt, str):
            texto = opt.strip()
            if texto:
                normalized.append({"texto": texto, "orden": len(normalized) + 1})
            continue

        if not isinstance(opt, dict):
            continue

        texto = (
            opt.get("texto")
            or opt.get("label")
            or opt.get("nombre")
            or opt.get("title")
            or opt.get("value")
        )
        texto_limpio = str(texto).strip() if texto is not None else ""
        if not texto_limpio:
            continue

        opcion = dict(opt)
        opcion["_logical_ref_supplied"] = (
            "logical_ref" in opt or "option_ref" in opt
        )
        opcion["texto"] = texto_limpio
        raw_logical_ref = opcion.get("logical_ref")
        raw_option_ref = opcion.get("option_ref")
        if (
            raw_logical_ref not in (None, "")
            and raw_option_ref not in (None, "")
            and str(raw_logical_ref).strip() != str(raw_option_ref).strip()
        ):
            raise EncuestaError(
                "logical_ref y option_ref deben identificar la misma opcion",
                status_code=400,
                payload={
                    "reason_code": "survey_logical_ref_invalid",
                    "field": "option_ref",
                },
            )
        raw_ref = raw_option_ref if raw_option_ref not in (None, "") else raw_logical_ref
        opcion["logical_ref"] = _normalize_logical_ref(raw_ref, field="option_ref")
        orden = _coerce_int_or_none(opcion.get("orden"))
        if orden is None:
            orden = len(normalized) + 1
        opcion["orden"] = orden
        normalized.append(opcion)

    return normalized


_CONDITIONAL_LOGIC_KEYS = frozenset({"version", "show_if"})
_CONDITIONAL_SHOW_IF_KEYS = frozenset({"question_order", "option_order"})
_CONDITIONAL_SOURCE_TYPES = frozenset({"opcion_unica", "opcion_multiple"})
_CONDITIONAL_V2_GROUP_KEYS = frozenset({"kind", "operator", "children"})
_CONDITIONAL_V2_LEAF_KEYS = frozenset(
    {"kind", "question_ref", "option_ref"}
)
_CONDITIONAL_V2_OPERATORS = frozenset({"and", "or"})
_CONDITIONAL_V2_MAX_DEPTH = 4
_CONDITIONAL_V2_MAX_NODES = 64
_CONDITIONAL_V2_MAX_LEAVES = 32
_CONDITIONAL_V2_MAX_CHILDREN = 16


def _normalize_logical_ref(value: Any, *, field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise EncuestaError(
            f"{field} debe ser un identificador de texto",
            status_code=400,
            payload={
                "reason_code": "survey_logical_ref_invalid",
                "field": field,
            },
        )
    if not is_canonical_survey_logical_ref(value):
        raise EncuestaError(
            f"{field} debe tener hasta 160 caracteres seguros",
            status_code=400,
            payload={
                "reason_code": "survey_logical_ref_invalid",
                "field": field,
            },
        )
    return value


def _conditional_logic_error(
    detail: str,
    *,
    contract_version: str = "surveys.conditional_logic.v1",
    **context: Any,
) -> EncuestaError:
    clean_context = {key: value for key, value in context.items() if value is not None}
    return EncuestaError(
        "La logica condicional de la encuesta es invalida",
        status_code=400,
        payload={
            "contract_version": contract_version,
            "reason_code": "survey_conditional_logic_invalid",
            "action_hint": "fix_conditional_logic",
            "detail": detail,
            "context": clean_context,
            **clean_context,
        },
    )


def _strict_positive_order(
    value: Any,
    *,
    field: str,
    question_index: Optional[int] = None,
    question_order: Optional[int] = None,
    option_index: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _conditional_logic_error(
            f"{field} debe ser un entero positivo y no booleano",
            field=field,
            question_index=question_index,
            question_order=question_order,
            option_index=option_index,
            received_type=type(value).__name__,
        )
    return value


def _conditional_logic_contract_version(version: Any) -> str:
    if not isinstance(version, bool) and version == 2:
        return "surveys.conditional_logic.v2"
    return "surveys.conditional_logic.v1"


def _strict_conditional_ref(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not is_canonical_survey_logical_ref(value):
        raise _conditional_logic_error(
            f"{field} debe ser una referencia canonica segura",
            contract_version="surveys.conditional_logic.v2",
            field=field,
            received_type=type(value).__name__,
        )
    return value


def _normalize_conditional_v2_node(
    value: Any,
    *,
    field: str,
    depth: int,
    counters: Dict[str, int],
    seen_leaves: set[Tuple[str, str]],
    question_index: Optional[int],
    question_order: Optional[int],
) -> Dict[str, Any]:
    contract_version = "surveys.conditional_logic.v2"
    if depth > _CONDITIONAL_V2_MAX_DEPTH:
        raise _conditional_logic_error(
            "La expresion condicional supera la profundidad maxima",
            contract_version=contract_version,
            field=field,
            question_index=question_index,
            question_order=question_order,
            max_depth=_CONDITIONAL_V2_MAX_DEPTH,
        )
    if not isinstance(value, Mapping):
        raise _conditional_logic_error(
            f"{field} debe ser un objeto",
            contract_version=contract_version,
            field=field,
            question_index=question_index,
            question_order=question_order,
            received_type=type(value).__name__,
        )

    counters["nodes"] += 1
    if counters["nodes"] > _CONDITIONAL_V2_MAX_NODES:
        raise _conditional_logic_error(
            "La expresion condicional supera la cantidad maxima de nodos",
            contract_version=contract_version,
            field=field,
            question_index=question_index,
            question_order=question_order,
            max_nodes=_CONDITIONAL_V2_MAX_NODES,
        )

    kind = value.get("kind")
    keys = set(value.keys())
    if kind == "group":
        if keys != _CONDITIONAL_V2_GROUP_KEYS:
            raise _conditional_logic_error(
                "Un grupo v2 debe contener exactamente kind, operator y children",
                contract_version=contract_version,
                field=field,
                question_index=question_index,
                question_order=question_order,
                expected_keys=sorted(_CONDITIONAL_V2_GROUP_KEYS),
                received_keys=sorted(str(key) for key in keys),
            )
        operator = value.get("operator")
        if operator not in _CONDITIONAL_V2_OPERATORS:
            raise _conditional_logic_error(
                "operator debe ser and u or",
                contract_version=contract_version,
                field=f"{field}.operator",
                question_index=question_index,
                question_order=question_order,
                received_value=operator,
            )
        children = value.get("children")
        if not isinstance(children, list):
            raise _conditional_logic_error(
                "children debe ser una lista",
                contract_version=contract_version,
                field=f"{field}.children",
                question_index=question_index,
                question_order=question_order,
                received_type=type(children).__name__,
            )
        if not children or len(children) > _CONDITIONAL_V2_MAX_CHILDREN:
            raise _conditional_logic_error(
                "Cada grupo debe contener entre 1 y 16 hijos",
                contract_version=contract_version,
                field=f"{field}.children",
                question_index=question_index,
                question_order=question_order,
                child_count=len(children),
                max_children=_CONDITIONAL_V2_MAX_CHILDREN,
            )
        return {
            "kind": "group",
            "operator": operator,
            "children": [
                _normalize_conditional_v2_node(
                    child,
                    field=f"{field}.children[{child_index}]",
                    depth=depth + 1,
                    counters=counters,
                    seen_leaves=seen_leaves,
                    question_index=question_index,
                    question_order=question_order,
                )
                for child_index, child in enumerate(children)
            ],
        }

    if kind == "option_selected":
        if keys != _CONDITIONAL_V2_LEAF_KEYS:
            raise _conditional_logic_error(
                "Una hoja v2 debe contener exactamente kind, question_ref y option_ref",
                contract_version=contract_version,
                field=field,
                question_index=question_index,
                question_order=question_order,
                expected_keys=sorted(_CONDITIONAL_V2_LEAF_KEYS),
                received_keys=sorted(str(key) for key in keys),
            )
        question_ref = _strict_conditional_ref(
            value.get("question_ref"),
            field=f"{field}.question_ref",
        )
        option_ref = _strict_conditional_ref(
            value.get("option_ref"),
            field=f"{field}.option_ref",
        )
        leaf_key = (question_ref, option_ref)
        if leaf_key in seen_leaves:
            raise _conditional_logic_error(
                "La misma condicion option_selected no puede repetirse",
                contract_version=contract_version,
                field=field,
                question_index=question_index,
                question_order=question_order,
                source_question_ref=question_ref,
                source_option_ref=option_ref,
            )
        seen_leaves.add(leaf_key)
        counters["leaves"] += 1
        if counters["leaves"] > _CONDITIONAL_V2_MAX_LEAVES:
            raise _conditional_logic_error(
                "La expresion condicional supera la cantidad maxima de hojas",
                contract_version=contract_version,
                field=field,
                question_index=question_index,
                question_order=question_order,
                max_leaves=_CONDITIONAL_V2_MAX_LEAVES,
            )
        return {
            "kind": "option_selected",
            "question_ref": question_ref,
            "option_ref": option_ref,
        }

    raise _conditional_logic_error(
        "Cada nodo v2 debe ser group u option_selected",
        contract_version=contract_version,
        field=f"{field}.kind",
        question_index=question_index,
        question_order=question_order,
        received_value=kind,
    )


def _iter_conditional_v2_leaves(node: Mapping[str, Any]):
    stack = [node]
    while stack:
        current = stack.pop()
        if current["kind"] == "option_selected":
            yield current
            continue
        stack.extend(reversed(current["children"]))


def _iter_mandatory_conditional_v2_leaves(node: Mapping[str, Any]):
    """Yield leaves that every successful path through ``node`` must satisfy."""

    if node["kind"] == "option_selected":
        yield node
        return
    if node["operator"] != "and":
        return
    for child in node["children"]:
        yield from _iter_mandatory_conditional_v2_leaves(child)


def _iter_conditional_v2_and_groups(node: Mapping[str, Any]):
    """Yield every conjunction, including conjunctions nested below an OR."""

    if node["kind"] == "option_selected":
        return
    if node["operator"] == "and":
        yield node
    for child in node["children"]:
        yield from _iter_conditional_v2_and_groups(child)


def _normalize_conditional_logic(
    value: Any,
    *,
    question_index: Optional[int] = None,
    question_order: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _conditional_logic_error(
            "conditional_logic debe ser null o un objeto",
            question_index=question_index,
            question_order=question_order,
            field="conditional_logic",
            received_type=type(value).__name__,
        )

    version = value.get("version")
    contract_version = _conditional_logic_contract_version(version)
    keys = set(value.keys())
    if keys != _CONDITIONAL_LOGIC_KEYS:
        raise _conditional_logic_error(
            "conditional_logic debe contener exactamente version y show_if",
            contract_version=contract_version,
            question_index=question_index,
            question_order=question_order,
            field="conditional_logic",
            expected_keys=sorted(_CONDITIONAL_LOGIC_KEYS),
            received_keys=sorted(str(key) for key in keys),
        )

    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in {1, 2}
    ):
        raise _conditional_logic_error(
            "conditional_logic.version debe ser el entero 1 o 2",
            contract_version=contract_version,
            question_index=question_index,
            question_order=question_order,
            field="conditional_logic.version",
            received_type=type(version).__name__,
            received_value=version,
        )

    show_if = value.get("show_if")
    if version == 2:
        normalized_show_if = _normalize_conditional_v2_node(
            show_if,
            field="conditional_logic.show_if",
            depth=1,
            counters={"nodes": 0, "leaves": 0},
            seen_leaves=set(),
            question_index=question_index,
            question_order=question_order,
        )
        if normalized_show_if["kind"] != "group":
            raise _conditional_logic_error(
                "conditional_logic.show_if debe ser un grupo raiz",
                contract_version="surveys.conditional_logic.v2",
                field="conditional_logic.show_if",
                question_index=question_index,
                question_order=question_order,
            )
        return {"version": 2, "show_if": normalized_show_if}

    if not isinstance(show_if, Mapping):
        raise _conditional_logic_error(
            "conditional_logic.show_if debe ser un objeto",
            question_index=question_index,
            question_order=question_order,
            field="conditional_logic.show_if",
            received_type=type(show_if).__name__,
        )
    show_if_keys = set(show_if.keys())
    if show_if_keys != _CONDITIONAL_SHOW_IF_KEYS:
        raise _conditional_logic_error(
            "show_if debe contener exactamente question_order y option_order",
            question_index=question_index,
            question_order=question_order,
            field="conditional_logic.show_if",
            expected_keys=sorted(_CONDITIONAL_SHOW_IF_KEYS),
            received_keys=sorted(str(key) for key in show_if_keys),
        )

    source_question_order = _strict_positive_order(
        show_if.get("question_order"),
        field="conditional_logic.show_if.question_order",
        question_index=question_index,
        question_order=question_order,
    )
    source_option_order = _strict_positive_order(
        show_if.get("option_order"),
        field="conditional_logic.show_if.option_order",
        question_index=question_index,
        question_order=question_order,
    )
    return {
        "version": 1,
        "show_if": {
            "question_order": source_question_order,
            "option_order": source_option_order,
        },
    }


def normalize_survey_conditional_logic(
    value: Any,
    *,
    question_index: Optional[int] = None,
    question_order: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Normalize the shared survey conditional-logic contract.

    Canonical document materializers use this public boundary so AST shape,
    reference syntax, and complexity limits cannot drift from executable
    survey validation.
    """

    return _normalize_conditional_logic(
        value,
        question_index=question_index,
        question_order=question_order,
    )


def _validate_pregunta_payload(pregunta: Dict[str, Any], index: int) -> Dict[str, Any]:
    payload = dict(pregunta)
    missing: List[str] = []
    payload["_logical_ref_supplied"] = (
        "logical_ref" in pregunta or "question_ref" in pregunta
    )

    raw_logical_ref = payload.get("logical_ref")
    raw_question_ref = payload.get("question_ref")
    if (
        raw_logical_ref not in (None, "")
        and raw_question_ref not in (None, "")
        and str(raw_logical_ref).strip() != str(raw_question_ref).strip()
    ):
        raise EncuestaError(
            "logical_ref y question_ref deben identificar la misma pregunta",
            status_code=400,
            payload={
                "reason_code": "survey_logical_ref_invalid",
                "field": "question_ref",
                "question_index": index,
            },
        )
    raw_ref = raw_question_ref if raw_question_ref not in (None, "") else raw_logical_ref
    payload["logical_ref"] = _normalize_logical_ref(raw_ref, field="question_ref")

    raw_tipo = payload.get("tipo") or payload.get("type")
    if not raw_tipo:
        missing.append("tipo")
    else:
        payload["tipo"] = _normalize_pregunta_tipo(raw_tipo)

    texto = (
        payload.get("texto")
        or payload.get("titulo")
        or payload.get("title")
        or payload.get("pregunta")
    )
    if not texto:
        missing.append("texto")
    else:
        payload["texto"] = texto

    if missing:
        raise EncuestaError(
            f"Pregunta #{index + 1} incompleta: falta {', '.join(missing)}"
        )

    if payload.get("orden") is None:
        payload["orden"] = index + 1
    else:
        payload["orden"] = _strict_positive_order(
            payload.get("orden"),
            field="question.order",
            question_index=index,
        )

    if "obligatoria" not in payload and "required" in payload:
        payload["obligatoria"] = bool(payload.get("required"))

    if "opciones" not in payload and payload.get("options") is not None:
        payload["opciones"] = payload.get("options") or []

    if "min_selecciones" not in payload and payload.get("minSeleccion") is not None:
        payload["min_selecciones"] = payload.get("minSeleccion")
    if "max_selecciones" not in payload and payload.get("maxSeleccion") is not None:
        payload["max_selecciones"] = payload.get("maxSeleccion")

    tipo = _normalize_pregunta_tipo(payload.get("tipo"))
    payload["tipo"] = tipo

    payload["min_selecciones"] = _coerce_int_or_none(
        payload.get("min_selecciones")
    )
    payload["max_selecciones"] = _coerce_int_or_none(
        payload.get("max_selecciones")
    )
    min_selections = payload["min_selecciones"]
    max_selections = payload["max_selecciones"]
    if min_selections is not None and min_selections < 0:
        raise _conditional_logic_error(
            "min_selecciones no puede ser negativo",
            field="min_selecciones",
            question_index=index,
            question_order=payload["orden"],
        )
    if max_selections is not None and max_selections < 0:
        raise _conditional_logic_error(
            "max_selecciones no puede ser negativo",
            field="max_selecciones",
            question_index=index,
            question_order=payload["orden"],
        )
    if (
        min_selections is not None
        and max_selections is not None
        and min_selections > max_selections
    ):
        raise _conditional_logic_error(
            "min_selecciones no puede superar max_selecciones",
            field="selection_bounds",
            question_index=index,
            question_order=payload["orden"],
        )
    if tipo in {"opcion_unica", "rating_emoji"} and (
        (min_selections is not None and min_selections > 1)
        or (max_selections is not None and max_selections > 1)
    ):
        raise _conditional_logic_error(
            "Las preguntas de seleccion unica admiten como maximo una opcion",
            field="selection_bounds",
            question_index=index,
            question_order=payload["orden"],
            question_type=tipo,
        )

    raw_opciones = payload.get("opciones") or []
    for option_index, raw_option in enumerate(raw_opciones):
        if isinstance(raw_option, Mapping) and raw_option.get("orden") is not None:
            _strict_positive_order(
                raw_option.get("orden"),
                field="option.order",
                question_index=index,
                question_order=payload["orden"],
                option_index=option_index,
            )

    opciones = _normalize_option_entries(raw_opciones)
    payload["opciones"] = opciones
    if tipo in {"opcion_unica", "opcion_multiple", "rating_emoji"} and not opciones:
        raise EncuestaError(f"Pregunta #{index + 1} requiere opciones")

    has_conditional_logic = "conditional_logic" in payload
    has_spanish_alias = "logica_condicional" in payload
    if has_conditional_logic and has_spanish_alias:
        if payload.get("conditional_logic") != payload.get("logica_condicional"):
            raise _conditional_logic_error(
                "No se pueden enviar conditional_logic y logica_condicional con valores distintos",
                question_index=index,
                question_order=payload["orden"],
                field="conditional_logic",
            )
    raw_conditional_logic = (
        payload.get("conditional_logic")
        if has_conditional_logic
        else payload.get("logica_condicional")
    )
    payload["conditional_logic"] = _normalize_conditional_logic(
        raw_conditional_logic,
        question_index=index,
        question_order=payload["orden"],
    )
    payload.pop("logica_condicional", None)

    return payload


def _validate_instrument_payload(
    preguntas_payload: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized = [
        _validate_pregunta_payload(dict(raw_payload or {}), index)
        for index, raw_payload in enumerate(preguntas_payload or [])
    ]

    questions_by_order: Dict[int, Dict[str, Any]] = {}
    questions_by_ref: Dict[str, Dict[str, Any]] = {}
    question_refs: set[str] = set()
    for index, question in enumerate(normalized):
        order = question["orden"]
        if order in questions_by_order:
            raise _conditional_logic_error(
                "Cada pregunta debe tener un orden unico",
                field="question.order",
                question_index=index,
                question_order=order,
                duplicate_order=order,
            )
        questions_by_order[order] = question

        question_ref = question.get("logical_ref")
        if question_ref is not None:
            if question_ref in question_refs:
                raise EncuestaError(
                    "Cada question_ref debe ser unico dentro de la encuesta",
                    status_code=400,
                    payload={
                        "reason_code": "survey_logical_ref_duplicate",
                        "field": "question_ref",
                        "question_ref": question_ref,
                    },
                )
            question_refs.add(question_ref)
            questions_by_ref[question_ref] = question

        option_orders: set[int] = set()
        option_refs: set[str] = set()
        for option_index, option in enumerate(question.get("opciones") or []):
            option_order = option["orden"]
            if option_order in option_orders:
                raise _conditional_logic_error(
                    "Cada opcion de una pregunta debe tener un orden unico",
                    field="option.order",
                    question_index=index,
                    question_order=order,
                    option_index=option_index,
                    duplicate_order=option_order,
                )
            option_orders.add(option_order)
            option_ref = option.get("logical_ref")
            if option_ref is not None:
                if option_ref in option_refs:
                    raise EncuestaError(
                        "Cada option_ref debe ser unico dentro de su pregunta",
                        status_code=400,
                        payload={
                            "reason_code": "survey_logical_ref_duplicate",
                            "field": "option_ref",
                            "question_order": order,
                            "option_ref": option_ref,
                        },
                    )
                option_refs.add(option_ref)

    if not normalized:
        return normalized

    first_order = min(questions_by_order)
    first_question = questions_by_order[first_order]
    if first_question.get("conditional_logic") is not None:
        first_logic = first_question["conditional_logic"]
        raise _conditional_logic_error(
            "La primera pregunta no puede ser condicional",
            contract_version=_conditional_logic_contract_version(
                first_logic.get("version")
            ),
            field="conditional_logic",
            question_order=first_order,
        )

    for question_order, question in questions_by_order.items():
        conditional_logic = question.get("conditional_logic")
        if conditional_logic is None:
            continue
        version = conditional_logic["version"]
        contract_version = _conditional_logic_contract_version(version)
        show_if = conditional_logic["show_if"]
        if version == 1:
            source_order = show_if["question_order"]
            source_option_order = show_if["option_order"]
            source_question = questions_by_order.get(source_order)
            if source_question is None:
                raise _conditional_logic_error(
                    "La pregunta fuente de show_if no existe",
                    field="conditional_logic.show_if.question_order",
                    question_order=question_order,
                    source_question_order=source_order,
                )
            # A strict backward-only edge makes the graph acyclic by construction
            # and avoids recursion over attacker-controlled instrument sizes.
            if source_order >= question_order:
                raise _conditional_logic_error(
                    "La pregunta fuente debe aparecer antes que la pregunta condicional",
                    field="conditional_logic.show_if.question_order",
                    question_order=question_order,
                    source_question_order=source_order,
                )
            if source_question.get("tipo") not in _CONDITIONAL_SOURCE_TYPES:
                raise _conditional_logic_error(
                    "La pregunta fuente debe ser de opcion unica o multiple",
                    field="conditional_logic.show_if.question_order",
                    question_order=question_order,
                    source_question_order=source_order,
                    source_question_type=source_question.get("tipo"),
                )
            source_option_orders = {
                option["orden"] for option in source_question.get("opciones") or []
            }
            if source_option_order not in source_option_orders:
                raise _conditional_logic_error(
                    "La opcion fuente de show_if no existe",
                    field="conditional_logic.show_if.option_order",
                    question_order=question_order,
                    source_question_order=source_order,
                    source_option_order=source_option_order,
                )
            continue

        for leaf in _iter_conditional_v2_leaves(show_if):
            source_question_ref = leaf["question_ref"]
            source_option_ref = leaf["option_ref"]
            source_question = questions_by_ref.get(source_question_ref)
            if source_question is None:
                raise _conditional_logic_error(
                    "La pregunta fuente de option_selected no existe",
                    contract_version=contract_version,
                    field="conditional_logic.show_if.question_ref",
                    question_order=question_order,
                    source_question_ref=source_question_ref,
                )
            source_order = source_question["orden"]
            if source_order >= question_order:
                raise _conditional_logic_error(
                    "La pregunta fuente debe aparecer antes que la pregunta condicional",
                    contract_version=contract_version,
                    field="conditional_logic.show_if.question_ref",
                    question_order=question_order,
                    source_question_order=source_order,
                    source_question_ref=source_question_ref,
                )
            if source_question.get("tipo") not in _CONDITIONAL_SOURCE_TYPES:
                raise _conditional_logic_error(
                    "La pregunta fuente debe ser de opcion unica o multiple",
                    contract_version=contract_version,
                    field="conditional_logic.show_if.question_ref",
                    question_order=question_order,
                    source_question_order=source_order,
                    source_question_ref=source_question_ref,
                    source_question_type=source_question.get("tipo"),
                )
            source_options_by_ref = {
                option.get("logical_ref"): option
                for option in source_question.get("opciones") or []
                if option.get("logical_ref") is not None
            }
            if source_option_ref not in source_options_by_ref:
                raise _conditional_logic_error(
                    "La opcion fuente no existe o no pertenece a la pregunta indicada",
                    contract_version=contract_version,
                    field="conditional_logic.show_if.option_ref",
                    question_order=question_order,
                    source_question_order=source_order,
                    source_question_ref=source_question_ref,
                    source_option_ref=source_option_ref,
                )

        for conjunction in _iter_conditional_v2_and_groups(show_if):
            mandatory_single_choice_options: Dict[str, str] = {}
            for leaf in _iter_mandatory_conditional_v2_leaves(conjunction):
                source_question_ref = leaf["question_ref"]
                source_question = questions_by_ref[source_question_ref]
                if source_question.get("tipo") != "opcion_unica":
                    continue
                source_option_ref = leaf["option_ref"]
                previous_option_ref = mandatory_single_choice_options.get(
                    source_question_ref
                )
                if (
                    previous_option_ref is not None
                    and previous_option_ref != source_option_ref
                ):
                    raise _conditional_logic_error(
                        "AND no puede exigir opciones distintas de una pregunta de opcion unica",
                        contract_version=contract_version,
                        field="conditional_logic.show_if",
                        question_order=question_order,
                        source_question_ref=source_question_ref,
                        first_option_ref=previous_option_ref,
                        conflicting_option_ref=source_option_ref,
                    )
                mandatory_single_choice_options[source_question_ref] = (
                    source_option_ref
                )

    return normalized


def validate_survey_instrument_payload(
    preguntas_payload: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Validate a complete instrument using the executable survey rules."""

    return _validate_instrument_payload(preguntas_payload)


def _persisted_question_payload(pregunta: EncPregunta) -> Dict[str, Any]:
    return {
        "id": pregunta.id,
        "question_ref": pregunta.logical_ref,
        "orden": pregunta.orden,
        "tipo": pregunta.tipo,
        "texto": pregunta.texto,
        "obligatoria": pregunta.obligatoria,
        "min_selecciones": pregunta.min_selecciones,
        "max_selecciones": pregunta.max_selecciones,
        "conditional_logic": deepcopy(pregunta.logica_condicional),
        "opciones": [
            {
                "id": opcion.id,
                "option_ref": opcion.logical_ref,
                "orden": opcion.orden,
                "texto": opcion.texto,
                "valor": opcion.valor,
            }
            for opcion in pregunta.opciones
        ],
    }


def _validate_persisted_instrument(encuesta: EncEncuesta) -> None:
    if not any(pregunta.logica_condicional is not None for pregunta in encuesta.preguntas):
        return
    _validate_instrument_payload(
        [_persisted_question_payload(pregunta) for pregunta in encuesta.preguntas]
    )


def _normalize_identity_aliases(data: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = dict(data)
    if "anonimo_permitido" not in normalized and "anonimato" in normalized:
        normalized["anonimo_permitido"] = normalized["anonimato"]
    if "requiere_identidad" not in normalized and "requiere_datos_contacto" in normalized:
        normalized["requiere_identidad"] = normalized["requiere_datos_contacto"]
    return normalized


def _apply_common_updates(encuesta: EncEncuesta, data: Dict[str, Any]) -> None:
    if "document_ref" in data:
        incoming_document_ref = _normalize_logical_ref(
            data.get("document_ref"),
            field="document_ref",
        )
        if (
            encuesta.document_ref is not None
            and incoming_document_ref != encuesta.document_ref
        ):
            raise EncuestaError(
                "document_ref no puede cambiar ni borrarse una vez persistido",
                status_code=409,
                payload={
                    "reason_code": "survey_logical_ref_immutable",
                    "field": "document_ref",
                    "encuesta_id": encuesta.id,
                },
            )
        if encuesta.document_ref is None:
            encuesta.document_ref = incoming_document_ref
    encuesta.titulo = data.get("titulo", encuesta.titulo)
    encuesta.descripcion = data.get("descripcion", encuesta.descripcion)
    encuesta.tipo = data.get("tipo", encuesta.tipo)
    encuesta.inicio_at = _parse_datetime(data.get("inicio_at")) or encuesta.inicio_at
    encuesta.fin_at = _parse_datetime(data.get("fin_at")) or encuesta.fin_at
    if "puntos_recompensa" in data:
        encuesta.puntos_recompensa = _coerce_int_or_none(data.get("puntos_recompensa"))
    if "requiere_identidad" in data:
        encuesta.requiere_identidad = bool(data["requiere_identidad"])
    if "politica_unicidad" in data and data["politica_unicidad"]:
        encuesta.politica_unicidad = data["politica_unicidad"]
    if "anonimo_permitido" in data:
        encuesta.anonimo_permitido = bool(data["anonimo_permitido"])
    if "es_votacion_envivo" in data:
        encuesta.es_votacion_envivo = bool(data["es_votacion_envivo"])
    if "mostrar_resultados_envivo" in data:
        encuesta.mostrar_resultados_envivo = bool(data["mostrar_resultados_envivo"])
    if "permitir_comentarios" in data:
        encuesta.permitir_comentarios = bool(data["permitir_comentarios"])
    if "tags" in data:
        _sync_encuesta_tags(encuesta, data.get("tags"))


def _build_pregunta_entities(encuesta: EncEncuesta, preguntas_payload: Sequence[Dict[str, Any]]) -> List[EncPregunta]:
    preguntas: List[EncPregunta] = []
    normalized_payloads = _validate_instrument_payload(preguntas_payload)
    for idx, payload in enumerate(normalized_payloads):
        pregunta_tipo = _normalize_pregunta_tipo(payload.get("tipo", "opcion_unica"))
        pregunta = EncPregunta(
            encuesta=encuesta,
            orden=int(payload.get("orden", idx + 1)),
            logical_ref=payload.get("logical_ref"),
            tipo=pregunta_tipo,
            texto=(payload.get("texto") or "").strip(),
            obligatoria=bool(payload.get("obligatoria", False)),
            min_selecciones=_coerce_int_or_none(payload.get("min_selecciones")),
            max_selecciones=_coerce_int_or_none(payload.get("max_selecciones")),
            logica_condicional=deepcopy(payload.get("conditional_logic")),
        )
        opciones_payload = payload.get("opciones") or []
        for opt in opciones_payload:
            texto_opcion = (
                opt.get("texto")
                or opt.get("label")
                or opt.get("nombre")
                or ""
            ).strip()
            if not texto_opcion:
                raise EncuestaError(
                    f"Pregunta #{idx + 1} contiene una opción sin texto"
                )
            opcion = EncOpcion(
                pregunta=pregunta,
                orden=int(opt.get("orden", len(pregunta.opciones) + 1)),
                logical_ref=opt.get("logical_ref"),
                texto=texto_opcion,
                valor=(opt["valor"] if "valor" in opt else opt.get("value")),
            )
            pregunta.opciones.append(opcion)
        preguntas.append(pregunta)
    return preguntas


def _preserve_persisted_logical_refs(
    encuesta: EncEncuesta,
    preguntas_payload: Sequence[Dict[str, Any]],
) -> None:
    """Keep canonical refs immutable when an editable survey is rebuilt."""

    existing_questions = {
        pregunta.id: pregunta
        for pregunta in encuesta.preguntas
        if pregunta.id is not None
    }
    for payload in preguntas_payload:
        question_id = _payload_int_id(payload, "id", "pregunta_id", "question_id")
        question = existing_questions.get(question_id)
        if question is None:
            continue
        incoming_ref = payload.get("logical_ref")
        if question.logical_ref:
            if (
                payload.get("_logical_ref_supplied")
                and incoming_ref != question.logical_ref
            ):
                raise EncuestaError(
                    "question_ref no puede cambiar una vez persistido",
                    status_code=409,
                    payload={
                        "reason_code": "survey_logical_ref_immutable",
                        "field": "question_ref",
                        "pregunta_id": question.id,
                    },
                )
            payload["logical_ref"] = question.logical_ref

        existing_options = {
            option.id: option
            for option in question.opciones
            if option.id is not None
        }
        for option_payload in payload.get("opciones") or []:
            option_id = _payload_int_id(
                option_payload,
                "id",
                "opcion_id",
                "option_id",
            )
            option = existing_options.get(option_id)
            if option is None or not option.logical_ref:
                continue
            incoming_option_ref = option_payload.get("logical_ref")
            if (
                option_payload.get("_logical_ref_supplied")
                and incoming_option_ref != option.logical_ref
            ):
                raise EncuestaError(
                    "option_ref no puede cambiar una vez persistido",
                    status_code=409,
                    payload={
                        "reason_code": "survey_logical_ref_immutable",
                        "field": "option_ref",
                        "opcion_id": option.id,
                    },
                )
            option_payload["logical_ref"] = option.logical_ref


def _payload_int_id(payload: Mapping[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = payload.get(key)
        coerced = _coerce_int_or_none(value)
        if coerced is not None:
            return coerced
    return None


def _prepare_non_destructive_question_updates(
    encuesta: EncEncuesta,
    preguntas_payload: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[int, EncPregunta]]:
    """Validate text/config edits without mutating an answered survey.

    Public surveys with responses can receive full-form PUT payloads from the
    admin UI. Rebuilding the questions would break historical answer
    references, but rejecting every payload with ``preguntas`` makes normal
    saves fail. This path allows same-shape updates and rejects only structural
    mutations: new/deleted questions, type changes, or new/deleted options.
    """

    existing_questions = {pregunta.id: pregunta for pregunta in encuesta.preguntas if pregunta.id is not None}
    normalized_payloads = _validate_instrument_payload(preguntas_payload)
    seen_questions: set[int] = set()

    for payload in normalized_payloads:
        question_id = _payload_int_id(payload, "id", "pregunta_id", "question_id")
        if question_id is None or question_id not in existing_questions:
            raise EncuestaError(
                "No se puede modificar la estructura de una encuesta con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_structure_locked",
                    "detail": "La pregunta no existe en la encuesta publicada.",
                    "encuesta_id": encuesta.id,
                },
            )
        if question_id in seen_questions:
            raise EncuestaError(
                "No se puede modificar la estructura de una encuesta con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_structure_locked",
                    "detail": "La misma pregunta aparece mas de una vez en el payload.",
                    "encuesta_id": encuesta.id,
                },
            )
        seen_questions.add(question_id)

        question = existing_questions[question_id]
        if (
            payload.get("_logical_ref_supplied")
            and payload.get("logical_ref") != question.logical_ref
        ):
            raise EncuestaError(
                "No se puede modificar question_ref con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_logical_ref_immutable",
                    "field": "question_ref",
                    "pregunta_id": question_id,
                },
            )
        if _normalize_pregunta_tipo(payload.get("tipo")) != question.tipo:
            raise EncuestaError(
                "No se puede modificar el tipo de una pregunta con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_structure_locked",
                    "detail": "El tipo de pregunta no puede cambiar despues de recibir respuestas.",
                    "encuesta_id": encuesta.id,
                    "pregunta_id": question_id,
                },
            )
        if payload.get("orden") != question.orden:
            raise EncuestaError(
                "No se puede modificar el orden de una pregunta con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_structure_locked",
                    "detail": "El orden de la pregunta no puede cambiar despues de recibir respuestas.",
                    "encuesta_id": encuesta.id,
                    "pregunta_id": question_id,
                },
            )
        if payload.get("conditional_logic") != question.logica_condicional:
            raise EncuestaError(
                "No se puede modificar la logica condicional de una encuesta con respuestas registradas",
                status_code=409,
                payload={
                    "reason_code": "survey_structure_locked",
                    "detail": "La logica condicional no puede cambiar despues de recibir respuestas.",
                    "encuesta_id": encuesta.id,
                    "pregunta_id": question_id,
                },
            )

        existing_options = {opcion.id: opcion for opcion in question.opciones if opcion.id is not None}
        incoming_options = list(payload.get("opciones") or [])
        if existing_options or incoming_options:
            if len(incoming_options) != len(existing_options):
                raise EncuestaError(
                    "No se puede modificar las opciones de una encuesta con respuestas registradas",
                    status_code=409,
                    payload={
                        "reason_code": "survey_structure_locked",
                        "detail": "No se pueden agregar o quitar opciones despues de recibir respuestas.",
                        "encuesta_id": encuesta.id,
                        "pregunta_id": question_id,
                    },
                )
            seen_options: set[int] = set()
            for option_payload in incoming_options:
                option_id = _payload_int_id(option_payload, "id", "opcion_id", "option_id")
                if option_id is None or option_id not in existing_options:
                    raise EncuestaError(
                        "No se puede modificar las opciones de una encuesta con respuestas registradas",
                        status_code=409,
                        payload={
                            "reason_code": "survey_structure_locked",
                            "detail": "La opcion no existe en la encuesta publicada.",
                            "encuesta_id": encuesta.id,
                            "pregunta_id": question_id,
                        },
                    )
                if option_id in seen_options:
                    raise EncuestaError(
                        "No se puede modificar las opciones de una encuesta con respuestas registradas",
                        status_code=409,
                        payload={
                            "reason_code": "survey_structure_locked",
                            "detail": "La misma opcion aparece mas de una vez en el payload.",
                            "encuesta_id": encuesta.id,
                            "pregunta_id": question_id,
                        },
                    )
                seen_options.add(option_id)
                if option_payload.get("orden") != existing_options[option_id].orden:
                    raise EncuestaError(
                        "No se puede modificar el orden de las opciones con respuestas registradas",
                        status_code=409,
                        payload={
                            "reason_code": "survey_structure_locked",
                            "detail": "El orden de las opciones no puede cambiar despues de recibir respuestas.",
                            "encuesta_id": encuesta.id,
                            "pregunta_id": question_id,
                            "opcion_id": option_id,
                        },
                    )
                if (
                    option_payload.get("_logical_ref_supplied")
                    and option_payload.get("logical_ref")
                    != existing_options[option_id].logical_ref
                ):
                    raise EncuestaError(
                        "No se puede modificar option_ref con respuestas registradas",
                        status_code=409,
                        payload={
                            "reason_code": "survey_logical_ref_immutable",
                            "field": "option_ref",
                            "opcion_id": option_id,
                        },
                    )

    if set(existing_questions) != seen_questions:
        raise EncuestaError(
            "No se puede modificar la estructura de una encuesta con respuestas registradas",
            status_code=409,
            payload={
                "reason_code": "survey_structure_locked",
                "detail": "El payload debe conservar todas las preguntas existentes.",
                "encuesta_id": encuesta.id,
                "missing_question_ids": sorted(set(existing_questions) - seen_questions),
            },
        )


    return normalized_payloads, existing_questions


def _apply_prepared_non_destructive_question_updates(
    normalized_payloads: Sequence[Dict[str, Any]],
    existing_questions: Mapping[int, EncPregunta],
) -> None:
    for payload in normalized_payloads:
        question_id = _payload_int_id(payload, "id", "pregunta_id", "question_id")
        if question_id is None:
            continue
        question = existing_questions[question_id]
        question.texto = (payload.get("texto") or "").strip()
        question.obligatoria = bool(payload.get("obligatoria", False))
        question.min_selecciones = _coerce_int_or_none(payload.get("min_selecciones"))
        question.max_selecciones = _coerce_int_or_none(payload.get("max_selecciones"))
        question.logica_condicional = deepcopy(payload.get("conditional_logic"))

        existing_options = {opcion.id: opcion for opcion in question.opciones if opcion.id is not None}
        for option_payload in payload.get("opciones") or []:
            option_id = _payload_int_id(option_payload, "id", "opcion_id", "option_id")
            option = existing_options.get(option_id)
            if option is None:
                continue
            option.texto = (
                option_payload.get("texto")
                or option_payload.get("label")
                or option_payload.get("nombre")
                or ""
            ).strip()
            option.valor = option_payload.get("valor") or option_payload.get("value")


def _apply_non_destructive_question_updates(
    encuesta: EncEncuesta,
    preguntas_payload: Sequence[Dict[str, Any]],
) -> None:
    prepared = _prepare_non_destructive_question_updates(encuesta, preguntas_payload)
    _apply_prepared_non_destructive_question_updates(*prepared)


def _normalize_tags(tags: Optional[Sequence[Any]]) -> List[str]:
    if not tags:
        return []
    normalized: List[str] = []
    seen: set = set()
    for raw in tags:
        cleaned = _clean_str(raw, max_length=120)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    return normalized


def _sync_encuesta_tags(encuesta: EncEncuesta, tags: Optional[Sequence[Any]]) -> None:
    normalized = _normalize_tags(tags)
    target_keys = {tag.lower() for tag in normalized}
    existing: List[EncSegmento] = [seg for seg in encuesta.segmentos if seg.clave == "tag"]
    existing_lookup = {str(seg.valor or "").lower(): seg for seg in existing}

    # Remove segmentos that are no longer present.
    for seg in list(existing):
        value_key = str(seg.valor or "").lower()
        if value_key not in target_keys:
            encuesta.segmentos.remove(seg)

    for tag in normalized:
        key = tag.lower()
        existing_segment = existing_lookup.get(key)
        if existing_segment and existing_segment in encuesta.segmentos:
            if (existing_segment.valor or "") != tag:
                existing_segment.valor = tag
            continue
        encuesta.segmentos.append(EncSegmento(encuesta=encuesta, clave="tag", valor=tag))


def _guess_auto_seed_defaults(
    *,
    municipality_label: Optional[str],
    slug_hint: Optional[str],
    tenant_id: Optional[int],
) -> Tuple[Optional[str], Optional[str]]:
    geo_key: Optional[str] = None
    municipality_value: Optional[str] = (municipality_label or None)

    catalog = _geo_catalog()
    if slug_hint:
        parts = [part for part in slug_hint.split("-") if part]
        for part in reversed(parts):
            if part.isdigit():
                continue
            entry = catalog.get(part)
            if entry:
                geo_key = part
                if not municipality_value:
                    municipality_value = (
                        entry.get("municipality")
                        or entry.get("label")
                        or entry.get("name")
                    )
                break

    if geo_key is None and tenant_id is not None:
        profile = _match_bootstrap_profile(tenant_id)
        if profile:
            geo_key = profile.get("geo_key") or profile.get("key") or geo_key
            if not municipality_value:
                municipality_value = profile.get("municipality_label")

    if geo_key is None and municipality_value:
        normalized = _slugify(str(municipality_value))
        if normalized:
            geo_key = normalized

    return geo_key, municipality_value


def _normalize_auto_seed_config(
    raw_cfg: Optional[Dict[str, Any]],
    *,
    municipality_label: Optional[str],
    slug_hint: Optional[str],
    tenant_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    config = raw_cfg if isinstance(raw_cfg, dict) else None
    default_geo, default_municipality = _guess_auto_seed_defaults(
        municipality_label=municipality_label,
        slug_hint=slug_hint,
        tenant_id=tenant_id,
    )

    if config is None:
        return None

    enabled = bool(config.get("enabled", True))
    cantidad_raw = (
        config.get("cantidad")
        or config.get("count")
        or config.get("responses")
    )
    try:
        cantidad = int(cantidad_raw) if cantidad_raw is not None else 100
    except (TypeError, ValueError):
        cantidad = 100
    if cantidad < 0:
        cantidad = 0

    municipality_value = (
        config.get("municipality_label")
        or config.get("municipality")
        or config.get("city")
        or default_municipality
    )
    geo_key = (
        config.get("geo_profile_key")
        or config.get("geo_key")
        or config.get("geo")
        or default_geo
    )
    if not geo_key and municipality_value:
        geo_key = _slugify(str(municipality_value))

    normalized = {
        "enabled": enabled,
        "cantidad": cantidad,
        "geo_profile_key": geo_key,
        "municipality_label": municipality_value,
        "label": config.get("label") or _AUTO_SEED_DEFAULT_LABEL,
    }

    if not enabled or cantidad <= 0:
        return None

    return normalized


def _persist_auto_seed_config(
    encuesta: EncEncuesta, config: Optional[Dict[str, Any]]
) -> None:
    existing = [
        segmento
        for segmento in encuesta.segmentos
        if segmento.clave == _AUTO_SEED_SEGMENT_KEY
    ]
    for segmento in existing:
        encuesta.segmentos.remove(segmento)

    if not config:
        return

    serialized = json.dumps(config, ensure_ascii=False)
    encuesta.segmentos.append(
        EncSegmento(
            encuesta=encuesta,
            clave=_AUTO_SEED_SEGMENT_KEY,
            valor=serialized,
        )
    )


def _get_auto_seed_config(encuesta: EncEncuesta) -> Optional[Dict[str, Any]]:
    for segmento in encuesta.segmentos:
        if segmento.clave != _AUTO_SEED_SEGMENT_KEY:
            continue
        try:
            raw_value = json.loads(segmento.valor or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_value, dict):
            continue

        enabled = bool(raw_value.get("enabled", True))
        cantidad_raw = (
            raw_value.get("cantidad")
            or raw_value.get("count")
            or raw_value.get("responses")
        )
        try:
            cantidad = int(cantidad_raw) if cantidad_raw is not None else 100
        except (TypeError, ValueError):
            cantidad = 100

        if not enabled or cantidad <= 0:
            return None

        config = dict(raw_value)
        config["enabled"] = True
        config["cantidad"] = cantidad
        config.setdefault("label", _AUTO_SEED_DEFAULT_LABEL)
        return config

    return None


def create_encuesta(
    data: Dict[str, Any],
    user: Any,
    *,
    commit: bool = True,
) -> EncEncuesta:
    if not data:
        raise EncuestaError("Payload vacío")

    payload = _normalize_identity_aliases(deepcopy(data))
    raw_auto_seed_cfg = payload.pop("auto_seed_demo", None)
    payload.pop("quick_actions", None)
    municipality_hint = payload.get("municipality") or payload.get("municipio")

    tenant_id = _determine_tenant_id(user)
    titulo = (payload.get("titulo") or "").strip()
    if not titulo:
        raise EncuestaError("El título es requerido")

    slug_seed = payload.get("slug") or f"{tenant_id}-{titulo}"
    slug = _generate_unique_slug(_slugify(slug_seed))

    auto_seed_cfg = _normalize_auto_seed_config(
        raw_auto_seed_cfg,
        municipality_label=municipality_hint,
        slug_hint=slug_seed,
        tenant_id=tenant_id,
    )
    if auto_seed_cfg and not commit:
        raise EncuestaError(
            "auto_seed_demo no es compatible con una creacion transaccional diferida",
            status_code=422,
            payload={
                "reason_code": "survey_auto_seed_deferred_unsupported",
                "action_hint": "remove_auto_seed_demo",
            },
        )

    encuesta = EncEncuesta(
        tenant_id=tenant_id,
        document_ref=_normalize_logical_ref(
            payload.get("document_ref"),
            field="document_ref",
        ),
        slug=slug,
        titulo=titulo,
        descripcion=payload.get("descripcion"),
        tipo=payload.get("tipo", "opinion"),
        estado="borrador",
        inicio_at=_parse_datetime(payload.get("inicio_at")),
        fin_at=_parse_datetime(payload.get("fin_at")),
        requiere_identidad=bool(payload.get("requiere_identidad", False)),
        politica_unicidad=payload.get("politica_unicidad", "libre"),
        anonimo_permitido=bool(payload.get("anonimo_permitido", True)),
        es_votacion_envivo=bool(payload.get("es_votacion_envivo", False)),
        mostrar_resultados_envivo=bool(payload.get("mostrar_resultados_envivo", False)),
        permitir_comentarios=bool(payload.get("permitir_comentarios", False)),
        created_by=getattr(user, "id", None),
        puntos_recompensa=_coerce_int_or_none(payload.get("puntos_recompensa")),
    )

    preguntas_payload = payload.get("preguntas") or []
    encuesta.preguntas = _build_pregunta_entities(encuesta, preguntas_payload)
    _sync_encuesta_tags(encuesta, payload.get("tags"))
    _persist_auto_seed_config(encuesta, auto_seed_cfg)

    db.session.add(encuesta)
    try:
        db.session.flush()
        if commit:
            db.session.commit()
    except IntegrityError as exc:
        if commit:
            db.session.rollback()
        raise EncuestaError("No se pudo crear la encuesta (slug duplicado?)") from exc

    if commit:
        current_app.logger.info(
            "[encuestas] Encuesta %s creada por %s",
            encuesta.id,
            getattr(user, "id", None),
        )
    else:
        current_app.logger.debug(
            "[encuestas] Encuesta %s preparada para commit atomico por %s",
            encuesta.id,
            getattr(user, "id", None),
        )

    if auto_seed_cfg:
        seed_encuesta_respuestas_demo(
            encuesta.id,
            user,
            cantidad=auto_seed_cfg.get("cantidad", 100),
            geo_profile_key=auto_seed_cfg.get("geo_profile_key"),
            municipality_label=auto_seed_cfg.get("municipality_label"),
        )

    return encuesta


def update_encuesta(encuesta_id: int, data: Dict[str, Any], user: Any) -> EncEncuesta:
    data = _normalize_identity_aliases(data)
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(encuesta, user)

    encuesta = _acquire_encuesta_write_guard(encuesta_id)
    _ensure_tenant_access(encuesta, user)

    expected_revision = _expected_structure_revision(data)
    if (
        "preguntas" in data
        and expected_revision is not None
        and expected_revision != int(encuesta.structure_revision or 1)
    ):
        raise _structure_revision_conflict(encuesta, expected_revision)

    if encuesta.estado == "cerrada":
        raise EncuestaError(
            "La encuesta está cerrada y no se puede modificar",
            status_code=409,
        )

    if encuesta.estado not in {"borrador", "publicada"}:
        raise EncuestaError("La encuesta no se puede modificar", status_code=409)

    puede_actualizar_estructura = not _survey_structure_is_locked(encuesta)
    municipality_hint = data.get("municipality") or data.get("municipio")
    has_auto_seed_update = "auto_seed_demo" in data
    raw_auto_seed_cfg = data.get("auto_seed_demo") if has_auto_seed_update else None

    # Preflight the complete instrument and every operation that can raise an
    # EncuestaError before mutating the ORM object or flushing DELETEs.
    normalized_questions: Optional[List[Dict[str, Any]]] = None
    prepared_locked_questions: Optional[
        Tuple[List[Dict[str, Any]], Dict[int, EncPregunta]]
    ] = None
    if "preguntas" in data:
        normalized_questions = _validate_instrument_payload(data.get("preguntas") or [])
        _preserve_persisted_logical_refs(encuesta, normalized_questions)
        if not puede_actualizar_estructura:
            prepared_locked_questions = _prepare_non_destructive_question_updates(
                encuesta,
                normalized_questions,
            )

    raw_slug = None
    if "slug" in data:
        raw_slug = data.get("slug")
    elif "codigo" in data:
        raw_slug = data.get("codigo")

    candidate_slug: Optional[str] = None
    if raw_slug is not None:
        candidate_slug = _slugify(str(raw_slug))
        if not candidate_slug:
            raise EncuestaError("El slug no puede quedar vacío", status_code=400)
        if candidate_slug != encuesta.slug:
            exists = (
                db.session.query(EncEncuesta.id)
                .filter(EncEncuesta.slug == candidate_slug, EncEncuesta.id != encuesta.id)
                .first()
            )
            if exists:
                raise EncuestaError("Ya existe una encuesta con ese slug", status_code=409)

    # Parse date values now so an invalid later field cannot leave title or
    # description dirty when a direct caller catches and commits the session.
    if data.get("inicio_at"):
        _parse_datetime(data.get("inicio_at"))
    if data.get("fin_at"):
        _parse_datetime(data.get("fin_at"))

    prepared_auto_seed_config: Optional[Dict[str, Any]] = None
    if has_auto_seed_update:
        prepared_auto_seed_config = _normalize_auto_seed_config(
            raw_auto_seed_cfg,
            municipality_label=municipality_hint,
            slug_hint=candidate_slug or encuesta.slug,
            tenant_id=encuesta.tenant_id,
        )

    if candidate_slug is not None:
        encuesta.slug = candidate_slug

    _apply_common_updates(encuesta, data)

    if normalized_questions is not None:
        if not puede_actualizar_estructura:
            assert prepared_locked_questions is not None
            _apply_prepared_non_destructive_question_updates(*prepared_locked_questions)
        else:
            encuesta.structure_revision = int(encuesta.structure_revision or 1) + 1
            encuesta.preguntas.clear()
            db.session.flush()
            nuevas_preguntas = _build_pregunta_entities(encuesta, normalized_questions)
            encuesta.preguntas.extend(nuevas_preguntas)

    if has_auto_seed_update:
        _persist_auto_seed_config(encuesta, prepared_auto_seed_config)

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("Error al actualizar la encuesta") from exc

    current_app.logger.info("[encuestas] Encuesta %s actualizada por %s", encuesta.id, getattr(user, "id", None))
    return encuesta


def duplicate_encuesta(encuesta_id: int, data: Optional[Dict[str, Any]], user: Any) -> EncEncuesta:
    """Create an editable draft copy without touching the published source.

    Published surveys with responses can only receive non-destructive edits.
    When the admin needs to add/remove questions or options, the professional
    flow is to create a new draft version and keep historical answers anchored
    to the original survey.
    """

    source = db.session.get(EncEncuesta, encuesta_id)
    if not source:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(source, user)
    source = _acquire_encuesta_write_guard(encuesta_id)
    _ensure_tenant_access(source, user)
    _validate_persisted_instrument(source)

    payload = data or {}
    requested_title = _clean_str(payload.get("titulo") or payload.get("title"), max_length=255)
    title = requested_title or f"{source.titulo} (nueva version)"
    requested_slug = payload.get("slug") or payload.get("codigo") or f"{source.slug}-nueva-version"
    slug = _generate_unique_slug(_slugify(str(requested_slug)))

    cloned = EncEncuesta(
        tenant_id=source.tenant_id,
        slug=slug,
        titulo=title,
        descripcion=source.descripcion,
        tipo=source.tipo,
        estado="borrador",
        puntos_recompensa=source.puntos_recompensa,
        inicio_at=None,
        fin_at=source.fin_at,
        requiere_identidad=source.requiere_identidad,
        politica_unicidad=source.politica_unicidad,
        anonimo_permitido=source.anonimo_permitido,
        es_votacion_envivo=source.es_votacion_envivo,
        mostrar_resultados_envivo=source.mostrar_resultados_envivo,
        permitir_comentarios=source.permitir_comentarios,
        created_by=getattr(user, "id", None),
    )

    for question in source.preguntas:
        cloned_question = EncPregunta(
            orden=question.orden,
            logical_ref=question.logical_ref,
            tipo=question.tipo,
            texto=question.texto,
            obligatoria=question.obligatoria,
            min_selecciones=question.min_selecciones,
            max_selecciones=question.max_selecciones,
            logica_condicional=deepcopy(question.logica_condicional),
        )
        for option in question.opciones:
            cloned_question.opciones.append(
                EncOpcion(
                    orden=option.orden,
                    logical_ref=option.logical_ref,
                    texto=option.texto,
                    valor=option.valor,
                )
            )
        cloned.preguntas.append(cloned_question)

    _sync_encuesta_tags(cloned, _collect_encuesta_tags(source))
    db.session.add(cloned)

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo duplicar la encuesta", status_code=409) from exc

    current_app.logger.info(
        "[encuestas] Encuesta %s duplicada como %s por %s",
        source.id,
        cloned.id,
        getattr(user, "id", None),
    )
    return cloned


def _ensure_publication_window(encuesta: EncEncuesta) -> None:
    if encuesta.inicio_at and encuesta.fin_at and encuesta.inicio_at > encuesta.fin_at:
        raise EncuestaError("La fecha de inicio no puede ser posterior a la de cierre")


def publicar_encuesta(encuesta_id: int, user: Any) -> Tuple[EncEncuesta, EncLink]:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(encuesta, user)
    encuesta = _acquire_encuesta_write_guard(encuesta_id)
    _ensure_tenant_access(encuesta, user)
    if encuesta.estado not in {"borrador", "publicada"}:
        raise EncuestaError("La encuesta no se puede publicar", status_code=409)
    if not encuesta.preguntas:
        raise EncuestaError("La encuesta debe tener preguntas para publicarse")

    _validate_persisted_instrument(encuesta)
    _ensure_publication_window(encuesta)

    encuesta.estado = "publicada"
    if not encuesta.inicio_at:
        encuesta.inicio_at = _public_schedule_now()

    link = _resolve_current_public_link(encuesta)
    if link is None:
        link = EncLink(
            encuesta_id=encuesta.id,
            slug_publico=encuesta.slug,
            canal="web",
        )
        db.session.add(link)
    slug_publico = link.slug_publico

    auto_seed_cfg = _get_auto_seed_config(encuesta)
    auto_seed_params: Optional[Dict[str, Any]] = None
    if auto_seed_cfg and auto_seed_cfg.get("enabled", True):
        try:
            cantidad_int = int(auto_seed_cfg.get("cantidad", 0))
        except (TypeError, ValueError):
            cantidad_int = 0
        if cantidad_int > 0 and encuesta.respuestas.count() == 0:
            auto_seed_params = {
                "cantidad": cantidad_int,
                "geo_profile_key": auto_seed_cfg.get("geo_profile_key"),
                "municipality_label": auto_seed_cfg.get("municipality_label"),
            }

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo publicar la encuesta") from exc

    current_app.logger.info(
        "[encuestas] Encuesta %s publicada con slug %s por %s",
        encuesta.id,
        link.slug_publico,
        getattr(user, "id", None),
    )

    if auto_seed_params and _demo_seed_runtime_allowed():
        try:
            seed_encuesta_respuestas_demo(
                encuesta.id,
                user,
                cantidad=auto_seed_params["cantidad"],
                geo_profile_key=auto_seed_params.get("geo_profile_key"),
                municipality_label=auto_seed_params.get("municipality_label"),
            )
            current_app.logger.info(
                "[encuestas] Respuestas demo generadas automáticamente al publicar encuesta %s",
                encuesta.id,
            )
        except EncuestaError:
            current_app.logger.exception(
                "[encuestas] Error al generar respuestas demo para la encuesta %s tras publicarla",
                encuesta.id,
            )
    elif auto_seed_params:
        current_app.logger.info(
            "[encuestas] Auto seed demo omitido para encuesta %s: modo demo deshabilitado",
            encuesta.id,
        )

    return encuesta, link


def cerrar_encuesta(encuesta_id: int, user: Any) -> EncEncuesta:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    _ensure_tenant_access(encuesta, user)
    encuesta = _acquire_encuesta_write_guard(encuesta_id)
    _ensure_tenant_access(encuesta, user)
    encuesta.estado = "cerrada"
    encuesta.fin_at = encuesta.fin_at or _public_schedule_now()
    db.session.commit()
    current_app.logger.info("[encuestas] Encuesta %s cerrada por %s", encuesta.id, getattr(user, "id", None))
    return encuesta


def delete_encuesta(encuesta_id: int, user: Any) -> None:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    _ensure_tenant_access(encuesta, user)
    encuesta = _acquire_encuesta_write_guard(encuesta_id)
    _ensure_tenant_access(encuesta, user)
    tenant_id = encuesta.tenant_id

    materialization = SurveyDraftMaterialization.query.filter_by(
        tenant_id=tenant_id,
        survey_id=encuesta.id,
    ).first()
    if materialization is not None:
        raise EncuestaError(
            "Una encuesta materializada no puede eliminarse porque conserva un recibo auditable",
            status_code=409,
            payload={
                "contract_version": "surveys.materialization.v1",
                "reason_code": "survey_materialization_delete_blocked",
                "retryable": False,
                "action_hint": "archive_or_close_survey",
                "survey_id": encuesta.id,
                "draft_id": materialization.draft_id,
                "draft_revision": materialization.draft_revision,
            },
        )

    db.session.delete(encuesta)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise EncuestaError("No se pudo eliminar la encuesta") from exc

    _bootstrap_skip_registry().add(tenant_id)
    current_app.logger.info(
        "[encuestas] Encuesta %s eliminada por %s", encuesta_id, getattr(user, "id", None)
    )


def _resolve_profile_tenant_id(profile: Dict[str, Any]) -> Optional[int]:
    tenant_id = profile.get("tenant_id")
    if tenant_id:
        return int(tenant_id)

    env_name = profile.get("tenant_env")
    if env_name:
        env_value = _parse_int(os.getenv(env_name))
        if env_value:
            profile["tenant_id"] = env_value
            return env_value

    fallback = profile.get("fallback_tenant_id")
    if fallback:
        profile["tenant_id"] = fallback
        return fallback

    return None


def _get_bootstrap_user_for_profile(profile: Dict[str, Any], tenant_id: int) -> Optional[User]:
    candidate_tenant = _resolve_profile_tenant_id(profile) or tenant_id

    base_query = User.query.filter(User.tipo_chat == "municipio")
    if candidate_tenant:
        user = (
            base_query.filter(
                or_(
                    User.municipio_id == candidate_tenant,
                    User.id == candidate_tenant,
                )
            )
            .order_by(User.id.asc())
            .first()
        )
        if user:
            profile["tenant_id"] = _determine_tenant_id(user)
            return user

    keywords: Sequence[str] = profile.get("keywords") or ()
    if not keywords:
        return None

    like_filters = []
    for keyword in keywords:
        like = f"%{keyword}%"
        like_filters.extend(
            [
                User.nombre_empresa.ilike(like),
                User.name.ilike(like),
                User.email.ilike(like),
                User.ciudad.ilike(like),
            ]
        )

    keyword_query = User.query.filter(User.tipo_chat == "municipio")
    if tenant_id:
        keyword_query = keyword_query.filter(
            or_(User.municipio_id == tenant_id, User.id == tenant_id)
        )
    if like_filters:
        keyword_query = keyword_query.filter(or_(*like_filters))

    user = keyword_query.order_by(User.id.asc()).first()
    if user:
        profile["tenant_id"] = _determine_tenant_id(user)
        return user

    return None


def _match_bootstrap_profile(tenant_id: int) -> Optional[Dict[str, Any]]:
    for profile in _BOOTSTRAP_PROFILES:
        resolved = _resolve_profile_tenant_id(profile)
        if resolved is not None and resolved == tenant_id:
            return profile

    for profile in _BOOTSTRAP_PROFILES:
        if profile.get("tenant_id") is not None:
            continue
        user = _get_bootstrap_user_for_profile(profile, tenant_id)
        if user and _determine_tenant_id(user) == tenant_id:
            return profile
    return None


def _resolve_geo_metadata_for_tenant(tenant_id: int) -> Optional[Dict[str, Any]]:
    profile = _match_bootstrap_profile(tenant_id)
    if profile:
        return _resolve_geo_metadata(
            municipality=profile.get("municipality_label"),
            profile_key=profile.get("geo_key") or profile.get("key"),
        )
    return None


def _bootstrap_sample_if_needed(tenant_id: int) -> None:
    # Safety net if migrations lag: ensure the reward column exists to avoid 500s
    ensure_enc_encuesta_schema(db.session)

    if not (_BOOTSTRAP_SAMPLE_ENABLED or _demo_seed_runtime_allowed()):
        return

    profile = _match_bootstrap_profile(tenant_id)
    if not profile:
        return

    if tenant_id in _bootstrap_skip_registry():
        return

    existing = EncEncuesta.query.filter_by(tenant_id=tenant_id).count()
    if existing:
        return

    user = _get_bootstrap_user_for_profile(profile, tenant_id)
    if not user:
        current_app.logger.warning(
            "[encuestas] No se encontró un usuario municipal de %s para crear la encuesta demo",
            profile.get("key", "desconocido"),
        )
        _bootstrap_skip_registry().add(tenant_id)
        return

    inicio = datetime.now(timezone.utc)
    fin = inicio + timedelta(days=45)
    payload_builder: Callable[[datetime, datetime], Sequence[Dict[str, Any]]] = profile[
        "payload_builder"
    ]
    raw_payloads = payload_builder(inicio, fin)
    if isinstance(raw_payloads, dict):
        payloads = [raw_payloads]
    else:
        payloads = list(raw_payloads)

    created = 0
    for payload in payloads:
        try:
            encuesta = create_encuesta(payload, user)
        except EncuestaError:
            current_app.logger.exception(
                "[encuestas] No se pudo crear la encuesta demo de %s",
                profile.get("key"),
            )
            continue

        if profile.get("auto_publish", True):
            try:
                encuesta, link = publicar_encuesta(encuesta.id, user)
            except EncuestaError:
                current_app.logger.exception(
                    "[encuestas] No se pudo publicar la encuesta demo de %s",
                    profile.get("key"),
                )
                continue
            current_app.logger.info(
                "[encuestas] Encuesta demo de %s publicada automáticamente con slug %s",
                profile.get("key"),
                link.slug_publico,
            )
        else:
            current_app.logger.info(
                "[encuestas] Encuesta demo de %s creada automáticamente con id %s",
                profile.get("key"),
                encuesta.id,
            )
        created += 1

    if not created:
        _bootstrap_skip_registry().add(tenant_id)


def _prepare_template_payloads(
    raw_payloads: Sequence[Dict[str, Any]] | Dict[str, Any],
    municipio_label: str,
    geo_metadata: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if isinstance(raw_payloads, dict):
        payloads = [deepcopy(raw_payloads)]
    else:
        payloads = [deepcopy(item) for item in raw_payloads]

    if geo_metadata:
        for idx, payload in enumerate(payloads):
            payloads[idx] = _augment_payload_with_geo(payload, geo_metadata, municipio_label)

    return payloads


def _collect_all_template_payloads(
    inicio: datetime, fin: datetime
) -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()

    for profile in _BOOTSTRAP_PROFILES:
        key = profile.get("key")
        municipio_label = (
            profile.get("municipality_label")
            or (key.replace("_", " ") if isinstance(key, str) else None)
            or "Tu municipio"
        )
        builder: Callable[[datetime, datetime], Sequence[Dict[str, Any]]] = profile["payload_builder"]
        raw_payloads = builder(inicio, fin)
        geo_metadata = _resolve_geo_metadata(
            municipality=profile.get("municipality_label"),
            profile_key=profile.get("geo_key") or key,
        )
        payloads = _prepare_template_payloads(raw_payloads, municipio_label, geo_metadata)
        catalog.append(
            {
                "key": key,
                "municipality": municipio_label,
                "templates": payloads,
                "geo": geo_metadata,
            }
        )
        if key:
            seen_keys.add(key)

    if "generic" not in seen_keys:
        generic_geo = _resolve_geo_metadata(municipality="Tu municipio")
        generic_payloads = _prepare_template_payloads(
            _build_bootstrap_payloads("Tu municipio", inicio, fin, geo_metadata=generic_geo),
            "Tu municipio",
            generic_geo,
        )
        catalog.append(
            {
                "key": "generic",
                "municipality": "Tu municipio",
                "templates": generic_payloads,
                "geo": generic_geo,
            }
        )

    return catalog


def list_template_payloads(tenant_id: int, scope: Optional[str] = None) -> Dict[str, Any]:
    inicio = datetime.now(timezone.utc)
    fin = inicio + timedelta(days=45)
    profile = _match_bootstrap_profile(tenant_id)
    geo_metadata = _resolve_geo_metadata_for_tenant(tenant_id)

    if profile:
        builder: Callable[[datetime, datetime], Sequence[Dict[str, Any]]] = profile["payload_builder"]
        raw_payloads = builder(inicio, fin)
        municipio_label = profile.get("municipality_label") or profile.get("key") or "Tu municipio"
    else:
        municipio_label = "Tu municipio"
        raw_payloads = _build_bootstrap_payloads(
            municipio_label,
            inicio,
            fin,
            geo_metadata=geo_metadata,
        )

    payloads = _prepare_template_payloads(raw_payloads, municipio_label, geo_metadata)

    response: Dict[str, Any] = {
        "templates": payloads,
        "municipality": municipio_label,
        "geo": geo_metadata,
    }

    scope_key = (scope or "").strip().lower()
    if scope_key in {"all", "todos", "todas", "full"}:
        response["all_templates"] = _collect_all_template_payloads(inicio, fin)

    return response


def _resolve_public_slug(encuesta: EncEncuesta) -> Optional[str]:
    slug_publico = None
    for link in sorted(encuesta.links, key=lambda link: (link.id or 0), reverse=True):
        if link.slug_publico:
            slug_publico = link.slug_publico
            break

    if not slug_publico and encuesta.estado == "publicada":
        slug_publico = encuesta.slug

    return slug_publico


def _public_slug_matches_current_base(slug_publico: Optional[str], encuesta_slug: Optional[str]) -> bool:
    if not slug_publico or not encuesta_slug:
        return False
    normalized_public = str(slug_publico).strip().lower()
    normalized_base = str(encuesta_slug).strip().lower()
    return normalized_public == normalized_base or normalized_public.startswith(f"{normalized_base}-")


def _resolve_current_public_link(encuesta: EncEncuesta) -> Optional[EncLink]:
    """Return the canonical public link for the survey without creating churn.

    Older deployments created a new random public slug on every publish. The
    admin UI then kept showing an older slug while the publish endpoint returned
    a different one. Keep the latest link when it still belongs to the current
    base slug; otherwise prefer another matching link before creating a new one.
    """

    links = sorted(encuesta.links, key=lambda link: (link.id or 0), reverse=True)
    if not links:
        return None

    latest = next((link for link in links if link.slug_publico), None)
    if latest and _public_slug_matches_current_base(latest.slug_publico, encuesta.slug):
        return latest

    for link in links:
        if _public_slug_matches_current_base(link.slug_publico, encuesta.slug):
            return link

    return latest


def _public_url_for_slug(slug_publico: Optional[str]) -> Optional[str]:
    if not slug_publico:
        return None

    base_url: Optional[str] = None
    try:
        configured = (
            current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
            or current_app.config.get("PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL")
            or current_app.config.get("FRONTEND_URL")
            or current_app.config.get("PUBLIC_FRONTEND_URL")
        )
        if isinstance(configured, str) and configured.strip():
            base_url = configured.rstrip("/")
    except RuntimeError:
        base_url = None

    if not base_url:
        return f"/e/{slug_publico}"

    return f"{base_url}/e/{slug_publico}"


def _public_api_endpoint_for_slug(slug_publico: Optional[str]) -> Optional[str]:
    if not slug_publico:
        return None
    return f"/api/public/encuestas/v1/{slug_publico}"


def list_encuestas(tenant_id: int, estado: Optional[str] = None) -> List[EncEncuesta]:
    _bootstrap_sample_if_needed(tenant_id)
    query = (
        EncEncuesta.query.options(
            joinedload(EncEncuesta.links),
            joinedload(EncEncuesta.segmentos),
        )
        .filter_by(tenant_id=tenant_id)
    )
    if estado:
        query = query.filter_by(estado=estado)
    return query.order_by(EncEncuesta.created_at.desc()).all()


def _public_encuestas_list_options() -> List[Any]:
    options: List[Any] = [joinedload(EncEncuesta.links)]
    try:
        options.append(
            load_only(
                EncEncuesta.id,
                EncEncuesta.tenant_id,
                EncEncuesta.slug,
                EncEncuesta.titulo,
                EncEncuesta.descripcion,
                EncEncuesta.tipo,
                EncEncuesta.estado,
                EncEncuesta.inicio_at,
                EncEncuesta.fin_at,
                EncEncuesta.es_votacion_envivo,
                EncEncuesta.mostrar_resultados_envivo,
                EncEncuesta.permitir_comentarios,
                EncEncuesta.created_at,
                EncEncuesta.updated_at,
            )
        )
    except (AttributeError, TypeError):
        pass
    return [option for option in options if option is not None]


def list_public_encuestas_for_tenant(
    tenant_id: int,
    limit: int = 10,
    offset: int = 0,
) -> List[Tuple[EncEncuesta, str]]:
    """Return active public surveys for a tenant along with their public slugs."""

    try:
        requested_limit = int(limit or 10)
    except (TypeError, ValueError):
        requested_limit = 10
    safe_limit = requested_limit if requested_limit > 0 else 10
    safe_limit = min(safe_limit, 25)
    try:
        requested_offset = int(offset or 0)
    except (TypeError, ValueError):
        requested_offset = 0
    safe_offset = max(0, requested_offset)
    wanted_count = safe_limit + safe_offset
    candidate_limit = max(wanted_count * 4, 25)
    query = (
        EncEncuesta.query.options(*_public_encuestas_list_options())
        .filter(EncEncuesta.tenant_id == tenant_id)
        .filter(EncEncuesta.estado == "publicada")
        .order_by(
            EncEncuesta.created_at.desc(),
            EncEncuesta.id.desc(),
        )
        .limit(candidate_limit)
    )

    encuestas = query.all()
    resultados: List[Tuple[EncEncuesta, str]] = []

    for encuesta in encuestas:
        if not encuesta.esta_activa():
            continue
        slug_publico = _resolve_public_slug(encuesta)
        if not slug_publico:
            continue
        resultados.append((encuesta, slug_publico))
        if len(resultados) >= wanted_count:
            break

    return resultados[safe_offset : safe_offset + safe_limit]


def get_encuesta(encuesta_id: int, tenant_id: Optional[int] = None, user: Any = None) -> EncEncuesta:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    if tenant_id and encuesta.tenant_id != tenant_id:
        raise EncuestaError("Encuesta fuera del tenant", status_code=403)
    if user is not None:
        _ensure_tenant_access(encuesta, user)
    return encuesta






def _is_bootstrap_demo_survey(encuesta: EncEncuesta) -> bool:
    """Return True when the survey can be identified as bootstrap demo content."""

    if not encuesta:
        return False

    templates = _bootstrap_templates()
    if not templates:
        return False

    titulo = (getattr(encuesta, "titulo", "") or "").strip().lower()
    descripcion = (getattr(encuesta, "descripcion", "") or "").strip().lower()

    for template in templates:
        if not isinstance(template, dict):
            continue
        template_title = str(template.get("titulo") or "").strip().lower()
        template_desc = str(template.get("descripcion") or "").strip().lower()

        if template_title and titulo == template_title:
            return True
        if template_title and template_title in titulo:
            return True
        if template_desc and descripcion and template_desc == descripcion:
            return True

    return False

def _ensure_demo_public_window(encuesta: EncEncuesta) -> None:
    """Keep bootstrap demo surveys publicly accessible when their window expired."""

    if not encuesta or encuesta.estado != "publicada":
        return

    profile = _match_bootstrap_profile(getattr(encuesta, "tenant_id", None) or 0)
    if not profile:
        return
    if not _is_bootstrap_demo_survey(encuesta):
        return

    now = _public_schedule_now()
    local_tz = now.tzinfo or timezone.utc
    inicio_at = encuesta.inicio_at
    if inicio_at and inicio_at.tzinfo is None:
        inicio_at = inicio_at.replace(tzinfo=local_tz)
    elif inicio_at:
        inicio_at = inicio_at.astimezone(local_tz)

    fin_at = encuesta.fin_at
    if fin_at and fin_at.tzinfo is None:
        fin_at = fin_at.replace(tzinfo=local_tz)
    elif fin_at:
        fin_at = fin_at.astimezone(local_tz)

    should_update = False
    if inicio_at and inicio_at > now:
        encuesta.inicio_at = now - timedelta(minutes=5)
        should_update = True

    if fin_at and fin_at < now:
        encuesta.fin_at = now + timedelta(days=365)
        should_update = True

    if should_update:
        try:
            db.session.add(encuesta)
            db.session.commit()
            current_app.logger.info(
                "[encuestas] Refreshed public window for bootstrap demo survey %s", encuesta.id
            )
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "[encuestas] Failed to refresh public window for bootstrap demo survey %s",
                getattr(encuesta, "id", None),
            )

def get_public_encuesta(
    slug_publico: str,
    *,
    allow_inactive_for_user: Optional[Any] = None,
    preferred_tenant_id: Optional[int] = None,
    allow_inactive_for_receipt_lookup: bool = False,
) -> EncEncuesta:
    normalized_slug = (slug_publico or "").strip().lower()
    if not normalized_slug:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    preferred_tenant: Optional[int] = None
    if preferred_tenant_id is not None:
        try:
            preferred_tenant = int(preferred_tenant_id)
        except (TypeError, ValueError):
            preferred_tenant = None

    def _pick_best_candidate(items: Sequence[Optional[EncEncuesta]]) -> Optional[EncEncuesta]:
        """Prefer currently active public surveys when multiple rows share a slug."""
        normalized_items = [candidate for candidate in items if candidate is not None]
        if not normalized_items:
            return None

        tenant_filtered = normalized_items
        if preferred_tenant is not None:
            matches = [
                candidate for candidate in normalized_items if int(candidate.tenant_id or 0) == preferred_tenant
            ]
            if matches:
                tenant_filtered = matches

        fallback: Optional[EncEncuesta] = tenant_filtered[0] if tenant_filtered else None
        for candidate in tenant_filtered:
            if candidate.estado == "publicada" and candidate.esta_activa():
                return candidate
        return fallback

    encuesta: Optional[EncEncuesta] = None
    matching_links = (
        EncLink.query.filter(func.lower(EncLink.slug_publico) == normalized_slug)
        .order_by(EncLink.id.desc())
        .all()
    )
    if matching_links:
        encuesta = _pick_best_candidate([link.encuesta for link in matching_links])
    else:
        slug_matches = (
            EncEncuesta.query.filter(func.lower(EncEncuesta.slug) == normalized_slug)
            .order_by(EncEncuesta.id.desc())
            .all()
        )
        encuesta = _pick_best_candidate(slug_matches)
        if encuesta is None:
            alias_match = _PUBLIC_SLUG_ALIAS_RE.match(normalized_slug)
            if alias_match:
                base_slug = alias_match.group("base")
                base_slug_matches = (
                    EncEncuesta.query.filter(func.lower(EncEncuesta.slug) == base_slug)
                    .order_by(EncEncuesta.id.desc())
                    .all()
                )
                encuesta = _pick_best_candidate(base_slug_matches)

    if encuesta is None and re.fullmatch(r"[0-9a-z]{5,12}", normalized_slug):
        short_link = (
            EncLink.query.filter(
                EncLink.slug_publico.ilike(f"%-{normalized_slug}")
            )
            .order_by(EncLink.id.desc())
            .first()
        )
        if short_link:
            encuesta = short_link.encuesta

    if encuesta is None:
        raise EncuestaError("Encuesta no encontrada", status_code=404)

    preview_user = allow_inactive_for_user
    if preview_user is not None:
        try:
            _ensure_tenant_access(encuesta, preview_user)
        except EncuestaError:
            preview_user = None
        else:
            return encuesta

    # Exactly-once recovery must remain possible after the participation window
    # closes.  This bypass is deliberately internal and only used by the receipt
    # lookup, which first proves that the caller's tenant-scoped submission key
    # already has a committed receipt.  It never authorizes a new response.
    if allow_inactive_for_receipt_lookup:
        return encuesta

    if encuesta.estado != "publicada":
        raise EncuestaError(
            "La encuesta no está activa",
            status_code=403,
            payload={"reason_code": "survey_not_published"},
        )

    if current_app.config.get("ENABLE_DEMO_MODE"):
        _ensure_demo_public_window(encuesta)
    if not encuesta.esta_activa():
        raise EncuestaError(
            "La encuesta no está en su ventana de participación",
            status_code=403,
            payload={"reason_code": "survey_outside_active_window"},
        )
    return encuesta


def build_unique_fingerprint(
    encuesta: EncEncuesta,
    tenant_id: int,
    dni: Optional[str] = None,
    phone: Optional[str] = None,
    user_id: Optional[Any] = None,
    ip: Optional[str] = None,
    anon_cookie: Optional[str] = None,
) -> Optional[str]:
    policy = str(encuesta.politica_unicidad or "libre").strip().lower()
    if policy == "libre":
        return None

    def _clean_identifier(value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
        else:
            cleaned = str(value).strip()
        return cleaned or None

    dni_clean = _clean_identifier(dni)
    phone_clean = _clean_identifier(phone)
    user_id_clean = _clean_identifier(user_id)
    cookie_clean = _clean_identifier(anon_cookie)
    ip_clean = _clean_identifier(ip)

    source_parts: List[str] = [
        f"encuesta:{encuesta.id}",
        f"tenant:{tenant_id}",
        f"policy:{policy}",
    ]

    appended = False

    def _append(tag: str, value: Optional[str]):
        nonlocal appended
        if value is None:
            return
        source_parts.append(f"{tag}:{value}")
        appended = True

    if policy in {"por_dni", "dni"}:
        _append("dni", dni_clean)
    elif policy in {"por_phone", "phone"}:
        _append("phone", phone_clean)
    elif policy in {"por_cookie", "cookie"}:
        _append("cookie", cookie_clean)
    elif policy in {"por_ip", "ip"}:
        _append("ip", ip_clean)
    elif policy in {"por_dni_o_phone", "dni_o_phone"}:
        _append("dni", dni_clean)
        _append("phone", phone_clean)
    elif policy in {"por_usuario", "usuario", "user_id", "por_user_id"}:
        _append("user_id", user_id_clean)
    else:
        # fallback usa todo lo disponible
        _append("user_id", user_id_clean)
        _append("dni", dni_clean)
        _append("phone", phone_clean)
        _append("cookie", cookie_clean)
        _append("ip", ip_clean)

    if not appended:
        # No contamos con la información necesaria para construir una huella estable.
        return None

    canonical = "|".join(source_parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_AUTHENTICATED_USER_POLICIES = {"por_usuario", "usuario", "user_id", "por_user_id"}


def resolve_optional_survey_bearer_user(
    authorization_header: Optional[str],
    *,
    contract_version: str = "surveys.public_response.v2",
) -> Optional[User]:
    authorization = str(authorization_header or "").strip()
    if not authorization:
        return None

    scheme, separator, raw_token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    if not separator or not raw_token.strip():
        raise EncuestaError(
            "El token Bearer es invalido.",
            status_code=401,
            payload={
                "contract_version": contract_version,
                "reason_code": "invalid_auth_token",
                "action_hint": "authenticate",
                "required_identity": ["bearer"],
            },
        )

    user = user_from_token(raw_token.strip())
    if user is None:
        raise EncuestaError(
            "El token Bearer es invalido o expiro.",
            status_code=401,
            payload={
                "contract_version": contract_version,
                "reason_code": "invalid_auth_token",
                "action_hint": "authenticate",
                "required_identity": ["bearer"],
            },
        )
    return user


def _resolve_authenticated_response_user(authenticated_user: Optional[User]) -> Optional[User]:
    candidate = authenticated_user
    if candidate is None and has_request_context():
        candidate = resolve_optional_survey_bearer_user(request.headers.get("Authorization"))
        if candidate is None and request.blueprint == "portal_api":
            candidate = getattr(g, "viewer", None)
    if candidate is None:
        return None

    try:
        candidate_id = int(getattr(candidate, "id", None))
    except (TypeError, ValueError):
        candidate_id = None
    if not candidate_id:
        raise EncuestaError(
            "La identidad autenticada no es valida.",
            status_code=401,
            payload={
                "contract_version": "surveys.public_response.v2",
                "reason_code": "invalid_authenticated_identity",
                "action_hint": "authenticate",
                "required_identity": ["bearer"],
            },
        )

    persisted_user = db.session.get(User, candidate_id)
    if persisted_user is None:
        raise EncuestaError(
            "La identidad autenticada no existe.",
            status_code=401,
            payload={
                "contract_version": "surveys.public_response.v2",
                "reason_code": "invalid_authenticated_identity",
                "action_hint": "authenticate",
                "required_identity": ["bearer"],
            },
        )
    return persisted_user


def _validate_required_identity(
    encuesta: EncEncuesta,
    payload: Mapping[str, Any],
    *,
    authenticated_user_id: Optional[int],
) -> None:
    policy = str(getattr(encuesta, "politica_unicidad", "") or "libre").strip().lower()
    authentication_required = (
        not bool(getattr(encuesta, "anonimo_permitido", True))
        or policy in _AUTHENTICATED_USER_POLICIES
    )
    if authentication_required and authenticated_user_id is None:
        raise EncuestaError(
            "Esta votacion requiere una sesion autenticada para registrar la participacion.",
            status_code=401,
            payload={
                "contract_version": "surveys.public_response.v2",
                "reason_code": "authentication_required",
                "action_hint": "authenticate",
                "required_identity": ["bearer"],
                "message": "Inicia sesion para participar en esta votacion.",
            },
        )

    if not (
        bool(getattr(encuesta, "requiere_identidad", False))
        or not bool(getattr(encuesta, "anonimo_permitido", True))
        or policy in _AUTHENTICATED_USER_POLICIES
    ):
        return

    def _clean_identity(value: Any) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    user_id = _clean_identity(authenticated_user_id)
    dni = _clean_identity(payload.get("dni") or payload.get("documento") or payload.get("document"))
    phone = _clean_identity(
        payload.get("phone")
        or payload.get("telefono")
        or payload.get("tel")
        or payload.get("whatsapp")
    )

    requirements_by_policy = {
        "por_dni": ("dni",),
        "dni": ("dni",),
        "por_phone": ("phone",),
        "phone": ("phone",),
        "por_telefono": ("phone",),
        "telefono": ("phone",),
        "por_usuario": ("user_id",),
        "usuario": ("user_id",),
        "user_id": ("user_id",),
        "por_user_id": ("user_id",),
    }
    requirements = requirements_by_policy.get(policy)
    identity_values = {"user_id": user_id, "dni": dni, "phone": phone}

    if requirements and not any(identity_values.get(key) for key in requirements):
        missing_label = " o ".join(requirements)
        raise EncuestaError(
            "Esta votacion requiere identidad verificada para registrar la participacion.",
            status_code=400,
            payload={
                "contract_version": "surveys.public_response.v2",
                "reason_code": "identity_required",
                "action_hint": "provide_identity",
                "required_identity": list(requirements),
                "message": f"Necesitamos {missing_label} para esta votacion.",
            },
        )

    if not any(identity_values.values()):
        raise EncuestaError(
            "Esta votacion requiere identidad verificada para registrar la participacion.",
            status_code=400,
            payload={
                "contract_version": "surveys.public_response.v2",
                "reason_code": "identity_required",
                "action_hint": "provide_identity",
                "required_identity": ["user_id", "dni", "phone"],
                "message": "Necesitamos DNI, telefono o usuario verificado para esta votacion.",
            },
        )


def _sanitize_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = (value or "").strip()
    if not text:
        return None
    # Simple HTML stripping: replace brackets
    text = text.replace("<", " ").replace(">", " ")
    return text[:2000]


def _extract_pregunta_id(item: Mapping[str, Any]) -> Optional[int]:
    candidate_keys = (
        "pregunta_id",
        "preguntaId",
        "question_id",
        "questionId",
        "id",
    )
    for key in candidate_keys:
        value = item.get(key)
        if isinstance(value, Mapping):
            nested = value.get("id")
            if nested is not None:
                value = nested
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_opcion_ids(item: Mapping[str, Any], pregunta: EncPregunta) -> List[int]:
    list_candidate_keys = (
        "opcion_ids",
        "opcionIds",
        "option_ids",
        "optionIds",
        "opciones",
        "opciones_ids",
        "options",
        "selected_option_ids",
        "selectedOptionIds",
    )
    raw: Any = None
    for key in list_candidate_keys:
        if item.get(key) is not None:
            raw = item.get(key)
            break

    if raw is None:
        single_candidate_keys = (
            "opcion_id",
            "opcionId",
            "option_id",
            "optionId",
            "selected_option",
            "selectedOption",
            "valor",
            "value",
        )
        for key in single_candidate_keys:
            value = item.get(key)
            if value is None:
                continue
            raw = value if isinstance(value, (list, tuple, set)) else [value]
            break

    if isinstance(raw, Mapping):
        nested_id = raw.get("id") or raw.get("option_id") or raw.get("value") or raw.get("valor")
        if nested_id is not None:
            raw = [nested_id]
        else:
            raw = list(raw.values())

    if raw is None:
        candidate_values: List[Any] = []
    elif isinstance(raw, (list, tuple, set)):
        candidate_values = list(raw)
    else:
        candidate_values = [raw]

    opciones_validas = {op.id: op for op in pregunta.opciones}
    resolved: List[int] = []
    fallback_labels: List[str] = []

    for value in candidate_values:
        candidate = value
        if isinstance(candidate, Mapping):
            candidate = (
                candidate.get("id")
                or candidate.get("option_id")
                or candidate.get("value")
                or candidate.get("valor")
            )
        if candidate in (None, ""):
            continue
        try:
            resolved.append(int(candidate))
            continue
        except (TypeError, ValueError):
            pass

        text_value = str(candidate).strip()
        if text_value:
            fallback_labels.append(text_value.lower())

    if fallback_labels:
        for label in fallback_labels:
            for opcion in opciones_validas.values():
                candidates = [opcion.valor, opcion.texto]
                for candidate in candidates:
                    if not candidate:
                        continue
                    if str(candidate).strip().lower() == label:
                        resolved.append(opcion.id)
                        break
                else:
                    continue
                break

    unique_resolved: List[int] = []
    seen: set[int] = set()
    for oid in resolved:
        if oid in seen:
            continue
        seen.add(oid)
        unique_resolved.append(oid)
    return unique_resolved


def _extract_texto_libre(item: Mapping[str, Any]) -> Optional[str]:
    candidate_keys = (
        "texto_libre",
        "textoLibre",
        "texto",
        "text",
        "value",
        "valor",
        "respuesta",
        "answer",
        "freeText",
    )
    for key in candidate_keys:
        value = item.get(key)
        if isinstance(value, Mapping):
            nested = (
                value.get("texto")
                or value.get("text")
                or value.get("value")
                or value.get("valor")
            )
            value = nested
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            if value.strip():
                return value
            continue
        return str(value)
    return None


def _evaluate_conditional_v2_node(
    node: Mapping[str, Any],
    *,
    visible_question_refs: set[str],
    selected_option_refs_by_question_ref: Mapping[str, set[str]],
) -> bool:
    if node["kind"] == "option_selected":
        source_question_ref = node["question_ref"]
        return (
            source_question_ref in visible_question_refs
            and node["option_ref"]
            in selected_option_refs_by_question_ref.get(source_question_ref, set())
        )

    results = (
        _evaluate_conditional_v2_node(
            child,
            visible_question_refs=visible_question_refs,
            selected_option_refs_by_question_ref=(
                selected_option_refs_by_question_ref
            ),
        )
        for child in node["children"]
    )
    return all(results) if node["operator"] == "and" else any(results)


def _is_conditional_question_visible(
    pregunta: EncPregunta,
    *,
    visible_question_orders: set[int],
    selected_orders_by_question_order: Mapping[int, set[int]],
    visible_question_refs: Optional[set[str]] = None,
    selected_option_refs_by_question_ref: Optional[
        Mapping[str, set[str]]
    ] = None,
) -> Tuple[bool, Optional[int], Optional[int]]:
    conditional_logic = pregunta.logica_condicional
    if conditional_logic is None:
        return True, None, None
    show_if = conditional_logic["show_if"]
    if conditional_logic["version"] == 2:
        visible = _evaluate_conditional_v2_node(
            show_if,
            visible_question_refs=visible_question_refs or set(),
            selected_option_refs_by_question_ref=(
                selected_option_refs_by_question_ref or {}
            ),
        )
        return visible, None, None

    source_question_order = show_if["question_order"]
    source_option_order = show_if["option_order"]
    visible = (
        source_question_order in visible_question_orders
        and source_option_order
        in selected_orders_by_question_order.get(source_question_order, set())
    )
    return visible, source_question_order, source_option_order


class SurveyVisibilityPlan:
    """Validated, request-local visibility plan reusable across responses."""

    def __init__(self, encuesta: EncEncuesta) -> None:
        _validate_persisted_instrument(encuesta)
        self.questions = tuple(
            sorted(encuesta.preguntas, key=lambda item: item.orden)
        )
        self._options_by_question_id = {
            pregunta.id: {option.id: option for option in pregunta.opciones}
            for pregunta in self.questions
        }
        self._options_by_question_ref = {
            pregunta.logical_ref: {
                option.logical_ref: option
                for option in pregunta.opciones
                if option.logical_ref is not None
            }
            for pregunta in self.questions
            if pregunta.logical_ref is not None
        }

    @staticmethod
    def _selection_values(value: Any) -> Tuple[Any, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(
            value, (Sequence, set, frozenset)
        ):
            return (value,)
        return tuple(value)

    def evaluate(
        self,
        *,
        selected_option_ids_by_question_id: Optional[
            Mapping[int, Sequence[int] | set[int]]
        ] = None,
        selected_option_refs_by_question_ref: Optional[
            Mapping[str, Sequence[str] | set[str]]
        ] = None,
    ) -> Tuple[EncPregunta, ...]:
        """Return visible questions; hidden-source selections never propagate."""

        ids_by_question = selected_option_ids_by_question_id or {}
        refs_by_question = selected_option_refs_by_question_ref or {}
        visible_questions: List[EncPregunta] = []
        visible_question_orders: set[int] = set()
        selected_orders_by_question_order: Dict[int, set[int]] = {}
        visible_question_refs: set[str] = set()
        selected_refs_by_question_ref: Dict[str, set[str]] = {}

        for pregunta in self.questions:
            visible, _, _ = _is_conditional_question_visible(
                pregunta,
                visible_question_orders=visible_question_orders,
                selected_orders_by_question_order=(
                    selected_orders_by_question_order
                ),
                visible_question_refs=visible_question_refs,
                selected_option_refs_by_question_ref=(
                    selected_refs_by_question_ref
                ),
            )
            if not visible:
                continue

            visible_questions.append(pregunta)
            visible_question_orders.add(pregunta.orden)
            if pregunta.logical_ref is not None:
                visible_question_refs.add(pregunta.logical_ref)

            selected_options_by_id: Dict[int, EncOpcion] = {}
            for raw_id in self._selection_values(
                ids_by_question.get(pregunta.id)
            ):
                try:
                    option_id = int(raw_id)
                except (TypeError, ValueError, OverflowError):
                    continue
                option = self._options_by_question_id.get(
                    pregunta.id, {}
                ).get(option_id)
                if option is not None:
                    selected_options_by_id[option.id] = option

            if pregunta.logical_ref is not None:
                for raw_ref in self._selection_values(
                    refs_by_question.get(pregunta.logical_ref)
                ):
                    if not isinstance(raw_ref, str):
                        continue
                    option = self._options_by_question_ref.get(
                        pregunta.logical_ref, {}
                    ).get(raw_ref)
                    if option is not None:
                        selected_options_by_id[option.id] = option

            selected_options = selected_options_by_id.values()
            selected_orders_by_question_order[pregunta.orden] = {
                option.orden for option in selected_options
            }
            if pregunta.logical_ref is not None:
                selected_refs_by_question_ref[pregunta.logical_ref] = {
                    option.logical_ref
                    for option in selected_options
                    if option.logical_ref is not None
                }

        return tuple(visible_questions)


def compile_survey_visibility(encuesta: EncEncuesta) -> SurveyVisibilityPlan:
    """Compile and validate one survey without process-global mutable caching."""

    return SurveyVisibilityPlan(encuesta)


def resolve_visible_survey_questions(
    encuesta: EncEncuesta,
    *,
    selected_option_ids_by_question_id: Optional[
        Mapping[int, Sequence[int] | set[int]]
    ] = None,
    selected_option_refs_by_question_ref: Optional[
        Mapping[str, Sequence[str] | set[str]]
    ] = None,
) -> Tuple[EncPregunta, ...]:
    """One-shot wrapper around :func:`compile_survey_visibility`."""

    return compile_survey_visibility(encuesta).evaluate(
        selected_option_ids_by_question_id=(
            selected_option_ids_by_question_id
        ),
        selected_option_refs_by_question_ref=(
            selected_option_refs_by_question_ref
        ),
    )


def _validate_legacy_respuesta_payload(
    encuesta: EncEncuesta,
    respuestas_payload: Sequence[Dict[str, Any]],
) -> List[EncRespuestaDetalle]:
    """Preserve historical answer semantics for instruments without branching."""

    detalles: List[EncRespuestaDetalle] = []
    preguntas_map = {p.id: p for p in encuesta.preguntas}
    answered_ids: set[int] = set()

    for item in respuestas_payload:
        pregunta_id = _extract_pregunta_id(item)
        if not pregunta_id:
            raise EncuestaError("Respuesta sin pregunta_id")
        pregunta = preguntas_map.get(pregunta_id)
        if not pregunta:
            raise EncuestaError("Pregunta invalida en respuestas")
        if pregunta.id in answered_ids:
            raise EncuestaError(
                "Cada pregunta debe aparecer una sola vez en respuestas",
                status_code=400,
                payload={
                    "contract_version": "surveys.public_response.v2",
                    "reason_code": "duplicate_question_response",
                    "action_hint": "merge_question_answers",
                    "question_id": pregunta.id,
                },
            )

        respuesta_detalle = EncRespuestaDetalle(pregunta_id=pregunta.id)
        answered_ids.add(pregunta.id)

        if pregunta.tipo in {"opcion_unica", "opcion_multiple", "rating_emoji"}:
            opcion_ids = _extract_opcion_ids(item, pregunta)
            opciones_validas = {op.id: op for op in pregunta.opciones}
            seleccionadas: List[int] = []
            for opcion_id in opcion_ids:
                opcion = opciones_validas.get(opcion_id)
                if not opcion:
                    raise EncuestaError("Opcion invalida seleccionada")
                seleccionadas.append(opcion.id)
                detalles.append(
                    EncRespuestaDetalle(
                        pregunta_id=pregunta.id,
                        opcion_id=opcion.id,
                    )
                )
            if pregunta.tipo == "rating_emoji" and len(seleccionadas) > 1:
                raise EncuestaError("La pregunta admite una sola opcion seleccionada")
            if pregunta.obligatoria and not seleccionadas:
                raise EncuestaError("Pregunta obligatoria sin opciones seleccionadas")
            min_sel = pregunta.min_selecciones or (1 if pregunta.obligatoria else 0)
            max_sel = (
                1
                if pregunta.tipo == "rating_emoji"
                else pregunta.max_selecciones or len(opciones_validas)
            )
            if not (min_sel <= len(seleccionadas) <= max_sel):
                raise EncuestaError("Cantidad de opciones seleccionadas fuera de rango")
            continue

        if pregunta.tipo == "abierta":
            texto = _sanitize_text(_extract_texto_libre(item))
            if pregunta.obligatoria and not texto:
                raise EncuestaError("Pregunta abierta obligatoria sin texto")
            respuesta_detalle.texto_libre = texto
            detalles.append(respuesta_detalle)
            continue

        raise EncuestaError("Tipo de pregunta no soportado")

    for pregunta in encuesta.preguntas:
        if pregunta.obligatoria and pregunta.id not in answered_ids:
            raise EncuestaError("Falta responder una pregunta obligatoria")
    return detalles


def _validate_conditional_respuesta_payload(
    encuesta: EncEncuesta,
    respuestas_payload: Sequence[Dict[str, Any]],
) -> List[EncRespuestaDetalle]:
    """Validate answers in two passes so branching cannot be bypassed."""

    _validate_persisted_instrument(encuesta)
    preguntas_map = {p.id: p for p in encuesta.preguntas}
    parsed_by_question_id: Dict[int, Dict[str, Any]] = {}

    # Pass one: validate every submitted answer without attaching it to a
    # response entity. A later visibility error therefore cannot persist data.
    for item in respuestas_payload:
        pregunta_id = _extract_pregunta_id(item)
        if not pregunta_id:
            raise EncuestaError("Respuesta sin pregunta_id")
        pregunta = preguntas_map.get(pregunta_id)
        if not pregunta:
            raise EncuestaError("Pregunta invalida en respuestas")
        if pregunta.id in parsed_by_question_id:
            raise EncuestaError(
                "Cada pregunta debe aparecer una sola vez en respuestas",
                status_code=400,
                payload={
                    "contract_version": "surveys.public_response.v2",
                    "reason_code": "duplicate_question_response",
                    "action_hint": "merge_question_answers",
                    "question_id": pregunta.id,
                },
            )

        parsed: Dict[str, Any] = {
            "details": [],
            "has_substantive_answer": False,
            "selected_option_orders": set(),
            "selected_option_refs": set(),
        }
        parsed_by_question_id[pregunta.id] = parsed

        if pregunta.tipo in {"opcion_unica", "opcion_multiple", "rating_emoji"}:
            opcion_ids = _extract_opcion_ids(item, pregunta)
            opciones_validas = {op.id: op for op in pregunta.opciones}
            seleccionadas: List[int] = []
            for opcion_id in opcion_ids:
                opcion = opciones_validas.get(opcion_id)
                if not opcion:
                    raise EncuestaError("Opcion invalida seleccionada")
                seleccionadas.append(opcion.id)
                parsed["selected_option_orders"].add(opcion.orden)
                if opcion.logical_ref is not None:
                    parsed["selected_option_refs"].add(opcion.logical_ref)
                parsed["details"].append(
                    EncRespuestaDetalle(
                        pregunta_id=pregunta.id,
                        opcion_id=opcion.id,
                    )
                )

            if pregunta.tipo in {"opcion_unica", "rating_emoji"} and len(seleccionadas) > 1:
                raise EncuestaError("La pregunta admite una sola opcion seleccionada")
            min_sel = pregunta.min_selecciones or 0
            max_sel = (
                1
                if pregunta.tipo in {"opcion_unica", "rating_emoji"}
                else pregunta.max_selecciones or len(opciones_validas)
            )
            if not (min_sel <= len(seleccionadas) <= max_sel):
                raise EncuestaError("Cantidad de opciones seleccionadas fuera de rango")
            parsed["has_substantive_answer"] = bool(seleccionadas)
            continue

        if pregunta.tipo == "abierta":
            texto = _sanitize_text(_extract_texto_libre(item))
            parsed["details"].append(
                EncRespuestaDetalle(
                    pregunta_id=pregunta.id,
                    texto_libre=texto,
                )
            )
            parsed["has_substantive_answer"] = bool(texto)
            continue

        raise EncuestaError("Tipo de pregunta no soportado")

    # Pass two: derive visibility strictly from earlier, validated selections.
    detalles: List[EncRespuestaDetalle] = []
    visible_question_orders: set[int] = set()
    selected_orders_by_question_order: Dict[int, set[int]] = {}
    visible_question_refs: set[str] = set()
    selected_option_refs_by_question_ref: Dict[str, set[str]] = {}
    for pregunta in sorted(encuesta.preguntas, key=lambda item: item.orden):
        visible, source_question_order, source_option_order = (
            _is_conditional_question_visible(
                pregunta,
                visible_question_orders=visible_question_orders,
                selected_orders_by_question_order=selected_orders_by_question_order,
                visible_question_refs=visible_question_refs,
                selected_option_refs_by_question_ref=(
                    selected_option_refs_by_question_ref
                ),
            )
        )

        submitted = parsed_by_question_id.get(pregunta.id)
        if not visible:
            if submitted is not None:
                raise EncuestaError(
                    "Se envio una respuesta para una pregunta oculta",
                    status_code=400,
                    payload={
                        "contract_version": "surveys.public_response.v2",
                        "reason_code": "hidden_question_answered",
                        "action_hint": "remove_hidden_answer",
                        "question_id": pregunta.id,
                        "question_order": pregunta.orden,
                        "source_question_order": source_question_order,
                        "source_option_order": source_option_order,
                    },
                )
            continue

        visible_question_orders.add(pregunta.orden)
        if pregunta.logical_ref is not None:
            visible_question_refs.add(pregunta.logical_ref)
        if pregunta.obligatoria and (
            submitted is None or not submitted["has_substantive_answer"]
        ):
            raise EncuestaError(
                "Falta responder una pregunta obligatoria visible",
                status_code=400,
                payload={
                    "contract_version": "surveys.public_response.v2",
                    "reason_code": "required_visible_question_missing",
                    "action_hint": "answer_visible_question",
                    "question_id": pregunta.id,
                    "question_order": pregunta.orden,
                },
            )
        if submitted is None:
            selected_orders_by_question_order[pregunta.orden] = set()
            if pregunta.logical_ref is not None:
                selected_option_refs_by_question_ref[pregunta.logical_ref] = set()
            continue

        selected_orders_by_question_order[pregunta.orden] = set(
            submitted["selected_option_orders"]
        )
        if pregunta.logical_ref is not None:
            selected_option_refs_by_question_ref[pregunta.logical_ref] = set(
                submitted["selected_option_refs"]
            )
        detalles.extend(submitted["details"])

    return detalles


def _validate_respuesta_payload(
    encuesta: EncEncuesta,
    respuestas_payload: Sequence[Dict[str, Any]],
) -> List[EncRespuestaDetalle]:
    if not any(pregunta.logica_condicional is not None for pregunta in encuesta.preguntas):
        return _validate_legacy_respuesta_payload(encuesta, respuestas_payload)
    return _validate_conditional_respuesta_payload(encuesta, respuestas_payload)


def _persist_respuesta_entity(
    respuesta: EncRespuesta,
    detalles: Sequence[EncRespuestaDetalle],
    *,
    commit: bool = True,
    expected_structure_revision: Optional[int] = None,
) -> EncRespuesta:
    if not detalles:
        raise EncuestaError("Debe enviar respuestas")

    encuesta = _acquire_encuesta_response_guard(respuesta.encuesta_id)
    current_revision = int(encuesta.structure_revision or 1)
    if (
        expected_structure_revision is not None
        and current_revision != int(expected_structure_revision)
    ):
        raise _survey_concurrency_error(
            "La estructura de la encuesta cambio mientras se preparaba la respuesta.",
            reason_code="survey_structure_changed",
        )

    if encuesta.structure_locked_at is None:
        encuesta.structure_locked_at = datetime.now(timezone.utc)

    respuesta.detalles = list(detalles)
    db.session.add(respuesta)

    try:
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except IntegrityError as exc:
        if commit:
            db.session.rollback()
        message = str(getattr(exc, "orig", exc)).lower()
        if "uq_enc_respuesta_huella" in message or "huella_unica" in message:
            raise _survey_duplicate_response_error() from exc

        logger = _current_app_logger()
        if logger:
            logger.exception("[encuestas] Error al guardar respuesta")
        raise EncuestaError("No se pudo guardar la respuesta", status_code=500) from exc

    return respuesta


def _clean_str(value: Optional[Any], *, max_length: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
    else:
        cleaned = str(value).strip()
    if not cleaned:
        return None
    if max_length is not None:
        return cleaned[:max_length]
    return cleaned


def _coerce_int(value: Optional[Any]) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Optional[Any]) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _infer_age_from_birth_year(year: Optional[int]) -> Optional[int]:
    if not year:
        return None
    now_year = datetime.now(timezone.utc).year
    age = now_year - year
    if age < 0 or age > 120:
        return None
    return age


def _infer_birth_year_from_age(age: Optional[int]) -> Optional[int]:
    if age is None:
        return None
    if age < 0 or age > 120:
        return None
    return datetime.now(timezone.utc).year - age


def _compute_age_group(age: Optional[int]) -> Optional[str]:
    if age is None:
        return None
    buckets = (
        (12, "0-12"),
        (17, "13-17"),
        (24, "18-24"),
        (34, "25-34"),
        (44, "35-44"),
        (54, "45-54"),
        (64, "55-64"),
    )
    for max_age, label in buckets:
        if age <= max_age:
            return label
    return "65+"


def _normalize_genero(value: Optional[Any]) -> Optional[str]:
    text = _clean_str(value, max_length=30)
    if not text:
        return None
    lowered = text.lower()
    mapping = {
        "f": "femenino",
        "fem": "femenino",
        "female": "femenino",
        "femenino": "femenino",
        "m": "masculino",
        "masc": "masculino",
        "male": "masculino",
        "masculino": "masculino",
        "nb": "no_binario",
        "non binary": "no_binario",
        "no binario": "no_binario",
        "no_binario": "no_binario",
    }
    if lowered in mapping:
        return mapping[lowered]
    for key, mapped in mapping.items():
        if lowered == key:
            return mapped
    return text[:30]


def _safe_json_value(raw: Optional[str]) -> Optional[Any]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _normalize_metadata(value: Optional[Any]) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        normalized_list = [_normalize_metadata(item) for item in value]
        return normalized_list
    if isinstance(value, dict):
        normalized_dict: Dict[str, Any] = {}
        for key, item in value.items():
            normalized_dict[str(key)] = _normalize_metadata(item)
        return normalized_dict
    try:
        json_value = json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return None
    return json_value


def _coerce_respuestas_payload(value: Optional[Any]) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        parsed = _safe_json_value(value)
        if parsed is None:
            return []
        return _coerce_respuestas_payload(parsed)
    if isinstance(value, Mapping):
        numeric_children: List[Tuple[int, Mapping[str, Any]]] = []
        for key, item in value.items():
            if isinstance(item, Mapping) and (
                isinstance(key, int) or (isinstance(key, str) and key.isdigit())
            ):
                numeric_children.append((int(key), item))
            else:
                numeric_children = []
                break
        if numeric_children:
            return [dict(child) for _, child in sorted(numeric_children, key=lambda pair: pair[0])]
        return [dict(value)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                parsed_item = _safe_json_value(item)
                if isinstance(parsed_item, Mapping):
                    normalized.append(dict(parsed_item))
                    continue
            if isinstance(item, Mapping):
                normalized.append(dict(item))
        return normalized
    return []


def _canonical_receipt_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EncuestaError(
                "La respuesta contiene un numero no valido",
                status_code=400,
                payload={"reason_code": "survey_submission_payload_invalid"},
            )
        return 0 if value == 0 else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_receipt_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_receipt_value(item) for item in value]
    if isinstance(value, set):
        normalized = [_canonical_receipt_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    return unicodedata.normalize("NFC", str(value))


def _survey_response_payload_hash(
    encuesta: EncEncuesta,
    payload: Mapping[str, Any],
    request_ctx: Mapping[str, Any],
    *,
    authenticated_user_id: Optional[int],
    detalles: Sequence[EncRespuestaDetalle],
) -> str:
    """Hash the logical, persistible submission while excluding transport secrets."""

    canonical_body = {
        str(key): value
        for key, value in payload.items()
        if key not in _SURVEY_RECEIPT_EXCLUDED_FIELDS
        and key not in {"respuestas", "answers", "user_id", "userId"}
    }
    canonical_answers: Dict[int, Dict[str, Any]] = {}
    for detalle in detalles:
        answer = canonical_answers.setdefault(
            int(detalle.pregunta_id),
            {"question_id": int(detalle.pregunta_id), "option_ids": [], "text": None},
        )
        if detalle.opcion_id is not None:
            answer["option_ids"].append(int(detalle.opcion_id))
        if detalle.texto_libre is not None:
            answer["text"] = _sanitize_text(detalle.texto_libre)
    for answer in canonical_answers.values():
        answer["option_ids"] = sorted(set(answer["option_ids"]))

    canonical = {
        "canonical_version": SURVEY_RESPONSE_CANONICAL_VERSION,
        "tenant_id": int(encuesta.tenant_id),
        "survey_id": int(encuesta.id),
        "instrument_revision": int(encuesta.structure_revision or 1),
        "participant": {
            "authenticated_user_id": authenticated_user_id,
            "anon_id": payload.get("anon_id")
            or payload.get("anonId")
            or request_ctx.get("anon_id"),
            "dni": payload.get("dni")
            or payload.get("documento")
            or payload.get("document"),
            "phone": payload.get("phone")
            or payload.get("telefono")
            or payload.get("tel")
            or payload.get("whatsapp"),
        },
        "answers": [canonical_answers[key] for key in sorted(canonical_answers)],
        "body": canonical_body,
    }
    encoded = json.dumps(
        _canonical_receipt_value(canonical),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _survey_submission_id_hash(tenant_id: int, submission_id: str) -> str:
    scoped = f"{SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION}:{int(tenant_id)}:{submission_id}"
    return hashlib.sha256(scoped.encode("utf-8")).hexdigest()


def _mark_survey_response_receipt(
    respuesta: EncRespuesta,
    receipt: SurveyResponseReceipt,
    submission_id: str,
    *,
    replayed: bool,
) -> EncRespuesta:
    respuesta.instrument_revision = int(receipt.instrument_revision)
    respuesta.submission_id = submission_id
    respuesta.submission_replayed = bool(replayed)
    respuesta.submission_persisted = True
    respuesta.submission_receipt_id = int(receipt.id)
    respuesta.submission_receipt_contract_version = receipt.contract_version
    return respuesta


def _resolve_survey_response_receipt(
    encuesta: EncEncuesta,
    submission_id: str,
    payload_hash: str,
) -> Optional[EncRespuesta]:
    receipt = SurveyResponseReceipt.query.filter_by(
        tenant_id=encuesta.tenant_id,
        submission_id_hash=_survey_submission_id_hash(encuesta.tenant_id, submission_id),
    ).first()
    if receipt is None:
        return None
    if (
        int(receipt.survey_id) != int(encuesta.id)
        or receipt.payload_hash != payload_hash
        or receipt.canonical_version != SURVEY_RESPONSE_CANONICAL_VERSION
        or int(receipt.instrument_revision) != int(encuesta.structure_revision or 1)
    ):
        raise _survey_submission_conflict_error()
    respuesta = receipt.response or db.session.get(EncRespuesta, receipt.response_id)
    if respuesta is None:
        raise EncuestaError(
            "El recibo de la respuesta no tiene una respuesta asociada",
            status_code=500,
            payload={
                "contract_version": SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION,
                "reason_code": "survey_submission_receipt_corrupt",
                "retryable": False,
                "action_hint": "contact_support",
            },
        )
    return _mark_survey_response_receipt(
        respuesta,
        receipt,
        submission_id,
        replayed=True,
    )


def survey_response_receipt_contract(respuesta: EncRespuesta) -> Optional[Dict[str, Any]]:
    submission_id = getattr(respuesta, "submission_id", None)
    receipt_id = getattr(respuesta, "submission_receipt_id", None)
    if not submission_id or receipt_id is None:
        return None
    replayed = bool(getattr(respuesta, "submission_replayed", False))
    return {
        "contract_version": getattr(
            respuesta,
            "submission_receipt_contract_version",
            SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION,
        ),
        "canonical_version": SURVEY_RESPONSE_CANONICAL_VERSION,
        "receipt_id": int(receipt_id),
        "submission_id": submission_id,
        "response_id": int(respuesta.id),
        "instrument_revision": int(getattr(respuesta, "instrument_revision", 1) or 1),
        "state": "committed",
        "disposition": "replayed" if replayed else "accepted",
        "persisted": True,
        "replayed": replayed,
    }


def find_survey_response_replay(
    slug_publico: str,
    payload: Mapping[str, Any],
    request_ctx: Mapping[str, Any],
    *,
    submission_id: Optional[str],
    preferred_tenant_id: Optional[int] = None,
    authenticated_user: Optional[User] = None,
) -> Optional[EncRespuesta]:
    """Return a committed replay before one-shot security checks are repeated."""

    if submission_id is None:
        return None
    encuesta = get_public_encuesta(
        slug_publico,
        preferred_tenant_id=preferred_tenant_id,
        allow_inactive_for_receipt_lookup=True,
    )
    # Do not expose or validate an inactive instrument for a fresh submission.
    # Only the holder of a key that already resolves to a durable receipt may
    # continue into the exact-payload verification below.
    existing_receipt = SurveyResponseReceipt.query.filter_by(
        tenant_id=encuesta.tenant_id,
        submission_id_hash=_survey_submission_id_hash(
            encuesta.tenant_id,
            submission_id,
        ),
    ).first()
    if existing_receipt is None:
        return None

    submitted_revision = _submitted_instrument_revision(payload)
    expected_revision = int(encuesta.structure_revision or 1)
    if submitted_revision is not None and submitted_revision != expected_revision:
        raise _stale_instrument_error(encuesta, submitted_revision)
    authenticated_response_user = _resolve_authenticated_response_user(authenticated_user)
    authenticated_user_id = (
        int(authenticated_response_user.id)
        if authenticated_response_user is not None
        else None
    )
    raw_respuestas = payload.get("respuestas")
    if raw_respuestas is None and "answers" in payload:
        raw_respuestas = payload.get("answers")
    respuestas_payload = _coerce_respuestas_payload(raw_respuestas)
    if not respuestas_payload:
        raise EncuestaError("Debe enviar respuestas")
    detalles = _validate_respuesta_payload(encuesta, respuestas_payload)
    payload_hash = _survey_response_payload_hash(
        encuesta,
        payload,
        request_ctx,
        authenticated_user_id=authenticated_user_id,
        detalles=detalles,
    )
    return _resolve_survey_response_receipt(encuesta, submission_id, payload_hash)


def _build_survey_response_analytics_event(
    encuesta: EncEncuesta,
    respuesta: EncRespuesta,
    *,
    slug_publico: str,
    respuestas_payload: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the immutable, PII-minimized analytics intent for one response."""

    tenant = db.session.get(TenantProfile, encuesta.tenant_id)
    tenant_type = _clean_str(getattr(tenant, "tipo", None), max_length=20) or "municipio"
    public_slug = _resolve_public_slug(encuesta) or encuesta.slug or slug_publico
    event_name = (
        "vote_submitted"
        if bool(getattr(encuesta, "es_votacion_envivo", False))
        or str(getattr(encuesta, "tipo", "") or "").strip().lower() in {"votacion", "votacion_envivo", "live_vote"}
        else "survey_answer_submitted"
    )
    selected_options: List[Dict[str, Any]] = []
    open_answers = 0
    for detalle in respuesta.detalles or []:
        if getattr(detalle, "opcion_id", None) is not None:
            selected_options.append(
                {
                    "pregunta_id": detalle.pregunta_id,
                    "opcion_id": detalle.opcion_id,
                }
            )
        if getattr(detalle, "texto_libre", None):
            open_answers += 1

    payload = {
        "contract_version": "analytics.survey_response_event.v1",
        "encuesta_id": encuesta.id,
        "survey_id": encuesta.id,
        "slug": public_slug,
        "slug_publico": public_slug,
        "response_id": respuesta.id,
        "respuesta_id": respuesta.id,
        "survey_type": getattr(encuesta, "tipo", None),
        "is_live_vote": bool(getattr(encuesta, "es_votacion_envivo", False)),
        "live_results_visible": bool(getattr(encuesta, "mostrar_resultados_envivo", False)),
        "answers_count": len(respuestas_payload),
        "selected_options_count": len(selected_options),
        "open_answers_count": open_answers,
        "selected_options": selected_options[:40],
        "has_geo": respuesta.lat is not None and respuesta.lng is not None,
        "has_contact_identity": bool(respuesta.user_id or respuesta.dni or respuesta.phone),
        "has_demographics": bool(respuesta.genero or respuesta.rango_etario or respuesta.edad),
        "utm_source": respuesta.utm_source,
        "utm_campaign": respuesta.utm_campaign,
        "barrio": respuesta.barrio,
        "ciudad": respuesta.ciudad,
        "provincia": respuesta.provincia,
        "pais": respuesta.pais,
    }
    return {
        "tenant_id": encuesta.tenant_id,
        "event_name": event_name,
        "payload": payload,
        "user_id": _coerce_int(respuesta.user_id),
        "anon_id": respuesta.huella_unica or None,
        "channel": respuesta.canal or "public_survey",
        "session_id": respuesta.huella_unica or None,
        "lat": respuesta.lat,
        "lng": respuesta.lng,
        "entity_ref": f"survey:{encuesta.id}:response:{respuesta.id}",
        "tenant_type": tenant_type,
    }


def _track_survey_response_analytics(
    encuesta: EncEncuesta,
    respuesta: EncRespuesta,
    *,
    slug_publico: str,
    respuestas_payload: Sequence[Dict[str, Any]],
    commit: bool = True,
) -> bool:
    """Feed one response into analytics; durable callers use the outbox instead."""

    if not analytics_ingestor:
        return False

    try:
        event = _build_survey_response_analytics_event(
            encuesta,
            respuesta,
            slug_publico=slug_publico,
            respuestas_payload=respuestas_payload,
        )
        analytics_ingestor.track(
            **event,
            commit=commit,
        )
        return True
    except Exception:
        logger = _current_app_logger()
        if logger:
            logger.exception(
                "[encuestas] analytics ingest failed for response encuesta_id=%s respuesta_id=%s",
                getattr(encuesta, "id", None),
                getattr(respuesta, "id", None),
            )
        return False


def _grant_survey_reward_effect(
    encuesta: EncEncuesta,
    respuesta: EncRespuesta,
    authenticated_user: User,
    *,
    reward_points: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    commit: bool = True,
) -> str:
    """Credit the durable survey entitlement inside the caller's transaction.

    The outbox owns first-response arbitration.  This helper only guarantees
    that the resulting ledger entry is idempotent and that balance + ledger are
    committed atomically with the effect completion when ``commit=False``.
    """

    try:
        reward_points = int(
            encuesta.puntos_recompensa if reward_points is None else reward_points
        )
    except (TypeError, ValueError):
        reward_points = 0
    if reward_points <= 0:
        return "skipped"

    tenant_id = encuesta.tenant_id
    expected_user_id = _coerce_int(getattr(respuesta, "user_id", None))
    if expected_user_id is None or expected_user_id != _coerce_int(authenticated_user.id):
        raise ValueError("La identidad de la recompensa no coincide con la respuesta")
    if _coerce_int(getattr(respuesta, "tenant_id", None)) != _coerce_int(tenant_id):
        raise ValueError("El tenant de la recompensa no coincide con la respuesta")

    idempotency_key = (
        _clean_str(idempotency_key, max_length=160)
        or f"survey_reward:{encuesta.id}:user:{authenticated_user.id}"
    )

    try:
        locked_user = (
            User.query.filter_by(id=authenticated_user.id)
            .with_for_update()
            .first()
        )
        if locked_user is None:
            raise ValueError("Usuario no encontrado para acreditar puntos")

        existing_reward = PointsTransaction.query.filter_by(
            user_id=locked_user.id,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        ).first()
        if existing_reward is not None:
            return "already_credited"

        # Historical rows predate the indexed idempotency column.  Keep the
        # metadata fallback so deploying the migration never credits them twice.
        historical_rewards = PointsTransaction.query.filter_by(
            user_id=locked_user.id,
            tenant_id=tenant_id,
            tipo="encuesta",
        ).all()
        for transaction in historical_rewards:
            metadata = (
                transaction.metadata_payload
                if isinstance(transaction.metadata_payload, dict)
                else {}
            )
            if metadata.get("idempotency_key") == idempotency_key:
                return "already_credited"

        locked_user.saldo_puntos = (locked_user.saldo_puntos or 0) + reward_points
        db.session.add(
            PointsTransaction(
                user_id=locked_user.id,
                tenant_id=tenant_id,
                tipo="encuesta",
                delta=reward_points,
                saldo_final=locked_user.saldo_puntos,
                idempotency_key=idempotency_key,
                metadata_payload={
                    "idempotency_key": idempotency_key,
                    "source": "survey_response",
                    "survey_id": encuesta.id,
                    "response_id": respuesta.id,
                },
            )
        )
        if commit:
            db.session.commit()
        return "credited"
    except Exception:
        if commit:
            db.session.rollback()
        raise


def _grant_survey_reward_once(
    encuesta: EncEncuesta,
    respuesta: EncRespuesta,
    authenticated_user: User,
) -> bool:
    """Compatibility wrapper for callers that still own their commit."""

    return _grant_survey_reward_effect(
        encuesta,
        respuesta,
        authenticated_user,
        commit=True,
    ) == "credited"


def save_respuesta(
    slug_publico: str,
    payload: Dict[str, Any],
    request_ctx: Dict[str, Any],
    *,
    preferred_tenant_id: Optional[int] = None,
    authenticated_user: Optional[User] = None,
    commit: bool = True,
    emit_realtime_update: bool = True,
    grant_reward: bool = True,
    submission_id: Optional[str] = None,
) -> EncRespuesta:
    if not isinstance(payload, dict):
        if isinstance(payload, Mapping):
            payload = dict(payload)
        else:
            raise EncuestaError("Debe enviar respuestas")
    else:
        payload = dict(payload)

    submission_id = resolve_survey_submission_id(
        payload,
        header_value=submission_id,
        required=False,
    )
    if submission_id is not None and not commit:
        raise EncuestaError(
            "submission_id no admite una transaccion externa sin savepoint propietario",
            status_code=500,
            payload={
                "contract_version": SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION,
                "reason_code": "survey_submission_external_transaction_unsupported",
                "retryable": False,
                "action_hint": "call_with_commit_or_omit_submission_id",
            },
        )

    if submission_id is not None:
        replay = find_survey_response_replay(
            slug_publico,
            payload,
            request_ctx,
            submission_id=submission_id,
            preferred_tenant_id=preferred_tenant_id,
            authenticated_user=authenticated_user,
        )
        if replay is not None:
            return replay

    encuesta = get_public_encuesta(slug_publico, preferred_tenant_id=preferred_tenant_id)
    encuesta = _acquire_encuesta_response_guard(encuesta.id)
    _ensure_locked_public_encuesta(encuesta)
    expected_structure_revision = int(encuesta.structure_revision or 1)

    submitted_instrument_revision = _submitted_instrument_revision(payload)
    if (
        submitted_instrument_revision is not None
        and submitted_instrument_revision != expected_structure_revision
    ):
        raise _stale_instrument_error(encuesta, submitted_instrument_revision)

    # Public callers may send these legacy fields, but they never establish identity.
    payload.pop("user_id", None)
    payload.pop("userId", None)
    authenticated_response_user = _resolve_authenticated_response_user(authenticated_user)
    authenticated_user_id = (
        int(authenticated_response_user.id)
        if authenticated_response_user is not None
        else None
    )

    raw_respuestas = payload.get("respuestas")
    if raw_respuestas is None and "answers" in payload:
        raw_respuestas = payload.get("answers")
    respuestas_payload = _coerce_respuestas_payload(raw_respuestas)
    if not isinstance(respuestas_payload, Sequence) or not respuestas_payload:
        raise EncuestaError("Debe enviar respuestas")

    detalles = _validate_respuesta_payload(encuesta, respuestas_payload)
    submission_payload_hash: Optional[str] = None
    if submission_id is not None:
        submission_payload_hash = _survey_response_payload_hash(
            encuesta,
            payload,
            request_ctx,
            authenticated_user_id=authenticated_user_id,
            detalles=detalles,
        )
        replay = _resolve_survey_response_receipt(
            encuesta,
            submission_id,
            submission_payload_hash,
        )
        if replay is not None:
            return replay
    metadata_raw = payload.get("metadata")
    metadata = _normalize_metadata(metadata_raw)
    metadata_dict = metadata if isinstance(metadata, dict) else None
    metadata_payload = metadata if isinstance(metadata, (dict, list)) else None

    tenant_id = encuesta.tenant_id
    _validate_required_identity(
        encuesta,
        payload,
        authenticated_user_id=authenticated_user_id,
    )

    dni = payload.get("dni") or payload.get("documento") or payload.get("document")
    phone = payload.get("phone") or payload.get("telefono") or payload.get("tel") or payload.get("whatsapp")
    user_id = authenticated_user_id
    anon_cookie = request_ctx.get("anon_id")
    ip = request_ctx.get("ip")

    fingerprint = build_unique_fingerprint(
        encuesta,
        tenant_id,
        dni=dni,
        phone=phone,
        user_id=user_id,
        ip=ip,
        anon_cookie=anon_cookie,
    )
    policy = str(encuesta.politica_unicidad or "libre").strip().lower()
    if fingerprint is None and policy != "libre":
        required_identifiers = {
            "por_cookie": ["anon_id"],
            "cookie": ["anon_id"],
            "por_dni": ["dni"],
            "dni": ["dni"],
            "por_phone": ["phone"],
            "phone": ["phone"],
            "por_ip": ["ip"],
            "ip": ["ip"],
            "por_dni_o_phone": ["dni", "phone"],
            "dni_o_phone": ["dni", "phone"],
            "por_usuario": ["user_id"],
            "usuario": ["user_id"],
            "user_id": ["user_id"],
            "por_user_id": ["user_id"],
        }.get(policy, ["stable_identifier"])
        raise EncuestaError(
            "No se pudo formar una huella estable para validar la participacion",
            status_code=400,
            payload={
                "contract_version": "surveys.public_response.v2",
                "reason_code": "stable_fingerprint_required",
                "action_hint": "provide_anon_id" if "anon_id" in required_identifiers else "provide_identity",
                "uniqueness_policy": policy,
                "required_identifiers": required_identifiers,
            },
        )
    if fingerprint:
        existing = EncRespuesta.query.filter_by(encuesta_id=encuesta.id, huella_unica=fingerprint).first()
        if existing:
            raise _survey_duplicate_response_error()

    genero = _normalize_genero(payload.get("genero") or payload.get("sexo"))
    edad = _coerce_int(payload.get("edad"))
    anio_nacimiento = _coerce_int(payload.get("anio_nacimiento"))
    rango_etario = _clean_str(payload.get("rango_etario"), max_length=30)

    lat = _coerce_float(payload.get("lat"))
    lng = _coerce_float(payload.get("lng"))
    barrio = _clean_str(payload.get("barrio"), max_length=120)
    ciudad = _clean_str(payload.get("ciudad"), max_length=120)
    provincia = _clean_str(payload.get("provincia"), max_length=120)
    pais = _clean_str(payload.get("pais"), max_length=120)

    canal = _clean_str(payload.get("canal"), max_length=64)
    request_canal = _clean_str(request_ctx.get("canal"), max_length=64)
    canal = canal or request_canal or "web"

    submitted_override = None
    metadata_rango = None

    if metadata_dict:
        metadata_canal = _clean_str(metadata_dict.get("canal"), max_length=64)
        if metadata_canal:
            canal = metadata_canal

        submitted_raw = metadata_dict.get("submittedAt") or metadata_dict.get("submitted_at")
        if submitted_raw:
            submitted_override = _parse_datetime(str(submitted_raw))

        demographics = metadata_dict.get("demographics")
        if isinstance(demographics, dict):
            genero = genero or _normalize_genero(
                demographics.get("genero")
                or demographics.get("gender")
                or demographics.get("sexo")
            )
            metadata_rango = _clean_str(
                demographics.get("rangoEtario") or demographics.get("rango_etario"),
                max_length=30,
            )
            if edad is None:
                edad = _coerce_int(demographics.get("edad"))
            if anio_nacimiento is None:
                anio_nacimiento = _coerce_int(
                    demographics.get("anioNacimiento")
                    or demographics.get("anio_nacimiento")
                )
            ubicacion = demographics.get("ubicacion") or demographics.get("ubicación")
            if isinstance(ubicacion, dict):
                if lat is None:
                    lat = _coerce_float(ubicacion.get("lat"))
                if lng is None:
                    lng = _coerce_float(ubicacion.get("lng"))
                barrio = barrio or _clean_str(ubicacion.get("barrio"), max_length=120)
                ciudad = ciudad or _clean_str(ubicacion.get("ciudad"), max_length=120)
                provincia = provincia or _clean_str(ubicacion.get("provincia"), max_length=120)
                pais = pais or _clean_str(ubicacion.get("pais"), max_length=120)

    if anio_nacimiento is None and payload.get("fecha_nacimiento"):
        fecha = _parse_datetime(payload["fecha_nacimiento"])
        if fecha:
            anio_nacimiento = fecha.year
    if edad is None:
        edad = _coerce_int(payload.get("edad_aproximada"))
    if edad is None:
        edad = _infer_age_from_birth_year(anio_nacimiento)
    if anio_nacimiento is None:
        anio_nacimiento = _infer_birth_year_from_age(edad)
    rango_etario = rango_etario or metadata_rango or _compute_age_group(edad)

    submitted_at = submitted_override or datetime.now(timezone.utc)

    respuesta = EncRespuesta(
        encuesta_id=encuesta.id,
        tenant_id=tenant_id,
        huella_unica=fingerprint,
        user_id=user_id,
        dni=dni,
        phone=phone,
        ip=ip,
        ua=request_ctx.get("user_agent"),
        lat=lat,
        lng=lng,
        utm_source=payload.get("utm_source"),
        utm_campaign=payload.get("utm_campaign"),
        canal=canal,
        genero=genero,
        edad=edad,
        anio_nacimiento=anio_nacimiento,
        rango_etario=rango_etario,
        barrio=barrio,
        ciudad=ciudad,
        provincia=provincia,
        pais=pais,
        metadata_payload=metadata_payload,
        submitted_at=submitted_at,
        content_hash=None,
    )

    try:
        respuesta = _persist_respuesta_entity(
            respuesta,
            detalles,
            commit=False,
            expected_structure_revision=expected_structure_revision,
        )
    except EncuestaError as exc:
        if commit:
            db.session.rollback()
        if (
            submission_id is not None
            and submission_payload_hash is not None
            and str((exc.payload or {}).get("reason_code") or "")
            == "survey_response_duplicate"
        ):
            replay = _resolve_survey_response_receipt(
                encuesta,
                submission_id,
                submission_payload_hash,
            )
            if replay is not None:
                return replay
        raise

    receipt: Optional[SurveyResponseReceipt] = None
    if submission_id is not None and submission_payload_hash is not None:
        receipt = SurveyResponseReceipt(
            tenant_id=tenant_id,
            survey_id=encuesta.id,
            response_id=respuesta.id,
            submission_id_hash=_survey_submission_id_hash(tenant_id, submission_id),
            payload_hash=submission_payload_hash,
            canonical_version=SURVEY_RESPONSE_CANONICAL_VERSION,
            instrument_revision=expected_structure_revision,
            contract_version=SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION,
        )
        db.session.add(receipt)

    try:
        from services.survey_response_effects import stage_survey_response_effects

        stage_survey_response_effects(
            encuesta,
            respuesta,
            slug_publico=slug_publico,
            respuestas_payload=respuestas_payload,
            authenticated_user=authenticated_response_user,
            grant_reward=grant_reward,
            emit_realtime_update=emit_realtime_update,
            stage_analytics=True,
        )
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except IntegrityError as exc:
        if commit:
            db.session.rollback()
        if submission_id is not None and submission_payload_hash is not None:
            replay = _resolve_survey_response_receipt(
                encuesta,
                submission_id,
                submission_payload_hash,
            )
            if replay is not None:
                return replay
            current_app.logger.exception(
                "[encuestas] Error al guardar recibo idempotente para encuesta %s",
                encuesta.id,
            )
            raise EncuestaError(
                "No se pudo confirmar el recibo de la respuesta",
                status_code=500,
                payload={
                    "contract_version": SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION,
                    "reason_code": "survey_submission_receipt_failed",
                    "retryable": True,
                    "action_hint": "retry_same_submission_id",
                },
            ) from exc
        raise EncuestaError("No se pudo confirmar la respuesta durable", status_code=500) from exc
    except Exception:
        if commit:
            db.session.rollback()
        raise

    if receipt is not None and submission_id is not None:
        respuesta = _mark_survey_response_receipt(
            respuesta,
            receipt,
            submission_id,
            replayed=False,
        )
    # Unmapped response metadata lets every transport acknowledge the exact
    # instrument accepted without exposing the administrative structure guard.
    respuesta.instrument_revision = expected_structure_revision

    current_app.logger.info(
        "[encuestas] Nueva respuesta %s para encuesta %s desde %s",
        respuesta.id,
        encuesta.id,
        ip,
    )

    if commit:
        try:
            from services.survey_response_effects import dispatch_survey_response_effects

            dispatch_survey_response_effects(response_id=respuesta.id, limit=3)
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "[encuestas] Efectos post-commit pendientes para respuesta %s",
                respuesta.id,
            )

    return respuesta


def emit_survey_response_update(
    encuesta: EncEncuesta,
    slug_publico: str,
    *,
    event_envelope: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Emit the canonical post-commit live result update for one survey.

    ``event_envelope`` carries the durable outbox event identity.  It is nested
    under ``event`` so a retry cannot overwrite the live-results contract, and
    only the non-sensitive canonical fields are forwarded to clients.
    """

    if not encuesta.mostrar_resultados_envivo or not emit_survey_update:
        return False
    try:
        tenant_id = encuesta.tenant_id
        public_slug = _resolve_public_slug(encuesta) or encuesta.slug or slug_publico
        tenant_slug = None
        if tenant_id:
            tenant_profile = db.session.get(TenantProfile, tenant_id)
            tenant_slug = getattr(tenant_profile, "slug", None)
        try:
            from services.encuestas_analytics_service import calculate_live_results

            live_stats = calculate_live_results(
                public_slug,
                preferred_tenant_id=tenant_id,
                include_heatmap=True,
            )
            live_stats["legacy_results"] = _compute_live_results(encuesta)
        except Exception:
            current_app.logger.exception(
                "[encuestas] Error calculando live-results v2 para socket; se emite contrato legacy"
            )
            live_stats = _compute_live_results(encuesta)
        if event_envelope is not None and isinstance(live_stats, dict):
            safe_event: Dict[str, Any] = {}
            for field in (
                "contract_version",
                "event_id",
                "event_name",
                "tenant_id",
                "survey_id",
                "response_id",
                "slug",
            ):
                value = event_envelope.get(field)
                if value is not None:
                    safe_event[field] = value
            if safe_event:
                live_stats["event"] = safe_event

        emit_slugs = [public_slug, slug_publico]
        for emit_slug in dict.fromkeys(str(item).strip() for item in emit_slugs if item):
            emit_survey_update(emit_slug, live_stats, tenant_slug=tenant_slug)
        return True
    except Exception:
        current_app.logger.exception("[encuestas] Error al emitir update socket")
        return False


def _ensure_timezone(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_encuesta_activa(encuesta: EncEncuesta) -> bool:
    return encuesta.esta_activa()


def _pick_seed_submitted_at(
    rng: random.Random,
    *,
    scenario: str,
    now: datetime,
) -> datetime:
    """Build realistic timestamps for demo seeds.

    ``realtime`` concentrates activity in the last hours so live dashboards look
    active during demos; ``balanced`` keeps a broader 30-day spread.
    """

    normalized = (scenario or "balanced").strip().lower()
    if normalized == "realtime":
        roll = rng.random()
        if roll < 0.7:
            return now - timedelta(minutes=rng.randint(0, 120))
        if roll < 0.95:
            return now - timedelta(hours=rng.randint(2, 24), minutes=rng.randint(0, 59))
        return now - timedelta(days=rng.randint(1, 7), hours=rng.randint(0, 23))

    return now - timedelta(days=rng.randint(0, 28), minutes=rng.randint(0, 1440))


def _seed_weighted_choice(rng: random.Random, options: Sequence[str], weights: Sequence[float]) -> str:
    if not options:
        return ""
    return str(rng.choices(list(options), weights=list(weights), k=1)[0])


def seed_encuesta_respuestas_demo(
    encuesta_id: int,
    user: Any,
    cantidad: int = 100,
    *,
    geo_profile_key: Optional[str] = None,
    municipality_label: Optional[str] = None,
    seed: Optional[int] = None,
    reset_data: bool = False,
    scenario: str = "balanced",
) -> Dict[str, Any]:
    if cantidad <= 0:
        raise EncuestaError("Debe solicitar al menos una respuesta demo")

    encuesta = get_encuesta(encuesta_id, user=user)
    encuesta = _acquire_encuesta_write_guard(encuesta_id)
    _ensure_tenant_access(encuesta, user)
    _validate_persisted_instrument(encuesta)
    tenant_id = encuesta.tenant_id
    geo_metadata = _resolve_geo_metadata_for_tenant(tenant_id)
    if not geo_metadata and geo_profile_key:
        geo_metadata = _resolve_geo_metadata(profile_key=geo_profile_key)
    if not geo_metadata and municipality_label:
        geo_metadata = _resolve_geo_metadata(municipality=municipality_label)
    reset_summary: Optional[Dict[str, int]] = None
    if reset_data:
        reset_summary = _reset_encuesta_demo_data(encuesta)
        encuesta = _acquire_encuesta_write_guard(encuesta_id)
        _ensure_tenant_access(encuesta, user)
        _validate_persisted_instrument(encuesta)

    expected_structure_revision = int(encuesta.structure_revision or 1)

    rng = random.Random(seed)

    location_question = None
    otros_option = None
    standard_location_options: List[EncOpcion] = []
    for pregunta in encuesta.preguntas:
        if pregunta.tipo == "opcion_unica":
            for opcion in pregunta.opciones:
                if opcion.valor == "geo_autocomplete":
                    location_question = pregunta
                    otros_option = opcion
                    standard_location_options = [
                        opt for opt in pregunta.opciones if opt.id != opcion.id
                    ]
                    break
            if location_question:
                break

    comentarios = [
        "Gracias por escucharnos",
        "Sería bueno reforzar la iluminación en mi cuadra",
        "Excelente iniciativa para planificar mejoras",
        "Ojalá sigan estas encuestas participativas",
        "Necesitamos más controles y presencia ciudadana",
        "La propuesta me parece clara y necesaria",
        "Necesitamos seguimiento y tableros públicos en tiempo real",
    ]
    generos = ["femenino", "masculino", "no_binario", None]

    scenario_normalized = (scenario or "balanced").strip().lower()
    if scenario_normalized not in {"balanced", "realtime"}:
        raise EncuestaError("Scenario inválido. Valores soportados: balanced, realtime")

    if scenario_normalized == "realtime":
        canales = ["web", "whatsapp", "presencial"]
        canales_weights = [0.62, 0.28, 0.10]
        utm_sources = ["web", "qr", "campana"]
        utm_source_weights = [0.58, 0.24, 0.18]
        utm_campaigns = ["debate", "territorio", "digital", "inversionistas"]
        utm_campaign_weights = [0.34, 0.26, 0.22, 0.18]
    else:
        canales = ["web", "whatsapp", "presencial"]
        canales_weights = [0.45, 0.35, 0.20]
        utm_sources = ["web", "qr", "campana"]
        utm_source_weights = [0.40, 0.35, 0.25]
        utm_campaigns = ["demo", "lanzamiento", "presentacion", "inversionistas"]
        utm_campaign_weights = [0.35, 0.30, 0.20, 0.15]

    barrios_catalogo = list((geo_metadata or {}).get("neighborhoods") or [])
    distritos_catalogo = list((geo_metadata or {}).get("districts") or [])
    posibles_barrios = barrios_catalogo + distritos_catalogo

    created = 0
    skipped = 0
    attempts = 0
    now = datetime.now(timezone.utc)
    demo_batch_id = f"seed-{encuesta.id}-{int(now.timestamp())}"
    analytics_counter = {
        "canales": Counter(),
        "utm_source": Counter(),
        "utm_campaign": Counter(),
        "barrios": Counter(),
    }
    dni_usados: set[str] = set()
    phone_usados: set[str] = set()
    fingerprints: set[str] = set()

    while created < cantidad and attempts < cantidad * 6:
        attempts += 1

        dni = f"{rng.randint(20000000, 49999999):08d}"
        if dni in dni_usados:
            continue
        phone = f"+549261{rng.randint(4000000, 9999999):07d}"
        if phone in phone_usados:
            continue

        genero = rng.choice(generos)
        edad = rng.randint(18, 72)
        anio_nacimiento = datetime.now(timezone.utc).year - edad
        rango_etario = _compute_age_group(edad)

        lat, lng, barrio_hint = _pick_geo_point(geo_metadata, rng)
        barrio_label = barrio_hint or (rng.choice(posibles_barrios) if posibles_barrios else None)

        if lat is None or lng is None:
            # Fallback coordinates around Gran Mendoza to asegurar mapa visible.
            lat = rng.uniform(-33.2, -32.8)
            lng = rng.uniform(-68.9, -68.3)

        submitted_at = _pick_seed_submitted_at(
            rng,
            scenario=scenario_normalized,
            now=now,
        )

        respuestas_items: List[Dict[str, Any]] = []
        visible_question_orders: set[int] = set()
        selected_orders_by_question_order: Dict[int, set[int]] = {}
        visible_question_refs: set[str] = set()
        selected_option_refs_by_question_ref: Dict[str, set[str]] = {}
        for pregunta in sorted(encuesta.preguntas, key=lambda item: item.orden):
            visible, _, _ = _is_conditional_question_visible(
                pregunta,
                visible_question_orders=visible_question_orders,
                selected_orders_by_question_order=selected_orders_by_question_order,
                visible_question_refs=visible_question_refs,
                selected_option_refs_by_question_ref=(
                    selected_option_refs_by_question_ref
                ),
            )
            if not visible:
                continue
            visible_question_orders.add(pregunta.orden)
            if pregunta.logical_ref is not None:
                visible_question_refs.add(pregunta.logical_ref)

            if pregunta is location_question:
                opcion_ids: List[int] = []
                if standard_location_options and rng.random() > 0.2:
                    opcion = rng.choice(standard_location_options)
                    opcion_ids = [opcion.id]
                    barrio_label = barrio_label or opcion.texto
                elif otros_option is not None:
                    opcion_ids = [otros_option.id]
                    if not barrio_label:
                        barrio_label = f"Barrio sin registrar {rng.randint(1, 90)}"
                elif pregunta.opciones:
                    opcion = rng.choice(pregunta.opciones)
                    opcion_ids = [opcion.id]
                    barrio_label = barrio_label or opcion.texto
                if opcion_ids:
                    respuestas_items.append({"pregunta_id": pregunta.id, "opcion_ids": opcion_ids})
                    selected_orders_by_question_order[pregunta.orden] = {
                        opcion.orden
                        for opcion in pregunta.opciones
                        if opcion.id in opcion_ids
                    }
                    if pregunta.logical_ref is not None:
                        selected_option_refs_by_question_ref[
                            pregunta.logical_ref
                        ] = {
                            opcion.logical_ref
                            for opcion in pregunta.opciones
                            if opcion.id in opcion_ids
                            and opcion.logical_ref is not None
                        }
                else:
                    selected_orders_by_question_order[pregunta.orden] = set()
                    if pregunta.logical_ref is not None:
                        selected_option_refs_by_question_ref[
                            pregunta.logical_ref
                        ] = set()
                continue

            if pregunta.tipo in {"opcion_unica", "rating_emoji"} and pregunta.opciones:
                opcion = rng.choice(pregunta.opciones)
                respuestas_items.append({"pregunta_id": pregunta.id, "opcion_ids": [opcion.id]})
                selected_orders_by_question_order[pregunta.orden] = {opcion.orden}
                if pregunta.logical_ref is not None:
                    selected_option_refs_by_question_ref[
                        pregunta.logical_ref
                    ] = (
                        {opcion.logical_ref}
                        if opcion.logical_ref is not None
                        else set()
                    )
                continue

            if pregunta.tipo == "opcion_multiple" and pregunta.opciones:
                opciones = list(pregunta.opciones)
                max_sel = pregunta.max_selecciones or len(opciones)
                max_sel = min(max_sel, len(opciones))
                min_sel = pregunta.min_selecciones or (1 if pregunta.obligatoria else 0)
                min_sel = max(0, min_sel)
                if max_sel <= 0:
                    selected_orders_by_question_order[pregunta.orden] = set()
                    if pregunta.logical_ref is not None:
                        selected_option_refs_by_question_ref[
                            pregunta.logical_ref
                        ] = set()
                    continue
                cantidad_sel = rng.randint(max(1, min_sel), max_sel)
                seleccionadas = rng.sample(opciones, k=cantidad_sel)
                respuestas_items.append({
                    "pregunta_id": pregunta.id,
                    "opcion_ids": [op.id for op in seleccionadas],
                })
                selected_orders_by_question_order[pregunta.orden] = {
                    opcion.orden for opcion in seleccionadas
                }
                if pregunta.logical_ref is not None:
                    selected_option_refs_by_question_ref[
                        pregunta.logical_ref
                    ] = {
                        opcion.logical_ref
                        for opcion in seleccionadas
                        if opcion.logical_ref is not None
                    }
                continue

            texto = rng.choice(comentarios)
            respuestas_items.append({
                "pregunta_id": pregunta.id,
                "texto_libre": texto,
            })
            selected_orders_by_question_order[pregunta.orden] = set()
            if pregunta.logical_ref is not None:
                selected_option_refs_by_question_ref[pregunta.logical_ref] = set()

        if not respuestas_items:
            skipped += 1
            continue

        payload_data = {
            "dni": dni,
            "phone": phone,
            "respuestas": respuestas_items,
            "genero": genero,
            "edad": edad,
            "anio_nacimiento": anio_nacimiento,
            "rango_etario": rango_etario,
            "lat": lat,
            "lng": lng,
            "barrio": barrio_label,
            "ciudad": (geo_metadata or {}).get("municipality"),
            "provincia": (geo_metadata or {}).get("state"),
            "pais": (geo_metadata or {}).get("country"),
            "utm_source": _seed_weighted_choice(rng, utm_sources, utm_source_weights),
            "utm_campaign": _seed_weighted_choice(rng, utm_campaigns, utm_campaign_weights),
            "canal": _seed_weighted_choice(rng, canales, canales_weights),
        }

        request_ctx = {
            "ip": f"10.0.0.{rng.randint(1, 254)}",
            "anon_id": f"seed-{encuesta.id}-{attempts}",
            "user_agent": "demo-seed",
            "canal": payload_data["canal"],
        }

        try:
            detalles = _validate_respuesta_payload(encuesta, payload_data["respuestas"])
        except EncuestaError:
            skipped += 1
            continue

        fingerprint = build_unique_fingerprint(
            encuesta,
            tenant_id,
            dni=dni,
            phone=phone,
            ip=request_ctx["ip"],
            anon_cookie=request_ctx["anon_id"],
        )

        if fingerprint:
            if fingerprint in fingerprints:
                skipped += 1
                continue
            existing = EncRespuesta.query.filter_by(
                encuesta_id=encuesta.id,
                huella_unica=fingerprint,
            ).first()
            if existing:
                skipped += 1
                continue

        respuesta = EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=tenant_id,
            metadata_payload={
                "is_demo_seed": True,
                "demo_batch_id": demo_batch_id,
                "demo_scenario": scenario_normalized,
            },
            huella_unica=fingerprint,
            dni=dni,
            phone=phone,
            ip=request_ctx["ip"],
            ua=request_ctx.get("user_agent"),
            lat=payload_data["lat"],
            lng=payload_data["lng"],
            utm_source=payload_data["utm_source"],
            utm_campaign=payload_data["utm_campaign"],
            canal=payload_data["canal"],
            genero=_normalize_genero(payload_data.get("genero")),
            edad=payload_data.get("edad"),
            anio_nacimiento=payload_data.get("anio_nacimiento"),
            rango_etario=payload_data.get("rango_etario"),
            barrio=_clean_str(payload_data.get("barrio"), max_length=120),
            ciudad=_clean_str(payload_data.get("ciudad"), max_length=120),
            provincia=_clean_str(payload_data.get("provincia"), max_length=120),
            pais=_clean_str(payload_data.get("pais"), max_length=120),
            submitted_at=submitted_at,
        )

        try:
            _persist_respuesta_entity(
                respuesta,
                detalles,
                expected_structure_revision=expected_structure_revision,
            )
        except EncuestaError as exc:
            if (exc.payload or {}).get("reason_code") in {
                "survey_concurrent_update",
                "survey_structure_changed",
            }:
                raise
            skipped += 1
            continue

        dni_usados.add(dni)
        phone_usados.add(phone)
        if fingerprint:
            fingerprints.add(fingerprint)
        created += 1
        analytics_counter["canales"][payload_data["canal"]] += 1
        analytics_counter["utm_source"][payload_data["utm_source"]] += 1
        analytics_counter["utm_campaign"][payload_data["utm_campaign"]] += 1
        if payload_data.get("barrio"):
            analytics_counter["barrios"][payload_data["barrio"]] += 1

    current_app.logger.info(
        "[encuestas] Seed demo agregó %s respuestas a la encuesta %s (saltadas=%s)",
        created,
        encuesta.id,
        skipped,
    )

    if created == 0:
        # Release the lock-only transaction.  The durable structure marker is
        # intentionally written only when at least one response commits.
        db.session.rollback()

    return {
        "encuesta_id": encuesta.id,
        "creadas": created,
        "omitidas": skipped,
        "objetivo": cantidad,
        "seed": seed,
        "scenario": scenario_normalized,
        "reset": reset_summary,
        "demo_batch_id": demo_batch_id,
        "analytics_preview": {
            "canales": dict(analytics_counter["canales"]),
            "utm_source": dict(analytics_counter["utm_source"]),
            "utm_campaign": dict(analytics_counter["utm_campaign"]),
            "top_barrios": [
                {"label": label, "value": value}
                for label, value in analytics_counter["barrios"].most_common(5)
            ],
        },
    }


def _reset_encuesta_demo_data(encuesta: EncEncuesta) -> Dict[str, int]:
    respuesta_ids = [
        respuesta_id
        for (respuesta_id,) in (
            db.session.query(EncRespuesta.id)
            .filter_by(encuesta_id=encuesta.id)
            .all()
        )
    ]
    respuestas_count = len(respuesta_ids)
    comentarios_count = (
        db.session.query(EncComentario.id)
        .filter_by(encuesta_id=encuesta.id)
        .count()
    )

    if respuesta_ids:
        (
            db.session.query(EncRespuestaDetalle)
            .filter(EncRespuestaDetalle.respuesta_id.in_(respuesta_ids))
            .delete(synchronize_session=False)
        )
        (
            db.session.query(EncRespuesta)
            .filter(EncRespuesta.id.in_(respuesta_ids))
            .delete(synchronize_session=False)
        )

    db.session.query(EncComentario).filter_by(encuesta_id=encuesta.id).delete(
        synchronize_session=False
    )
    db.session.commit()

    current_app.logger.info(
        "[encuestas] Reset demo datos encuesta %s (respuestas=%s comentarios=%s)",
        encuesta.id,
        respuestas_count,
        comentarios_count,
    )

    return {
        "respuestas": respuestas_count,
        "comentarios": comentarios_count,
    }


def _collect_recent_geo_points(
    encuestas: Sequence[EncEncuesta],
    limit_per_encuesta: int = 200,
) -> Dict[int, List[Dict[str, Any]]]:
    encuesta_ids = [encuesta.id for encuesta in encuestas if encuesta.id]
    if not encuesta_ids:
        return {}

    max_rows = limit_per_encuesta * len(encuesta_ids)
    query = (
        EncRespuesta.query.options(
            load_only(
                EncRespuesta.encuesta_id,
                EncRespuesta.lat,
                EncRespuesta.lng,
                EncRespuesta.barrio,
                EncRespuesta.ciudad,
                EncRespuesta.provincia,
                EncRespuesta.submitted_at,
            )
        )
        .filter(EncRespuesta.encuesta_id.in_(encuesta_ids))
        .filter(EncRespuesta.lat.isnot(None))
        .filter(EncRespuesta.lng.isnot(None))
        .order_by(EncRespuesta.submitted_at.desc(), EncRespuesta.id.desc())
        .limit(max_rows)
    )

    points: Dict[int, List[Dict[str, Any]]] = {encuesta_id: [] for encuesta_id in encuesta_ids}
    for respuesta in query:
        bucket = points.get(respuesta.encuesta_id)
        if bucket is None:
            continue
        if len(bucket) >= limit_per_encuesta:
            continue
        bucket.append(
            {
                "lat": respuesta.lat,
                "lng": respuesta.lng,
                "barrio": respuesta.barrio,
                "ciudad": respuesta.ciudad,
                "provincia": respuesta.provincia,
                "submitted_at": respuesta.submitted_at.isoformat()
                if respuesta.submitted_at
                else None,
            }
        )
    return points


def _collect_admin_panel_stats(
    encuestas: Sequence[EncEncuesta],
) -> Dict[int, Dict[str, Any]]:
    encuesta_ids = [encuesta.id for encuesta in encuestas if encuesta.id]
    if not encuesta_ids:
        return {}

    raw_stats = {
        encuesta_id: {
            "total_respuestas": 0,
            "respuestas_ultimas_24h": 0,
            "respuestas_con_coordenadas": 0,
            "participantes_unicos": set(),
            "ultima_respuesta_at": None,
            "canales": Counter(),
            "utm": Counter(),
        }
        for encuesta_id in encuesta_ids
    }

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    respuestas = (
        EncRespuesta.query.options(
            load_only(
                EncRespuesta.id,
                EncRespuesta.encuesta_id,
                EncRespuesta.submitted_at,
                EncRespuesta.lat,
                EncRespuesta.lng,
                EncRespuesta.huella_unica,
                EncRespuesta.user_id,
                EncRespuesta.dni,
                EncRespuesta.phone,
                EncRespuesta.ip,
                EncRespuesta.canal,
                EncRespuesta.utm_source,
                EncRespuesta.utm_campaign,
            )
        )
        .filter(EncRespuesta.encuesta_id.in_(encuesta_ids))
        .all()
    )

    for respuesta in respuestas:
        stats = raw_stats.get(respuesta.encuesta_id)
        if not stats:
            continue

        stats["total_respuestas"] += 1

        submitted_at = respuesta.submitted_at
        if submitted_at is not None:
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
            else:
                submitted_at = submitted_at.astimezone(timezone.utc)
        if submitted_at and submitted_at >= cutoff:
            stats["respuestas_ultimas_24h"] += 1
        if submitted_at and (
            stats["ultima_respuesta_at"] is None
            or submitted_at > stats["ultima_respuesta_at"]
        ):
            stats["ultima_respuesta_at"] = submitted_at

        if respuesta.lat is not None and respuesta.lng is not None:
            stats["respuestas_con_coordenadas"] += 1

        fingerprint = (
            respuesta.huella_unica
            or (respuesta.user_id and f"user:{respuesta.user_id}")
            or (
                respuesta.dni
                and respuesta.dni.strip()
                and f"dni:{respuesta.dni.strip()}"
            )
            or (
                respuesta.phone
                and respuesta.phone.strip()
                and f"phone:{respuesta.phone.strip()}"
            )
            or (respuesta.ip and f"ip:{respuesta.ip}")
        )
        stats["participantes_unicos"].add(
            fingerprint or f"anon:{respuesta.encuesta_id}:{respuesta.id}"
        )

        canal = respuesta.canal or "sin_canal"
        stats["canales"][canal] += 1
        utm_key = (respuesta.utm_source or "n/a", respuesta.utm_campaign or "n/a")
        stats["utm"][utm_key] += 1

    result: Dict[int, Dict[str, Any]] = {}
    for encuesta_id, data in raw_stats.items():
        ultima_dt = data["ultima_respuesta_at"]
        ultima = _ensure_timezone(ultima_dt).isoformat() if ultima_dt else None
        result[encuesta_id] = {
            "total_respuestas": data["total_respuestas"],
            "respuestas_ultimas_24h": data["respuestas_ultimas_24h"],
            "respuestas_con_coordenadas": data["respuestas_con_coordenadas"],
            "participantes_unicos": len(data["participantes_unicos"]),
            "ultima_respuesta_at": ultima,
            "canales": {canal: count for canal, count in data["canales"].items()},
            "utm": [
                {
                    "utm_source": source,
                    "utm_campaign": campaign,
                    "conteo": count,
                }
                for (source, campaign), count in sorted(
                    data["utm"].items(), key=lambda item: item[1], reverse=True
                )
            ],
        }

    return result


def _empty_panel_metrics() -> Dict[str, Any]:
    return {
        "total_respuestas": 0,
        "respuestas_ultimas_24h": 0,
        "respuestas_con_coordenadas": 0,
        "participantes_unicos": 0,
        "ultima_respuesta_at": None,
        "canales": {},
        "utm": [],
    }


def build_admin_list_payload(
    encuestas: Sequence[EncEncuesta],
) -> Dict[str, Any]:
    stats_map = _collect_admin_panel_stats(encuestas)
    geo_points = _collect_recent_geo_points(encuestas)
    encuestas_payload: List[Dict[str, Any]] = []
    estados = Counter()
    total_respuestas = 0
    total_geo = 0
    total_24h = 0
    activas = 0
    con_respuestas = 0

    seed_profiles_map = _geo_catalog()
    for encuesta in encuestas:
        data = serialize_encuesta(encuesta)
        metricas = stats_map.get(encuesta.id or -1, _empty_panel_metrics())
        data["metricas"] = metricas
        data["esta_activa"] = _is_encuesta_activa(encuesta)
        data["slug_publico"] = _resolve_public_slug(encuesta)
        geo_metadata = _resolve_geo_metadata_for_tenant(encuesta.tenant_id)
        data["geo"] = {
            "points": geo_points.get(encuesta.id or -1, []),
            "bounds": geo_metadata.get("bounds") if geo_metadata else None,
            "center": geo_metadata.get("center") if geo_metadata else None,
        }

        auto_seed_cfg = _get_auto_seed_config(encuesta) or {}
        if not auto_seed_cfg:
            default_geo, default_municipality = _guess_auto_seed_defaults(
                municipality_label=None,
                slug_hint=encuesta.slug,
                tenant_id=encuesta.tenant_id,
            )
            auto_seed_cfg = {
                "cantidad": 100,
                "geo_profile_key": default_geo,
                "municipality_label": default_municipality,
                "label": _AUTO_SEED_DEFAULT_LABEL,
            }
        data["seed_demo"] = {
            "label": auto_seed_cfg.get("label") or _AUTO_SEED_DEFAULT_LABEL,
            "cantidad": auto_seed_cfg.get("cantidad", 100),
            "geo_profile_key": auto_seed_cfg.get("geo_profile_key"),
            "municipality_label": auto_seed_cfg.get("municipality_label"),
            "endpoint": f"/api/encuestas/{encuesta.id}/seed-demo",
        }
        encuestas_payload.append(data)

        estados[encuesta.estado] += 1
        total_respuestas += metricas["total_respuestas"]
        total_geo += metricas["respuestas_con_coordenadas"]
        total_24h += metricas["respuestas_ultimas_24h"]
        if metricas["total_respuestas"] > 0:
            con_respuestas += 1
        if data["esta_activa"]:
            activas += 1

    resumen = {
        "total": len(encuestas_payload),
        "por_estado": dict(estados),
        "activas": activas,
        "con_respuestas": con_respuestas,
        "total_respuestas": total_respuestas,
        "respuestas_con_coordenadas": total_geo,
        "respuestas_ultimas_24h": total_24h,
    }

    seed_profiles = []
    for key, entry in (seed_profiles_map or {}).items():
        if not isinstance(entry, dict):
            continue
        seed_profiles.append(
            {
                "key": key,
                "municipality": entry.get("municipality"),
                "state": entry.get("state"),
                "country": entry.get("country"),
                "label": entry.get("municipality")
                or entry.get("label")
                or key.replace("_", " ").title(),
                "bounds": entry.get("bounds"),
                "center": entry.get("center"),
            }
        )

    seed_defaults = {
        "label": _AUTO_SEED_DEFAULT_LABEL,
        "cantidad": 100,
        "geo_profile_key": seed_profiles[0]["key"] if seed_profiles else None,
        "municipality_label": seed_profiles[0]["municipality"] if seed_profiles else None,
    }

    return {
        "encuestas": encuestas_payload,
        "resumen": resumen,
        "seed_demo": {"defaults": seed_defaults, "profiles": seed_profiles},
    }


def serialize_respuesta(respuesta: EncRespuesta) -> Dict[str, Any]:
    detalles_serializados: List[Dict[str, Any]] = []
    for detalle in respuesta.detalles:
        pregunta = detalle.pregunta
        opcion = detalle.opcion
        detalles_serializados.append(
            {
                "pregunta_id": detalle.pregunta_id,
                "pregunta_texto": pregunta.texto if pregunta else None,
                "pregunta_orden": pregunta.orden if pregunta else None,
                "pregunta_tipo": pregunta.tipo if pregunta else None,
                "opcion_id": detalle.opcion_id,
                "opcion_texto": opcion.texto if opcion else None,
                "texto_libre": detalle.texto_libre,
            }
        )

    return {
        "id": respuesta.id,
        "encuesta_id": respuesta.encuesta_id,
        "submitted_at": respuesta.submitted_at.isoformat() if respuesta.submitted_at else None,
        "canal": respuesta.canal,
        "utm_source": respuesta.utm_source,
        "utm_campaign": respuesta.utm_campaign,
        "dni": respuesta.dni,
        "phone": respuesta.phone,
        "ip": respuesta.ip,
        "lat": respuesta.lat,
        "lng": respuesta.lng,
        "user_id": respuesta.user_id,
        "genero": respuesta.genero,
        "edad": respuesta.edad,
        "anio_nacimiento": respuesta.anio_nacimiento,
        "rango_etario": respuesta.rango_etario,
        "barrio": respuesta.barrio,
        "ciudad": respuesta.ciudad,
        "provincia": respuesta.provincia,
        "pais": respuesta.pais,
        "metadata": respuesta.metadata_payload,
        "detalles": detalles_serializados,
    }


def list_respuestas(
    encuesta_id: int,
    user: Any,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Tuple[EncEncuesta, List[EncRespuesta], int, int, int]:
    encuesta = get_encuesta(encuesta_id, user=user)

    try:
        limit_value = int(limit) if limit is not None else 50
    except (TypeError, ValueError) as exc:
        raise EncuestaError("Parámetro 'limit' inválido") from exc
    if limit_value <= 0:
        raise EncuestaError("El parámetro 'limit' debe ser mayor a 0")
    limit_value = min(limit_value, 200)

    try:
        offset_value = int(offset) if offset is not None else 0
    except (TypeError, ValueError) as exc:
        raise EncuestaError("Parámetro 'offset' inválido") from exc
    if offset_value < 0:
        raise EncuestaError("El parámetro 'offset' no puede ser negativo")

    total = EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count()

    query = (
        EncRespuesta.query.options(
            joinedload(EncRespuesta.detalles).joinedload(EncRespuestaDetalle.pregunta),
            joinedload(EncRespuesta.detalles).joinedload(EncRespuestaDetalle.opcion),
        )
        .filter_by(encuesta_id=encuesta.id)
        .order_by(EncRespuesta.submitted_at.desc(), EncRespuesta.id.desc())
        .offset(offset_value)
        .limit(limit_value)
    )

    respuestas = query.all()
    return encuesta, respuestas, total, limit_value, offset_value


def _collect_encuesta_tags(encuesta: EncEncuesta) -> List[str]:
    tags: List[str] = []
    seen: set = set()
    ordered_segmentos = sorted(
        encuesta.segmentos,
        key=lambda segmento: (str(segmento.valor or "").lower(), segmento.id or 0),
    )
    for segmento in ordered_segmentos:
        if segmento.clave != "tag":
            continue
        value = _clean_str(segmento.valor, max_length=120)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(value)
    return tags


def serialize_encuesta(encuesta: EncEncuesta) -> Dict[str, Any]:
    def _serialize_question(pregunta: EncPregunta) -> Dict[str, Any]:
        response_type, internal_type = _map_pregunta_tipo_for_response(pregunta.tipo)
        question_payload = {
            "id": pregunta.id,
            "question_ref": pregunta.logical_ref,
            "orden": pregunta.orden,
            "tipo": response_type,
            "type": response_type,
            "tipo_interno": internal_type,
            "texto": pregunta.texto,
            "obligatoria": pregunta.obligatoria,
            "min_selecciones": pregunta.min_selecciones,
            "max_selecciones": pregunta.max_selecciones,
            "conditional_logic": deepcopy(pregunta.logica_condicional),
            "opciones": [
                {
                    "id": opcion.id,
                    "option_ref": opcion.logical_ref,
                    "orden": opcion.orden,
                    "texto": opcion.texto,
                    "valor": opcion.valor,
                }
                for opcion in pregunta.opciones
            ],
        }
        return question_payload

    slug_publico = _resolve_public_slug(encuesta)
    url_publica = _public_url_for_slug(slug_publico)

    return {
        "id": encuesta.id,
        "tenant_id": encuesta.tenant_id,
        "document_ref": encuesta.document_ref,
        "slug": encuesta.slug,
        "slug_publico": slug_publico,
        "canonical_slug": slug_publico or encuesta.slug,
        "url_publica": url_publica,
        "share_url": url_publica,
        "public_api_endpoint": _public_api_endpoint_for_slug(slug_publico),
        "titulo": encuesta.titulo,
        "descripcion": encuesta.descripcion,
        "tipo": encuesta.tipo,
        "estado": encuesta.estado,
        "inicio_at": encuesta.inicio_at.isoformat() if encuesta.inicio_at else None,
        "fin_at": encuesta.fin_at.isoformat() if encuesta.fin_at else None,
        "puntos_recompensa": encuesta.puntos_recompensa or 0,
        "requiere_identidad": encuesta.requiere_identidad,
        "requiere_datos_contacto": encuesta.requiere_identidad,
        "politica_unicidad": encuesta.politica_unicidad,
        "anonimo_permitido": encuesta.anonimo_permitido,
        "anonimato": encuesta.anonimo_permitido,
        "es_votacion_envivo": encuesta.es_votacion_envivo,
        "mostrar_resultados_envivo": encuesta.mostrar_resultados_envivo,
        "permitir_comentarios": encuesta.permitir_comentarios,
        "structure_guard": {
            "contract_version": "surveys.structure_guard.v1",
            "revision": int(encuesta.structure_revision or 1),
            "locked": _survey_structure_is_locked(encuesta),
            "locked_at": (
                encuesta.structure_locked_at.isoformat()
                if encuesta.structure_locked_at is not None
                else None
            ),
        },
        "tags": _collect_encuesta_tags(encuesta),
        "preguntas": [_serialize_question(pregunta) for pregunta in encuesta.preguntas],
    }


def serialize_public_encuesta(encuesta: EncEncuesta, slug_publico: Optional[str] = None) -> Dict[str, Any]:
    data = serialize_encuesta(encuesta)
    # Optimistic-lock metadata is an administrative editing contract and is
    # never part of the public participation surface.
    data.pop("structure_guard", None)
    data["instrument_revision"] = int(encuesta.structure_revision or 1)
    uniqueness_policy = str(encuesta.politica_unicidad or "libre").strip().lower()
    authentication_required = (
        uniqueness_policy in _AUTHENTICATED_USER_POLICIES
        or not bool(encuesta.anonimo_permitido)
    )
    rewards_available = int(getattr(encuesta, "puntos_recompensa", 0) or 0) > 0
    data["auth_mode"] = (
        "required"
        if authentication_required
        else "optional"
        if rewards_available
        else "anonymous"
    )
    data["frontend_contract"] = {
        **(data.get("frontend_contract") if isinstance(data.get("frontend_contract"), dict) else {}),
        "auth_mode": data["auth_mode"],
        "identity": {
            "mode": data["auth_mode"],
            "provider": "chatboc_session",
        },
        "idempotency": {
            "contract_version": SURVEY_RESPONSE_RECEIPT_CONTRACT_VERSION,
            "supported": True,
            "required": True,
            "required_for_exactly_once": True,
            "header": "Idempotency-Key",
            "body_field": "submission_id",
            "min_length": 8,
            "max_length": 128,
            "accepted_status": 201,
            "replay_status": 200,
        },
    }
    canonical_slug = _resolve_public_slug(encuesta) or encuesta.slug
    requested_slug = slug_publico or canonical_slug
    data["slug"] = requested_slug
    data["slug_publico"] = canonical_slug
    data["canonical_slug"] = canonical_slug
    data["requested_slug"] = requested_slug
    data["slug_alias_used"] = bool(requested_slug and canonical_slug and requested_slug != canonical_slug)
    data["url_publica"] = _public_url_for_slug(canonical_slug)
    data["share_url"] = data["url_publica"]
    share_text = (
        f"Participa en {data.get('titulo') or 'esta encuesta'}: {data['url_publica']}"
        if data.get("url_publica")
        else None
    )
    data["whatsapp_share_text"] = share_text
    data["whatsapp_share_url"] = (
        f"https://wa.me/?text={quote_plus(share_text)}" if share_text else None
    )
    data["share_whatsapp_url"] = data["whatsapp_share_url"]
    data["public_api_endpoint"] = _public_api_endpoint_for_slug(canonical_slug)
    # Public payload hides estado and flags not needed
    data.pop("estado", None)

    if encuesta.mostrar_resultados_envivo:
        data["resultados_envivo"] = _compute_live_results(encuesta)

    if encuesta.permitir_comentarios:
        # Include recent comments or link to comments endpoint
        pass

    return data


def _compute_live_results(encuesta: EncEncuesta) -> Dict[str, Any]:
    """Aggregate results for live display."""
    results = {
        "total_respuestas": EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count(),
        "preguntas": {}
    }

    # Simple aggregation for closed questions
    for pregunta in encuesta.preguntas:
        if pregunta.tipo in ("opcion_unica", "opcion_multiple", "rating_emoji"):
            # Count details per option
            counts = (
                db.session.query(EncRespuestaDetalle.opcion_id, func.count(EncRespuestaDetalle.id))
                .filter(EncRespuestaDetalle.pregunta_id == pregunta.id)
                .group_by(EncRespuestaDetalle.opcion_id)
                .all()
            )
            opcion_counts = {oid: count for oid, count in counts if oid}

            opciones_data = []
            for opcion in pregunta.opciones:
                opciones_data.append({
                    "id": opcion.id,
                    "texto": opcion.texto,
                    "votos": opcion_counts.get(opcion.id, 0)
                })

            results["preguntas"][pregunta.id] = {
                "tipo": pregunta.tipo,
                "opciones": opciones_data
            }

    return results


def create_comentario(encuesta_id: int, payload: Dict[str, Any], user: Optional[User]) -> EncComentario:
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta or not encuesta.permitir_comentarios:
        raise EncuestaError("Comentarios no habilitados para esta encuesta", status_code=403)

    texto = (payload.get("texto") or "").strip()
    if not texto:
        raise EncuestaError("El comentario no puede estar vacío")

    comment_mode = str(payload.get("mode") or payload.get("comment_mode") or "").strip().lower()
    comment_mode = "social" if comment_mode == "social" else "anon"

    auth_provider = (
        payload.get("auth_provider")
        or payload.get("provider")
        or payload.get("social_provider")
    )
    auth_provider = str(auth_provider or "").strip().lower() or None
    if auth_provider and auth_provider not in {"facebook", "google", "instagram"}:
        auth_provider = None

    auth_user_id = str(payload.get("auth_user_id") or "").strip() or None
    auth_email = str(payload.get("auth_email") or "").strip() or None
    auth_first_name = str(payload.get("auth_first_name") or "").strip()
    auth_last_name = str(payload.get("auth_last_name") or "").strip()

    if comment_mode == "social":
        if not auth_provider:
            raise EncuestaError("Proveedor social requerido para comentarios registrados", status_code=400)
        if not (auth_user_id or auth_email or (user and getattr(user, "id", None))):
            raise EncuestaError("Identidad social incompleta para publicar comentario", status_code=400)

    display_name = (
        " ".join(part for part in [auth_first_name, auth_last_name] if part).strip()
        if comment_mode == "social"
        else ""
    )
    if not display_name:
        display_name = (payload.get("nombre") or payload.get("nombre_autor") or "").strip()
    if not display_name and comment_mode == "social":
        display_name = auth_email or (f"Usuario {auth_provider.title()}" if auth_provider else "")

    anon_id = payload.get("anon_id")
    if comment_mode == "social":
        social_identity = auth_user_id or auth_email or str(getattr(user, "id", "")).strip()
        safe_identity = re.sub(r"[^a-zA-Z0-9_\-@.]", "", social_identity or "")[:80]
        if safe_identity:
            anon_id = f"social:{auth_provider}:{safe_identity}"

    comentario = EncComentario(
        encuesta_id=encuesta.id,
        user_id=getattr(user, "id", None) if user else None,
        anon_id=anon_id,
        nombre_autor=display_name or None,
        texto=texto,
        estado="publicado"
    )

    db.session.add(comentario)
    db.session.commit()

    # Best-effort analytics instrumentation for comment UX funnel.
    if analytics_ingestor:
        try:
            mode_changed_from = str(payload.get("previous_mode") or payload.get("prev_mode") or "").strip().lower()
            base_payload = {
                "encuesta_id": encuesta.id,
                "slug": _resolve_public_slug(encuesta) or encuesta.slug,
                "comment_mode": comment_mode,
                "auth_provider": auth_provider,
                "has_user_id": bool(getattr(user, "id", None)),
            }
            if mode_changed_from and mode_changed_from in {"anon", "social"} and mode_changed_from != comment_mode:
                analytics_ingestor.track(
                    tenant_id=encuesta.tenant_id,
                    event_name="survey_comment_mode_changed",
                    payload={**base_payload, "from_mode": mode_changed_from, "to_mode": comment_mode},
                    user_id=getattr(user, "id", None),
                    anon_id=anon_id,
                    channel=payload.get("channel") or "public_survey",
                    tenant_type="municipio",
                    entity_ref=str(encuesta.id),
                )

            analytics_ingestor.track(
                tenant_id=encuesta.tenant_id,
                event_name="survey_comment_submitted",
                payload=base_payload,
                user_id=getattr(user, "id", None),
                anon_id=anon_id,
                channel=payload.get("channel") or "public_survey",
                tenant_type="municipio",
                entity_ref=str(encuesta.id),
            )
        except Exception:
            logger = _current_app_logger()
            if logger:
                logger.exception(
                    "[encuestas] analytics ingest failed for comment encuesta_id=%s",
                    encuesta.id,
                )

    # Emit live event
    if emit_survey_comment:
        try:
            slug_publico = _resolve_public_slug(encuesta) or encuesta.slug
            data = {
                "id": comentario.id,
                "texto": _safe_text_value(comentario.texto, fallback=""),
                "nombre_autor": _safe_text_value(comentario.nombre_autor or (user.name if user else "Anónimo"), fallback="Anónimo"),
                "fecha": comentario.created_at.isoformat(),
                "user_id": comentario.user_id
            }
            data.update(_comment_identity_payload(user))
            if isinstance(comentario.anon_id, str) and comentario.anon_id.startswith("social:"):
                parts = comentario.anon_id.split(":", 2)
                if len(parts) == 3:
                    data["comment_mode"] = "social"
                    data["auth_provider"] = parts[1]
            emit_survey_comment(slug_publico, data)
        except Exception:
            current_app.logger.exception("[encuestas] Error al emitir comentario socket")

    return comentario


def _comment_identity_payload(user: Optional[User]) -> Dict[str, Any]:
    identity = get_user_profile_identity(user) if user else {}
    avatar_url = identity.get("avatar_url") if identity.get("avatar_consent") else None
    avatar_source = identity.get("avatar_source") if avatar_url else None
    avatar_consent = bool(avatar_url and identity.get("avatar_consent"))
    return {
        "avatar_url": avatar_url,
        "picture": avatar_url,
        "avatar_source": avatar_source,
        "avatar_consent": avatar_consent,
        "profile_picture_consent": avatar_consent,
        "avatar_policy": "consented_upload_or_social_only",
    }


def serialize_public_comment(comentario: EncComentario) -> Dict[str, Any]:
    comment_mode = "anon"
    auth_provider = None
    auth_user_id = None
    anon_ref = comentario.anon_id
    if isinstance(anon_ref, str) and anon_ref.startswith("social:"):
        parts = anon_ref.split(":", 2)
        if len(parts) == 3:
            comment_mode = "social"
            auth_provider = parts[1] or None
            auth_user_id = parts[2] or None

    user = getattr(comentario, "user", None)
    return {
        "id": comentario.id,
        "texto": _safe_text_value(comentario.texto, fallback=""),
        "nombre_autor": _safe_text_value(comentario.nombre_autor or (user.name if user else "Anónimo"), fallback="Anónimo"),
        "fecha": comentario.created_at.isoformat() if comentario.created_at else None,
        "user_id": comentario.user_id,
        "anon_id": comentario.anon_id,
        "comment_mode": comment_mode,
        "auth_provider": auth_provider,
        "auth_user_id": auth_user_id,
        **_comment_identity_payload(user),
    }


def list_comentarios(encuesta_id: int, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    base_query = (
        EncComentario.query.filter_by(encuesta_id=encuesta_id, estado="publicado")
        .order_by(EncComentario.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    try:
        query = base_query
        if _enc_comentario_has_report_count():
            query = query.options(load_only(
                EncComentario.id,
                EncComentario.texto,
                EncComentario.nombre_autor,
                EncComentario.created_at,
                EncComentario.user_id,
                EncComentario.anon_id,
                EncComentario.estado,
                EncComentario.report_count,
            ), joinedload(EncComentario.user).load_only(User.id, User.name, User.accesibilidad))
        else:
            query = query.options(load_only(
                EncComentario.id,
                EncComentario.texto,
                EncComentario.nombre_autor,
                EncComentario.created_at,
                EncComentario.user_id,
                EncComentario.anon_id,
                EncComentario.estado,
            ), joinedload(EncComentario.user).load_only(User.id, User.name, User.accesibilidad))
        rows = query.all()
    except Exception as exc:
        logger = _current_app_logger()
        if logger:
            logger.warning("[encuestas] list_comentarios degraded due to schema mismatch: %s", exc)
        rows = (
            base_query
            .with_entities(
                EncComentario.id,
                EncComentario.texto,
                EncComentario.nombre_autor,
                EncComentario.created_at,
                EncComentario.user_id,
                EncComentario.anon_id,
            )
            .all()
        )
        return [
            {
                "id": row.id,
                "texto": _safe_text_value(row.texto, fallback=""),
                "nombre_autor": _safe_text_value(row.nombre_autor, fallback="Anónimo"),
                "fecha": row.created_at.isoformat() if row.created_at else None,
                "user_id": row.user_id,
                "anon_id": row.anon_id,
            }
            for row in rows
        ]

    results = []
    for c in rows:
        comment_mode = "anon"
        auth_provider = None
        auth_user_id = None
        anon_ref = c.anon_id
        if isinstance(anon_ref, str) and anon_ref.startswith("social:"):
            parts = anon_ref.split(":", 2)
            if len(parts) == 3:
                comment_mode = "social"
                auth_provider = parts[1] or None
                auth_user_id = parts[2] or None

        results.append({
            "id": c.id,
            "texto": _safe_text_value(c.texto, fallback=""),
            "nombre_autor": _safe_text_value(c.nombre_autor or (c.user.name if c.user else "Anónimo"), fallback="Anónimo"),
            "fecha": c.created_at.isoformat(),
            "user_id": c.user_id,
            "anon_id": c.anon_id,
            "comment_mode": comment_mode,
            "auth_provider": auth_provider,
            "auth_user_id": auth_user_id,
            **_comment_identity_payload(c.user),
        })
    return results


def reportar_comentario(comentario_id: int) -> EncComentario:
    comentario = db.session.get(EncComentario, comentario_id)
    if not comentario:
        raise EncuestaError("Comentario no encontrado", status_code=404)

    comentario.report_count += 1
    # Auto-ocultar si recibe muchos reportes (ej: 5)
    if comentario.report_count >= 5 and comentario.estado == "publicado":
        comentario.estado = "revision"

    db.session.commit()
    return comentario


def administrar_comentario(comentario_id: int, accion: str, user: Any) -> EncComentario:
    comentario = db.session.get(EncComentario, comentario_id)
    if not comentario:
        raise EncuestaError("Comentario no encontrado", status_code=404)

    encuesta = db.session.get(EncEncuesta, comentario.encuesta_id)
    _ensure_tenant_access(encuesta, user)

    if accion == "aprobar":
        comentario.estado = "publicado"
        comentario.report_count = 0 # Reset reports on approval
    elif accion == "ocultar":
        comentario.estado = "oculto"
    elif accion == "eliminar":
        comentario.estado = "eliminado" # Soft delete logic or actual delete
    else:
        raise EncuestaError("Acción inválida", status_code=400)

    db.session.commit()
    return comentario


def list_all_comentarios_admin(encuesta_id: int, user: Any, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    encuesta = get_encuesta(encuesta_id, user=user)

    query = (
        EncComentario.query.filter_by(encuesta_id=encuesta.id)
        .order_by(EncComentario.report_count.desc(), EncComentario.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    results = []
    for c in query:
        results.append({
            "id": c.id,
            "texto": _safe_text_value(c.texto, fallback=""),
            "nombre_autor": _safe_text_value(c.nombre_autor, fallback="Anónimo"),
            "fecha": c.created_at.isoformat(),
            "estado": c.estado,
            "report_count": c.report_count,
            "user_id": c.user_id,
        })
    return results

def get_public_encuesta_by_id(encuesta_id: int) -> EncEncuesta:
    """Retrieves a public survey by ID directly, ensuring it's published."""
    encuesta = db.session.get(EncEncuesta, encuesta_id)
    if not encuesta:
        raise EncuestaError("Encuesta no encontrada", status_code=404)
    if encuesta.estado != "publicada":
        raise EncuestaError(
            "La encuesta no está activa",
            status_code=403,
            payload={"reason_code": "survey_not_published"},
        )
    return encuesta
