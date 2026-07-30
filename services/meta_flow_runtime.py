"""Tenant-bound runtime for Meta WhatsApp Flows Data Exchange."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hmac
import hashlib
import io
import json
import os
import re
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from flask import current_app
from sqlalchemy.orm import joinedload
from werkzeug.datastructures import FileStorage

from models import (
    AuditEvent,
    EncEncuesta,
    MarketOrder,
    MunicipioTicket,
    Order,
    OrderEvent,
    PedidoConversacional,
    ProviderSender,
    PymePedido,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    TicketComentario,
    User,
    WhatsAppFlowInteraction,
    db,
)
from services.meta_flow_data_exchange import (
    DATA_API_VERSION,
    MetaFlowActionError,
    MetaFlowConfigurationError,
    MetaFlowEndpointConfig,
    MetaFlowRequestContext,
)
from services.attachment_service import create_attachment_with_thumbnail
from services.gcs_service import guardar_adjunto_y_thumbnail
from services.meta_flow_media import (
    DownloadedFlowMedia,
    MetaFlowMediaError,
    download_claim_evidence_media,
    normalize_claim_evidence,
)
from services.tenant_ticket_scope import scoped_municipio_ticket_query
from services.whatsapp_flow_security import (
    verify_whatsapp_flow_endpoint_token,
    whatsapp_flow_token_key_ready,
)


CLAIM_FLOW_ID = "claim_tracking_helpdesk"
CLAIM_EVIDENCE_FLOW_ID = "claim_evidence"
ORDER_FLOW_ID = "order_checkout"
SURVEY_FLOW_ID = "survey_vote"
SUPPORTED_FLOW_IDS = frozenset(
    {CLAIM_FLOW_ID, CLAIM_EVIDENCE_FLOW_ID, ORDER_FLOW_ID, SURVEY_FLOW_ID}
)

_ENDPOINT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,126}$")
_SAFE_WABA = re.compile(r"[^A-Za-z0-9]+")
_TICKET_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_PIN = re.compile(r"^[A-Za-z0-9]{4,12}$")
_ORDER_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SURVEY_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")

_READY_INTERACTION_STATES = frozenset({"sent", "send_uncertain", "consumed"})
_CLAIM_SCREENS = frozenset({"CLAIM_LOOKUP", "CLAIM_RESULT"})
_CLAIM_EVIDENCE_SCREENS = frozenset(
    {
        "CLAIM_EVIDENCE_LOOKUP",
        "CLAIM_EVIDENCE_PHOTOS",
        "CLAIM_EVIDENCE_DOCUMENTS",
        "CLAIM_EVIDENCE_SUCCESS",
    }
)
_ORDER_SCREENS = frozenset({"ORDER_DETAILS", "ORDER_CONFIRM"})
_SURVEY_QUESTION_SCREENS = (
    "SURVEY_QUESTION_ONE",
    "SURVEY_QUESTION_TWO",
    "SURVEY_QUESTION_THREE",
    "SURVEY_QUESTION_FOUR",
    "SURVEY_QUESTION_FIVE",
)
_SURVEY_SCREENS = frozenset((*_SURVEY_QUESTION_SCREENS, "SURVEY_CONFIRM"))
_MAX_NATIVE_SURVEY_OPTIONS = 20

_STATUS_LABELS = {
    "nuevo": "Recibido",
    "open": "Recibido",
    "abierto": "Recibido",
    "pendiente": "En validacion",
    "validando": "En validacion",
    "asignado": "Asignado",
    "esperando_agente": "Esperando agente",
    "en_proceso": "En proceso",
    "in_progress": "En proceso",
    "resuelto": "Resuelto",
    "resolved": "Resuelto",
    "cerrado": "Cerrado",
    "closed": "Cerrado",
}


class EndpointTokenVerifier(Protocol):
    """Endpoint-safe verifier; it must not require client-provided identity."""

    def __call__(
        self,
        token: str,
        *,
        secret: str,
        tenant_id: int,
        allowed_flow_ids: Iterable[str],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _ResolvedEndpoint:
    endpoint_id: str
    tenant: TenantProfile
    waba_id: str
    senders: tuple[ProviderSender, ...]
    primary_sender: ProviderSender
    private_key_pem: str
    private_key_passphrase: str | None
    app_secret: str
    previous_app_secret: str | None
    flow_token_secret: str | None

    @property
    def sender_ids(self) -> tuple[int, ...]:
        return tuple(sorted({int(sender.id) for sender in self.senders}))


@dataclass(frozen=True)
class _VerifiedInvocation:
    interaction_id: int
    tenant_id: int
    provider_sender_id: int
    flow_id: str
    meta_flow_id: str | None
    recipient_hash: str
    data_contract: tuple[str, ...]


class MetaFlowRuntime:
    """Resolve WABA endpoints and expose side-effect-free Flow handlers."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        token_verifier: EndpointTokenVerifier | None = None,
    ) -> None:
        self._environ = environ if environ is not None else os.environ
        self._token_verifier = token_verifier or verify_whatsapp_flow_endpoint_token

    @property
    def token_verifier_ready(self) -> bool:
        return callable(self._token_verifier)

    def resolve(self, endpoint_id: str) -> MetaFlowEndpointConfig | None:
        normalized_endpoint = _normalize_endpoint_id(endpoint_id)
        resolved = self._resolve_endpoint(normalized_endpoint)
        if resolved is None:
            return None

        handlers = {
            "init": self._handler(resolved, "init"),
            "back": self._handler(resolved, "back"),
            "data_exchange": self._handler(resolved, "data_exchange"),
            "error": self._handler(resolved, "error"),
        }
        return MetaFlowEndpointConfig(
            endpoint_id=resolved.endpoint_id,
            tenant_id=resolved.tenant.id,
            waba_id=resolved.waba_id,
            private_key_pem=resolved.private_key_pem,
            private_key_passphrase=resolved.private_key_passphrase,
            app_secret=resolved.app_secret,
            previous_app_secret=resolved.previous_app_secret,
            handlers=handlers,
        )

    def __call__(self, endpoint_id: str) -> MetaFlowEndpointConfig | None:
        return self.resolve(endpoint_id)

    def _resolve_endpoint(self, endpoint_id: str) -> _ResolvedEndpoint | None:
        senders = (
            ProviderSender.query.options(joinedload(ProviderSender.tenant))
            .filter(ProviderSender.channel == "whatsapp")
            .order_by(ProviderSender.updated_at.desc(), ProviderSender.id.desc())
            .all()
        )
        matched = [sender for sender in senders if _sender_matches_endpoint(sender, endpoint_id)]
        if not matched:
            return None

        scopes = {
            (int(sender.tenant_id), str(sender.waba_id or "").strip())
            for sender in matched
        }
        if len(scopes) != 1:
            raise MetaFlowConfigurationError("endpoint_scope_ambiguous")
        tenant_id, waba_id = next(iter(scopes))
        if not waba_id:
            raise MetaFlowConfigurationError("waba_id_not_configured")

        tenant = matched[0].tenant
        if tenant is None or int(tenant.id) != tenant_id or not bool(tenant.is_active):
            raise MetaFlowConfigurationError("tenant_scope_invalid")

        scoped_senders = tuple(
            sender
            for sender in senders
            if int(sender.tenant_id) == tenant_id
            and str(sender.waba_id or "").strip() == waba_id
        )
        if not scoped_senders:
            raise MetaFlowConfigurationError("waba_sender_not_found")
        primary = matched[0]
        sections = _secret_sections(primary, tenant)
        fallback_prefix = _waba_env_prefix(waba_id)

        private_key = _resolve_secret(
            sections,
            ("private_key_ref", "private_key_pem_ref", "waba_private_key_ref"),
            f"{fallback_prefix}_PRIVATE_KEY_PEM",
            self._environ,
            required=True,
            multiline=True,
        )
        passphrase = _resolve_secret(
            sections,
            ("private_key_passphrase_ref", "passphrase_ref"),
            f"{fallback_prefix}_PRIVATE_KEY_PASSPHRASE",
            self._environ,
        )
        app_secret = _resolve_secret(
            sections,
            ("app_secret_ref", "meta_app_secret_ref"),
            f"{fallback_prefix}_APP_SECRET",
            self._environ,
            required=True,
        )
        previous_secret = _resolve_secret(
            sections,
            ("previous_app_secret_ref", "previous_meta_app_secret_ref"),
            f"{fallback_prefix}_PREVIOUS_APP_SECRET",
            self._environ,
        )
        token_secret = str(self._environ.get("WHATSAPP_FLOW_TOKEN_KEY_V1") or "").strip()
        if not whatsapp_flow_token_key_ready(token_secret):
            token_secret = _resolve_secret(
                sections,
                ("flow_token_key_ref", "flow_token_secret_ref"),
                f"{fallback_prefix}_FLOW_TOKEN_KEY_V1",
                self._environ,
                required=True,
            )
        if not whatsapp_flow_token_key_ready(token_secret):
            raise MetaFlowConfigurationError("flow_token_key_not_configured")
        return _ResolvedEndpoint(
            endpoint_id=endpoint_id,
            tenant=tenant,
            waba_id=waba_id,
            senders=scoped_senders,
            primary_sender=primary,
            private_key_pem=private_key or "",
            private_key_passphrase=passphrase,
            app_secret=app_secret or "",
            previous_app_secret=previous_secret,
            flow_token_secret=token_secret,
        )

    def _handler(
        self,
        endpoint: _ResolvedEndpoint,
        expected_action: str,
    ) -> Callable[[Mapping[str, Any], MetaFlowRequestContext], Mapping[str, Any]]:
        def handle(
            payload: Mapping[str, Any],
            context: MetaFlowRequestContext,
        ) -> Mapping[str, Any]:
            _validate_context(context, endpoint, expected_action)
            _validate_version(payload)
            if expected_action == "error":
                invocation = self._verify_invocation(payload, endpoint)
                _validate_screen(payload.get("screen"), invocation.flow_id, optional=True)
                return _handle_error_notification(payload)

            invocation = self._verify_invocation(payload, endpoint)
            if expected_action == "init":
                return self._handle_init(payload, endpoint, invocation)
            if expected_action == "back":
                return self._handle_back(payload, endpoint, invocation)
            return self._handle_data_exchange(payload, endpoint, invocation)

        return handle

    def _verify_invocation(
        self,
        payload: Mapping[str, Any],
        endpoint: _ResolvedEndpoint,
    ) -> _VerifiedInvocation:
        token = _required_string(payload.get("flow_token"), "flow_token_invalid", 4096)
        if not endpoint.flow_token_secret:
            raise _action_error(
                "flow_token_key_not_configured",
                "Flow session verification is unavailable.",
                503,
            )
        verifier = self._token_verifier
        if not callable(verifier):
            raise _action_error(
                "flow_token_endpoint_verifier_unavailable",
                "Flow session verification is unavailable.",
                503,
            )
        try:
            verified = verifier(
                token,
                secret=endpoint.flow_token_secret,
                tenant_id=int(endpoint.tenant.id),
                allowed_flow_ids=SUPPORTED_FLOW_IDS,
            )
        except MetaFlowActionError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "flow_token_verification_failed")
            if code in {
                "invalid_flow_token",
                "expired_flow_token",
                "unknown_flow_invocation",
                "inactive_flow_invocation",
                "flow_token_scope_mismatch",
                "flow_token_sender_mismatch",
                "flow_invocation_scope_mismatch",
                "unrecognized_flow",
            }:
                raise _action_error(
                    "invalid_flow_token",
                    "This message is no longer available.",
                    427,
                ) from exc
            raise _action_error(
                "flow_token_verification_failed",
                "Flow session verification is unavailable.",
                503,
            ) from exc
        if not isinstance(verified, Mapping):
            raise _action_error(
                "flow_token_verification_failed",
                "Flow session verification is unavailable.",
                503,
            )
        try:
            invocation = _VerifiedInvocation(
                interaction_id=int(verified.get("interaction_id")),
                tenant_id=int(verified.get("tenant_id")),
                provider_sender_id=int(verified.get("provider_sender_id")),
                flow_id=str(verified.get("flow_id") or "").strip(),
                meta_flow_id=str(verified.get("meta_flow_id") or "").strip() or None,
                recipient_hash=str(verified.get("recipient_hash") or "").strip(),
                data_contract=tuple(
                    str(item)
                    for item in (verified.get("data_contract") or ())
                    if isinstance(item, str)
                ),
            )
        except (TypeError, ValueError) as exc:
            raise _action_error(
                "flow_token_scope_invalid",
                "This message is no longer available.",
                427,
            ) from exc
        if (
            invocation.interaction_id <= 0
            or invocation.tenant_id != int(endpoint.tenant.id)
            or invocation.provider_sender_id not in endpoint.sender_ids
            or invocation.flow_id not in SUPPORTED_FLOW_IDS
            or not re.fullmatch(r"[0-9a-f]{64}", invocation.recipient_hash)
        ):
            raise _action_error(
                "flow_token_scope_invalid",
                "This message is no longer available.",
                427,
            )
        return invocation

    def _handle_init(
        self,
        payload: Mapping[str, Any],
        endpoint: _ResolvedEndpoint,
        invocation: _VerifiedInvocation,
    ) -> Mapping[str, Any]:
        _validate_screen(payload.get("screen"), invocation.flow_id, optional=True)
        if invocation.flow_id == ORDER_FLOW_ID:
            self._load_order_context(endpoint, invocation)
            return {"screen": "ORDER_DETAILS", "data": {}}
        if invocation.flow_id == SURVEY_FLOW_ID:
            survey, _, interaction = self._load_survey_context(endpoint, invocation)
            staged = _survey_staged_answers(interaction.metadata_json)
            visible_questions = _visible_native_survey_questions(survey, staged)
            return _survey_question_response(
                survey,
                visible_questions,
                0,
                interaction=interaction,
            )
        if invocation.flow_id == CLAIM_EVIDENCE_FLOW_ID:
            return {"screen": "CLAIM_EVIDENCE_LOOKUP", "data": {}}
        return {"screen": "CLAIM_LOOKUP", "data": {}}

    def _handle_back(
        self,
        payload: Mapping[str, Any],
        endpoint: _ResolvedEndpoint,
        invocation: _VerifiedInvocation,
    ) -> Mapping[str, Any]:
        _validate_screen(payload.get("screen"), invocation.flow_id, optional=False)
        if invocation.flow_id == ORDER_FLOW_ID:
            self._load_order_context(endpoint, invocation)
            return {"screen": "ORDER_DETAILS", "data": {}}
        if invocation.flow_id == SURVEY_FLOW_ID:
            survey, _, interaction = self._load_survey_context(
                endpoint,
                invocation,
            )
            staged = _survey_staged_answers(interaction.metadata_json)
            visible_questions = _visible_native_survey_questions(survey, staged)
            current_screen = str(payload.get("screen") or "")
            if current_screen == "SURVEY_CONFIRM":
                target_index = len(visible_questions) - 1
            else:
                current_index = _SURVEY_QUESTION_SCREENS.index(current_screen)
                if current_index >= len(visible_questions):
                    raise _action_error(
                        "survey_screen_out_of_range",
                        "Flow screen is invalid.",
                        400,
                    )
                target_index = max(0, current_index - 1)
            return _survey_question_response(
                survey,
                visible_questions,
                target_index,
                interaction=interaction,
            )
        if invocation.flow_id == CLAIM_EVIDENCE_FLOW_ID:
            current_screen = str(payload.get("screen") or "")
            target_screen = {
                "CLAIM_EVIDENCE_SUCCESS": "CLAIM_EVIDENCE_DOCUMENTS",
                "CLAIM_EVIDENCE_DOCUMENTS": "CLAIM_EVIDENCE_PHOTOS",
            }.get(current_screen, "CLAIM_EVIDENCE_LOOKUP")
            return {"screen": target_screen, "data": {}}
        return {"screen": "CLAIM_LOOKUP", "data": {}}

    def _handle_data_exchange(
        self,
        payload: Mapping[str, Any],
        endpoint: _ResolvedEndpoint,
        invocation: _VerifiedInvocation,
    ) -> Mapping[str, Any]:
        _validate_screen(payload.get("screen"), invocation.flow_id, optional=False)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise _action_error("flow_input_invalid", "Flow input is invalid.", 400)
        if invocation.flow_id in {CLAIM_FLOW_ID, CLAIM_EVIDENCE_FLOW_ID}:
            expected_lookup_screen = (
                "CLAIM_LOOKUP"
                if invocation.flow_id == CLAIM_FLOW_ID
                else "CLAIM_EVIDENCE_LOOKUP"
            )
            if payload.get("screen") != expected_lookup_screen:
                raise _action_error("flow_screen_invalid", "Flow screen is invalid.", 400)
            response, ticket, lookup_code = _claim_lookup_response(
                data,
                endpoint.tenant,
                result_screen=(
                    "CLAIM_RESULT"
                    if invocation.flow_id == CLAIM_FLOW_ID
                    else "CLAIM_EVIDENCE_PHOTOS"
                ),
            )
            self._store_claim_context(
                endpoint=endpoint,
                invocation=invocation,
                ticket=ticket,
                lookup_code=lookup_code,
            )
            return response

        if invocation.flow_id == SURVEY_FLOW_ID:
            return self._handle_survey_data_exchange(
                payload,
                data,
                endpoint,
                invocation,
            )

        if payload.get("screen") != "ORDER_DETAILS":
            raise _action_error("flow_screen_invalid", "Flow screen is invalid.", 400)
        _validate_order_input(data)
        order = self._load_order_context(endpoint, invocation)
        return {
            "screen": "ORDER_CONFIRM",
            "data": {
                "order_summary": _order_summary(order),
                "total_display": _order_total_display(order),
            },
        }

    def _handle_survey_data_exchange(
        self,
        payload: Mapping[str, Any],
        data: Mapping[str, Any],
        endpoint: _ResolvedEndpoint,
        invocation: _VerifiedInvocation,
    ) -> Mapping[str, Any]:
        current_screen = str(payload.get("screen") or "")
        if current_screen not in _SURVEY_QUESTION_SCREENS:
            raise _action_error("flow_screen_invalid", "Flow screen is invalid.", 400)
        survey, _, interaction = self._load_survey_context(endpoint, invocation)
        staged = _survey_staged_answers(interaction.metadata_json)
        visible_questions = _visible_native_survey_questions(survey, staged)
        screen_index = _SURVEY_QUESTION_SCREENS.index(current_screen)
        if screen_index >= len(visible_questions):
            raise _action_error("survey_screen_out_of_range", "Flow screen is invalid.", 400)

        selected_option = _required_string(
            data.get("selected_option"),
            "survey_option_required",
            32,
        )
        if not selected_option.isdigit():
            raise _action_error("survey_option_invalid", "Survey option is invalid.", 400)
        question = visible_questions[screen_index]
        option_ids = {int(option.id) for option in question.opciones}
        selected_option_id = int(selected_option)
        if selected_option_id not in option_ids:
            raise _action_error("survey_option_invalid", "Survey option is invalid.", 400)

        metadata = dict(interaction.metadata_json or {})
        staged[int(question.id)] = selected_option_id
        visible_after_answer = _visible_native_survey_questions(survey, staged)
        staged = _prune_native_survey_answers(visible_after_answer, staged)
        metadata["survey_staged_answers"] = {
            str(question_id): option_id
            for question_id, option_id in staged.items()
        }
        metadata["survey_last_screen"] = current_screen
        metadata["survey_navigation"] = {
            "instrument_revision": int(survey.structure_revision or 1),
            "visible_question_ids": [
                int(item.id) for item in visible_after_answer
            ],
            "answered_question_ids": sorted(staged),
        }
        interaction.metadata_json = metadata
        db.session.add(interaction)
        db.session.commit()

        next_index = screen_index + 1
        if next_index < len(visible_after_answer):
            return _survey_question_response(
                survey,
                visible_after_answer,
                next_index,
                interaction=interaction,
            )
        return _survey_confirmation_response(survey, visible_after_answer)

    def _store_claim_context(
        self,
        *,
        endpoint: _ResolvedEndpoint,
        invocation: _VerifiedInvocation,
        ticket: Any,
        lookup_code: str,
    ) -> None:
        interaction = WhatsAppFlowInteraction.query.filter_by(
            id=invocation.interaction_id,
            tenant_id=int(endpoint.tenant.id),
            provider_sender_id=invocation.provider_sender_id,
            flow_id=invocation.flow_id,
        ).first()
        if interaction is None or interaction.status not in _READY_INTERACTION_STATES:
            raise _action_error(
                "claim_context_unavailable",
                "The claim linked to this Flow is unavailable.",
                409,
            )
        metadata = dict(interaction.metadata_json or {})
        metadata["claim_context"] = _claim_context_for_ticket(
            ticket,
            lookup_code=lookup_code,
        )
        interaction.metadata_json = metadata
        db.session.add(interaction)
        db.session.commit()

    def _load_order_context(
        self,
        endpoint: _ResolvedEndpoint,
        invocation: _VerifiedInvocation,
    ) -> Any:
        interaction = WhatsAppFlowInteraction.query.filter_by(
            id=invocation.interaction_id,
            tenant_id=int(endpoint.tenant.id),
            provider_sender_id=invocation.provider_sender_id,
        ).first()
        if interaction is None or interaction.status not in _READY_INTERACTION_STATES:
            raise _action_error(
                "order_context_unavailable",
                "The order linked to this Flow is unavailable.",
                409,
            )
        if interaction.flow_id != ORDER_FLOW_ID:
            raise _action_error(
                "order_context_scope_mismatch",
                "The order linked to this Flow is unavailable.",
                403,
            )
        order_ref = _order_reference(interaction.metadata_json)
        if order_ref is None:
            raise _action_error(
                "order_context_missing",
                "The order linked to this Flow is unavailable.",
                409,
            )
        order = _lookup_order(int(endpoint.tenant.id), order_ref)
        if order is None:
            raise _action_error(
                "order_context_unavailable",
                "The order linked to this Flow is unavailable.",
                404,
            )
        return order

    def _load_survey_context(
        self,
        endpoint: _ResolvedEndpoint,
        invocation: _VerifiedInvocation,
    ) -> tuple[EncEncuesta, tuple[Any, ...], WhatsAppFlowInteraction]:
        interaction = WhatsAppFlowInteraction.query.filter_by(
            id=invocation.interaction_id,
            tenant_id=int(endpoint.tenant.id),
            provider_sender_id=invocation.provider_sender_id,
            flow_id=SURVEY_FLOW_ID,
        ).first()
        if interaction is None or interaction.status not in _READY_INTERACTION_STATES:
            raise _action_error(
                "survey_context_unavailable",
                "The survey linked to this Flow is unavailable.",
                409,
            )
        survey, questions, _ = _resolve_survey_context(
            int(endpoint.tenant.id),
            (interaction.metadata_json or {}).get("survey_context"),
            require_instrument_revision=True,
        )
        return survey, questions, interaction


def create_meta_flow_runtime_resolver(
    *,
    environ: Mapping[str, str] | None = None,
    token_verifier: EndpointTokenVerifier | None = None,
) -> MetaFlowRuntime:
    return MetaFlowRuntime(environ=environ, token_verifier=token_verifier)


def authorize_order_context(tenant_id: int, raw_context: Any) -> dict[str, str]:
    """Validate an order reference against its owning tenant before a Flow send."""

    if not isinstance(raw_context, Mapping):
        raise _action_error(
            "order_context_missing",
            "An authorized order context is required for this Flow.",
            400,
        )
    reference = _order_reference({"order_context": raw_context})
    if reference is None:
        raise _action_error(
            "order_context_invalid",
            "The order linked to this Flow is invalid.",
            400,
        )
    kind, identifier = reference
    canonical_kind = _canonical_order_kind(kind)
    if canonical_kind is None:
        raise _action_error(
            "order_context_invalid",
            "The order linked to this Flow is invalid.",
            400,
        )
    if _lookup_order(int(tenant_id), (canonical_kind, identifier)) is None:
        raise _action_error(
            "order_context_unavailable",
            "The order linked to this Flow is unavailable.",
            404,
        )
    return {"kind": canonical_kind, "id": identifier}


def authorize_survey_context(tenant_id: int, raw_context: Any) -> dict[str, Any]:
    """Validate one published quick vote before issuing a signed Flow token."""

    _, _, context = _resolve_survey_context(int(tenant_id), raw_context)
    return context


def _validated_whatsapp_flow_completion(
    *,
    tenant_id: int,
    interaction_id: int,
    submission: Mapping[str, Any],
) -> tuple[int, WhatsAppFlowInteraction, Mapping[str, Any], str]:
    """Return the durable interaction and a canonical digest for one submission."""

    if not isinstance(submission, Mapping):
        raise _action_error("flow_completion_invalid", "Flow completion is invalid.", 400)
    try:
        normalized_interaction_id = int(interaction_id)
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _action_error(
            "flow_completion_scope_mismatch",
            "Flow completion is invalid.",
            403,
        ) from exc
    if normalized_interaction_id <= 0 or normalized_tenant_id <= 0:
        raise _action_error(
            "flow_completion_scope_mismatch",
            "Flow completion is invalid.",
            403,
        )
    interaction = WhatsAppFlowInteraction.query.filter_by(
        id=normalized_interaction_id,
        tenant_id=normalized_tenant_id,
    ).first()
    if interaction is None or interaction.status not in _READY_INTERACTION_STATES:
        raise _action_error(
            "flow_completion_unavailable",
            "Flow completion is unavailable.",
            409,
        )

    correlation = submission.get("correlation")
    try:
        correlated_interaction_id = int(
            correlation.get("interaction_id") or 0
        ) if isinstance(correlation, Mapping) else 0
    except (TypeError, ValueError, OverflowError) as exc:
        raise _action_error(
            "flow_completion_scope_mismatch",
            "Flow completion is invalid.",
            403,
        ) from exc
    if correlated_interaction_id != interaction.id:
        raise _action_error("flow_completion_scope_mismatch", "Flow completion is invalid.", 403)
    flow = submission.get("flow")
    submitted_flow_id = str(flow.get("id") or "").strip() if isinstance(flow, Mapping) else ""
    if submitted_flow_id != interaction.flow_id or submitted_flow_id not in SUPPORTED_FLOW_IDS:
        raise _action_error("flow_completion_scope_mismatch", "Flow completion is invalid.", 403)
    payload = submission.get("payload")
    answers = payload.get("answers") if isinstance(payload, Mapping) else None
    if not isinstance(answers, Mapping):
        raise _action_error("flow_completion_answers_missing", "Flow completion is invalid.", 400)

    try:
        canonical_payload = json.dumps(
            {
                "version": "whatsapp.flow_completion.v1",
                "interaction_id": interaction.id,
                "flow_id": interaction.flow_id,
                "answers": answers,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _action_error("flow_completion_invalid", "Flow completion is invalid.", 400) from exc
    payload_hash = hashlib.sha256(canonical_payload).hexdigest()
    return normalized_tenant_id, interaction, answers, payload_hash


def replay_whatsapp_flow_completion(
    *,
    tenant_id: int,
    interaction_id: int,
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay a final receipt without re-running any completion side effect.

    A consumed interaction without a final receipt is deliberately ambiguous
    and fails closed.  The caller must never infer that it is safe to apply the
    business action again merely because the provider redelivered a message.
    """

    _, interaction, _, payload_hash = _validated_whatsapp_flow_completion(
        tenant_id=tenant_id,
        interaction_id=interaction_id,
        submission=submission,
    )
    completion = dict(interaction.metadata_json or {}).get("completion")
    if (
        interaction.status != "consumed"
        or not isinstance(completion, Mapping)
        or completion.get("status") not in {"applied", "rejected"}
    ):
        raise _action_error(
            "flow_completion_replay_unavailable",
            "Flow completion replay is unavailable.",
            409,
        )
    return _completion_response_from_metadata(completion, payload_hash=payload_hash)


def apply_whatsapp_flow_completion(
    *,
    tenant_id: int,
    interaction_id: int,
    submission: Mapping[str, Any],
    actor_user_id: int | None = None,
    anon_id: str | None = None,
) -> dict[str, Any]:
    """Apply one verified terminal Flow submission without committing it.

    The caller consumes the one-time invocation and commits both operations in
    the same transaction. Client answers can update contact/delivery fields,
    but record identity, totals and payment state always come from the durable
    interaction created by the admin send endpoint.
    """

    normalized_tenant_id, interaction, answers, payload_hash = (
        _validated_whatsapp_flow_completion(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            submission=submission,
        )
    )

    initial_metadata = dict(interaction.metadata_json or {})
    previous_completion = initial_metadata.get("completion")
    if (
        isinstance(previous_completion, Mapping)
        and previous_completion.get("status") in {"applied", "rejected"}
    ):
        return _completion_response_from_metadata(
            previous_completion,
            payload_hash=payload_hash,
        )

    if interaction.flow_id == CLAIM_FLOW_ID:
        result = _apply_claim_completion(
            tenant_id=normalized_tenant_id,
            interaction=interaction,
            answers=answers,
            actor_user_id=actor_user_id,
            anon_id=anon_id,
        )
    elif interaction.flow_id == CLAIM_EVIDENCE_FLOW_ID:
        result = _apply_claim_evidence_completion(
            tenant_id=normalized_tenant_id,
            interaction=interaction,
            answers=answers,
            actor_user_id=actor_user_id,
            anon_id=anon_id,
        )
    elif interaction.flow_id == ORDER_FLOW_ID:
        result = _apply_order_completion(
            tenant_id=normalized_tenant_id,
            interaction=interaction,
            answers=answers,
            actor_user_id=actor_user_id,
        )
    else:
        result = _apply_survey_completion(
            tenant_id=normalized_tenant_id,
            interaction=interaction,
            answers=answers,
            actor_user_id=actor_user_id,
            anon_id=anon_id,
        )

    completion_metadata = {
        "status": "applied",
        "flow_id": interaction.flow_id,
        "entity_kind": result["entity"]["kind"],
        "entity_id": str(result["entity"]["id"]),
        "field_names": sorted(str(key) for key in answers.keys()),
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "message_body": result["message_body"],
        "source": result["fuente"],
        "payload_hash": payload_hash,
        "response": _completion_response_snapshot(result),
    }
    if result.get("attachment_count") is not None:
        completion_metadata["attachment_count"] = int(result["attachment_count"])
    # Completion handlers may add server-owned navigation or evidence state.
    # Merge the receipt into that current value instead of restoring the stale
    # snapshot captured before the handler ran.
    metadata = dict(interaction.metadata_json or {})
    metadata["completion"] = completion_metadata
    interaction.metadata_json = metadata
    db.session.add(interaction)
    db.session.add(
        AuditEvent(
            tenant_id=normalized_tenant_id,
            actor_user_id=actor_user_id,
            event_type=f"whatsapp_flow.{interaction.flow_id}.completed",
            resource_type=result["entity"]["kind"],
            resource_id=str(result["entity"]["id"]),
            details={
                "interaction_id": interaction.id,
                "flow_id": interaction.flow_id,
                "provider_sender_id": interaction.provider_sender_id,
                "field_names": completion_metadata["field_names"],
                "attachment_count": completion_metadata.get("attachment_count"),
            },
        )
    )
    return result


def record_whatsapp_flow_completion_rejection(
    *,
    tenant_id: int,
    interaction_id: int,
    code: str,
    actor_user_id: int | None = None,
    submission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a bounded rejection after an invocation was consumed."""

    interaction = WhatsAppFlowInteraction.query.filter_by(
        id=int(interaction_id),
        tenant_id=int(tenant_id),
    ).first()
    if interaction is None:
        raise _action_error("flow_completion_unavailable", "Flow completion is unavailable.", 409)
    safe_code = str(code or "flow_completion_invalid")[:80]
    response = {
        "message_body": (
            "No pudimos aplicar los datos del formulario de forma segura. "
            "Abrilo nuevamente desde el mensaje original o contacta a la mesa de ayuda."
        ),
        "options_list": [{"texto": "Menu", "action_id": "menu_principal"}],
        "message_type": "interactive_buttons",
        "fuente": "whatsapp_flow_completion_rejected",
        "generar_audio": True,
    }
    payload_hash = None
    if isinstance(submission, Mapping):
        try:
            _, validated_interaction, _, payload_hash = _validated_whatsapp_flow_completion(
                tenant_id=tenant_id,
                interaction_id=interaction_id,
                submission=submission,
            )
        except MetaFlowActionError:
            payload_hash = None
        else:
            if validated_interaction.id != interaction.id:
                payload_hash = None
    metadata = dict(interaction.metadata_json or {})
    previous_completion = metadata.get("completion")
    if (
        isinstance(previous_completion, Mapping)
        and previous_completion.get("status") in {"applied", "rejected"}
    ):
        return _completion_response_from_metadata(
            previous_completion,
            payload_hash=payload_hash,
        )
    completion = {
        "status": "rejected",
        "flow_id": interaction.flow_id,
        "code": safe_code,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "message_body": response["message_body"],
        "source": response["fuente"],
        "response": _completion_response_snapshot(response),
    }
    if payload_hash:
        completion["payload_hash"] = payload_hash
    metadata["completion"] = completion
    interaction.metadata_json = metadata
    db.session.add(interaction)
    db.session.add(
        AuditEvent(
            tenant_id=int(tenant_id),
            actor_user_id=actor_user_id,
            event_type="whatsapp_flow.completion_rejected",
            resource_type="whatsapp_flow_interaction",
            resource_id=str(interaction.id),
            details={"flow_id": interaction.flow_id, "code": safe_code},
        )
    )
    return response


def _apply_claim_completion(
    *,
    tenant_id: int,
    interaction: WhatsAppFlowInteraction,
    answers: Mapping[str, Any],
    actor_user_id: int | None,
    anon_id: str | None,
) -> dict[str, Any]:
    metadata = interaction.metadata_json if isinstance(interaction.metadata_json, Mapping) else {}
    claim_context = metadata.get("claim_context")
    ticket, claim_kind, expected_code = _load_claim_context(tenant_id, claim_context)
    submitted_code = _required_string(
        answers.get("ticket_number"),
        "ticket_number_invalid",
        64,
    ).upper()
    if not hmac.compare_digest(submitted_code, expected_code.upper()):
        raise _action_error("claim_context_scope_mismatch", "Flow completion is invalid.", 403)

    note = _optional_completion_text(
        answers.get("follow_up_note"),
        code="follow_up_note_invalid",
        max_length=1000,
    )
    realtime_event = None
    if note:
        if isinstance(ticket, (MunicipioTicket, PymeTicket)):
            comment = TicketComentario(
                municipio_ticket_id=ticket.id if isinstance(ticket, MunicipioTicket) else None,
                pyme_ticket_id=ticket.id if isinstance(ticket, PymeTicket) else None,
                comentario=note,
                user_id=actor_user_id,
                anon_id=str(anon_id or "")[:80] or None,
                es_admin=False,
                origen="whatsapp_flow",
                estado_ticket=getattr(ticket, "estado", None),
            )
            db.session.add(comment)
            db.session.flush()
            realtime_event = {
                "ticket_type": "municipio" if isinstance(ticket, MunicipioTicket) else "pyme",
                "ticket_id": ticket.id,
                "comment_id": comment.id,
            }
        else:
            extra = dict(ticket.datos_extra or {})
            comments = list(extra.get("whatsapp_flow_comments") or [])[-49:]
            comments.append(
                {
                    "text": note,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "actor_user_id": actor_user_id,
                }
            )
            extra["whatsapp_flow_comments"] = comments
            ticket.datos_extra = extra
        if hasattr(ticket, "ultima_actividad"):
            ticket.ultima_actividad = datetime.now(timezone.utc)
        db.session.add(ticket)

    action_text = "Comentario agregado" if note else "Consulta finalizada"
    result = {
        "message_body": (
            f"{action_text} en el reclamo *{expected_code}*. "
            "La gestion queda vinculada a su historial y podes volver a consultarla cuando lo necesites."
        ),
        "options_list": [
            {"texto": "Menu", "action_id": "menu_principal"},
            {"texto": "Ayuda", "action_id": "ayuda"},
        ],
        "message_type": "interactive_buttons",
        "fuente": "whatsapp_flow_claim_completed",
        "generar_audio": True,
        "entity": {"kind": claim_kind, "id": ticket.id},
    }
    if realtime_event:
        result["realtime_event"] = realtime_event
    return result


def _apply_claim_evidence_completion(
    *,
    tenant_id: int,
    interaction: WhatsAppFlowInteraction,
    answers: Mapping[str, Any],
    actor_user_id: int | None,
    anon_id: str | None,
) -> dict[str, Any]:
    metadata = interaction.metadata_json if isinstance(interaction.metadata_json, Mapping) else {}
    claim_context = metadata.get("claim_context")
    ticket, claim_kind, expected_code = _load_claim_context(tenant_id, claim_context)
    submitted_code = _required_string(
        answers.get("ticket_number"),
        "ticket_number_invalid",
        64,
    ).upper()
    if not hmac.compare_digest(submitted_code, expected_code.upper()):
        raise _action_error("claim_context_scope_mismatch", "Flow completion is invalid.", 403)

    try:
        descriptors = normalize_claim_evidence(
            answers.get("photos"),
            answers.get("documents"),
        )
    except MetaFlowMediaError as exc:
        raise _action_error(exc.code, "Claim evidence is invalid.", exc.status_code) from exc

    provider_sender = ProviderSender.query.filter_by(
        id=interaction.provider_sender_id,
        tenant_id=int(tenant_id),
    ).first()
    if provider_sender is None:
        raise _action_error(
            "claim_evidence_sender_unavailable",
            "Claim evidence is unavailable.",
            409,
        )
    try:
        downloaded = download_claim_evidence_media(
            provider_sender=provider_sender,
            descriptors=descriptors,
            app_config=current_app.config,
        )
        with db.session.begin_nested():
            evidence_items, comment_ids = _persist_claim_evidence(
                ticket=ticket,
                interaction=interaction,
                expected_code=expected_code,
                downloaded=downloaded,
                actor_user_id=actor_user_id,
                anon_id=anon_id,
            )
    except MetaFlowMediaError as exc:
        raise _action_error(exc.code, "Claim evidence is unavailable.", exc.status_code) from exc

    result = {
        "message_body": (
            f"Recibimos {len(evidence_items)} archivo"
            f"{'s' if len(evidence_items) != 1 else ''} para el reclamo *{expected_code}*. "
            "La evidencia ya esta disponible para el equipo y en el historial del reclamo."
        ),
        "options_list": [
            {"texto": "Ver reclamo", "action_id": "consultar_estado_reclamo"},
            {"texto": "Menu", "action_id": "menu_principal"},
        ],
        "message_type": "interactive_buttons",
        "fuente": "whatsapp_flow_claim_evidence_completed",
        "generar_audio": True,
        "entity": {"kind": claim_kind, "id": ticket.id},
        "attachment_count": len(evidence_items),
    }
    if comment_ids:
        result["realtime_event"] = {
            "ticket_type": (
                "municipio"
                if isinstance(ticket, MunicipioTicket)
                else "pyme" if isinstance(ticket, PymeTicket) else "tenant"
            ),
            "ticket_id": ticket.id,
            "comment_id": comment_ids[-1],
            "comment_ids": comment_ids,
            "attachment_count": len(evidence_items),
            "source": "whatsapp_flow",
        }
    return result


def _persist_claim_evidence(
    *,
    ticket: Any,
    interaction: WhatsAppFlowInteraction,
    expected_code: str,
    downloaded: Sequence[DownloadedFlowMedia],
    actor_user_id: int | None,
    anon_id: str | None,
) -> tuple[list[dict[str, Any]], list[int]]:
    extra = dict(getattr(ticket, "datos_extra", None) or {})
    batches = [
        item
        for item in (extra.get("whatsapp_flow_evidence") or [])
        if isinstance(item, Mapping)
    ][-19:]
    existing = next(
        (
            item
            for item in batches
            if str(item.get("interaction_id") or "") == str(interaction.id)
        ),
        None,
    )
    if isinstance(existing, Mapping):
        return list(existing.get("items") or []), []

    now = datetime.now(timezone.utc)
    evidence_items: list[dict[str, Any]] = []
    comment_ids: list[int] = []
    tenant_attachments = list(extra.get("attachments") or [])
    tenant_comments = list(extra.get("comments") or [])

    for index, media in enumerate(downloaded, start=1):
        storage = FileStorage(
            stream=io.BytesIO(media.content),
            filename=media.file_name,
            content_type=media.mime_type,
        )
        attachment_id: int | str | None = None
        if isinstance(ticket, (MunicipioTicket, PymeTicket)):
            attachment = create_attachment_with_thumbnail(
                storage,
                user_id=actor_user_id,
            )
            if attachment is None:
                raise MetaFlowMediaError("claim_evidence_storage_failed", status_code=503)
            attachment.tipo = "whatsapp_flow_evidence"
            if isinstance(ticket, MunicipioTicket):
                attachment.municipio_ticket_id = ticket.id
            else:
                attachment.pyme_ticket_id = ticket.id
            db.session.add(attachment)
            db.session.flush()
            attachment_id = attachment.id
            comment = TicketComentario(
                municipio_ticket_id=ticket.id if isinstance(ticket, MunicipioTicket) else None,
                pyme_ticket_id=ticket.id if isinstance(ticket, PymeTicket) else None,
                comentario=f"Evidencia recibida: {media.file_name}",
                user_id=actor_user_id,
                anon_id=str(anon_id or "")[:80] or None,
                es_admin=False,
                origen="whatsapp_flow",
                estado_ticket=getattr(ticket, "estado", None),
                archivo_adjunto_id=attachment.id,
            )
            db.session.add(comment)
            db.session.flush()
            comment_ids.append(comment.id)
        else:
            upload_result = guardar_adjunto_y_thumbnail(
                storage,
                kind="whatsapp-flow-evidence",
            )
            if not upload_result:
                raise MetaFlowMediaError("claim_evidence_storage_failed", status_code=503)
            attachment_id = f"flow-{interaction.id}-{index}"
            attachment_payload = {
                "id": attachment_id,
                "url": upload_result.get("original_url"),
                "name": upload_result.get("original_name") or media.file_name,
                "filename": upload_result.get("unique_name") or media.file_name,
                "mime_type": upload_result.get("mimetype") or media.mime_type,
                "size": upload_result.get("size") or media.size,
                "source": "whatsapp_flow",
                "flow_id": CLAIM_EVIDENCE_FLOW_ID,
                "interaction_id": interaction.id,
                "status": "ready",
                "uploaded_at": now.isoformat(),
            }
            tenant_attachments.append(attachment_payload)
            tenant_comments.append(
                {
                    "id": f"flow-comment-{interaction.id}-{index}",
                    "body": f"Evidencia recibida: {media.file_name}",
                    "origin": "whatsapp_flow",
                    "visibility": "public",
                    "created_at": now.isoformat(),
                    "attachmentInfo": attachment_payload,
                }
            )

        evidence_items.append(
            {
                "kind": media.kind,
                "attachment_id": attachment_id,
                "provider_media_ref": hashlib.sha256(
                    media.media_id.encode("utf-8")
                ).hexdigest(),
                "file_name": media.file_name,
                "mime_type": media.mime_type,
                "file_size": media.size,
                "sha256": media.sha256_hex,
                "status": "ready",
            }
        )

    batch = {
        "interaction_id": interaction.id,
        "flow_id": CLAIM_EVIDENCE_FLOW_ID,
        "source": "whatsapp_flow",
        "ticket_number": expected_code,
        "received_at": now.isoformat(),
        "items": evidence_items,
    }
    batches.append(batch)
    extra["whatsapp_flow_evidence"] = batches[-20:]
    if isinstance(ticket, TenantTicket):
        extra["attachments"] = tenant_attachments[-50:]
        extra["comments"] = tenant_comments[-100:]
    ticket.datos_extra = extra
    if hasattr(ticket, "ultima_actividad"):
        ticket.ultima_actividad = now
    db.session.add(ticket)
    return evidence_items, comment_ids


def _apply_order_completion(
    *,
    tenant_id: int,
    interaction: WhatsAppFlowInteraction,
    answers: Mapping[str, Any],
    actor_user_id: int | None,
) -> dict[str, Any]:
    confirmation = answers.get("confirm_order")
    if confirmation is not True and str(confirmation or "").strip().lower() not in {"true", "1"}:
        raise _action_error("order_confirmation_required", "Order confirmation is required.", 400)
    full_name = _required_string(answers.get("full_name"), "full_name_invalid", 120)
    phone = _required_string(answers.get("phone"), "phone_invalid", 40)
    if len("".join(character for character in phone if character.isdigit())) < 8:
        raise _action_error("phone_invalid", "Order input is invalid.", 400)
    delivery_address = _required_string(
        answers.get("delivery_address"),
        "delivery_address_invalid",
        240,
    )
    delivery_notes = _optional_completion_text(
        answers.get("delivery_notes"),
        code="delivery_notes_invalid",
        max_length=500,
    )

    metadata = interaction.metadata_json if isinstance(interaction.metadata_json, Mapping) else {}
    order_reference = _order_reference(metadata)
    if order_reference is None:
        raise _action_error("order_context_missing", "The order linked to this Flow is unavailable.", 409)
    order = _lookup_order(int(tenant_id), order_reference)
    if order is None:
        raise _action_error("order_context_unavailable", "The order linked to this Flow is unavailable.", 404)
    order_kind = _canonical_order_kind(order_reference[0])
    if order_kind is None:
        raise _action_error("order_context_invalid", "The order linked to this Flow is invalid.", 400)

    if isinstance(order, Order):
        order.buyer_name = full_name
        order.buyer_phone = phone
        order.buyer_notes = delivery_notes or order.buyer_notes
        address_payload = dict(order.delivery_address or {})
        address_payload.update({"address": delivery_address, "source": "whatsapp_flow"})
        order.delivery_address = address_payload
        if str(order.status or "").lower() in {"created", "pending", "pendiente"}:
            order.status = "confirmed"
        order.channel = "whatsapp"
    elif isinstance(order, MarketOrder):
        order.contact_name = full_name
        order.contact_phone = phone
        order.note = delivery_notes or order.note
        order_metadata = dict(order.metadata_payload or {})
        order_metadata["delivery_address"] = {
            "address": delivery_address,
            "source": "whatsapp_flow",
        }
        order.metadata_payload = order_metadata
        if str(order.status or "").lower() in {"created", "pending", "pendiente"}:
            order.status = "confirmed"
        order.channel = "whatsapp"
        db.session.add(
            OrderEvent(
                market_order_id=order.id,
                type="whatsapp_flow_confirmed",
                payload={
                    "interaction_id": interaction.id,
                    "delivery_address": delivery_address,
                    "delivery_notes": delivery_notes,
                },
            )
        )
    elif isinstance(order, PymePedido):
        order.nombre_cliente = full_name
        order.telefono_cliente = phone
        order.direccion = delivery_address
        if str(order.estado or "").lower() in {"created", "pending", "pendiente", "nuevo"}:
            order.estado = "confirmado"
        db.session.add(
            OrderEvent(
                pyme_pedido_id=order.id,
                type="whatsapp_flow_confirmed",
                payload={
                    "interaction_id": interaction.id,
                    "delivery_address": delivery_address,
                    "delivery_notes": delivery_notes,
                },
            )
        )
    elif isinstance(order, PedidoConversacional):
        order_metadata = dict(order.metadata_payload or {})
        contact = dict(order_metadata.get("contact") or {})
        contact.update(
            {
                "name": full_name,
                "phone": phone,
                "address": delivery_address,
            }
        )
        order_metadata["contact"] = contact
        order_metadata["delivery_notes"] = delivery_notes
        order_metadata["confirmed_via"] = "whatsapp_flow"
        order_metadata["confirmed_interaction_id"] = interaction.id
        order.metadata_payload = order_metadata
        if str(order.estado or "").lower() in {"created", "pending", "pendiente", "nuevo"}:
            order.estado = "confirmed"
        order.origen = "whatsapp"
    else:
        raise _action_error("order_context_invalid", "The order linked to this Flow is invalid.", 400)

    db.session.add(order)
    return {
        "message_body": (
            "Pedido confirmado. Guardamos los datos de entrega y lo dejamos listo para continuar "
            "con pago, preparacion o seguimiento segun corresponda."
        ),
        "options_list": [
            {"texto": "Ver pedido", "action_id": "consultar_estado_pedido"},
            {"texto": "Menu", "action_id": "menu_principal"},
        ],
        "message_type": "interactive_buttons",
        "fuente": "whatsapp_flow_order_completed",
        "generar_audio": True,
        "entity": {"kind": order_kind, "id": order.id},
    }


def _apply_survey_completion(
    *,
    tenant_id: int,
    interaction: WhatsAppFlowInteraction,
    answers: Mapping[str, Any],
    actor_user_id: int | None,
    anon_id: str | None,
) -> dict[str, Any]:
    confirmation = answers.get("confirm_vote")
    if confirmation is not True and str(confirmation or "").strip().lower() not in {
        "true",
        "1",
    }:
        raise _action_error(
            "survey_confirmation_required",
            "Survey confirmation is required.",
            400,
        )

    metadata = interaction.metadata_json if isinstance(interaction.metadata_json, Mapping) else {}
    survey, questions, survey_context = _resolve_survey_context(
        tenant_id,
        metadata.get("survey_context"),
        require_instrument_revision=True,
    )
    staged_answers = _survey_staged_answers(metadata)
    visible_questions = _visible_native_survey_questions(survey, staged_answers)
    staged = _prune_native_survey_answers(visible_questions, staged_answers)
    response_rows: list[dict[str, int]] = []
    for question in visible_questions:
        selected_id = staged_answers.get(int(question.id))
        if selected_id is None:
            raise _action_error(
                "survey_answers_incomplete",
                "Survey answers are incomplete.",
                409,
            )
        if selected_id not in {int(option.id) for option in question.opciones}:
            raise _action_error(
                "survey_option_invalid",
                "Survey option is invalid.",
                400,
            )
        response_rows.append(
            {"pregunta_id": int(question.id), "opcion_id": selected_id}
        )

    completion_metadata = dict(metadata)
    completion_metadata["survey_staged_answers"] = {
        str(question_id): option_id
        for question_id, option_id in staged.items()
    }
    completion_metadata["survey_navigation"] = {
        "instrument_revision": int(survey.structure_revision or 1),
        "visible_question_ids": [
            int(question.id) for question in visible_questions
        ],
        "answered_question_ids": sorted(staged),
    }
    interaction.metadata_json = completion_metadata
    db.session.add(interaction)

    from services.encuestas_service import EncuestaError, save_respuesta

    authenticated_user = (
        db.session.get(User, int(actor_user_id)) if actor_user_id else None
    )
    phone = str(anon_id or "").strip() or None
    payload = {
        "respuestas": response_rows,
        "source": "whatsapp_flow",
        "canal": "whatsapp_flow",
        "anon_id": interaction.recipient_hash,
        "phone": phone,
        "instrument_revision": survey_context["instrument_revision"],
        "metadata": {
            "source": "whatsapp_native_flow",
            "flow_id": SURVEY_FLOW_ID,
            "interaction_id": interaction.id,
            "instrument_revision": survey_context["instrument_revision"],
            "visible_question_ids": [
                int(question.id) for question in visible_questions
            ],
            "adaptive_navigation": any(
                question.logica_condicional is not None for question in questions
            ),
        },
    }
    request_context = {
        "anon_id": interaction.recipient_hash,
        "ip": None,
        "user_agent": "WhatsApp Native Flow",
        "referer": None,
        "canal": "whatsapp_flow",
    }
    try:
        _ensure_physical_outer_transaction_for_nested_write()
        with db.session.begin_nested():
            response = save_respuesta(
                survey_context["slug"],
                payload,
                request_context,
                preferred_tenant_id=tenant_id,
                authenticated_user=authenticated_user,
                commit=False,
                # The outer WhatsApp transaction commits the response and both
                # post-commit effects together.  The webhook dispatches them
                # only after invocation consumption is durably committed.
                emit_realtime_update=True,
                grant_reward=False,
            )
    except EncuestaError as exc:
        reason = (
            str((exc.payload or {}).get("reason_code") or "").strip()
            if isinstance(getattr(exc, "payload", None), Mapping)
            else ""
        )
        if reason == "survey_response_duplicate":
            code = "survey_already_answered"
        elif reason in {"survey_concurrent_update", "survey_structure_changed"}:
            # Preserve the retryable concurrency reason without pretending the
            # vote was registered.  The caller owns retry policy and must not
            # automatically replay an ambiguous external submission.
            code = reason
        elif reason in {"authentication_required", "identity_required"}:
            code = reason
        else:
            code = "survey_submission_invalid"
        raise _action_error(code, "Survey response could not be saved.", exc.status_code) from exc

    results_url = _survey_results_url(survey_context["slug"])
    message = "Participacion registrada."
    if survey.mostrar_resultados_envivo:
        message += f" Podes seguir los resultados en {results_url}."
    return {
        "message_body": message,
        "options_list": [
            {
                "texto": "Ver resultados",
                "type": "url",
                "url": results_url,
                "action_id": "open_survey_results",
            },
            {"texto": "Menu", "action_id": "menu_principal"},
        ],
        "message_type": "interactive_buttons",
        "fuente": "whatsapp_flow_survey_completed",
        "generar_audio": True,
        "instrument_revision": getattr(response, "instrument_revision", None),
        "entity": {"kind": "survey_response", "id": response.id},
        "realtime_event": {
            "kind": "survey_vote",
            "survey_id": survey.id,
            "survey_slug": survey_context["slug"],
        },
    }


def _ensure_physical_outer_transaction_for_nested_write() -> None:
    """Make a SQLite SAVEPOINT subordinate to a real outer transaction.

    Pysqlite does not emit ``BEGIN`` for read-only work.  If ``begin_nested``
    is the first physical transaction statement, releasing that SAVEPOINT can
    commit its writes even though SQLAlchemy still exposes a logical outer
    transaction.  The WhatsApp Flow caller intentionally owns the final
    commit/rollback, so start a real write transaction before the savepoint.
    PostgreSQL and other supported drivers already provide the expected outer
    transaction semantics and need no special statement.
    """

    connection = db.session.connection()
    if connection.dialect.name != "sqlite":
        return

    connection_fairy = connection.connection
    driver_connection = getattr(
        connection_fairy,
        "driver_connection",
        connection_fairy,
    )
    if getattr(driver_connection, "in_transaction", False):
        return
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _survey_results_url(slug: str) -> str:
    configured = (
        current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
        or current_app.config.get("PUBLIC_FRONTEND_URL")
        or current_app.config.get("FRONTEND_URL")
        or os.getenv("PUBLIC_FRONTEND_URL")
        or os.getenv("FRONTEND_URL")
        or "https://www.chatboc.ar"
    )
    base_url = str(configured).strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        base_url = "https://www.chatboc.ar"
    return f"{base_url}/e/{slug}?resultados=1"


def _load_claim_context(
    tenant_id: int,
    raw_context: Any,
) -> tuple[Any, str, str]:
    if not isinstance(raw_context, Mapping):
        raise _action_error("claim_context_missing", "The claim linked to this Flow is unavailable.", 409)
    kind = str(raw_context.get("kind") or "").strip().lower()
    raw_id = str(raw_context.get("id") or "").strip()
    expected_code = str(raw_context.get("ticket_number") or "").strip().upper()
    if not raw_id.isdigit() or not expected_code or not _TICKET_CODE.fullmatch(expected_code):
        raise _action_error("claim_context_invalid", "The claim linked to this Flow is invalid.", 400)
    ticket_id = int(raw_id)
    if kind == "municipio":
        tenant = db.session.get(TenantProfile, int(tenant_id))
        ticket = (
            scoped_municipio_ticket_query(tenant)
            .filter(MunicipioTicket.id == ticket_id)
            .first()
        )
    elif kind == "pyme":
        ticket = PymeTicket.query.filter_by(id=ticket_id, tenant_id=int(tenant_id)).first()
    elif kind == "tenant":
        ticket = TenantTicket.query.filter_by(id=ticket_id, tenant_id=int(tenant_id)).first()
    else:
        raise _action_error("claim_context_invalid", "The claim linked to this Flow is invalid.", 400)
    if ticket is None:
        raise _action_error("claim_context_unavailable", "The claim linked to this Flow is unavailable.", 404)
    return ticket, f"{kind}_ticket", expected_code


def _optional_completion_text(value: Any, *, code: str, max_length: int) -> str | None:
    if value in (None, ""):
        return None
    return _required_string(value, code, max_length)


_COMPLETION_RESPONSE_KEYS = frozenset(
    {
        "message_body",
        "options_list",
        "message_type",
        "fuente",
        "generar_audio",
        "skip_audio_generation",
        "instrument_revision",
        "attachment_count",
        "entity",
    }
)


def _completion_response_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Persist only response fields that are safe to replay after commit.

    ``realtime_event`` is intentionally absent: replaying a confirmation must
    never re-emit a vote, CRM event, attachment notification or ticket update.
    """

    snapshot: dict[str, Any] = {}
    for key in _COMPLETION_RESPONSE_KEYS:
        if key not in value:
            continue
        try:
            snapshot[key] = json.loads(
                json.dumps(value[key], ensure_ascii=False, separators=(",", ":"))
            )
        except (TypeError, ValueError, OverflowError):
            continue
    return snapshot


def _completion_response_from_metadata(
    value: Mapping[str, Any],
    *,
    payload_hash: str | None = None,
) -> dict[str, Any]:
    stored_payload_hash = str(value.get("payload_hash") or "").strip()
    if (
        payload_hash
        and stored_payload_hash
        and not hmac.compare_digest(stored_payload_hash, payload_hash)
    ):
        raise _action_error(
            "flow_completion_payload_conflict",
            "Flow completion payload conflicts with the applied receipt.",
            409,
        )

    stored_response = value.get("response")
    if isinstance(stored_response, Mapping):
        response = _completion_response_snapshot(stored_response)
        if str(response.get("message_body") or "").strip():
            response.setdefault("options_list", [{"texto": "Menu", "action_id": "menu_principal"}])
            response.setdefault("message_type", "interactive_buttons")
            response.setdefault("fuente", "whatsapp_flow_completion_replay")
            response.setdefault("generar_audio", True)
            return response

    response = {
        "message_body": str(value.get("message_body") or "Formulario aplicado correctamente."),
        "options_list": [{"texto": "Menu", "action_id": "menu_principal"}],
        "message_type": "interactive_buttons",
        "fuente": str(value.get("source") or "whatsapp_flow_completion_replay"),
        "generar_audio": True,
        "entity": {
            "kind": str(value.get("entity_kind") or "record"),
            "id": str(value.get("entity_id") or ""),
        },
    }
    if value.get("attachment_count") is not None:
        response["attachment_count"] = int(value["attachment_count"])
    return response


def _resolve_survey_context(
    tenant_id: int,
    raw_context: Any,
    *,
    require_instrument_revision: bool = False,
) -> tuple[EncEncuesta, tuple[Any, ...], dict[str, Any]]:
    if not isinstance(raw_context, Mapping):
        raise _action_error(
            "survey_context_missing",
            "A published survey context is required for this Flow.",
            400,
        )
    slug = str(
        raw_context.get("slug")
        or raw_context.get("survey_slug")
        or raw_context.get("public_token")
        or ""
    ).strip()
    if not _SURVEY_SLUG.fullmatch(slug):
        raise _action_error(
            "survey_context_invalid",
            "The survey linked to this Flow is invalid.",
            400,
        )

    from services.encuestas_service import EncuestaError, get_public_encuesta

    try:
        survey = get_public_encuesta(slug, preferred_tenant_id=int(tenant_id))
    except EncuestaError as exc:
        raise _action_error(
            "survey_context_unavailable",
            "The survey linked to this Flow is unavailable.",
            exc.status_code,
        ) from exc
    if int(survey.tenant_id or 0) != int(tenant_id):
        raise _action_error(
            "survey_context_unavailable",
            "The survey linked to this Flow is unavailable.",
            404,
        )
    supplied_id = str(raw_context.get("id") or raw_context.get("survey_id") or "").strip()
    if supplied_id and (not supplied_id.isdigit() or int(supplied_id) != int(survey.id)):
        raise _action_error(
            "survey_context_scope_mismatch",
            "The survey linked to this Flow is unavailable.",
            403,
        )
    supplied_revision = raw_context.get("instrument_revision")
    if supplied_revision in (None, ""):
        supplied_revision = raw_context.get("structure_revision")
    current_revision = int(survey.structure_revision or 1)
    has_conditional_logic = any(
        question.logica_condicional is not None for question in survey.preguntas
    )
    if (
        require_instrument_revision
        and has_conditional_logic
        and supplied_revision in (None, "")
    ):
        raise _action_error(
            "survey_instrument_revision_required",
            "Adaptive survey context is missing its instrument revision.",
            409,
        )
    if supplied_revision not in (None, ""):
        try:
            expected_revision = int(supplied_revision)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _action_error(
                "survey_context_invalid",
                "The survey linked to this Flow is invalid.",
                400,
            ) from exc
        if (
            isinstance(supplied_revision, bool)
            or expected_revision <= 0
            or str(supplied_revision).strip() != str(expected_revision)
        ):
            raise _action_error(
                "survey_context_invalid",
                "The survey linked to this Flow is invalid.",
                400,
            )
        if expected_revision != current_revision:
            raise _action_error(
                "survey_structure_changed",
                "The survey changed after this Flow was sent.",
                409,
            )
    if int(survey.puntos_recompensa or 0) > 0:
        raise _action_error(
            "survey_rewards_require_webview",
            "Reward surveys require the authenticated web experience.",
            409,
        )
    uniqueness_policy = str(survey.politica_unicidad or "libre").strip().lower()
    if not bool(survey.anonimo_permitido) or uniqueness_policy in {
        "por_usuario",
        "usuario",
        "user_id",
        "por_user_id",
    }:
        raise _action_error(
            "survey_authentication_requires_webview",
            "Authenticated surveys require the web experience.",
            409,
        )
    if uniqueness_policy in {"por_dni", "dni", "por_ip", "ip"}:
        raise _action_error(
            "survey_identity_policy_requires_webview",
            "This survey identity policy requires the web experience.",
            409,
        )
    if uniqueness_policy not in {
        "libre",
        "por_cookie",
        "cookie",
        "por_phone",
        "phone",
        "por_telefono",
        "telefono",
        "por_dni_o_phone",
        "dni_o_phone",
    }:
        raise _action_error(
            "survey_uniqueness_policy_requires_webview",
            "This survey uniqueness policy requires the web experience.",
            409,
        )

    questions = tuple(survey.preguntas or ())
    if not 1 <= len(questions) <= len(_SURVEY_QUESTION_SCREENS):
        raise _action_error(
            "survey_question_count_unsupported",
            "This survey is not compatible with the native quick-vote Flow.",
            409,
        )
    for question in questions:
        options = tuple(question.opciones or ())
        if (
            str(question.tipo or "").strip().lower() != "opcion_unica"
            or not bool(question.obligatoria)
            or not 2 <= len(options) <= _MAX_NATIVE_SURVEY_OPTIONS
        ):
            raise _action_error(
                "survey_question_type_unsupported",
                "This survey is not compatible with the native quick-vote Flow.",
                409,
            )
        if any(not str(option.texto or "").strip() for option in options):
            raise _action_error(
                "survey_option_invalid",
                "This survey contains an invalid option.",
                409,
            )
    # Compile once before authorizing a send. This validates persisted v1/v2
    # references and proves that the native runtime can evaluate the same
    # visibility graph as canonical public responses.
    _visible_native_survey_questions(survey, {})
    return survey, questions, {
        "id": str(survey.id),
        "slug": slug,
        "instrument_revision": current_revision,
    }


def _survey_staged_answers(metadata: Any) -> dict[int, int]:
    if not isinstance(metadata, Mapping):
        return {}
    raw_staged = metadata.get("survey_staged_answers")
    if not isinstance(raw_staged, Mapping):
        return {}
    staged: dict[int, int] = {}
    for raw_question_id, raw_option_id in raw_staged.items():
        try:
            question_id = int(raw_question_id)
            option_id = int(raw_option_id)
        except (TypeError, ValueError, OverflowError):
            continue
        if question_id > 0 and option_id > 0:
            staged[question_id] = option_id
    return staged


def _visible_native_survey_questions(
    survey: EncEncuesta,
    staged: Mapping[int, int],
) -> tuple[Any, ...]:
    from services.encuestas_service import EncuestaError, compile_survey_visibility

    selections = {
        int(question_id): {int(option_id)}
        for question_id, option_id in staged.items()
    }
    try:
        visible_questions = compile_survey_visibility(survey).evaluate(
            selected_option_ids_by_question_id=selections,
        )
    except EncuestaError as exc:
        raise _action_error(
            "survey_conditional_logic_invalid",
            "This survey is not compatible with adaptive native navigation.",
            409,
        ) from exc
    if not visible_questions:
        raise _action_error(
            "survey_conditional_logic_invalid",
            "This survey is not compatible with adaptive native navigation.",
            409,
        )
    return tuple(visible_questions)


def _prune_native_survey_answers(
    visible_questions: Sequence[Any],
    staged: Mapping[int, int],
) -> dict[int, int]:
    valid_options_by_question = {
        int(question.id): {int(option.id) for option in question.opciones}
        for question in visible_questions
    }
    return {
        int(question_id): int(option_id)
        for question_id, option_id in staged.items()
        if int(question_id) in valid_options_by_question
        and int(option_id) in valid_options_by_question[int(question_id)]
    }


def _survey_question_response(
    survey: EncEncuesta,
    questions: Sequence[Any],
    index: int,
    *,
    interaction: WhatsAppFlowInteraction | None = None,
) -> dict[str, Any]:
    if index < 0 or index >= len(questions):
        raise _action_error("survey_screen_out_of_range", "Flow screen is invalid.", 400)
    question = questions[index]
    staged = _survey_staged_answers(
        interaction.metadata_json if interaction is not None else None
    )
    visible_question_ids = {int(item.id) for item in questions}
    answered = len(visible_question_ids.intersection(staged))
    adaptive = any(
        item.logica_condicional is not None for item in survey.preguntas
    )
    progress = (
        f"Pregunta {index + 1}"
        if adaptive
        else f"Pregunta {index + 1} de {len(questions)}"
    )
    if answered:
        saved_label = "respuesta guardada" if answered == 1 else "respuestas guardadas"
        progress += f" - {answered} {saved_label}"
    return {
        "screen": _SURVEY_QUESTION_SCREENS[index],
        "data": {
            "survey_title": str(survey.titulo or "Votacion")[:120],
            "progress_label": progress,
            "question_text": str(question.texto or "Elegi una opcion")[:500],
            "options": [
                {"id": str(option.id), "title": str(option.texto or "")[:120]}
                for option in question.opciones
            ],
        },
    }


def _survey_confirmation_response(
    survey: EncEncuesta,
    visible_questions: Sequence[Any],
) -> dict[str, Any]:
    answer_count = len(visible_questions)
    answer_summary = (
        "1 respuesta lista para enviar."
        if answer_count == 1
        else f"{answer_count} respuestas listas para enviar."
    )
    return {
        "screen": "SURVEY_CONFIRM",
        "data": {
            "survey_title": str(survey.titulo or "Votacion")[:120],
            "answer_summary": answer_summary,
            "results_note": (
                "Al finalizar recibiras el acceso a los resultados en vivo."
                if survey.mostrar_resultados_envivo
                else "Tu participacion quedara registrada al finalizar."
            ),
        },
    }


def _normalize_endpoint_id(value: Any) -> str:
    endpoint_id = str(value or "").strip()
    if not _ENDPOINT_ID.fullmatch(endpoint_id):
        raise MetaFlowConfigurationError("endpoint_id_invalid")
    return endpoint_id


def _sender_matches_endpoint(sender: ProviderSender, endpoint_id: str) -> bool:
    waba_id = str(sender.waba_id or "").strip()
    if waba_id and hmac.compare_digest(waba_id, endpoint_id):
        return True
    aliases = _endpoint_aliases(sender)
    return any(hmac.compare_digest(alias, endpoint_id) for alias in aliases)


def _endpoint_aliases(sender: ProviderSender) -> set[str]:
    aliases: set[str] = set()
    metadata = sender.metadata_json if isinstance(sender.metadata_json, Mapping) else {}
    tenant = sender.tenant
    tenant_config = (
        tenant.configuracion
        if tenant is not None and isinstance(tenant.configuracion, Mapping)
        else {}
    )
    for section in _configuration_sections(metadata, tenant_config):
        for key in (
            "endpoint_id",
            "endpoint_alias",
            "data_exchange_endpoint_id",
            "meta_flow_data_exchange_endpoint_id",
            "meta_flow_endpoint_id",
        ):
            _append_aliases(aliases, section.get(key))
        for key in (
            "aliases",
            "endpoint_aliases",
            "data_exchange_endpoint_aliases",
            "meta_flow_data_exchange_endpoint_aliases",
        ):
            _append_aliases(aliases, section.get(key))
    return aliases


def _append_aliases(target: set[str], value: Any) -> None:
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    for item in values:
        alias = str(item or "").strip()
        if _ENDPOINT_ID.fullmatch(alias):
            target.add(alias)


def _configuration_sections(
    sender_metadata: Mapping[str, Any],
    tenant_config: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    sections: list[Mapping[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, Mapping) and value not in sections:
            sections.append(value)

    add(sender_metadata)
    add(sender_metadata.get("meta_flow_data_exchange"))
    add(sender_metadata.get("meta_flow"))
    add(tenant_config.get("meta_flow_data_exchange"))
    meta_platform = tenant_config.get("meta_platform")
    add(meta_platform)
    if isinstance(meta_platform, Mapping):
        add(meta_platform.get("data_exchange"))
    add(tenant_config)
    return tuple(sections)


def _secret_sections(
    sender: ProviderSender,
    tenant: TenantProfile,
) -> tuple[Mapping[str, Any], ...]:
    metadata = sender.metadata_json if isinstance(sender.metadata_json, Mapping) else {}
    tenant_config = tenant.configuracion if isinstance(tenant.configuracion, Mapping) else {}
    return _configuration_sections(metadata, tenant_config)


def _waba_env_prefix(waba_id: str) -> str:
    safe_waba = _SAFE_WABA.sub("_", waba_id).strip("_").upper()
    if not safe_waba or len(safe_waba) > 80:
        raise MetaFlowConfigurationError("waba_id_invalid")
    return f"META_FLOW_WABA_{safe_waba}"


def _resolve_secret(
    sections: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
    fallback_env: str,
    environ: Mapping[str, str],
    *,
    required: bool = False,
    multiline: bool = False,
) -> str | None:
    reference: Any = None
    for section in sections:
        for key in keys:
            if key in section:
                reference = section.get(key)
                break
        if reference is not None:
            break
    env_name = _secret_env_name(reference, fallback_env)
    value = environ.get(env_name)
    if value is None or value == "":
        if required:
            raise MetaFlowConfigurationError("endpoint_secret_not_configured")
        return None
    normalized = str(value)
    if multiline:
        normalized = normalized.replace("\\r\\n", "\n").replace("\\n", "\n")
    if not normalized.strip():
        if required:
            raise MetaFlowConfigurationError("endpoint_secret_not_configured")
        return None
    return normalized


def _secret_env_name(reference: Any, fallback_env: str) -> str:
    if reference is None or reference == "":
        return fallback_env
    if isinstance(reference, Mapping):
        reference = reference.get("env") or reference.get("name")
    raw = str(reference or "").strip()
    if raw.lower().startswith("env:"):
        raw = raw.split(":", 1)[1].strip()
    if not _ENV_NAME.fullmatch(raw):
        raise MetaFlowConfigurationError("secret_reference_invalid")
    return raw


def _validate_context(
    context: MetaFlowRequestContext,
    endpoint: _ResolvedEndpoint,
    expected_action: str,
) -> None:
    if (
        str(context.endpoint_id) != endpoint.endpoint_id
        or str(context.tenant_id) != str(endpoint.tenant.id)
        or str(context.waba_id) != endpoint.waba_id
        or str(context.action) != expected_action
    ):
        raise _action_error(
            "endpoint_scope_mismatch",
            "Flow endpoint scope is invalid.",
            403,
        )


def _validate_version(payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping) or payload.get("version") != DATA_API_VERSION:
        raise _action_error(
            "unsupported_data_api_version",
            "Unsupported Flow Data API version.",
            400,
        )


def _validate_screen(value: Any, flow_id: str, *, optional: bool) -> str | None:
    screen = str(value or "").strip()
    if not screen and optional:
        return None
    if flow_id == CLAIM_FLOW_ID:
        allowed = _CLAIM_SCREENS
    elif flow_id == CLAIM_EVIDENCE_FLOW_ID:
        allowed = _CLAIM_EVIDENCE_SCREENS
    elif flow_id == ORDER_FLOW_ID:
        allowed = _ORDER_SCREENS
    else:
        allowed = _SURVEY_SCREENS
    if screen not in allowed:
        raise _action_error("flow_screen_invalid", "Flow screen is invalid.", 400)
    return screen


def _handle_error_notification(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping) or data.get("error") is None:
        raise _action_error("flow_error_notification_invalid", "Flow error is invalid.", 400)
    return {"data": {"acknowledged": True}}


def _claim_lookup_response(
    data: Mapping[str, Any],
    tenant: TenantProfile,
    *,
    result_screen: str = "CLAIM_RESULT",
) -> tuple[Mapping[str, Any], Any, str]:
    ticket_number = _required_string(
        data.get("ticket_number"),
        "ticket_number_invalid",
        64,
    )
    pin = _required_string(data.get("access_pin"), "access_pin_invalid", 12)
    if not _TICKET_CODE.fullmatch(ticket_number) or not _PIN.fullmatch(pin):
        raise _action_error("claim_credentials_invalid", "Claim credentials are invalid.", 400)
    ticket = _lookup_claim(tenant, ticket_number, pin)
    if ticket is None:
        raise _action_error("claim_not_found", "Claim was not found.", 404)
    status = str(getattr(ticket, "estado", None) or "nuevo").strip().lower()
    label = _STATUS_LABELS.get(status, "En seguimiento")
    last_update = (
        getattr(ticket, "ultima_actividad", None)
        or getattr(ticket, "updated_at", None)
        or getattr(ticket, "fecha", None)
        or getattr(ticket, "created_at", None)
    )
    return (
        {
            "screen": result_screen,
            "data": {
                "status": label,
                "last_update": _display_datetime(last_update),
                "summary": f"Tu reclamo continua en estado {label.lower()}.",
            },
        },
        ticket,
        ticket_number.strip().upper(),
    )


def _claim_context_for_ticket(ticket: Any, *, lookup_code: str) -> dict[str, str]:
    if isinstance(ticket, MunicipioTicket):
        kind = "municipio"
    elif isinstance(ticket, PymeTicket):
        kind = "pyme"
    elif isinstance(ticket, TenantTicket):
        kind = "tenant"
    else:
        raise _action_error(
            "claim_context_invalid",
            "The claim linked to this Flow is unavailable.",
            409,
        )
    return {
        "kind": kind,
        "id": str(ticket.id),
        "ticket_number": lookup_code,
    }


def _lookup_claim(tenant: TenantProfile, ticket_number: str, pin: str) -> Any | None:
    code = ticket_number.strip()
    upper = code.upper()
    prefix = upper[:2] if upper.startswith(("M-", "P-", "T-")) else None
    stripped = upper[2:] if prefix else code
    matches: list[Any] = []

    if prefix in (None, "M-"):
        municipal = (
            scoped_municipio_ticket_query(tenant)
            .filter(MunicipioTicket.nro_ticket == stripped)
            .first()
        )
        if municipal is not None and _pin_matches(
            getattr(municipal, "consulta_pin", None),
            pin,
        ):
            matches.append(municipal)

    if stripped.isdigit() and prefix in (None, "P-"):
        pyme = PymeTicket.query.filter_by(
            tenant_id=int(tenant.id),
            nro_ticket=int(stripped),
        ).first()
        if pyme is not None and _pin_matches(getattr(pyme, "consulta_pin", None), pin):
            matches.append(pyme)

    if stripped.isdigit() and prefix in (None, "T-"):
        tenant_ticket = TenantTicket.query.filter_by(
            tenant_id=int(tenant.id),
            id=int(stripped),
        ).first()
        if tenant_ticket is not None and _tenant_ticket_credentials_match(
            tenant_ticket,
            code,
            pin,
        ):
            matches.append(tenant_ticket)
    elif not stripped.isdigit() and prefix in (None, "T-"):
        tenant_rows = TenantTicket.query.filter(
            TenantTicket.tenant_id == int(tenant.id),
            TenantTicket.fingerprint == stripped,
        ).limit(2).all()
        matches.extend(
            row
            for row in tenant_rows
            if _tenant_ticket_credentials_match(row, code, pin)
        )

    if len(matches) > 1:
        raise _action_error("claim_lookup_ambiguous", "Claim lookup is unavailable.", 409)
    return matches[0] if matches else None


def _tenant_ticket_credentials_match(ticket: TenantTicket, code: str, pin: str) -> bool:
    extra = ticket.datos_extra if isinstance(ticket.datos_extra, Mapping) else {}
    stored_code = str(
        extra.get("nro_ticket")
        or extra.get("ticket_number")
        or extra.get("code")
        or ticket.fingerprint
        or ticket.id
    ).strip()
    candidate_codes = {stored_code.upper(), f"T-{stored_code}".upper()}
    stored_pin = extra.get("consulta_pin") or extra.get("access_pin") or extra.get("pin")
    return code.upper() in candidate_codes and _pin_matches(stored_pin, pin)


def _pin_matches(stored: Any, supplied: str) -> bool:
    stored_value = str(stored or "").strip()
    return bool(stored_value) and hmac.compare_digest(stored_value, supplied)


def _validate_order_input(data: Mapping[str, Any]) -> None:
    _required_string(data.get("full_name"), "full_name_invalid", 120)
    phone = _required_string(data.get("phone"), "phone_invalid", 40)
    if len("".join(character for character in phone if character.isdigit())) < 8:
        raise _action_error("phone_invalid", "Order input is invalid.", 400)
    _required_string(data.get("delivery_address"), "delivery_address_invalid", 240)
    notes = data.get("delivery_notes")
    if notes not in (None, ""):
        _required_string(notes, "delivery_notes_invalid", 500)


def _order_reference(metadata: Any) -> tuple[str, str] | None:
    if not isinstance(metadata, Mapping):
        return None
    context = metadata.get("order_context")
    if isinstance(context, Mapping):
        kind = str(context.get("kind") or context.get("model") or "").strip().lower()
        reference = context.get("id") or context.get("order_id")
        normalized = _normalized_order_reference(reference)
        if normalized and kind:
            return kind, normalized

    keys = (
        ("market", "market_order_id"),
        ("pyme", "pyme_pedido_id"),
        ("conversational", "pedido_conversacional_id"),
        ("order", "order_id"),
    )
    for kind, key in keys:
        normalized = _normalized_order_reference(metadata.get(key))
        if normalized:
            return kind, normalized
    reference = _normalized_order_reference(metadata.get("order_ref"))
    if reference and ":" in reference:
        kind, value = reference.split(":", 1)
        if kind.lower() in {"market", "pyme", "conversational", "order"} and value:
            return kind.lower(), value
    return None


def _normalized_order_reference(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized or not _ORDER_REFERENCE.fullmatch(normalized):
        return None
    return normalized


def _lookup_order(tenant_id: int, reference: tuple[str, str]) -> Any | None:
    kind, raw_id = reference
    if kind in {"order", "orders"}:
        return Order.query.filter_by(id=raw_id, tenant_id=tenant_id).first()
    if not raw_id.isdigit():
        return None
    numeric_id = int(raw_id)
    if kind in {"market", "marketorder", "market_order"}:
        return MarketOrder.legacy_safe_query().filter_by(
            id=numeric_id,
            tenant_id=tenant_id,
        ).first()
    if kind in {"pyme", "pymepedido", "pyme_pedido"}:
        return PymePedido.query.filter_by(id=numeric_id, tenant_id=tenant_id).first()
    if kind in {"conversational", "pedidoconversacional", "pedido_conversacional"}:
        return PedidoConversacional.query.filter_by(
            id=numeric_id,
            tenant_id=tenant_id,
        ).first()
    return None


def _canonical_order_kind(value: str) -> str | None:
    kind = str(value or "").strip().lower()
    if kind in {"order", "orders"}:
        return "order"
    if kind in {"market", "marketorder", "market_order"}:
        return "market"
    if kind in {"pyme", "pymepedido", "pyme_pedido"}:
        return "pyme"
    if kind in {"conversational", "pedidoconversacional", "pedido_conversacional"}:
        return "conversational"
    return None


def _order_summary(order: Any) -> str:
    items = _order_items(order)
    quantity = sum(max(1, item[1]) for item in items)
    if not items:
        return "Pedido listo para revision"
    label = "producto" if quantity == 1 else "productos"
    return f"{quantity} {label} en {len(items)} renglones"


def _order_items(order: Any) -> list[tuple[str, int]]:
    raw_items: Any
    if isinstance(order, PedidoConversacional):
        raw_items = order.items
    elif isinstance(order, PymePedido):
        try:
            raw_items = json.loads(order.detalles or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_items = []
    else:
        raw_items = getattr(order, "items", None)
    if not isinstance(raw_items, (list, tuple)):
        return []

    result: list[tuple[str, int]] = []
    for item in raw_items[:100]:
        if isinstance(item, Mapping):
            name = item.get("title") or item.get("name") or item.get("nombre") or "Producto"
            quantity = item.get("quantity") or item.get("cantidad") or 1
        else:
            name = getattr(item, "title", None) or getattr(item, "name_snapshot", None) or "Producto"
            quantity = getattr(item, "quantity", 1)
        try:
            normalized_quantity = max(1, min(int(quantity), 100000))
        except (TypeError, ValueError):
            normalized_quantity = 1
        result.append((_safe_public_text(name, 80) or "Producto", normalized_quantity))
    return result


def _order_total_display(order: Any) -> str:
    if isinstance(order, Order):
        amount = order.total
        currency = order.currency
    elif isinstance(order, MarketOrder):
        amount = order.total_monetary
        currency = order.currency
    elif isinstance(order, PymePedido):
        amount = order.monto_total
        currency = order.moneda
    else:
        amount = order.monto_monetario
        metadata = order.metadata_payload if isinstance(order.metadata_payload, Mapping) else {}
        currency = metadata.get("currency")
    if amount is None:
        return "Total a confirmar"
    return _format_money(amount, str(currency or "ARS"))


def _format_money(value: Any, currency: str) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return "Total a confirmar"
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    whole, fraction = f"{amount:.2f}".split(".")
    groups: list[str] = []
    remaining = whole
    while remaining:
        groups.append(remaining[-3:])
        remaining = remaining[:-3]
    grouped = ".".join(reversed(groups))
    symbol = "$" if currency.upper() == "ARS" else currency.upper()
    return f"{symbol} {sign}{grouped},{fraction}"


def _required_string(value: Any, code: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise _action_error(code, "Flow input is invalid.", 400)
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or "\x00" in normalized:
        raise _action_error(code, "Flow input is invalid.", 400)
    return normalized


def _safe_public_text(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:max_length]


def _display_datetime(value: Any) -> str:
    if not isinstance(value, datetime):
        return "Sin actualizaciones recientes"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _action_error(code: str, message: str, status_code: int) -> MetaFlowActionError:
    return MetaFlowActionError(code, message, status_code)


__all__ = [
    "CLAIM_EVIDENCE_FLOW_ID",
    "CLAIM_FLOW_ID",
    "ORDER_FLOW_ID",
    "SURVEY_FLOW_ID",
    "EndpointTokenVerifier",
    "MetaFlowRuntime",
    "apply_whatsapp_flow_completion",
    "authorize_order_context",
    "authorize_survey_context",
    "create_meta_flow_runtime_resolver",
    "record_whatsapp_flow_completion_rejection",
    "replay_whatsapp_flow_completion",
]
