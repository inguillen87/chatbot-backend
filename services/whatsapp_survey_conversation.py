"""Deterministic, tenant-scoped survey participation inside WhatsApp chat.

This transport deliberately owns no language understanding.  It accepts only
structured action identifiers or an exact option number/text/value, reloads the
instrument from the database on every turn, and delegates the durable write to
``save_respuesta``.  Instruments that need richer widgets or restricted
eligibility are routed to the governed web form without pretending a response
was recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import uuid
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import current_app

from models import (
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    EncRespuestaDetalle,
    TenantProfile,
    db,
)
from services.constants import CONTEXTO_MUNICIPIO
from services.encuestas_service import (
    EncuestaError,
    get_public_encuesta,
    public_survey_response_count_contract,
    save_respuesta,
    survey_response_receipt_contract,
)
from services.survey_governance import (
    SurveyGovernanceError,
    survey_governance_contract,
)
from services.survey_tenant_scope import (
    SurveyTenantScopeError,
    resolve_survey_tenant_scope_id,
)
from services.survey_response_provenance import (
    SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED,
    SURVEY_RESPONSE_ORIGIN_REAL,
    SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO,
    build_survey_response_provenance,
)
from models_survey_governance import SurveyGovernanceRelease


WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION = "municipio.whatsapp_survey.v1"
WHATSAPP_REQUIRED_DISCLOSURE_CONTRACT_VERSION = "whatsapp.required_disclosure.v1"
WHATSAPP_SURVEY_FLOW_STATE_KEY = "whatsapp_survey_response_v1"
WHATSAPP_SURVEY_CONVERSATION_STATE = "EN_FLUJO_ENCUESTA_WHATSAPP"

START_ACTION_PREFIX = "encuesta_responder::"
ACTION_PREFIX = "encuesta_wa::"

_ACTION_RE = re.compile(
    r"^encuesta_wa::(?P<kind>consent_accept|consent_reject|answer|cancel|retry)"
    r"::(?P<survey_id>[1-9][0-9]*)::(?P<revision>[1-9][0-9]*)"
    r"(?:::(?P<question_id>[1-9][0-9]*)::(?P<option_id>[1-9][0-9]*))?$"
)

_SUPPORTED_ELIGIBILITY_MODES = frozenset({"open", "self_attested"})
_SUPPORTED_UNIQUENESS_POLICIES = frozenset(
    {
        "libre",
        "por_cookie",
        "cookie",
        "por_phone",
        "phone",
        "por_dni_o_phone",
        "dni_o_phone",
        "por_usuario",
        "usuario",
        "user_id",
        "por_user_id",
    }
)
_PHONE_POLICIES = frozenset({"por_phone", "phone", "por_dni_o_phone", "dni_o_phone"})
_COOKIE_POLICIES = frozenset({"por_cookie", "cookie"})
_USER_POLICIES = frozenset({"por_usuario", "usuario", "user_id", "por_user_id"})

_WEB_FALLBACK_REASON_CODES = frozenset(
    {
        "survey_whatsapp_governed_release_required",
        "survey_whatsapp_restricted_eligibility",
        "survey_whatsapp_question_type_unsupported",
        "survey_whatsapp_option_count_unsupported",
        "survey_whatsapp_uniqueness_policy_unsupported",
        "survey_whatsapp_phone_required",
        "survey_whatsapp_anon_id_required",
        "survey_whatsapp_authentication_required",
    }
)

_CONSENT_ACCEPT_TEXTS = frozenset({"1", "acepto", "si, acepto", "sí, acepto"})
_CONSENT_REJECT_TEXTS = frozenset({"2", "no acepto"})
_MENU_TEXTS = frozenset({"menu", "menú", "menu principal", "menú principal"})
_CANCEL_TEXTS = frozenset({"cancelar"})

_WHATSAPP_LIST_ROW_LIMIT = 10
_WHATSAPP_ROW_TITLE_LIMIT = 24
_QUESTION_NAVIGATION_ROWS = 2
_WHATSAPP_QUESTION_OPTION_LIMIT = (
    _WHATSAPP_LIST_ROW_LIMIT - _QUESTION_NAVIGATION_ROWS
)


@dataclass(frozen=True)
class _LoadedInstrument:
    survey: EncEncuesta
    public_slug: str
    public_url: Optional[str]
    tenant_id: int
    structure_revision: int
    release_id: int
    snapshot_sha256: str
    eligibility_policy_version: str
    consent_policy_version: str
    eligibility_mode: str
    eligibility_declarations: tuple[str, ...]
    consent_public_text: str
    questions: tuple[EncPregunta, ...]


class WhatsAppSurveyConversationError(Exception):
    """Safe internal error with a stable machine reason."""

    def __init__(self, reason_code: str, *, public_message: Optional[str] = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.public_message = public_message


def is_whatsapp_survey_start_action(action_id: Any) -> bool:
    return str(action_id or "").strip().startswith(START_ACTION_PREFIX)


def is_whatsapp_survey_flow_action(action_id: Any) -> bool:
    normalized = str(action_id or "").strip()
    return bool(_ACTION_RE.fullmatch(normalized))


def has_active_whatsapp_survey_flow(context: Mapping[str, Any]) -> bool:
    municipal = _municipal_context(context, create=False)
    return isinstance(municipal.get(WHATSAPP_SURVEY_FLOW_STATE_KEY), Mapping)


def clear_whatsapp_survey_flow(context: Mapping[str, Any]) -> None:
    municipal = _municipal_context(context, create=False)
    municipal.pop(WHATSAPP_SURVEY_FLOW_STATE_KEY, None)
    if municipal.get("estado_conversacion") == WHATSAPP_SURVEY_CONVERSATION_STATE:
        municipal.pop("estado_conversacion", None)
    municipal.pop("menu_opciones", None)


def _municipal_context(context: Mapping[str, Any], *, create: bool) -> dict[str, Any]:
    raw_context_data = context.get("chat_db_context_data")
    if not isinstance(raw_context_data, dict):
        if not create or not isinstance(context, dict):
            return {}
        raw_context_data = {}
        context["chat_db_context_data"] = raw_context_data
    if create:
        municipal = raw_context_data.setdefault(CONTEXTO_MUNICIPIO, {})
    else:
        municipal = raw_context_data.get(CONTEXTO_MUNICIPIO, {})
    return municipal if isinstance(municipal, dict) else {}


def _resolve_authoritative_scope(context: Mapping[str, Any]) -> tuple[TenantProfile, int]:
    tenant = context.get("tenant_profile")
    if tenant is None:
        raw_tenant_id = context.get("tenant_id")
        try:
            tenant_id = int(raw_tenant_id)
        except (TypeError, ValueError, OverflowError):
            tenant_id = 0
        if tenant_id > 0:
            tenant = db.session.get(TenantProfile, tenant_id)
    if tenant is None or not bool(getattr(tenant, "is_active", True)):
        raise WhatsAppSurveyConversationError("survey_tenant_scope_unavailable")
    try:
        scope_id = resolve_survey_tenant_scope_id(tenant)
    except SurveyTenantScopeError as exc:
        raise WhatsAppSurveyConversationError(exc.reason_code) from exc
    return tenant, int(scope_id)


def _tenant_scoped_public_url(url: Optional[str], tenant: TenantProfile) -> Optional[str]:
    candidate = str(url or "").strip()
    if not candidate:
        return None
    tenant_slug = str(getattr(tenant, "slug", "") or "").strip().lower()
    if not tenant_slug:
        return candidate
    parsed = urlsplit(candidate)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["tenant_slug"] = tenant_slug
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _canonical_public_slug(survey: EncEncuesta, requested_slug: str) -> str:
    links = sorted(
        list(getattr(survey, "links", None) or []),
        key=lambda item: int(getattr(item, "id", 0) or 0),
        reverse=True,
    )
    for link in links:
        slug = str(getattr(link, "slug_publico", "") or "").strip().lower()
        if slug:
            return slug
    return str(getattr(survey, "slug", None) or requested_slug).strip().lower()


def _public_url_for(
    survey: EncEncuesta,
    public_slug: str,
    tenant: TenantProfile,
    explicit_public_url: Optional[str],
) -> Optional[str]:
    if explicit_public_url:
        return _tenant_scoped_public_url(explicit_public_url, tenant)
    base_url = str(
        current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
        or current_app.config.get("FRONTEND_URL")
        or current_app.config.get("APP_URL")
        or "https://www.chatboc.ar"
    ).rstrip("/")
    return _tenant_scoped_public_url(f"{base_url}/e/{public_slug}", tenant)


def _load_instrument(
    context: Mapping[str, Any],
    slug_publico: str,
    *,
    explicit_public_url: Optional[str] = None,
) -> _LoadedInstrument:
    tenant, tenant_id = _resolve_authoritative_scope(context)
    requested_slug = str(slug_publico or "").strip().lower()
    try:
        survey = get_public_encuesta(
            requested_slug,
            preferred_tenant_id=tenant_id,
            require_tenant_match=True,
        )
    except EncuestaError as exc:
        raise WhatsAppSurveyConversationError(
            str((exc.payload or {}).get("reason_code") or "survey_not_available")
        ) from exc
    if int(survey.tenant_id) != tenant_id:
        raise WhatsAppSurveyConversationError("survey_tenant_scope_mismatch")

    try:
        governance = survey_governance_contract(survey, validate_integrity=True)
    except SurveyGovernanceError as exc:
        raise WhatsAppSurveyConversationError(exc.reason_code) from exc
    except Exception as exc:
        current_app.logger.exception(
            "[whatsapp-survey] Governed contract unavailable survey_id=%s tenant_id=%s",
            survey.id,
            tenant_id,
        )
        raise WhatsAppSurveyConversationError(
            "survey_governance_contract_unavailable"
        ) from exc
    if governance.get("mode") != "governed_release":
        raise WhatsAppSurveyConversationError("survey_whatsapp_governed_release_required")
    if governance.get("accepting_responses") is not True:
        raise WhatsAppSurveyConversationError(
            str(governance.get("blocked_reason_code") or "survey_not_accepting_responses")
        )

    release = governance.get("active_release")
    if not isinstance(release, Mapping) or release.get("status") != "published":
        raise WhatsAppSurveyConversationError("survey_governance_release_not_active")
    release_governance = release.get("governance")
    if not isinstance(release_governance, Mapping):
        raise WhatsAppSurveyConversationError("survey_governance_contract_incomplete")
    eligibility = release_governance.get("eligibility")
    consent = release_governance.get("consent")
    if not isinstance(eligibility, Mapping) or not isinstance(consent, Mapping):
        raise WhatsAppSurveyConversationError("survey_governance_contract_incomplete")
    eligibility_mode = str(eligibility.get("mode") or "").strip().lower()
    if eligibility_mode not in _SUPPORTED_ELIGIBILITY_MODES:
        raise WhatsAppSurveyConversationError("survey_whatsapp_restricted_eligibility")
    public_text = str(consent.get("public_text") or "").strip()
    if not public_text:
        raise WhatsAppSurveyConversationError("survey_consent_public_text_required")

    questions = tuple(
        EncPregunta.query.filter_by(encuesta_id=survey.id)
        .order_by(EncPregunta.orden.asc(), EncPregunta.id.asc())
        .all()
    )
    if not questions:
        raise WhatsAppSurveyConversationError("survey_whatsapp_questions_required")
    for question in questions:
        options = tuple(
            EncOpcion.query.filter_by(pregunta_id=question.id)
            .order_by(EncOpcion.orden.asc(), EncOpcion.id.asc())
            .populate_existing()
            .all()
        )
        if (
            str(question.tipo or "").strip().lower() != "opcion_unica"
            or question.obligatoria is not True
            or question.logica_condicional is not None
            or len(options) < 2
        ):
            raise WhatsAppSurveyConversationError("survey_whatsapp_question_type_unsupported")
        if len(options) > _WHATSAPP_QUESTION_OPTION_LIMIT:
            raise WhatsAppSurveyConversationError(
                "survey_whatsapp_option_count_unsupported"
            )
        # Expire a previously loaded relationship so all later access in this
        # turn reflects the same database rows validated above.
        db.session.expire(question, ["opciones"])

    uniqueness_policy = str(survey.politica_unicidad or "libre").strip().lower()
    if uniqueness_policy not in _SUPPORTED_UNIQUENESS_POLICIES:
        raise WhatsAppSurveyConversationError("survey_whatsapp_uniqueness_policy_unsupported")
    phone = _participant_phone(context)
    anon_id = _participant_anon_id(context)
    viewer = context.get("viewer_user_obj")
    if uniqueness_policy in _PHONE_POLICIES and not phone:
        raise WhatsAppSurveyConversationError("survey_whatsapp_phone_required")
    if uniqueness_policy in _COOKIE_POLICIES and not anon_id:
        raise WhatsAppSurveyConversationError("survey_whatsapp_anon_id_required")
    if uniqueness_policy in _USER_POLICIES and not getattr(viewer, "id", None):
        raise WhatsAppSurveyConversationError("survey_whatsapp_authentication_required")
    if not bool(survey.anonimo_permitido) and not getattr(viewer, "id", None):
        raise WhatsAppSurveyConversationError("survey_whatsapp_authentication_required")

    public_slug = _canonical_public_slug(survey, requested_slug)
    try:
        release_id = int(release.get("release_id"))
        revision = int(survey.structure_revision or 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WhatsAppSurveyConversationError("survey_governance_contract_incomplete") from exc
    persisted_release = SurveyGovernanceRelease.query.filter_by(
        id=release_id,
        tenant_id=tenant_id,
        survey_id=survey.id,
        status="published",
    ).first()
    if persisted_release is None:
        raise WhatsAppSurveyConversationError("survey_governance_release_not_active")
    snapshot_sha256 = str(release.get("snapshot_sha256") or "").strip().lower()
    eligibility_version = str(persisted_release.eligibility_policy_version or "").strip()
    consent_version = str(persisted_release.consent_policy_version or "").strip()
    if (
        eligibility_version != str(eligibility.get("policy_version") or "").strip()
        or consent_version != str(consent.get("policy_version") or "").strip()
        or snapshot_sha256 != str(persisted_release.snapshot_sha256 or "").strip().lower()
    ):
        raise WhatsAppSurveyConversationError("survey_governance_contract_incomplete")
    if not all((release_id, revision, snapshot_sha256, eligibility_version, consent_version)):
        raise WhatsAppSurveyConversationError("survey_governance_contract_incomplete")

    declarations = eligibility.get("declarations")
    if not isinstance(declarations, Sequence) or isinstance(declarations, (str, bytes)):
        declarations = []
    return _LoadedInstrument(
        survey=survey,
        public_slug=public_slug,
        public_url=_public_url_for(
            survey,
            public_slug,
            tenant,
            explicit_public_url,
        ),
        tenant_id=tenant_id,
        structure_revision=revision,
        release_id=release_id,
        snapshot_sha256=snapshot_sha256,
        eligibility_policy_version=eligibility_version,
        consent_policy_version=consent_version,
        eligibility_mode=eligibility_mode,
        eligibility_declarations=tuple(str(item) for item in declarations if str(item).strip()),
        consent_public_text=public_text,
        questions=questions,
    )


def _participant_anon_id(context: Mapping[str, Any]) -> Optional[str]:
    raw = context.get("anon_id")
    normalized = str(raw or "").strip()
    return normalized[:255] or None


def _participant_phone(context: Mapping[str, Any]) -> Optional[str]:
    candidates: list[Any] = [context.get("phone"), context.get("telefono")]
    viewer = context.get("viewer_user_obj")
    if viewer is not None:
        candidates.extend((getattr(viewer, "telefono", None), getattr(viewer, "phone", None)))
    municipal = _municipal_context(context, create=False)
    contact = municipal.get("contacto_usuario")
    if isinstance(contact, Mapping):
        candidates.extend((contact.get("telefono"), contact.get("phone")))
    resolved_contact = context.get("resolved_contact")
    if isinstance(resolved_contact, Mapping):
        candidates.extend((resolved_contact.get("telefono"), resolved_contact.get("phone")))
    candidates.append(context.get("anon_id"))
    for raw in candidates:
        candidate = str(raw or "").strip()
        if candidate.lower().startswith("whatsapp:"):
            candidate = candidate.split(":", 1)[1].strip()
        compact = re.sub(r"[\s()\-.]", "", candidate)
        if re.fullmatch(r"\+?[1-9][0-9]{7,14}", compact):
            return compact[:32]
    return None


def _state_from_instrument(instrument: _LoadedInstrument) -> dict[str, Any]:
    return {
        "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
        "tenant_id": instrument.tenant_id,
        "survey_id": int(instrument.survey.id),
        "public_slug": instrument.public_slug,
        "public_url": instrument.public_url,
        "structure_revision": instrument.structure_revision,
        "release_id": instrument.release_id,
        "snapshot_sha256": instrument.snapshot_sha256,
        "eligibility_policy_version": instrument.eligibility_policy_version,
        "consent_policy_version": instrument.consent_policy_version,
        "eligibility_mode": instrument.eligibility_mode,
        "privacy_mode": str(instrument.survey.privacy_mode or "legacy"),
        "privacy_policy_version": instrument.survey.privacy_policy_version,
        "privacy_policy_url": instrument.survey.privacy_policy_url,
        "privacy_consent_required": bool(instrument.survey.privacy_consent_required),
        "submission_id": (
            f"wa-chat.{instrument.tenant_id}.{instrument.survey.id}.{uuid.uuid4().hex}"
        ),
        "step": "consent",
        "consent_accepted": False,
        "question_index": 0,
        "question_ids": [int(question.id) for question in instrument.questions],
        "answers": {},
    }


def _action(kind: str, state: Mapping[str, Any], question_id: Any = None, option_id: Any = None) -> str:
    base = (
        f"{ACTION_PREFIX}{kind}::{int(state['survey_id'])}::"
        f"{int(state['structure_revision'])}"
    )
    if question_id is not None and option_id is not None:
        return f"{base}::{int(question_id)}::{int(option_id)}"
    return base


def _navigation_options(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"texto": "Cancelar", "action_id": _action("cancel", state)},
        {"texto": "Menú principal", "action_id": "menu_principal"},
    ]


def _whatsapp_row_label(number: int, text: Any) -> str:
    prefix = f"{number}. "
    normalized = re.sub(r"\s+", " ", str(text or "")).strip() or "Opción"
    available = _WHATSAPP_ROW_TITLE_LIMIT - len(prefix)
    if len(normalized) > available:
        normalized = normalized[: max(1, available - 1)].rstrip() + "…"
    return f"{prefix}{normalized}"


def _consent_payload(instrument: _LoadedInstrument, state: Mapping[str, Any]) -> dict[str, Any]:
    eligibility_line = "Participación abierta."
    if instrument.eligibility_mode == "self_attested":
        declarations = ", ".join(instrument.eligibility_declarations)
        eligibility_line = "Elegibilidad autodeclarada"
        if declarations:
            eligibility_line += f" ({declarations})"
        eligibility_line += "."
    privacy_lines: list[str] = []
    if instrument.survey.privacy_consent_required:
        privacy_lines.append(
            "Al aceptar también prestás el consentimiento de privacidad requerido"
            + (
                f" para la versión {instrument.survey.privacy_policy_version}."
                if instrument.survey.privacy_policy_version
                else "."
            )
        )
    if instrument.survey.privacy_policy_url:
        privacy_lines.append(f"Política de privacidad: {instrument.survey.privacy_policy_url}")
    disclosure_parts = [
        f"*{instrument.survey.titulo}*",
        "Antes de responder, leé y aceptá este consentimiento:",
        instrument.consent_public_text,
        eligibility_line,
        *privacy_lines,
    ]
    disclosure_text = "\n\n".join(
        part for part in disclosure_parts if part != ""
    ).strip()
    disclosure_sha256 = hashlib.sha256(disclosure_text.encode("utf-8")).hexdigest()
    decision_prompt = (
        "Confirmá tu decisión para continuar. Si usás lector de pantalla, "
        "también podés escuchar el consentimiento en el audio adjunto."
    )
    options = [
        {
            "texto": "Sí, acepto",
            "action_id": _action("consent_accept", state),
        },
        {
            "texto": "No acepto",
            "action_id": _action("consent_reject", state),
        },
        {"texto": "Menú principal", "action_id": "menu_principal"},
    ]
    return {
        # The legal disclosure is transported as ordered, byte-bounded plain
        # messages before this short interactive prompt. Keeping the buttons
        # in the final message prevents WhatsApp from showing an acceptance
        # action before the citizen has received the complete text.
        "message_body": decision_prompt,
        "message_type": "interactive_buttons",
        "options_list": options,
        "_whatsapp_required_disclosure": {
            "contract_version": WHATSAPP_REQUIRED_DISCLOSURE_CONTRACT_VERSION,
            "kind": "survey_governance_consent",
            "body": disclosure_text,
            "sha256": disclosure_sha256,
        },
        "fuente": "encuesta_whatsapp_consentimiento_v1",
        "generar_audio": True,
        "audio_text": f"{disclosure_text}\n\n{decision_prompt}",
        "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
        "survey": {
            "id": int(instrument.survey.id),
            "slug": instrument.public_slug,
            "structure_revision": instrument.structure_revision,
            "release_id": instrument.release_id,
            "snapshot_sha256": instrument.snapshot_sha256,
            "eligibility_policy_version": instrument.eligibility_policy_version,
            "consent_policy_version": instrument.consent_policy_version,
        },
    }


def _question_payload(
    instrument: _LoadedInstrument,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    index = int(state.get("question_index") or 0)
    question = instrument.questions[index]
    options = tuple(question.opciones)
    body_lines = [
        f"*Pregunta {index + 1} de {len(instrument.questions)}*",
        str(question.texto).strip(),
        "",
    ]
    response_options: list[dict[str, Any]] = []
    for number, option in enumerate(options, start=1):
        body_lines.append(f"{number}. {str(option.texto).strip()}")
        response_options.append(
            {
                "texto": _whatsapp_row_label(number, option.texto),
                "action_id": _action(
                    "answer",
                    state,
                    question_id=question.id,
                    option_id=option.id,
                ),
            }
        )
    body_lines.extend(
        [
            "",
            "Respondé con el número o con el texto exacto de una opción.",
        ]
    )
    response_options.extend(_navigation_options(state))
    return {
        "message_body": "\n".join(body_lines),
        "message_type": (
            "interactive_buttons" if len(response_options) <= 3 else "interactive_list"
        ),
        "options_list": response_options,
        "fuente": "encuesta_whatsapp_pregunta_v1",
        "generar_audio": True,
        "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
        "survey_progress": {
            "current": index + 1,
            "total": len(instrument.questions),
            "question_id": int(question.id),
        },
    }


def _fallback_payload(
    *,
    public_url: Optional[str],
    reason_code: str,
    reveal_web_form: bool,
) -> dict[str, Any]:
    if reveal_web_form and public_url:
        message = (
            "Esta encuesta requiere un paso que se completa en el formulario seguro. "
            "No registramos ninguna respuesta en esta conversación.\n\n"
            f"Podés continuar acá: {public_url}"
        )
        options = [
            {"texto": "Abrir formulario", "type": "url", "url": public_url},
            {"texto": "Volver a encuestas", "action_id": "mostrar_menu_encuestas"},
            {"texto": "Menú principal", "action_id": "menu_principal"},
        ]
    else:
        message = (
            "No pudimos abrir esa encuesta dentro de esta conversación. "
            "No se registró ninguna respuesta. Volvé al listado para elegir una encuesta activa."
        )
        options = [
            {"texto": "Volver a encuestas", "action_id": "mostrar_menu_encuestas"},
            {"texto": "Menú principal", "action_id": "menu_principal"},
        ]
    return {
        "message_body": message,
        "message_type": "interactive_buttons",
        "options_list": options,
        "fuente": "encuesta_whatsapp_web_fallback_v1",
        "generar_audio": True,
        "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
        "reason_code": reason_code,
        "response_persisted": False,
    }


def start_whatsapp_survey_flow(
    context: Mapping[str, Any],
    slug_publico: str,
    *,
    public_url: Optional[str] = None,
) -> dict[str, Any]:
    """Start a governed survey without trusting menu metadata for scope."""

    clear_whatsapp_survey_flow(context)
    try:
        instrument = _load_instrument(
            context,
            slug_publico,
            explicit_public_url=public_url,
        )
    except WhatsAppSurveyConversationError as exc:
        reveal_web_form = exc.reason_code in _WEB_FALLBACK_REASON_CODES
        fallback_url = public_url
        if reveal_web_form:
            try:
                tenant, _tenant_id = _resolve_authoritative_scope(context)
                if not fallback_url:
                    base_url = str(
                        current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
                        or current_app.config.get("FRONTEND_URL")
                        or current_app.config.get("APP_URL")
                        or "https://www.chatboc.ar"
                    ).rstrip("/")
                    fallback_url = f"{base_url}/e/{str(slug_publico or '').strip().lower()}"
                fallback_url = _tenant_scoped_public_url(fallback_url, tenant)
            except WhatsAppSurveyConversationError:
                fallback_url = None
        return _fallback_payload(
            public_url=fallback_url,
            reason_code=exc.reason_code,
            reveal_web_form=reveal_web_form,
        )

    state = _state_from_instrument(instrument)
    municipal = _municipal_context(context, create=True)
    municipal[WHATSAPP_SURVEY_FLOW_STATE_KEY] = state
    municipal["estado_conversacion"] = WHATSAPP_SURVEY_CONVERSATION_STATE
    payload = _consent_payload(instrument, state)
    municipal["menu_opciones"] = list(payload["options_list"])
    return payload


def _validate_pinned_state(
    state: Mapping[str, Any],
    instrument: _LoadedInstrument,
) -> None:
    expected = {
        "tenant_id": instrument.tenant_id,
        "survey_id": int(instrument.survey.id),
        "structure_revision": instrument.structure_revision,
        "release_id": instrument.release_id,
        "snapshot_sha256": instrument.snapshot_sha256,
        "eligibility_policy_version": instrument.eligibility_policy_version,
        "consent_policy_version": instrument.consent_policy_version,
    }
    for field, expected_value in expected.items():
        actual = state.get(field)
        if field in {"tenant_id", "survey_id", "structure_revision", "release_id"}:
            try:
                actual = int(actual)
            except (TypeError, ValueError, OverflowError):
                actual = None
        if actual != expected_value:
            raise WhatsAppSurveyConversationError(
                "survey_whatsapp_pinned_contract_changed"
            )
    question_ids = [int(question.id) for question in instrument.questions]
    if list(state.get("question_ids") or []) != question_ids:
        raise WhatsAppSurveyConversationError("survey_whatsapp_pinned_contract_changed")


def _parse_action(action_id: Any) -> Optional[dict[str, int | str]]:
    match = _ACTION_RE.fullmatch(str(action_id or "").strip())
    if match is None:
        return None
    parsed: dict[str, int | str] = {"kind": match.group("kind")}
    for field in ("survey_id", "revision", "question_id", "option_id"):
        raw = match.group(field)
        if raw is not None:
            parsed[field] = int(raw)
    has_answer_coordinates = "question_id" in parsed and "option_id" in parsed
    if (parsed["kind"] == "answer") != has_answer_coordinates:
        return None
    return parsed


def _action_matches_state(parsed: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    return (
        int(parsed.get("survey_id") or 0) == int(state.get("survey_id") or 0)
        and int(parsed.get("revision") or 0)
        == int(state.get("structure_revision") or 0)
    )


def _exact_option_match(
    raw_text: Any,
    question: EncPregunta,
) -> tuple[Optional[EncOpcion], bool]:
    candidate = str(raw_text or "").strip()
    if not candidate:
        return None, False
    options = tuple(question.opciones)
    matches: list[EncOpcion] = []
    if candidate.isdigit():
        number = int(candidate)
        if 1 <= number <= len(options):
            matches.append(options[number - 1])
    folded = candidate.casefold()
    for option in options:
        exact_values = {
            str(option.texto or "").strip().casefold(),
            str(option.valor or "").strip().casefold(),
        }
        exact_values.discard("")
        if folded in exact_values and option not in matches:
            matches.append(option)
    return (matches[0], False) if len(matches) == 1 else (None, len(matches) > 1)


def _re_prompt(
    instrument: _LoadedInstrument,
    state: Mapping[str, Any],
    *,
    consent: bool,
    ambiguous: bool = False,
) -> dict[str, Any]:
    payload = (
        _consent_payload(instrument, state)
        if consent
        else _question_payload(instrument, state)
    )
    prefix = (
        "Esa respuesta coincide con más de una opción. Elegí el número correspondiente."
        if ambiguous
        else "No pude identificar una opción exacta."
    )
    payload["message_body"] = f"{prefix}\n\n{payload['message_body']}"
    payload["fuente"] = "encuesta_whatsapp_respuesta_ambigua_v1"
    return payload


def _semantic_location_fields(
    instrument: _LoadedInstrument,
    answers: Mapping[str, Any],
) -> dict[str, str]:
    mapped: dict[str, str] = {}
    semantic_fields = {
        "demographic:city": "ciudad",
        "demographic:province": "provincia",
    }
    for question in instrument.questions:
        target = semantic_fields.get(str(question.logical_ref or "").strip())
        if target is None:
            continue
        try:
            option_id = int(answers.get(str(question.id)))
        except (TypeError, ValueError, OverflowError):
            continue
        option = next((item for item in question.opciones if int(item.id) == option_id), None)
        value = str(getattr(option, "valor", None) or "").strip()
        if not value or len(value) > 120 or any(ord(char) < 32 for char in value):
            continue
        mapped[target] = value
    return mapped


def _shared_location_from_context(context: Mapping[str, Any]) -> dict[str, float]:
    """Return an explicitly shared, valid WGS84 point for the active turn."""

    if context.get("es_ubicacion") is not True:
        return {}
    raw_location = context.get("ubicacion_usuario")
    if not isinstance(raw_location, Mapping):
        return {}

    def _first_present(*keys: str) -> Any:
        for key in keys:
            if key in raw_location and raw_location.get(key) is not None:
                return raw_location.get(key)
        return None

    def _coordinate(value: Any, *, minimum: float, maximum: float) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            return None
        return parsed

    lat = _coordinate(
        _first_present("lat", "latitude", "latitud"),
        minimum=-90.0,
        maximum=90.0,
    )
    lng = _coordinate(
        _first_present("lng", "lon", "longitude", "longitud"),
        minimum=-180.0,
        maximum=180.0,
    )
    if lat is None or lng is None:
        return {}
    return {"lat": lat, "lng": lng}


def _remember_shared_location(
    context: Mapping[str, Any],
    instrument: _LoadedInstrument,
    state: Mapping[str, Any],
) -> None:
    """Pin a native WhatsApp location until the governed response is saved."""

    if not isinstance(state, dict):
        return
    if str(instrument.survey.privacy_mode or "legacy") == "source_anonymous":
        state.pop("shared_location", None)
        return
    location = _shared_location_from_context(context)
    if location:
        state["shared_location"] = location


def _build_submission_payload(
    context: Mapping[str, Any],
    instrument: _LoadedInstrument,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    answers = state.get("answers") if isinstance(state.get("answers"), Mapping) else {}
    response_items = []
    for question in instrument.questions:
        option_id = int(answers[str(question.id)])
        response_items.append(
            {"pregunta_id": int(question.id), "opcion_ids": [option_id]}
        )
    payload: dict[str, Any] = {
        "submission_id": str(state["submission_id"]),
        "instrument_revision": instrument.structure_revision,
        "respuestas": response_items,
        "canal": "whatsapp_chat",
        "phone": _participant_phone(context),
        "governance": {
            "release_id": instrument.release_id,
            "snapshot_sha256": instrument.snapshot_sha256,
            "eligibility_policy_version": instrument.eligibility_policy_version,
            "consent_policy_version": instrument.consent_policy_version,
            "consent_accepted": state.get("consent_accepted") is True,
            "eligibility_acknowledged": state.get("consent_accepted") is True,
        },
        **_semantic_location_fields(instrument, answers),
    }
    shared_location = state.get("shared_location")
    if isinstance(shared_location, Mapping):
        lat = shared_location.get("lat")
        lng = shared_location.get("lng")
        if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
            payload["lat"] = float(lat)
            payload["lng"] = float(lng)
    if instrument.survey.privacy_consent_required:
        payload["privacy_consent"] = state.get("consent_accepted") is True
        payload["privacy_policy_version"] = instrument.survey.privacy_policy_version
    return payload


def _live_results(instrument: _LoadedInstrument) -> Optional[dict[str, Any]]:
    if not instrument.survey.mostrar_resultados_envivo:
        return None
    response_count_query = db.session.query(db.func.count(EncRespuesta.id)).filter(
        EncRespuesta.tenant_id == instrument.tenant_id,
        EncRespuesta.encuesta_id == instrument.survey.id,
    )
    total = int(
        response_count_query.filter(
            EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL
        ).scalar()
        or 0
    )
    count_privacy = public_survey_response_count_contract(
        instrument.survey,
        total,
    )
    source_anonymous = count_privacy.get("privacy_mode") == "source_anonymous"
    if bool(count_privacy.get("suppressed")):
        # This is deliberately the complete public representation for every
        # source-anonymous cohort from zero through k-1.  Do not add aggregate,
        # option, provenance or origin counts here: WhatsApp acknowledgements
        # and duplicate/replay paths would otherwise become count oracles.
        return {
            "results_available": False,
            "privacy": count_privacy,
        }

    synthetic_count = int(
        response_count_query.filter(
            EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_SYNTHETIC_DEMO
        ).scalar()
        or 0
    )
    unverified_count = int(
        response_count_query.filter(
            EncRespuesta.response_origin
            == SURVEY_RESPONSE_ORIGIN_LEGACY_UNVERIFIED
        ).scalar()
        or 0
    )
    question_ids = [int(question.id) for question in instrument.questions]
    counts_by_question: dict[int, dict[int, int]] = {
        question_id: {} for question_id in question_ids
    }
    if question_ids:
        grouped_counts = (
            db.session.query(
                EncRespuestaDetalle.pregunta_id,
                EncRespuestaDetalle.opcion_id,
                db.func.count(EncRespuestaDetalle.id),
            )
            .join(
                EncRespuesta,
                EncRespuesta.id == EncRespuestaDetalle.respuesta_id,
            )
            .filter(
                EncRespuesta.tenant_id == instrument.tenant_id,
                EncRespuesta.encuesta_id == instrument.survey.id,
                EncRespuesta.response_origin == SURVEY_RESPONSE_ORIGIN_REAL,
                EncRespuestaDetalle.pregunta_id.in_(question_ids),
                EncRespuestaDetalle.opcion_id.isnot(None),
            )
            .group_by(
                EncRespuestaDetalle.pregunta_id,
                EncRespuestaDetalle.opcion_id,
            )
            .all()
        )
        for question_id, option_id, count in grouped_counts:
            counts_by_question.setdefault(int(question_id), {})[
                int(option_id)
            ] = int(count or 0)

    questions: list[dict[str, Any]] = []
    for question in instrument.questions:
        counts = counts_by_question.get(int(question.id), {})
        question_total = sum(int(value or 0) for value in counts.values())
        question_privacy = (
            public_survey_response_count_contract(
                instrument.survey,
                question_total,
            )
            if source_anonymous
            else None
        )
        if question_privacy and bool(question_privacy.get("suppressed")):
            questions.append(
                {
                    "question_id": int(question.id),
                    "text": question.texto,
                    "total": None,
                    "options": [],
                    "privacy": question_privacy,
                }
            )
            continue

        minimum = int((question_privacy or {}).get("minimum_cell_size") or 0)
        has_positive_small_option = minimum > 0 and any(
            0 < int(counts.get(option.id, 0) or 0) < minimum
            for option in question.opciones
        )
        if has_positive_small_option:
            questions.append(
                {
                    "question_id": int(question.id),
                    "text": question.texto,
                    "total": question_total,
                    "options": [],
                    "privacy": {
                        **(question_privacy or {}),
                        "suppressed": True,
                        "reason_code": "minimum_cell_size_not_met",
                    },
                }
            )
            continue

        question_payload = {
            "question_id": int(question.id),
            "text": question.texto,
            "total": question_total,
            "options": [
                {
                    "option_id": int(option.id),
                    "text": option.texto,
                    "votes": int(counts.get(option.id, 0) or 0),
                    "percentage": round(
                        (int(counts.get(option.id, 0) or 0) / question_total * 100),
                        2,
                    )
                    if question_total
                    else 0.0,
                }
                for option in question.opciones
            ],
        }
        if question_privacy is not None:
            question_payload["privacy"] = question_privacy
        questions.append(question_payload)
    results = {
        "total_responses": total,
        "questions": questions,
        "data_provenance": build_survey_response_provenance(
            real_count=total,
            synthetic_count=synthetic_count,
            unverified_count=unverified_count,
            mode="real",
        ),
    }
    if source_anonymous:
        results.update(
            {
                "results_available": True,
                "privacy": count_privacy,
            }
        )
    return results


def _completion_payload(
    instrument: _LoadedInstrument,
    state: Mapping[str, Any],
    response: EncRespuesta,
) -> dict[str, Any]:
    receipt = survey_response_receipt_contract(response)
    replayed = bool((receipt or {}).get("replayed"))
    receipt_id = int((receipt or {}).get("receipt_id") or response.id)
    results = _live_results(instrument)
    body_lines = [
        "✅ *Participación registrada*",
        f"Encuesta: {instrument.survey.titulo}",
        f"Recibo de participación: {receipt_id}",
    ]
    if results is not None and bool(
        (results.get("privacy") or {}).get("suppressed")
    ):
        privacy_reason = str(
            (results.get("privacy") or {}).get("reason_code") or ""
        ).strip()
        if privacy_reason == "source_anonymous_results_withheld_until_close":
            body_lines.append(
                "Los resultados agregados estarán disponibles cuando cierre la encuesta."
            )
        else:
            body_lines.append(
                "Los resultados agregados se mostrarán cuando alcancen el umbral mínimo de privacidad."
            )
    elif results is not None:
        body_lines.append(f"Respuestas registradas: {results['total_responses']}")
    else:
        body_lines.append("Los resultados no están publicados en vivo.")
    if instrument.public_url:
        body_lines.extend(("", f"Ver o compartir: {instrument.public_url}"))
    options = [
        {
            "texto": "Compartir encuesta",
            "action_id": f"encuesta_compartir::{instrument.public_slug}",
        },
        {"texto": "Ver encuestas", "action_id": "mostrar_menu_encuestas"},
        {"texto": "Menú principal", "action_id": "menu_principal"},
    ]
    return {
        "success": True,
        "message_body": "\n".join(body_lines),
        "message_type": "interactive_buttons",
        "options_list": options,
        "fuente": "encuesta_whatsapp_confirmada_v1",
        "generar_audio": True,
        "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
        "response_id": int(response.id),
        "receipt_id": receipt_id,
        "response_persisted": True,
        "idempotency": receipt,
        "replayed": replayed,
        "results": results,
        "share_url": instrument.public_url,
        "share_action_id": f"encuesta_compartir::{instrument.public_slug}",
        "survey": {
            "id": int(instrument.survey.id),
            "slug": instrument.public_slug,
            "structure_revision": instrument.structure_revision,
            "release_id": instrument.release_id,
            "snapshot_sha256": instrument.snapshot_sha256,
        },
    }


def _submission_retry_payload(
    state: Mapping[str, Any],
    *,
    reason_code: str,
) -> dict[str, Any]:
    if isinstance(state, dict):
        state["step"] = "retry"
        state["last_submission_error"] = reason_code
    return {
        "message_body": (
            "No pudimos confirmar el recibo en este momento. Conservamos la "
            "misma operación para que puedas reintentar sin duplicar tu respuesta."
        ),
        "message_type": "interactive_buttons",
        "options_list": [
            {"texto": "Reintentar", "action_id": _action("retry", state)},
            *_navigation_options(state),
        ],
        "fuente": "encuesta_whatsapp_reintento_v1",
        "generar_audio": True,
        "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
        "reason_code": reason_code,
        "response_persisted": False,
        "retryable": True,
    }


def _submit(
    context: Mapping[str, Any],
    instrument: _LoadedInstrument,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    if state.get("consent_accepted") is not True:
        return _re_prompt(instrument, state, consent=True)
    payload = _build_submission_payload(context, instrument, state)
    request_ctx = {
        "anon_id": _participant_anon_id(context),
        "canal": "whatsapp_chat",
        "ip": None,
        "user_agent": "chatboc-whatsapp-conversation",
    }
    viewer = context.get("viewer_user_obj")
    try:
        response = save_respuesta(
            instrument.public_slug,
            payload,
            request_ctx,
            preferred_tenant_id=instrument.tenant_id,
            require_tenant_match=True,
            authenticated_user=viewer if getattr(viewer, "id", None) else None,
            submission_id=str(state["submission_id"]),
            emit_realtime_update=True,
        )
    except EncuestaError as exc:
        db.session.rollback()
        reason_code = str((exc.payload or {}).get("reason_code") or "survey_submission_failed")
        if reason_code == "survey_response_duplicate":
            clear_whatsapp_survey_flow(context)
            return {
                "message_body": (
                    "Ya existe una participación registrada para esta encuesta con tu "
                    "identificador. No agregamos un voto duplicado."
                    + (f"\n\nVer o compartir: {instrument.public_url}" if instrument.public_url else "")
                ),
                "message_type": "interactive_buttons",
                "options_list": [
                    {
                        "texto": "Compartir encuesta",
                        "action_id": f"encuesta_compartir::{instrument.public_slug}",
                    },
                    {"texto": "Ver encuestas", "action_id": "mostrar_menu_encuestas"},
                    {"texto": "Menú principal", "action_id": "menu_principal"},
                ],
                "fuente": "encuesta_whatsapp_duplicada_v1",
                "generar_audio": True,
                "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
                "reason_code": reason_code,
                "response_persisted": False,
                "duplicate_prevented": True,
                "results": _live_results(instrument),
                "share_url": instrument.public_url,
            }
        if int(getattr(exc, "status_code", 500) or 500) >= 500:
            return _submission_retry_payload(state, reason_code=reason_code)
        clear_whatsapp_survey_flow(context)
        return _fallback_payload(
            public_url=instrument.public_url,
            reason_code=reason_code,
            reveal_web_form=True,
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "[whatsapp-survey] Unexpected submission failure survey_id=%s tenant_id=%s",
            instrument.survey.id,
            instrument.tenant_id,
        )
        return _submission_retry_payload(
            state,
            reason_code="survey_submission_unavailable",
        )
    payload = _completion_payload(instrument, state, response)
    clear_whatsapp_survey_flow(context)
    return payload


def handle_whatsapp_survey_flow_turn(
    context: Mapping[str, Any],
    *,
    text: Any,
    action_id: Any = None,
) -> Optional[dict[str, Any]]:
    """Consume one active WhatsApp survey turn, or return ``None`` if inactive."""

    municipal = _municipal_context(context, create=False)
    state = municipal.get(WHATSAPP_SURVEY_FLOW_STATE_KEY)
    if not isinstance(state, Mapping):
        return None

    normalized_action = str(action_id or "").strip()
    normalized_text = str(text or "").strip()
    lowered_text = normalized_text.casefold()
    if normalized_action == "menu_principal" or lowered_text in _MENU_TEXTS:
        clear_whatsapp_survey_flow(context)
        return {"_survey_navigation_action": "menu_principal"}
    if normalized_action == "cancelar" or lowered_text in _CANCEL_TEXTS:
        clear_whatsapp_survey_flow(context)
        return {
            "message_body": "Participación cancelada. No registramos ninguna respuesta.",
            "message_type": "interactive_buttons",
            "options_list": [
                {"texto": "Volver a encuestas", "action_id": "mostrar_menu_encuestas"},
                {"texto": "Menú principal", "action_id": "menu_principal"},
            ],
            "fuente": "encuesta_whatsapp_cancelada_v1",
            "generar_audio": True,
            "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
            "response_persisted": False,
        }

    public_slug = str(state.get("public_slug") or "").strip().lower()
    public_url = str(state.get("public_url") or "").strip() or None
    try:
        instrument = _load_instrument(
            context,
            public_slug,
            explicit_public_url=public_url,
        )
        _validate_pinned_state(state, instrument)
        _remember_shared_location(context, instrument, state)
    except WhatsAppSurveyConversationError as exc:
        clear_whatsapp_survey_flow(context)
        reveal_web_form = exc.reason_code in _WEB_FALLBACK_REASON_CODES
        fallback_url = None
        if reveal_web_form:
            try:
                tenant, _tenant_id = _resolve_authoritative_scope(context)
                fallback_url = _tenant_scoped_public_url(public_url, tenant)
            except WhatsAppSurveyConversationError:
                fallback_url = None
        return _fallback_payload(
            public_url=fallback_url,
            reason_code=exc.reason_code,
            reveal_web_form=reveal_web_form,
        )

    parsed_action = _parse_action(normalized_action)
    if normalized_action.startswith(ACTION_PREFIX):
        if parsed_action is None or not _action_matches_state(parsed_action, state):
            return _re_prompt(
                instrument,
                state,
                consent=state.get("step") == "consent",
            )
        kind = str(parsed_action["kind"])
        if kind == "cancel":
            clear_whatsapp_survey_flow(context)
            return {
                "message_body": "Participación cancelada. No registramos ninguna respuesta.",
                "message_type": "interactive_buttons",
                "options_list": [
                    {"texto": "Volver a encuestas", "action_id": "mostrar_menu_encuestas"},
                    {"texto": "Menú principal", "action_id": "menu_principal"},
                ],
                "fuente": "encuesta_whatsapp_cancelada_v1",
                "generar_audio": True,
                "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
                "response_persisted": False,
            }

    if state.get("step") == "consent":
        accept = bool(
            (parsed_action and parsed_action.get("kind") == "consent_accept")
            or (not normalized_action and lowered_text in _CONSENT_ACCEPT_TEXTS)
        )
        reject = bool(
            (parsed_action and parsed_action.get("kind") == "consent_reject")
            or (not normalized_action and lowered_text in _CONSENT_REJECT_TEXTS)
        )
        if reject:
            clear_whatsapp_survey_flow(context)
            return {
                "message_body": (
                    "Entendido. No aceptaste el consentimiento y no registramos ninguna respuesta."
                ),
                "message_type": "interactive_buttons",
                "options_list": [
                    {"texto": "Volver a encuestas", "action_id": "mostrar_menu_encuestas"},
                    {"texto": "Menú principal", "action_id": "menu_principal"},
                ],
                "fuente": "encuesta_whatsapp_consentimiento_rechazado_v1",
                "generar_audio": True,
                "contract_version": WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION,
                "response_persisted": False,
            }
        if not accept:
            return _re_prompt(instrument, state, consent=True)
        state["consent_accepted"] = True
        state["step"] = "question"
        state["question_index"] = 0
        payload = _question_payload(instrument, state)
        municipal["menu_opciones"] = list(payload["options_list"])
        return payload

    answers = state.get("answers")
    if not isinstance(answers, dict):
        answers = {}
        state["answers"] = answers
    question_index = int(state.get("question_index") or 0)
    if state.get("step") == "retry":
        if parsed_action and parsed_action.get("kind") == "retry":
            return _submit(context, instrument, state)
        return _submission_retry_payload(
            state,
            reason_code=str(
                state.get("last_submission_error") or "survey_submission_pending"
            ),
        )
    if question_index >= len(instrument.questions):
        return _submission_retry_payload(
            state,
            reason_code="survey_submission_pending",
        )

    question = instrument.questions[question_index]
    option: Optional[EncOpcion] = None
    ambiguous = False
    if parsed_action and parsed_action.get("kind") == "answer":
        if int(parsed_action.get("question_id") or 0) != int(question.id):
            return _re_prompt(instrument, state, consent=False)
        option_id = int(parsed_action.get("option_id") or 0)
        option = next(
            (candidate for candidate in question.opciones if int(candidate.id) == option_id),
            None,
        )
    elif not normalized_action:
        option, ambiguous = _exact_option_match(normalized_text, question)
    if option is None:
        return _re_prompt(
            instrument,
            state,
            consent=False,
            ambiguous=ambiguous,
        )

    answers[str(question.id)] = int(option.id)
    state["question_index"] = question_index + 1
    if int(state["question_index"]) < len(instrument.questions):
        payload = _question_payload(instrument, state)
        municipal["menu_opciones"] = list(payload["options_list"])
        return payload
    return _submit(context, instrument, state)


__all__ = [
    "ACTION_PREFIX",
    "START_ACTION_PREFIX",
    "WHATSAPP_SURVEY_CONVERSATION_STATE",
    "WHATSAPP_SURVEY_FLOW_CONTRACT_VERSION",
    "WHATSAPP_SURVEY_FLOW_STATE_KEY",
    "clear_whatsapp_survey_flow",
    "handle_whatsapp_survey_flow_turn",
    "has_active_whatsapp_survey_flow",
    "is_whatsapp_survey_flow_action",
    "is_whatsapp_survey_start_action",
    "start_whatsapp_survey_flow",
]
