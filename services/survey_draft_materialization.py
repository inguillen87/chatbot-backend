"""Atomic conversion of canonical survey drafts into executable surveys."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from database import db
from models import (
    EncEncuesta,
    SurveyDraft,
    SurveyDraftMaterialization,
    SurveyDraftMaterializationAlias,
)
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    normalize_survey_conditional_logic,
    serialize_encuesta,
    validate_survey_instrument_payload,
)
from services.survey_refs import is_canonical_survey_logical_ref


MATERIALIZATION_CONTRACT_VERSION = "surveys.materialization.v1"
SURVEY_DOCUMENT_V1 = "survey-document.v1"
SURVEY_DOCUMENT_V2 = "survey-document.v2"
# Kept as a compatibility alias for callers that imported the original
# singular constant before document v2 existed.
SUPPORTED_SCHEMA_VERSION = SURVEY_DOCUMENT_V1
SUPPORTED_SCHEMA_VERSIONS = frozenset({SURVEY_DOCUMENT_V1, SURVEY_DOCUMENT_V2})

_ROOT_REQUIRED = frozenset(
    {
        "document_ref",
        "title",
        "survey_type",
        "schedule",
        "policies",
        "experience",
        "questions",
        "extensions",
    }
)
_ROOT_OPTIONAL = frozenset({"schema_version", "revision", "slug", "description"})
_QUESTION_REQUIRED = frozenset(
    {
        "question_ref",
        "order",
        "type",
        "prompt",
        "required",
        "selection",
        "visibility",
        "options",
        "extensions",
    }
)
_QUESTION_OPTIONAL = frozenset({"persisted_id", "quarantine"})
_OPTION_REQUIRED = frozenset({"option_ref", "order", "label", "extensions"})
_OPTION_OPTIONAL = frozenset({"persisted_id", "value"})
_QUESTION_TYPE_MAP = {
    "single_choice": "opcion_unica",
    "multiple_choice": "opcion_multiple",
    "free_text": "abierta",
    "emoji_rating": "rating_emoji",
}
_UNSUPPORTED_LOSSY_TYPES = frozenset({"nps", "ranking", "location"})
_SURVEY_TYPES = frozenset({"opinion", "votacion", "sondeo", "planificacion"})
_UNIQUENESS_POLICIES = frozenset(
    {"por_dni", "por_phone", "por_ip", "por_cookie", "por_usuario", "libre"}
)


class SurveyDraftMaterializationError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        reason_code: str,
        action_hint: str,
        retryable: bool = False,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.reason_code = reason_code
        self.action_hint = action_hint
        self.retryable = retryable
        self.context = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": MATERIALIZATION_CONTRACT_VERSION,
            "ok": False,
            "persisted": False,
            "status_code": self.status_code,
            "reason_code": self.reason_code,
            "retryable": self.retryable,
            "action_hint": self.action_hint,
            "message": self.message,
            "error": {"code": self.status_code, "message": self.message},
            **self.context,
        }


@dataclass(frozen=True)
class SurveyDraftMaterializationResult:
    receipt: SurveyDraftMaterialization
    survey: EncEncuesta
    idempotency_key: str
    replayed: bool


def _invalid_document(
    detail: str,
    *,
    reason_code: str = "survey_document_invalid",
    **context: Any,
) -> SurveyDraftMaterializationError:
    return SurveyDraftMaterializationError(
        "El borrador no puede materializarse sin perdida de datos",
        status_code=422,
        reason_code=reason_code,
        action_hint="fix_survey_document",
        context={"detail": detail, **context},
    )


def _conditional_logic_document_error(
    exc: EncuestaError,
    *,
    prepared_questions: Sequence[Mapping[str, Any]],
    question_index: int | None = None,
) -> SurveyDraftMaterializationError:
    """Translate executable conditional validation into the draft contract."""

    error_payload = dict(exc.payload or {})
    raw_context = error_payload.get("context")
    conditional_context = (
        dict(raw_context) if isinstance(raw_context, Mapping) else {}
    )
    if question_index is None:
        raw_question_index = conditional_context.get(
            "question_index", error_payload.get("question_index")
        )
        if isinstance(raw_question_index, int) and not isinstance(
            raw_question_index, bool
        ):
            question_index = raw_question_index
    if question_index is None:
        question_order = conditional_context.get(
            "question_order", error_payload.get("question_order")
        )
        question_index = next(
            (
                index
                for index, question in enumerate(prepared_questions)
                if question.get("orden") == question_order
            ),
            None,
        )

    source_field = conditional_context.get("field", error_payload.get("field"))
    canonical_field = source_field if isinstance(source_field, str) else None
    if question_index is not None and isinstance(source_field, str):
        visibility_field = f"questions[{question_index}].visibility"
        if source_field == "conditional_logic":
            canonical_field = visibility_field
        elif source_field == "conditional_logic.version":
            canonical_field = f"{visibility_field}.version"
        elif source_field == "conditional_logic.show_if":
            canonical_field = f"{visibility_field}.root"
        elif source_field.startswith("conditional_logic.show_if."):
            suffix = source_field[len("conditional_logic.show_if") :]
            canonical_field = f"{visibility_field}.root{suffix}"

    context: dict[str, Any] = {
        "conditional_context": conditional_context,
    }
    conditional_contract_version = error_payload.get("contract_version")
    if conditional_contract_version is not None:
        context["conditional_contract_version"] = conditional_contract_version
    if canonical_field is not None:
        context["field"] = canonical_field
    return _invalid_document(
        error_payload.get("detail") or exc.message,
        **context,
    )


def _strict_object(
    value: Any,
    *,
    field: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid_document(f"{field} debe ser un objeto", field=field)
    payload = dict(value)
    keys = set(payload)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing or unknown:
        raise _invalid_document(
            f"{field} no cumple el schema cerrado",
            field=field,
            missing_fields=missing,
            unknown_fields=unknown,
        )
    return payload


def _validate_extensions(value: Any, *, field: str) -> None:
    if not isinstance(value, Mapping):
        raise _invalid_document(f"{field} debe ser un objeto", field=field)
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raise _invalid_document(
            f"{field} debe contener metadata JSON valida",
            field=field,
        )


def _safe_ref(value: Any, *, field: str) -> str:
    if not is_canonical_survey_logical_ref(value):
        raise _invalid_document(
            f"{field} debe ser un identificador estable de hasta 160 caracteres",
            field=field,
        )
    return value


def _required_string(value: Any, *, field: str, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_document(f"{field} debe ser texto no vacio", field=field)
    if value != value.strip():
        raise _invalid_document(
            f"{field} no admite espacios al inicio o al final",
            field=field,
        )
    if max_length is not None and len(value) > max_length:
        raise _invalid_document(
            f"{field} supera {max_length} caracteres",
            field=field,
        )
    return value


def _optional_string(
    value: Any,
    *,
    field: str,
    max_length: int | None = None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid_document(f"{field} debe ser texto o null", field=field)
    if not value or value != value.strip():
        raise _invalid_document(
            f"{field} debe ser texto no vacio sin espacios exteriores o null",
            field=field,
        )
    if max_length is not None and len(value) > max_length:
        raise _invalid_document(
            f"{field} supera {max_length} caracteres",
            field=field,
        )
    return value


def _optional_exact_string(
    value: Any,
    *,
    field: str,
    max_length: int | None = None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid_document(f"{field} debe ser texto o null", field=field)
    if max_length is not None and len(value) > max_length:
        raise _invalid_document(
            f"{field} supera {max_length} caracteres",
            field=field,
        )
    return value


def _strict_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid_document(f"{field} debe ser booleano", field=field)
    return value


def _nullable_non_negative_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid_document(
            f"{field} debe ser un entero no negativo o null",
            field=field,
        )
    return value


def _non_negative_int(value: Any, *, field: str) -> int:
    parsed = _nullable_non_negative_int(value, field=field)
    if parsed is None:
        raise _invalid_document(
            f"{field} debe ser un entero no negativo",
            field=field,
        )
    return parsed


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _invalid_document(f"{field} debe ser un entero positivo", field=field)
    return value


def _operation_fingerprint(
    *,
    tenant_id: int,
    survey_draft_id: int,
    draft_id: str,
    draft_revision: int,
    schema_version: str,
    payload_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "tenant_id": tenant_id,
            "survey_draft_id": survey_draft_id,
            "draft_id": draft_id,
            "draft_revision": draft_revision,
            "schema_version": schema_version,
            "payload_hash": payload_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_document_to_encuesta_payload(
    document: Mapping[str, Any],
    *,
    schema_version: str,
    draft_revision: int,
) -> tuple[dict[str, Any], str]:
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SurveyDraftMaterializationError(
            "La version del borrador no puede materializarse de forma segura",
            status_code=422,
            reason_code="unsupported_survey_document_schema",
            action_hint="migrate_draft_schema",
            context={
                "schema_version": schema_version,
                "supported_schema_versions": sorted(SUPPORTED_SCHEMA_VERSIONS),
            },
        )

    root = _strict_object(
        document,
        field="document",
        required=_ROOT_REQUIRED,
        optional=_ROOT_OPTIONAL,
    )
    embedded_schema = root.get("schema_version")
    if embedded_schema is not None and embedded_schema != schema_version:
        raise _invalid_document(
            "schema_version embebido no coincide con el schema persistido",
            field="schema_version",
            stored_schema_version=schema_version,
            embedded_schema_version=embedded_schema,
        )
    embedded_revision = root.get("revision")
    if embedded_revision is not None and embedded_revision != draft_revision:
        raise _invalid_document(
            "revision embebida no coincide con la revision persistida",
            field="revision",
            stored_revision=draft_revision,
            embedded_revision=embedded_revision,
        )

    _validate_extensions(root["extensions"], field="extensions")
    document_ref = _safe_ref(root["document_ref"], field="document_ref")
    title = _required_string(root["title"], field="title", max_length=255)
    survey_type = root["survey_type"]
    if survey_type not in _SURVEY_TYPES:
        raise _invalid_document(
            "survey_type no esta soportado",
            field="survey_type",
            survey_type=survey_type,
            supported_survey_types=sorted(_SURVEY_TYPES),
        )

    schedule = _strict_object(
        root["schedule"],
        field="schedule",
        required=frozenset({"starts_at", "ends_at"}),
    )
    starts_at = _optional_string(schedule["starts_at"], field="schedule.starts_at")
    ends_at = _optional_string(schedule["ends_at"], field="schedule.ends_at")

    policies = _strict_object(
        root["policies"],
        field="policies",
        required=frozenset({"uniqueness", "anonymous", "requires_contact_data"}),
    )
    uniqueness = policies["uniqueness"]
    if uniqueness not in _UNIQUENESS_POLICIES:
        raise _invalid_document(
            "policies.uniqueness no esta soportada",
            field="policies.uniqueness",
            uniqueness=uniqueness,
        )
    anonymous = _strict_bool(policies["anonymous"], field="policies.anonymous")
    requires_contact = _strict_bool(
        policies["requires_contact_data"],
        field="policies.requires_contact_data",
    )

    experience = _strict_object(
        root["experience"],
        field="experience",
        required=frozenset(
            {"live_voting", "show_live_results", "allow_comments", "reward_points"}
        ),
    )
    live_voting = _strict_bool(experience["live_voting"], field="experience.live_voting")
    show_live_results = _strict_bool(
        experience["show_live_results"],
        field="experience.show_live_results",
    )
    allow_comments = _strict_bool(
        experience["allow_comments"],
        field="experience.allow_comments",
    )
    reward_points = _non_negative_int(
        experience["reward_points"],
        field="experience.reward_points",
    )

    raw_questions = root["questions"]
    if not isinstance(raw_questions, Sequence) or isinstance(raw_questions, (str, bytes)):
        raise _invalid_document("questions debe ser una lista", field="questions")
    if not raw_questions:
        raise _invalid_document(
            "questions debe contener al menos una pregunta",
            field="questions",
        )

    prepared: list[dict[str, Any]] = []
    questions_by_ref: dict[str, dict[str, Any]] = {}
    question_orders: set[int] = set()
    for question_index, raw_question in enumerate(raw_questions):
        question = _strict_object(
            raw_question,
            field=f"questions[{question_index}]",
            required=_QUESTION_REQUIRED,
            optional=_QUESTION_OPTIONAL,
        )
        if question.get("persisted_id") is not None:
            raise _invalid_document(
                "persisted_id no puede copiarse a una encuesta nueva",
                field=f"questions[{question_index}].persisted_id",
                reason_code="unsupported_survey_document_persisted_id",
            )
        _validate_extensions(
            question["extensions"],
            field=f"questions[{question_index}].extensions",
        )
        question_ref = _safe_ref(
            question["question_ref"],
            field=f"questions[{question_index}].question_ref",
        )
        if question_ref in questions_by_ref:
            raise _invalid_document(
                "question_ref debe ser unico",
                field=f"questions[{question_index}].question_ref",
                question_ref=question_ref,
            )
        order = _positive_int(
            question["order"],
            field=f"questions[{question_index}].order",
        )
        if order in question_orders:
            raise _invalid_document(
                "El orden de preguntas debe ser unico",
                field=f"questions[{question_index}].order",
                order=order,
            )
        question_orders.add(order)

        question_type = question["type"]
        if question_type == "quarantined":
            quarantine = _strict_object(
                question.get("quarantine"),
                field=f"questions[{question_index}].quarantine",
                required=frozenset(
                    {"reason", "source", "source_type", "materializable"}
                ),
            )
            source_type = quarantine.get("source_type")
            if (
                quarantine.get("reason")
                not in {"unsupported_question_type", "invalid_materialization_shape"}
                or quarantine.get("source") not in {"admin", "durable_builder"}
                or not isinstance(source_type, str)
                or not source_type.strip()
                or quarantine.get("materializable") is not False
            ):
                raise _invalid_document(
                    "quarantine no cumple el contrato fail-closed",
                    field=f"questions[{question_index}].quarantine",
                )
            raise SurveyDraftMaterializationError(
                f"El tipo de pregunta '{source_type}' no puede materializarse sin perdida",
                status_code=422,
                reason_code="unsupported_survey_question_type",
                action_hint="replace_unsupported_question_type",
                context={
                    "question_index": question_index,
                    "question_ref": question_ref,
                    "question_type": source_type,
                    "known_lossy_type": source_type in _UNSUPPORTED_LOSSY_TYPES,
                    "supported_types": sorted(_QUESTION_TYPE_MAP),
                    "quarantine_reason": quarantine.get("reason"),
                },
            )
        if "quarantine" in question:
            raise _invalid_document(
                "Una pregunta materializable no puede declarar quarantine",
                field=f"questions[{question_index}].quarantine",
            )
        internal_type = _QUESTION_TYPE_MAP.get(question_type)
        if internal_type is None:
            raise SurveyDraftMaterializationError(
                f"El tipo de pregunta '{question_type}' no puede materializarse sin perdida",
                status_code=422,
                reason_code="unsupported_survey_question_type",
                action_hint="replace_unsupported_question_type",
                context={
                    "question_index": question_index,
                    "question_ref": question_ref,
                    "question_type": question_type,
                    "known_lossy_type": question_type in _UNSUPPORTED_LOSSY_TYPES,
                    "supported_types": sorted(_QUESTION_TYPE_MAP),
                },
            )
        prompt = _required_string(
            question["prompt"],
            field=f"questions[{question_index}].prompt",
        )
        required = _strict_bool(
            question["required"],
            field=f"questions[{question_index}].required",
        )
        selection = _strict_object(
            question["selection"],
            field=f"questions[{question_index}].selection",
            required=frozenset({"min", "max"}),
        )
        minimum = _nullable_non_negative_int(
            selection["min"],
            field=f"questions[{question_index}].selection.min",
        )
        maximum = _nullable_non_negative_int(
            selection["max"],
            field=f"questions[{question_index}].selection.max",
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _invalid_document(
                "selection.min no puede superar selection.max",
                field=f"questions[{question_index}].selection",
            )
        if internal_type in {"opcion_unica", "rating_emoji"} and (
            (minimum is not None and minimum > 1)
            or (maximum is not None and maximum > 1)
        ):
            raise _invalid_document(
                "Las preguntas de seleccion unica admiten como maximo una opcion",
                field=f"questions[{question_index}].selection",
            )

        raw_options = question["options"]
        if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
            raise _invalid_document(
                "options debe ser una lista",
                field=f"questions[{question_index}].options",
            )
        options: list[dict[str, Any]] = []
        options_by_ref: dict[str, dict[str, Any]] = {}
        option_orders: set[int] = set()
        for option_index, raw_option in enumerate(raw_options):
            option = _strict_object(
                raw_option,
                field=f"questions[{question_index}].options[{option_index}]",
                required=_OPTION_REQUIRED,
                optional=_OPTION_OPTIONAL,
            )
            if option.get("persisted_id") is not None:
                raise _invalid_document(
                    "persisted_id no puede copiarse a una encuesta nueva",
                    field=f"questions[{question_index}].options[{option_index}].persisted_id",
                    reason_code="unsupported_survey_document_persisted_id",
                )
            _validate_extensions(
                option["extensions"],
                field=f"questions[{question_index}].options[{option_index}].extensions",
            )
            option_ref = _safe_ref(
                option["option_ref"],
                field=f"questions[{question_index}].options[{option_index}].option_ref",
            )
            if option_ref in options_by_ref:
                raise _invalid_document(
                    "option_ref debe ser unico dentro de la pregunta",
                    field=f"questions[{question_index}].options[{option_index}].option_ref",
                    option_ref=option_ref,
                )
            option_order = _positive_int(
                option["order"],
                field=f"questions[{question_index}].options[{option_index}].order",
            )
            if option_order in option_orders:
                raise _invalid_document(
                    "El orden de opciones debe ser unico dentro de la pregunta",
                    field=f"questions[{question_index}].options[{option_index}].order",
                    order=option_order,
                )
            option_orders.add(option_order)
            normalized_option = {
                "option_ref": option_ref,
                "logical_ref": option_ref,
                "orden": option_order,
                "texto": _required_string(
                    option["label"],
                    field=f"questions[{question_index}].options[{option_index}].label",
                ),
                "valor": _optional_exact_string(
                    option.get("value"),
                    field=f"questions[{question_index}].options[{option_index}].value",
                    max_length=120,
                ),
            }
            options.append(normalized_option)
            options_by_ref[option_ref] = normalized_option

        if internal_type == "abierta" and options:
            raise _invalid_document(
                "free_text no admite opciones",
                field=f"questions[{question_index}].options",
            )
        if internal_type != "abierta" and not options:
            raise _invalid_document(
                f"{question_type} requiere al menos una opcion",
                field=f"questions[{question_index}].options",
            )
        if internal_type == "opcion_multiple" and (
            (minimum is not None and minimum > len(options))
            or (maximum is not None and maximum > len(options))
        ):
            raise _invalid_document(
                "selection no puede superar la cantidad de opciones",
                field=f"questions[{question_index}].selection",
            )

        normalized_question = {
            "question_ref": question_ref,
            "logical_ref": question_ref,
            "orden": order,
            "tipo": internal_type,
            "texto": prompt,
            "obligatoria": required,
            "min_selecciones": minimum,
            "max_selecciones": maximum,
            "opciones": options,
            "conditional_logic": None,
            "_visibility": question["visibility"],
            "_options_by_ref": options_by_ref,
        }
        prepared.append(normalized_question)
        questions_by_ref[question_ref] = normalized_question

    for question_index, question in enumerate(prepared):
        visibility = question.pop("_visibility")
        question.pop("_options_by_ref")
        if visibility is None:
            continue
        if schema_version == SURVEY_DOCUMENT_V2:
            visibility_payload = _strict_object(
                visibility,
                field=f"questions[{question_index}].visibility",
                required=frozenset({"version", "root"}),
            )
            visibility_version = visibility_payload["version"]
            if (
                isinstance(visibility_version, bool)
                or not isinstance(visibility_version, int)
                or visibility_version != 2
            ):
                raise _invalid_document(
                    "visibility.version debe ser exactamente el entero 2",
                    field=f"questions[{question_index}].visibility.version",
                    received_type=type(visibility_version).__name__,
                    received_value=visibility_version,
                )
            try:
                question["conditional_logic"] = normalize_survey_conditional_logic(
                    {
                        "version": 2,
                        "show_if": visibility_payload["root"],
                    },
                    question_index=question_index,
                    question_order=question["orden"],
                )
            except EncuestaError as exc:
                raise _conditional_logic_document_error(
                    exc,
                    prepared_questions=prepared,
                    question_index=question_index,
                ) from exc
            continue

        visibility_payload = _strict_object(
            visibility,
            field=f"questions[{question_index}].visibility",
            required=frozenset({"kind", "question_ref", "option_ref"}),
        )
        if visibility_payload["kind"] != "option_selected":
            raise _invalid_document(
                "visibility.kind no esta soportado",
                field=f"questions[{question_index}].visibility.kind",
                visibility_kind=visibility_payload["kind"],
            )
        source_question_ref = _safe_ref(
            visibility_payload["question_ref"],
            field=f"questions[{question_index}].visibility.question_ref",
        )
        source_option_ref = _safe_ref(
            visibility_payload["option_ref"],
            field=f"questions[{question_index}].visibility.option_ref",
        )
        source = questions_by_ref.get(source_question_ref)
        if source is None:
            raise _invalid_document(
                "visibility referencia una pregunta inexistente",
                field=f"questions[{question_index}].visibility.question_ref",
                source_question_ref=source_question_ref,
            )
        if source["orden"] >= question["orden"]:
            raise _invalid_document(
                "visibility solo puede referenciar una pregunta anterior",
                field=f"questions[{question_index}].visibility.question_ref",
                source_question_ref=source_question_ref,
            )
        if source["tipo"] not in {"opcion_unica", "opcion_multiple"}:
            raise _invalid_document(
                "visibility requiere una pregunta fuente seleccionable",
                field=f"questions[{question_index}].visibility.question_ref",
                source_question_ref=source_question_ref,
            )
        source_options = {
            option["logical_ref"]: option for option in source["opciones"]
        }
        source_option = source_options.get(source_option_ref)
        if source_option is None:
            raise _invalid_document(
                "visibility referencia una opcion inexistente",
                field=f"questions[{question_index}].visibility.option_ref",
                source_option_ref=source_option_ref,
            )
        question["conditional_logic"] = {
            "version": 1,
            "show_if": {
                "question_order": source["orden"],
                "option_order": source_option["orden"],
            },
        }

    if schema_version == SURVEY_DOCUMENT_V2:
        try:
            # Validate dangling references, option ownership, backward-only
            # edges, selectable sources, and cross-leaf invariants at the same
            # service boundary used by persisted executable surveys. The
            # normalized return value is deliberately discarded so the
            # canonical v2 AST remains reference-based and byte-stable.
            validate_survey_instrument_payload(prepared)
        except EncuestaError as exc:
            raise _conditional_logic_document_error(
                exc,
                prepared_questions=prepared,
            ) from exc

    payload = {
        "document_ref": document_ref,
        "titulo": title,
        "descripcion": _optional_exact_string(
            root.get("description"),
            field="description",
        ),
        "tipo": survey_type,
        "inicio_at": starts_at,
        "fin_at": ends_at,
        "politica_unicidad": uniqueness,
        "anonimo_permitido": anonymous,
        "requiere_identidad": requires_contact,
        "es_votacion_envivo": live_voting,
        "mostrar_resultados_envivo": show_live_results,
        "permitir_comentarios": allow_comments,
        "puntos_recompensa": reward_points,
        "preguntas": prepared,
    }
    slug = _optional_string(root.get("slug"), field="slug", max_length=160)
    if slug is not None:
        payload["slug"] = slug
    return payload, document_ref


def _alias_conflict(
    alias: SurveyDraftMaterializationAlias,
    *,
    draft_id: str,
    expected_revision: int,
) -> SurveyDraftMaterializationError:
    receipt = alias.materialization
    return SurveyDraftMaterializationError(
        "La clave de idempotencia ya pertenece a otra materializacion",
        status_code=409,
        reason_code="survey_materialization_idempotency_conflict",
        action_hint="use_new_idempotency_key",
        context={
            "draft_id": draft_id,
            "expected_revision": expected_revision,
            "current_draft_id": receipt.draft_id,
            "current_revision": receipt.draft_revision,
            "survey_id": receipt.survey_id,
        },
    )


def _receipt_corrupt_error(receipt_id: int | None) -> SurveyDraftMaterializationError:
    return SurveyDraftMaterializationError(
        "El recibo de materializacion no respeta el aislamiento del tenant",
        status_code=409,
        reason_code="survey_materialization_receipt_corrupt",
        action_hint="contact_support",
        context={"receipt_id": receipt_id},
    )


def _validate_receipt_tenant_scope(
    receipt: SurveyDraftMaterialization | None,
    *,
    tenant_id: int,
    alias: SurveyDraftMaterializationAlias | None = None,
) -> EncEncuesta:
    if receipt is None:
        raise _receipt_corrupt_error(None)

    survey = receipt.survey
    survey_draft = receipt.survey_draft
    invalid_scope = (
        receipt.tenant_id != tenant_id
        or (
            alias is not None
            and (
                alias.tenant_id != tenant_id
                or alias.tenant_id != receipt.tenant_id
            )
        )
        or survey is None
        or survey.tenant_id != tenant_id
        or survey_draft is None
        or survey_draft.tenant_id != tenant_id
    )
    if invalid_scope:
        raise _receipt_corrupt_error(receipt.id)
    return survey


def _result_from_alias(
    alias: SurveyDraftMaterializationAlias,
    *,
    tenant_id: int,
    draft_id: str,
    expected_revision: int,
    idempotency_key: str,
) -> SurveyDraftMaterializationResult:
    receipt = alias.materialization
    survey = _validate_receipt_tenant_scope(
        receipt,
        tenant_id=tenant_id,
        alias=alias,
    )
    if (
        receipt.draft_id != draft_id
        or receipt.draft_revision != expected_revision
        or alias.operation_fingerprint != receipt.operation_fingerprint
    ):
        raise _alias_conflict(
            alias,
            draft_id=draft_id,
            expected_revision=expected_revision,
        )
    return SurveyDraftMaterializationResult(
        receipt=receipt,
        survey=survey,
        idempotency_key=idempotency_key,
        replayed=True,
    )


def _find_alias(tenant_id: int, idempotency_key: str) -> SurveyDraftMaterializationAlias | None:
    return SurveyDraftMaterializationAlias.query.filter_by(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
    ).first()


def _find_receipt(
    tenant_id: int,
    draft_id: str,
    draft_revision: int,
) -> SurveyDraftMaterialization | None:
    return SurveyDraftMaterialization.query.filter_by(
        tenant_id=tenant_id,
        draft_id=draft_id,
        draft_revision=draft_revision,
    ).first()


def _persist_alias(
    receipt: SurveyDraftMaterialization,
    *,
    tenant_id: int,
    idempotency_key: str,
) -> SurveyDraftMaterializationAlias:
    alias = SurveyDraftMaterializationAlias(
        tenant_id=tenant_id,
        materialization=receipt,
        idempotency_key=idempotency_key,
        operation_fingerprint=receipt.operation_fingerprint,
    )
    db.session.add(alias)
    db.session.flush()
    return alias


def _replay_existing_receipt(
    receipt: SurveyDraftMaterialization,
    *,
    tenant_id: int,
    draft_id: str,
    expected_revision: int,
    idempotency_key: str,
) -> SurveyDraftMaterializationResult:
    _validate_receipt_tenant_scope(receipt, tenant_id=tenant_id)
    try:
        alias = _persist_alias(
            receipt,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raced_alias = _find_alias(tenant_id, idempotency_key)
        if raced_alias is None:
            raise SurveyDraftMaterializationError(
                "No se pudo reservar la clave de idempotencia",
                status_code=409,
                reason_code="survey_materialization_concurrent_conflict",
                action_hint="retry_same_request",
                retryable=True,
            )
        return _result_from_alias(
            raced_alias,
            tenant_id=tenant_id,
            draft_id=draft_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
    except OperationalError as exc:
        db.session.rollback()
        if not _is_retryable_lock_error(exc):
            raise SurveyDraftMaterializationError(
                "No se pudo acceder al almacenamiento de materializaciones",
                status_code=500,
                reason_code="survey_materialization_storage_error",
                action_hint="contact_support",
                retryable=False,
            ) from exc
        raise SurveyDraftMaterializationError(
            "Otra operacion esta confirmando la materializacion",
            status_code=409,
            reason_code="survey_materialization_concurrent_conflict",
            action_hint="retry_same_request",
            retryable=True,
        ) from exc
    return _result_from_alias(
        alias,
        tenant_id=tenant_id,
        draft_id=draft_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )


def _acquire_draft_write_guard(tenant_id: int, draft_id: str) -> SurveyDraft | None:
    result = db.session.execute(
        text(
            "UPDATE survey_draft SET revision = revision "
            "WHERE tenant_id = :tenant_id AND draft_id = :draft_id"
        ),
        {"tenant_id": tenant_id, "draft_id": draft_id},
    )
    if result.rowcount == 0:
        return None
    return (
        SurveyDraft.query.filter_by(tenant_id=tenant_id, draft_id=draft_id)
        .execution_options(populate_existing=True)
        .one()
    )


def _is_retryable_lock_error(exc: OperationalError) -> bool:
    original = getattr(exc, "orig", None)
    sqlstate = str(
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or ""
    ).strip()
    if sqlstate in {"40P01", "40001", "55P03"}:
        return True
    message = str(original or exc).lower()
    return "database is locked" in message or "database is busy" in message


def _verify_draft_payload_hash(draft: SurveyDraft) -> None:
    canonical = json.dumps(
        draft.payload or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    computed_hash = hashlib.sha256(canonical).hexdigest()
    if computed_hash != draft.payload_hash:
        raise SurveyDraftMaterializationError(
            "El contenido del borrador no coincide con su huella persistida",
            status_code=409,
            reason_code="survey_draft_integrity_failed",
            action_hint="restore_or_resave_draft",
            context={"draft_id": draft.draft_id, "revision": draft.revision},
        )


def _flush_materialization_receipt() -> None:
    """Explicit seam used to prove survey+receipt rollback atomicity."""

    db.session.flush()


def materialize_survey_draft(
    *,
    tenant: Any,
    draft_id: str,
    expected_revision: int,
    idempotency_key: str,
    user: Any,
) -> SurveyDraftMaterializationResult:
    tenant_id = int(tenant.id)

    alias = _find_alias(tenant_id, idempotency_key)
    if alias is not None:
        return _result_from_alias(
            alias,
            tenant_id=tenant_id,
            draft_id=draft_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    existing_receipt = _find_receipt(tenant_id, draft_id, expected_revision)
    if existing_receipt is not None:
        return _replay_existing_receipt(
            existing_receipt,
            tenant_id=tenant_id,
            draft_id=draft_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    try:
        draft = _acquire_draft_write_guard(tenant_id, draft_id)
    except OperationalError as exc:
        db.session.rollback()
        if not _is_retryable_lock_error(exc):
            raise SurveyDraftMaterializationError(
                "No se pudo acceder al almacenamiento de borradores",
                status_code=500,
                reason_code="survey_materialization_storage_error",
                action_hint="contact_support",
                retryable=False,
            ) from exc
        raise SurveyDraftMaterializationError(
            "El borrador esta siendo actualizado por otra operacion",
            status_code=409,
            reason_code="survey_materialization_concurrent_conflict",
            action_hint="retry_same_request",
            retryable=True,
        ) from exc
    if draft is None:
        db.session.rollback()
        raise SurveyDraftMaterializationError(
            "Borrador no encontrado",
            status_code=404,
            reason_code="survey_draft_not_found",
            action_hint="check_draft_id",
        )

    # The write guard serializes both SQLite and PostgreSQL. Recheck every
    # uniqueness target after acquiring it because another request may have won.
    alias = _find_alias(tenant_id, idempotency_key)
    if alias is not None:
        db.session.rollback()
        return _result_from_alias(
            alias,
            tenant_id=tenant_id,
            draft_id=draft_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
    existing_receipt = _find_receipt(tenant_id, draft_id, expected_revision)
    if existing_receipt is not None:
        return _replay_existing_receipt(
            existing_receipt,
            tenant_id=tenant_id,
            draft_id=draft_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    if draft.revision != expected_revision:
        db.session.rollback()
        raise SurveyDraftMaterializationError(
            "La revision del borrador esta desactualizada",
            status_code=409,
            reason_code="draft_revision_conflict",
            action_hint="reload_draft",
            context={
                "draft_id": draft_id,
                "expected_revision": expected_revision,
                "current_revision": draft.revision,
            },
        )

    _verify_draft_payload_hash(draft)
    embedded_schema = (draft.payload or {}).get("schema_version")
    if embedded_schema is not None and embedded_schema != draft.schema_version:
        db.session.rollback()
        raise _invalid_document(
            "schema_version embebido no coincide con el control persistido",
            field="schema_version",
            stored_schema_version=draft.schema_version,
            embedded_schema_version=embedded_schema,
        )
    payload, document_ref = canonical_document_to_encuesta_payload(
        draft.payload or {},
        schema_version=draft.schema_version,
        draft_revision=draft.revision,
    )
    if document_ref != draft.draft_id:
        db.session.rollback()
        raise SurveyDraftMaterializationError(
            "document_ref debe coincidir exactamente con draft_id",
            status_code=422,
            reason_code="survey_document_identity_mismatch",
            action_hint="align_document_ref_with_draft_id",
            context={
                "draft_id": draft.draft_id,
                "document_ref": document_ref,
            },
        )
    operation_fingerprint = _operation_fingerprint(
        tenant_id=tenant_id,
        survey_draft_id=draft.id,
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        schema_version=draft.schema_version,
        payload_hash=draft.payload_hash,
    )

    try:
        survey = create_encuesta(
            payload,
            user,
            commit=False,
            content_origin="draft_materialization",
            content_origin_ref=(
                f"draft:{draft.draft_id}:revision:{int(draft.revision)}:"
                f"{draft.payload_hash}"
            ),
        )
        if survey.tenant_id != tenant_id:
            db.session.rollback()
            raise SurveyDraftMaterializationError(
                "La encuesta creada no pertenece al tenant autorizado",
                status_code=500,
                reason_code="survey_materialization_tenant_invariant_failed",
                action_hint="contact_support",
                retryable=False,
            )
        receipt = SurveyDraftMaterialization(
            tenant_id=tenant_id,
            survey_draft_id=draft.id,
            survey_id=survey.id,
            draft_id=draft.draft_id,
            draft_revision=draft.revision,
            schema_version=draft.schema_version,
            document_ref=document_ref,
            payload_hash=draft.payload_hash,
            operation_fingerprint=operation_fingerprint,
            idempotency_key=idempotency_key,
            created_by=getattr(user, "id", None),
        )
        db.session.add(receipt)
        db.session.flush()
        alias = SurveyDraftMaterializationAlias(
            tenant_id=tenant_id,
            materialization=receipt,
            idempotency_key=idempotency_key,
            operation_fingerprint=operation_fingerprint,
        )
        db.session.add(alias)
        _flush_materialization_receipt()
        db.session.commit()
        return SurveyDraftMaterializationResult(
            receipt=receipt,
            survey=survey,
            idempotency_key=idempotency_key,
            replayed=False,
        )
    except IntegrityError as exc:
        # create_encuesta(commit=False), receipt and canonical alias share this
        # transaction. A failed receipt can therefore never leave an orphan.
        db.session.rollback()
        raced_alias = _find_alias(tenant_id, idempotency_key)
        if raced_alias is not None:
            return _result_from_alias(
                raced_alias,
                tenant_id=tenant_id,
                draft_id=draft_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        raced_receipt = _find_receipt(tenant_id, draft_id, expected_revision)
        if raced_receipt is not None:
            return _replay_existing_receipt(
                raced_receipt,
                tenant_id=tenant_id,
                draft_id=draft_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        raise SurveyDraftMaterializationError(
            "No se pudo confirmar la materializacion de forma atomica",
            status_code=409,
            reason_code="survey_materialization_conflict",
            action_hint="retry_same_request",
            retryable=True,
        ) from exc
    except EncuestaError:
        db.session.rollback()
        raise


def serialize_materialization_result(
    result: SurveyDraftMaterializationResult,
) -> dict[str, Any]:
    receipt = result.receipt
    return {
        "contract_version": MATERIALIZATION_CONTRACT_VERSION,
        "ok": True,
        "persisted": True,
        "replayed": result.replayed,
        "idempotency_key": result.idempotency_key,
        "receipt_id": receipt.id,
        "survey_id": receipt.survey_id,
        "draft": {
            "draft_id": receipt.draft_id,
            "revision": receipt.draft_revision,
            "schema_version": receipt.schema_version,
            "document_ref": receipt.document_ref,
            "payload_hash": receipt.payload_hash,
        },
        "survey": serialize_encuesta(result.survey),
    }
