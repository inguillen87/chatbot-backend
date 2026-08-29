"""Fail-closed durable delivery for CRM location and form actions.

The CRM timeline is always the first, authoritative write.  This module may
add one tenant-bound WhatsApp domain effect to the *same transaction*, but only
when every external-delivery prerequisite is already provable.  A missing or
ambiguous prerequisite returns an explicit ``internal_only`` decision; it
never converts a CRM action into an optimistic provider-success response.

The outbox contains opaque bindings only.  At worker preflight the current
ticket, immutable comment, authoritative audit event, recipient, sender and template
are reloaded and rebound.  Any mutation or tenant mismatch is permanent and
fails before provider I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import re
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import UUID

from flask import current_app, has_app_context
from sqlalchemy.orm import Session

from models import (
    AuditEvent,
    MessageTemplateRegistry,
    MunicipioTicket,
    Notification,
    NotificationAttempt,
    TenantProfile,
    TicketComentario,
    User,
    WhatsAppContactState,
    db,
)
from services.domain_effect_gate import (
    DomainEffectOutboxConfigurationError,
    resolve_domain_effect_outbox_policy,
)
from services.domain_effect_outbox import (
    AmbiguousDomainEffectError,
    DeliveredDomainEffect,
    DomainEffectClaim,
    DomainEffectRegistry,
    DomainEffectValidationError,
    PermanentDomainEffectError,
    PreparedDomainEffect,
    SkippedDomainEffect,
    stage_domain_effect,
)
from services.employee_ticket_access import employee_ticket_category_access_allows
from services.message_templates import whatsapp_template_lifecycle
from services.tenant_twilio_messaging import (
    TenantTwilioScopeError,
    prepare_bound_tenant_twilio_message,
    resolve_tenant_twilio_sender_snapshot,
    send_prepared_tenant_twilio_message,
)
from utils.roles import (
    ROLE_EMPLEADO,
    ROLE_SUPERADMIN,
    ROLE_TENANT_ADMIN,
    canonical_role,
    is_authorized_superadmin_user,
)
from utils.validators import normalize_phone


CRM_ACTION_AGGREGATE = "municipio_crm_action"
LOCATION_HANDLER = "ticket.crm_action.whatsapp.location.v1"
FORM_HANDLER = "ticket.crm_action.whatsapp.form.v1"
LOCATION_EFFECT_TYPE = "ticket.crm_action.whatsapp.location"
FORM_EFFECT_TYPE = "ticket.crm_action.whatsapp.form"
ACTION_AUDIT_CONTRACT = "inbox.crm_reply_action.audit.v1"
ACTION_AUDIT_EVENT_TYPE = "municipio_ticket.crm_reply_action"
ACTION_AUDIT_RESOURCE_TYPE = "municipio_ticket"
CALLBACK_CORRELATION_CONTRACT = "inbox.crm_action_callback.v1"
DELIVERY_CONTRACT = "inbox.crm_action_delivery.v1"

_SUPPORTED_ACTIONS = frozenset({"share_location", "share_form"})
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_SID_RE = re.compile(r"^HX[0-9a-fA-F]{32}$")
_VARIABLE_SOURCE_RE = re.compile(r"^[1-9][0-9]{0,2}$")
_SESSION_WINDOW = timedelta(hours=24)
_MAX_LABEL_LENGTH = 320
_MAX_FORM_URL_LENGTH = 1000
_CALLBACK_LEASE = timedelta(minutes=15)
_CALLBACK_OPERATIONAL_METADATA_KEYS = frozenset({"provider_call_started"})


class CrmActionDeliveryError(RuntimeError):
    """Caller-visible validation/authorization failure before staging."""

    def __init__(self, code: str):
        self.code = str(code or "crm_action_delivery_invalid")
        super().__init__(self.code)


@dataclass(frozen=True)
class CrmActionDeliveryDecision:
    action: str
    delivery_mode: str
    status: str
    reason_code: str
    effect_count: int = 0
    effect_id: int | None = None
    replayed: bool = False

    @property
    def durably_staged(self) -> bool:
        return self.delivery_mode == "durable_queue" and self.effect_count == 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": DELIVERY_CONTRACT,
            "action": self.action,
            "delivery_mode": self.delivery_mode,
            "status": self.status,
            "reason_code": self.reason_code,
            "external_dispatch": False,
            "provider_acceptance": False,
            "final_delivery": {
                "status": (
                    "pending_provider_callback"
                    if self.durably_staged
                    else "not_dispatched"
                ),
                "authoritative_source": (
                    "provider_status_callback"
                    if self.durably_staged
                    else "crm_timeline"
                ),
            },
            "outbox": {
                "durably_staged": self.durably_staged,
                "effect_count": self.effect_count,
                "effect_id": self.effect_id,
                "worker_authoritative": self.durably_staged,
                "direct_dispatch_performed": False,
                "idempotent_replay": self.replayed,
            },
            "operator_message": (
                "Acción guardada y encolada de forma durable. La entrega queda "
                "pendiente del proveedor y su callback."
                if self.durably_staged
                else "Acción guardada sólo en el CRM; no se realizó un envío externo."
            ),
        }


@dataclass(frozen=True)
class _LoadedCrmAction:
    ticket: MunicipioTicket
    comment: TicketComentario
    tenant: TenantProfile
    actor: User
    audit_event: AuditEvent
    action_payload: dict[str, Any]
    recipient: str
    action_binding: str


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise CrmActionDeliveryError("crm_action_payload_not_canonical") from exc


def _opaque_binding(secret: str, document: Mapping[str, Any]) -> str:
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise DomainEffectOutboxConfigurationError(
            "crm_action_delivery_secret_invalid"
        )
    return hmac.new(secret.encode("utf-8"), _canonical(document), hashlib.sha256).hexdigest()


def _validate_binding_payload(payload: Mapping[str, Any]) -> None:
    if set(payload) != {
        "action_binding",
        "provider_sender_binding",
        "destination_binding",
    }:
        raise DomainEffectValidationError("crm_action_delivery_payload_invalid")
    for key in payload:
        value = payload.get(key)
        if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
            raise DomainEffectValidationError("crm_action_delivery_payload_invalid")


def _ticket_binding_valid(
    ticket: MunicipioTicket,
    tenant: TenantProfile,
) -> bool:
    return bool(
        _positive_int(getattr(ticket, "tenant_id", None)) == _positive_int(tenant.id)
        and _positive_int(getattr(ticket, "municipio_id", None))
        == _positive_int(getattr(tenant, "municipio_id", None))
        and str(getattr(tenant, "tipo", "")).strip().lower() == "municipio"
        and bool(getattr(tenant, "is_active", False))
    )


def _authorize(
    *,
    ticket: MunicipioTicket,
    tenant: TenantProfile,
    actor: Any,
) -> None:
    role = canonical_role(getattr(actor, "rol", None))
    if role not in {ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, ROLE_EMPLEADO}:
        raise CrmActionDeliveryError("crm_action_role_forbidden")
    if role == ROLE_SUPERADMIN and not is_authorized_superadmin_user(actor):
        raise CrmActionDeliveryError("crm_action_superadmin_not_authorized")
    if not _ticket_binding_valid(ticket, tenant):
        raise CrmActionDeliveryError("crm_action_ticket_tenant_mismatch")
    if not employee_ticket_category_access_allows(actor, ticket):
        raise CrmActionDeliveryError("crm_action_ticket_scope_forbidden")
    if role == ROLE_EMPLEADO and _positive_int(getattr(ticket, "asignado_a_id", None)) != _positive_int(
        getattr(actor, "id", None)
    ):
        raise CrmActionDeliveryError("crm_action_employee_assignment_required")


def _normalized_location(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_lat = payload.get("lat")
    raw_lng = payload.get("lng")
    has_lat = raw_lat is not None and raw_lat != ""
    has_lng = raw_lng is not None and raw_lng != ""
    if has_lat != has_lng:
        return None

    lat: float | None = None
    lng: float | None = None
    if has_lat and has_lng:
        if isinstance(raw_lat, bool) or isinstance(raw_lng, bool):
            return None
        try:
            lat = float(raw_lat)
            lng = float(raw_lng)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            not math.isfinite(lat)
            or not math.isfinite(lng)
            or lat < -90
            or lat > 90
            or lng < -180
            or lng > 180
        ):
            return None

    address = str(payload.get("address") or "").strip()
    explicit_label = str(payload.get("label") or "").strip()
    label = explicit_label or address or "Ubicación compartida"
    if (
        not label
        or len(label) > _MAX_LABEL_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in label)
    ):
        return None
    if (
        len(address) > 300
        or any(ord(character) < 32 or ord(character) == 127 for character in address)
        or (lat is None and not address)
    ):
        return None
    return {
        "lat": lat,
        "lng": lng,
        "address": address or None,
        "label": label,
    }


def _normalized_form(payload: Mapping[str, Any]) -> dict[str, str] | None:
    slug = str(payload.get("form_slug") or "").strip().lower()
    label = str(payload.get("label") or "Formulario").strip()
    href = str(payload.get("href") or "").strip()
    if not slug or len(slug) > 160 or not label or len(label) > 255:
        return None
    if not href or len(href) > _MAX_FORM_URL_LENGTH:
        return None
    try:
        parsed = urlparse(href)
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    return {"form_slug": slug, "label": label, "href": href}


def resolve_tenant_public_form_action_payload(
    *,
    tenant_id: int,
    form_slug: str,
) -> dict[str, Any] | None:
    """Resolve a shareable form exclusively from the tenant publication registry.

    Caller-provided URLs and labels are deliberately ignored.  This helper is
    shared by the route integration and the worker so an event cannot preserve
    or dispatch a URL that was not published by this tenant's backend.
    """

    resolved_tenant_id = _positive_int(tenant_id)
    normalized_slug = str(form_slug or "").strip().lower()
    if (
        resolved_tenant_id is None
        or not normalized_slug
        or len(normalized_slug) > 160
        or any(ord(character) < 32 for character in normalized_slug)
    ):
        return None
    try:
        from services.encuestas_service import (
            list_public_encuestas_for_tenant,
            serialize_public_encuesta,
        )

        matches: list[dict[str, Any]] = []
        for encuesta, public_slug in list_public_encuestas_for_tenant(
            resolved_tenant_id,
            limit=25,
        ):
            payload = serialize_public_encuesta(encuesta, public_slug)
            candidate_slug = str(
                payload.get("slug_publico") or public_slug or ""
            ).strip().lower()
            if candidate_slug != normalized_slug:
                continue
            candidate = {
                "id": _positive_int(getattr(encuesta, "id", None)),
                "form_slug": candidate_slug,
                "label": str(payload.get("titulo") or "Formulario").strip(),
                "href": str(payload.get("share_url") or "").strip(),
                "kind": str(payload.get("tipo") or "encuesta").strip().lower(),
            }
            normalized = _normalized_form(candidate)
            if candidate["id"] is None or normalized is None:
                continue
            matches.append(
                {
                    "id": int(candidate["id"]),
                    **normalized,
                    "kind": candidate["kind"] or "encuesta",
                }
            )
        return matches[0] if len(matches) == 1 else None
    except Exception as exc:  # Publication lookup is an availability boundary.
        if has_app_context():
            current_app.logger.warning(
                "CRM action form publication lookup failed tenant=%s error_type=%s",
                resolved_tenant_id,
                type(exc).__name__,
            )
        return None


def _audit_resource_ref(*, ticket_id: int, comment_id: int) -> str:
    return f"{int(ticket_id)}:comment:{int(comment_id)}"


def _audit_details(
    *,
    tenant_id: int,
    ticket_id: int,
    comment_id: int,
    action: str,
    action_payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract_version": ACTION_AUDIT_CONTRACT,
        "tenant_id": int(tenant_id),
        "ticket_id": int(ticket_id),
        "comment_id": int(comment_id),
        "action": action,
        "action_payload": dict(action_payload),
        "delivery_intent": "whatsapp",
        "source": "crm_ticket_reply",
    }


def _validate_comment_binding(
    *,
    ticket: MunicipioTicket,
    comment: TicketComentario,
    actor: Any,
) -> tuple[int, int, int]:
    tenant_id = _positive_int(getattr(ticket, "tenant_id", None))
    ticket_id = _positive_int(getattr(ticket, "id", None))
    comment_id = _positive_int(getattr(comment, "id", None))
    if (
        tenant_id is None
        or ticket_id is None
        or comment_id is None
        or _positive_int(comment.municipio_ticket_id) != ticket_id
        or comment.pyme_ticket_id is not None
        or not bool(comment.es_admin)
        or _positive_int(comment.user_id)
        != _positive_int(getattr(actor, "id", None))
    ):
        raise CrmActionDeliveryError("crm_action_comment_binding_invalid")
    return tenant_id, ticket_id, comment_id


def _canonical_action_payload(
    *,
    action: str,
    action_payload: Mapping[str, Any],
    tenant_id: int,
) -> dict[str, Any]:
    if action == "share_location":
        normalized_location = _normalized_location(action_payload)
        if normalized_location is None:
            raise CrmActionDeliveryError("crm_action_location_payload_invalid")
        return normalized_location
    form_slug = str(action_payload.get("form_slug") or "").strip().lower()
    resolved_form = resolve_tenant_public_form_action_payload(
        tenant_id=tenant_id,
        form_slug=form_slug,
    )
    if resolved_form is None:
        raise CrmActionDeliveryError("crm_action_form_not_tenant_published")
    return resolved_form


def create_ticket_crm_action_audit_event(
    *,
    ticket: MunicipioTicket,
    comment: TicketComentario,
    tenant: TenantProfile,
    actor: Any,
    action: str,
    action_payload: Mapping[str, Any],
    session=None,
) -> AuditEvent:
    """Create or replay the one authoritative action event without committing.

    The ticket row is locked before checking/creating the event.  This gives
    the existing schema an application-level uniqueness fence for the
    ``ticket + comment`` resource while keeping the event in the caller's
    timeline transaction.
    """

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in _SUPPORTED_ACTIONS:
        raise CrmActionDeliveryError("crm_action_not_supported")
    if not isinstance(action_payload, Mapping):
        raise CrmActionDeliveryError("crm_action_payload_invalid")
    effect_session = session or db.session
    _authorize(ticket=ticket, tenant=tenant, actor=actor)
    tenant_id, ticket_id, comment_id = _validate_comment_binding(
        ticket=ticket,
        comment=comment,
        actor=actor,
    )
    locked_ticket = (
        effect_session.query(MunicipioTicket)
        .filter(
            MunicipioTicket.id == ticket_id,
            MunicipioTicket.tenant_id == tenant_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if locked_ticket is None:
        raise CrmActionDeliveryError("crm_action_ticket_tenant_mismatch")
    _authorize(ticket=locked_ticket, tenant=tenant, actor=actor)

    canonical_payload = _canonical_action_payload(
        action=normalized_action,
        action_payload=action_payload,
        tenant_id=tenant_id,
    )
    expected_details = _audit_details(
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        comment_id=comment_id,
        action=normalized_action,
        action_payload=canonical_payload,
    )
    resource_id = _audit_resource_ref(
        ticket_id=ticket_id,
        comment_id=comment_id,
    )
    matches = (
        effect_session.query(AuditEvent)
        .filter_by(
            tenant_id=tenant_id,
            event_type=ACTION_AUDIT_EVENT_TYPE,
            resource_type=ACTION_AUDIT_RESOURCE_TYPE,
            resource_id=resource_id,
        )
        .all()
    )
    if len(matches) > 1:
        raise CrmActionDeliveryError("crm_action_audit_event_duplicate")
    if matches:
        existing = matches[0]
        if (
            _positive_int(existing.actor_user_id)
            != _positive_int(getattr(actor, "id", None))
            or existing.details != expected_details
        ):
            raise CrmActionDeliveryError("crm_action_audit_event_conflict")
        return existing

    event = AuditEvent(
        tenant_id=tenant_id,
        actor_user_id=int(actor.id),
        event_type=ACTION_AUDIT_EVENT_TYPE,
        resource_type=ACTION_AUDIT_RESOURCE_TYPE,
        resource_id=resource_id,
        details=expected_details,
    )
    effect_session.add(event)
    effect_session.flush()
    if _positive_int(event.id) is None:
        raise CrmActionDeliveryError("crm_action_audit_event_not_persisted")
    return event


def _validated_audit_event(
    *,
    audit_event: AuditEvent,
    ticket: MunicipioTicket,
    comment: TicketComentario,
    tenant: TenantProfile,
    actor: Any,
    session,
) -> tuple[str, dict[str, Any]]:
    tenant_id, ticket_id, comment_id = _validate_comment_binding(
        ticket=ticket,
        comment=comment,
        actor=actor,
    )
    audit_event_id = _positive_int(getattr(audit_event, "id", None))
    resource_id = _audit_resource_ref(ticket_id=ticket_id, comment_id=comment_id)
    if audit_event_id is None:
        raise CrmActionDeliveryError("crm_action_audit_event_not_persisted")
    matches = (
        session.query(AuditEvent)
        .filter_by(
            tenant_id=tenant_id,
            event_type=ACTION_AUDIT_EVENT_TYPE,
            resource_type=ACTION_AUDIT_RESOURCE_TYPE,
            resource_id=resource_id,
        )
        .all()
    )
    if len(matches) != 1 or int(matches[0].id) != audit_event_id:
        raise CrmActionDeliveryError("crm_action_audit_event_not_unique")
    persisted = matches[0]
    details = persisted.details if isinstance(persisted.details, Mapping) else {}
    expected_keys = {
        "contract_version",
        "tenant_id",
        "ticket_id",
        "comment_id",
        "action",
        "action_payload",
        "delivery_intent",
        "source",
    }
    action = str(details.get("action") or "").strip().lower()
    payload = details.get("action_payload")
    if (
        set(details) != expected_keys
        or details.get("contract_version") != ACTION_AUDIT_CONTRACT
        or _positive_int(details.get("tenant_id")) != tenant_id
        or _positive_int(details.get("ticket_id")) != ticket_id
        or _positive_int(details.get("comment_id")) != comment_id
        or action not in _SUPPORTED_ACTIONS
        or not isinstance(payload, Mapping)
        or details.get("delivery_intent") != "whatsapp"
        or details.get("source") != "crm_ticket_reply"
        or _positive_int(persisted.actor_user_id)
        != _positive_int(getattr(actor, "id", None))
    ):
        raise CrmActionDeliveryError("crm_action_audit_event_binding_invalid")
    return action, dict(payload)


def _session_is_active(
    *,
    tenant_id: int,
    recipient: str,
    session,
    now: datetime | None = None,
) -> bool:
    state = session.query(WhatsAppContactState).filter_by(
        tenant_id=int(tenant_id),
        recipient=recipient,
    ).one_or_none()
    inbound = _utc(getattr(state, "last_inbound_at", None)) if state else None
    reference = _utc(now) or datetime.now(timezone.utc)
    if inbound is None or inbound > reference:
        return False
    return reference - inbound <= _SESSION_WINDOW


def _location_capability_enabled(sender: Any) -> bool:
    connection = getattr(sender, "provider_connection", None)
    capabilities = getattr(connection, "capabilities", None)
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    whatsapp = capabilities.get("whatsapp")
    whatsapp = whatsapp if isinstance(whatsapp, Mapping) else {}
    # One canonical top-level key plus the same key in a channel namespace.
    # Loose truthy values are intentionally rejected.
    return bool(
        capabilities.get("session_location_messages") is True
        or whatsapp.get("session_location_messages") is True
    )


def _template_delivery_config(row: MessageTemplateRegistry) -> Mapping[str, Any]:
    metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
    config = metadata.get("crm_form_delivery")
    return config if isinstance(config, Mapping) else {}


def _template_is_approved(row: MessageTemplateRegistry) -> bool:
    content_sid = str(row.content_sid or "").strip()
    if not _CONTENT_SID_RE.fullmatch(content_sid):
        return False
    lifecycle = whatsapp_template_lifecycle(
        row.status,
        source="message_template_registry",
        provider_reference=content_sid,
        observed_at=row.last_sync_at,
    )
    return bool(lifecycle.get("production_send_allowed"))


def _form_template(
    *,
    tenant_id: int,
    form_slug: str,
    session,
) -> tuple[MessageTemplateRegistry | None, str | None]:
    rows = session.query(MessageTemplateRegistry).filter_by(
        tenant_id=int(tenant_id),
        provider="twilio",
        channel="whatsapp",
    ).all()
    matches: list[MessageTemplateRegistry] = []
    for row in rows:
        config = _template_delivery_config(row)
        if config.get("enabled") is not True:
            continue
        if str(config.get("form_slug") or "").strip().lower() != form_slug:
            continue
        if _template_is_approved(row):
            matches.append(row)
    if not matches:
        return None, "whatsapp_form_content_not_approved"
    if len(matches) != 1:
        return None, "whatsapp_form_content_ambiguous"
    return matches[0], None


def _form_variables(
    row: MessageTemplateRegistry,
    *,
    form: Mapping[str, str],
    ticket: MunicipioTicket,
) -> tuple[dict[str, str] | None, str | None]:
    config = _template_delivery_config(row)
    raw_sources = config.get("variable_sources")
    if raw_sources is None:
        return {}, None
    if not isinstance(raw_sources, Mapping) or len(raw_sources) > 100:
        return None, "whatsapp_form_variable_sources_invalid"
    sources = {
        "form_url": form["href"],
        "form_slug": form["form_slug"],
        "form_label": form["label"],
        "ticket_reference": str(ticket.nro_ticket or ticket.id),
    }
    variables: dict[str, str] = {}
    for key, source_name in raw_sources.items():
        if not isinstance(key, str) or not _VARIABLE_SOURCE_RE.fullmatch(key):
            return None, "whatsapp_form_variable_sources_invalid"
        source = str(source_name or "").strip()
        if source not in sources:
            return None, "whatsapp_form_variable_sources_invalid"
        variables[key] = sources[source]
    indexes = sorted(int(key) for key in variables)
    if indexes != list(range(1, len(indexes) + 1)):
        return None, "whatsapp_form_variable_sources_invalid"
    return variables, None


def _action_document(
    *,
    tenant_id: int,
    audit_event_id: int,
    audit_actor_user_id: int,
    ticket_id: int,
    comment_id: int,
    action: str,
    action_payload: Mapping[str, Any],
    recipient: str,
    template_registry_id: int | None,
    content_variables: Mapping[str, Any] | None,
    notification_id: str,
    notification_attempt_id: str,
) -> dict[str, Any]:
    return {
        "contract_version": DELIVERY_CONTRACT,
        "tenant_id": tenant_id,
        "audit_contract_version": ACTION_AUDIT_CONTRACT,
        "audit_event_id": audit_event_id,
        "audit_actor_user_id": audit_actor_user_id,
        "ticket_id": ticket_id,
        "comment_id": comment_id,
        "action": action,
        "action_payload": dict(action_payload),
        "recipient": recipient,
        "template_registry_id": template_registry_id,
        "content_variables": dict(content_variables or {}),
        "notification_id": notification_id,
        "notification_attempt_id": notification_attempt_id,
    }


def _bound_uuid(
    secret: str,
    *,
    purpose: str,
    tenant_id: int,
    audit_event_id: int,
) -> str:
    digest = bytearray(
        hmac.new(
            secret.encode("utf-8"),
            f"{CALLBACK_CORRELATION_CONTRACT}:{purpose}:{tenant_id}:{audit_event_id}".encode(
                "utf-8"
            ),
            hashlib.sha256,
        ).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(digest)))


def _callback_correlation_ids(
    secret: str,
    *,
    tenant_id: int,
    audit_event_id: int,
) -> tuple[str, str]:
    return (
        _bound_uuid(
            secret,
            purpose="notification",
            tenant_id=tenant_id,
            audit_event_id=audit_event_id,
        ),
        _bound_uuid(
            secret,
            purpose="attempt",
            tenant_id=tenant_id,
            audit_event_id=audit_event_id,
        ),
    )


def _callback_metadata(
    *,
    audit_event_id: int,
    ticket_id: int,
    comment_id: int,
    action: str,
) -> dict[str, Any]:
    return {
        "contract_version": CALLBACK_CORRELATION_CONTRACT,
        "audit_event_id": int(audit_event_id),
        "ticket_id": int(ticket_id),
        "comment_id": int(comment_id),
        "action": action,
        "dispatch_owner": "ticket_domain_effect_outbox",
    }


def _ensure_callback_correlation(
    *,
    session,
    secret: str,
    tenant_id: int,
    audit_event_id: int,
    ticket_id: int,
    comment_id: int,
    action: str,
    recipient: str,
    sender: Any,
    sender_binding: str,
    template_registry_id: int | None,
    content_sid: str | None,
    content_variables: Mapping[str, Any],
    payload_digest: str,
) -> tuple[Notification, NotificationAttempt]:
    notification_id, attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=tenant_id,
        audit_event_id=audit_event_id,
    )
    metadata = _callback_metadata(
        audit_event_id=audit_event_id,
        ticket_id=ticket_id,
        comment_id=comment_id,
        action=action,
    )
    notification = session.get(Notification, notification_id)
    attempt = session.get(NotificationAttempt, attempt_id)
    if notification is None and attempt is None:
        notification = Notification(
            id=notification_id,
            tenant_id=tenant_id,
            channel="whatsapp",
            recipient=recipient,
            body=f"CRM action {action}",
            status=Notification.STATUS_BLOCKED,
            idempotency_key=f"crm-action:{audit_event_id}",
            max_retries=0,
            attempt_count=0,
            metadata_json=metadata,
            message_template_registry_id=template_registry_id,
            provider_connection_id=int(sender.provider_connection_id),
            provider_sender_id=int(sender.id),
            sender_binding=sender_binding,
            content_sid=content_sid,
            content_variables=dict(content_variables),
            payload_digest=payload_digest,
            provider_status=Notification.PROVIDER_STATUS_UNKNOWN,
        )
        attempt = NotificationAttempt(
            id=attempt_id,
            notification_id=notification_id,
            tenant_id=tenant_id,
            attempt_number=1,
            status=NotificationAttempt.STATUS_BLOCKED,
            provider="twilio",
            provider_status=Notification.PROVIDER_STATUS_UNKNOWN,
            metadata_json=metadata,
        )
        session.add(notification)
        session.add(attempt)
        session.flush()
        return notification, attempt
    if notification is None or attempt is None:
        raise CrmActionDeliveryError("crm_action_callback_correlation_incomplete")
    expected_notification = {
        "tenant_id": tenant_id,
        "channel": "whatsapp",
        "recipient": recipient,
        "idempotency_key": f"crm-action:{audit_event_id}",
        "message_template_registry_id": template_registry_id,
        "provider_connection_id": int(sender.provider_connection_id),
        "provider_sender_id": int(sender.id),
        "sender_binding": sender_binding,
        "content_sid": content_sid,
        "content_variables": dict(content_variables),
        "payload_digest": payload_digest,
        "metadata_json": metadata,
    }
    if any(
        getattr(notification, key) != value
        for key, value in expected_notification.items()
    ):
        raise CrmActionDeliveryError("crm_action_callback_notification_conflict")
    if (
        attempt.notification_id != notification_id
        or _positive_int(attempt.tenant_id) != tenant_id
        or int(attempt.attempt_number) != 1
        or attempt.provider != "twilio"
        or attempt.metadata_json != metadata
    ):
        raise CrmActionDeliveryError("crm_action_callback_attempt_conflict")
    return notification, attempt


def _internal_only(action: str, reason_code: str) -> CrmActionDeliveryDecision:
    return CrmActionDeliveryDecision(
        action=action,
        delivery_mode="internal_event",
        status="recorded_in_crm",
        reason_code=reason_code,
    )


def stage_legacy_ticket_crm_action_delivery(
    *,
    ticket: MunicipioTicket,
    comment: TicketComentario,
    tenant: TenantProfile,
    actor: Any,
    audit_event: AuditEvent,
    session=None,
    registry: DomainEffectRegistry | None = None,
) -> CrmActionDeliveryDecision:
    """Stage one real WhatsApp action or return an honest CRM-only decision.

    The caller must invoke this before committing the timeline transaction.
    Authorization or payload errors raise.  Runtime/configuration blockers are
    normal decisions because the already-created CRM event remains useful.
    """

    if not has_app_context():
        raise DomainEffectOutboxConfigurationError("domain_effect_app_context_required")
    if not isinstance(ticket, MunicipioTicket) or not isinstance(comment, TicketComentario):
        raise CrmActionDeliveryError("crm_action_aggregate_invalid")
    effect_session = session or db.session
    _authorize(ticket=ticket, tenant=tenant, actor=actor)
    tenant_id, ticket_id, comment_id = _validate_comment_binding(
        ticket=ticket,
        comment=comment,
        actor=actor,
    )
    normalized_action, audit_payload = _validated_audit_event(
        audit_event=audit_event,
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=actor,
        session=effect_session,
    )
    audit_event_id = int(audit_event.id)
    audit_actor_user_id = int(audit_event.actor_user_id)

    policy = resolve_domain_effect_outbox_policy(
        current_app.config,
        tenant_id=tenant_id,
    )
    if not policy.enabled:
        return _internal_only(normalized_action, "domain_effect_outbox_disabled")

    recipient = normalize_phone(str(ticket.telefono_vecino or ""))
    if not recipient:
        return _internal_only(normalized_action, "requester_phone_invalid")
    snapshot = resolve_tenant_twilio_sender_snapshot(
        tenant_id=tenant_id,
        channel="whatsapp",
        session=effect_session,
    )
    if snapshot.reason_code or snapshot.sender is None:
        return _internal_only(
            normalized_action,
            snapshot.reason_code or "whatsapp_tenant_sender_resolution_invalid",
        )

    template_registry_id: int | None = None
    content_sid: str | None = None
    content_variables: dict[str, str] = {}
    if normalized_action == "share_location":
        normalized_payload = _normalized_location(audit_payload)
        if normalized_payload is None or normalized_payload != audit_payload:
            raise CrmActionDeliveryError("crm_action_audit_location_invalid")
        if normalized_payload["lat"] is None or normalized_payload["lng"] is None:
            return _internal_only(
                normalized_action,
                "whatsapp_location_coordinates_required",
            )
        if not _location_capability_enabled(snapshot.sender):
            return _internal_only(normalized_action, "whatsapp_location_capability_missing")
        if not _session_is_active(
            tenant_id=tenant_id,
            recipient=recipient,
            session=effect_session,
        ):
            return _internal_only(normalized_action, "whatsapp_session_window_closed")
        persistent_action = (
            f"geo:{normalized_payload['lat']!r},{normalized_payload['lng']!r}|"
            f"{normalized_payload['label']}"
        )
        preflight = prepare_bound_tenant_twilio_message(
            tenant_id=tenant_id,
            channel="whatsapp",
            expected_sender_binding=snapshot.binding,
            recipient=recipient,
            persistent_actions=(persistent_action,),
            session=effect_session,
        )
    else:
        normalized_form = _normalized_form(audit_payload)
        resolved_form = resolve_tenant_public_form_action_payload(
            tenant_id=tenant_id,
            form_slug=str(audit_payload.get("form_slug") or ""),
        )
        if normalized_form is None or resolved_form is None:
            return _internal_only(normalized_action, "tenant_form_not_available")
        if resolved_form != audit_payload:
            return _internal_only(normalized_action, "tenant_form_publication_changed")
        normalized_payload = resolved_form
        template, template_reason = _form_template(
            tenant_id=tenant_id,
            form_slug=normalized_form["form_slug"],
            session=effect_session,
        )
        if template is None:
            return _internal_only(normalized_action, template_reason or "whatsapp_form_content_missing")
        template_registry_id = int(template.id)
        variables, variable_reason = _form_variables(
            template,
            form=normalized_payload,
            ticket=ticket,
        )
        if variables is None:
            return _internal_only(normalized_action, variable_reason or "whatsapp_form_variables_invalid")
        content_variables = variables
        content_sid = str(template.content_sid or "").strip()
        preflight = prepare_bound_tenant_twilio_message(
            tenant_id=tenant_id,
            channel="whatsapp",
            expected_sender_binding=snapshot.binding,
            recipient=recipient,
            template_registry_id=template_registry_id,
            content_variables=content_variables,
            session=effect_session,
        )

    if preflight.reason_code or preflight.prepared is None:
        return _internal_only(
            normalized_action,
            preflight.reason_code or "whatsapp_action_preflight_invalid",
        )
    if not str(preflight.prepared.params.get("status_callback") or "").strip():
        return _internal_only(normalized_action, "whatsapp_status_callback_missing")

    secret = str(policy.secret or "")
    notification_id, notification_attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=tenant_id,
        audit_event_id=audit_event_id,
    )
    action_document = _action_document(
        tenant_id=tenant_id,
        audit_event_id=audit_event_id,
        audit_actor_user_id=audit_actor_user_id,
        ticket_id=ticket_id,
        comment_id=comment_id,
        action=normalized_action,
        action_payload=normalized_payload,
        recipient=recipient,
        template_registry_id=template_registry_id,
        content_variables=content_variables,
        notification_id=notification_id,
        notification_attempt_id=notification_attempt_id,
    )
    action_binding = _opaque_binding(secret, action_document)
    payload_digest = hashlib.sha256(_canonical(action_document)).hexdigest()
    _ensure_callback_correlation(
        session=effect_session,
        secret=secret,
        tenant_id=tenant_id,
        audit_event_id=audit_event_id,
        ticket_id=ticket_id,
        comment_id=comment_id,
        action=normalized_action,
        recipient=recipient,
        sender=snapshot.sender,
        sender_binding=snapshot.binding,
        template_registry_id=template_registry_id,
        content_sid=content_sid,
        content_variables=content_variables,
        payload_digest=payload_digest,
    )
    destination_binding = _opaque_binding(
        secret,
        {
            "contract_version": DELIVERY_CONTRACT,
            "tenant_id": tenant_id,
            "audit_event_id": audit_event_id,
            "ticket_id": ticket_id,
            "recipient": recipient,
            "notification_attempt_id": notification_attempt_id,
        },
    )
    effect_registry = registry or CRM_ACTION_DOMAIN_EFFECT_REGISTRY
    handler = LOCATION_HANDLER if normalized_action == "share_location" else FORM_HANDLER
    effect_type = LOCATION_EFFECT_TYPE if normalized_action == "share_location" else FORM_EFFECT_TYPE
    result = stage_domain_effect(
        tenant_id=tenant_id,
        aggregate_type=CRM_ACTION_AGGREGATE,
        aggregate_ref=str(audit_event_id),
        effect_type=effect_type,
        handler_name=handler,
        channel="whatsapp",
        recipient_ref="role:ticket.requester",
        effect_key=f"{CRM_ACTION_AGGREGATE}:{audit_event_id}:{handler}",
        intent_secret=secret,
        registry=effect_registry,
        payload={
            "action_binding": action_binding,
            "provider_sender_binding": snapshot.binding,
            "destination_binding": destination_binding,
        },
        max_attempts=policy.max_attempts,
        max_payload_bytes=policy.max_payload_bytes,
        session=effect_session,
    )
    return CrmActionDeliveryDecision(
        action=normalized_action,
        delivery_mode="durable_queue",
        status="durably_staged",
        reason_code=("idempotent_replay_domain_effect_preserved" if result.replayed else "domain_effect_durably_staged"),
        effect_count=1,
        effect_id=result.effect_id,
        replayed=result.replayed,
    )


def _loaded_action(
    claim: DomainEffectClaim,
    *,
    action: str,
) -> _LoadedCrmAction:
    if claim.aggregate_type != CRM_ACTION_AGGREGATE:
        raise PermanentDomainEffectError("crm_action_aggregate_type_invalid")
    audit_event_id = _positive_int(claim.aggregate_ref)
    tenant_id = _positive_int(claim.tenant_id)
    if audit_event_id is None or tenant_id is None:
        raise PermanentDomainEffectError("crm_action_audit_event_ref_invalid")
    audit_event = AuditEvent.query.filter_by(
        id=audit_event_id,
        tenant_id=tenant_id,
        event_type=ACTION_AUDIT_EVENT_TYPE,
        resource_type=ACTION_AUDIT_RESOURCE_TYPE,
    ).one_or_none()
    if audit_event is None:
        raise PermanentDomainEffectError("crm_action_audit_event_missing")
    details = audit_event.details if isinstance(audit_event.details, Mapping) else {}
    expected_keys = {
        "contract_version",
        "tenant_id",
        "ticket_id",
        "comment_id",
        "action",
        "action_payload",
        "delivery_intent",
        "source",
    }
    ticket_id = _positive_int(details.get("ticket_id"))
    comment_id = _positive_int(details.get("comment_id"))
    source_action = str(details.get("action") or "").strip().lower()
    raw_payload = details.get("action_payload")
    if (
        set(details) != expected_keys
        or details.get("contract_version") != ACTION_AUDIT_CONTRACT
        or _positive_int(details.get("tenant_id")) != tenant_id
        or ticket_id is None
        or comment_id is None
        or source_action != action
        or source_action not in _SUPPORTED_ACTIONS
        or not isinstance(raw_payload, Mapping)
        or details.get("delivery_intent") != "whatsapp"
        or details.get("source") != "crm_ticket_reply"
        or audit_event.resource_id
        != _audit_resource_ref(ticket_id=ticket_id, comment_id=comment_id)
    ):
        raise PermanentDomainEffectError("crm_action_audit_event_binding_invalid")
    source_matches = AuditEvent.query.filter_by(
        tenant_id=tenant_id,
        event_type=ACTION_AUDIT_EVENT_TYPE,
        resource_type=ACTION_AUDIT_RESOURCE_TYPE,
        resource_id=audit_event.resource_id,
    ).all()
    if len(source_matches) != 1 or int(source_matches[0].id) != audit_event_id:
        raise PermanentDomainEffectError("crm_action_audit_event_not_unique")

    ticket = MunicipioTicket.query.filter_by(id=ticket_id, tenant_id=tenant_id).one_or_none()
    comment = TicketComentario.query.filter_by(
        id=comment_id,
        municipio_ticket_id=ticket_id,
    ).one_or_none()
    tenant = db.session.get(TenantProfile, tenant_id)
    actor = db.session.get(User, _positive_int(audit_event.actor_user_id))
    if ticket is None or comment is None or tenant is None or actor is None:
        raise PermanentDomainEffectError("crm_action_audit_binding_target_missing")
    if (
        not _ticket_binding_valid(ticket, tenant)
        or not bool(comment.es_admin)
        or comment.pyme_ticket_id is not None
        or _positive_int(comment.user_id) != _positive_int(actor.id)
    ):
        raise PermanentDomainEffectError("crm_action_ticket_binding_invalid")
    try:
        _authorize(ticket=ticket, tenant=tenant, actor=actor)
    except CrmActionDeliveryError as exc:
        raise PermanentDomainEffectError(exc.code) from exc
    recipient = normalize_phone(str(ticket.telefono_vecino or ""))
    return _LoadedCrmAction(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=actor,
        audit_event=audit_event,
        action_payload=dict(raw_payload),
        recipient=recipient or "",
        action_binding=str(claim.payload.get("action_binding") or ""),
    )


def _verify_destination_binding(
    *,
    claim: DomainEffectClaim,
    secret: str,
    audit_event_id: int,
    ticket_id: int,
    recipient: str,
    notification_attempt_id: str,
) -> None:
    expected = _opaque_binding(
        secret,
        {
            "contract_version": DELIVERY_CONTRACT,
            "tenant_id": int(claim.tenant_id),
            "audit_event_id": audit_event_id,
            "ticket_id": ticket_id,
            "recipient": recipient,
            "notification_attempt_id": notification_attempt_id,
        },
    )
    if not hmac.compare_digest(expected, str(claim.payload.get("destination_binding") or "")):
        raise PermanentDomainEffectError("crm_action_recipient_binding_invalid")


def _strict_callback_correlation(
    *,
    session,
    loaded: _LoadedCrmAction,
    secret: str,
    action: str,
    sender: Any,
    sender_binding: str,
    template_registry_id: int | None,
    content_sid: str | None,
    content_variables: Mapping[str, Any],
    payload_digest: str,
    require_reserved: bool,
) -> tuple[Notification, NotificationAttempt]:
    tenant_id = int(loaded.tenant.id)
    audit_event_id = int(loaded.audit_event.id)
    notification_id, attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=tenant_id,
        audit_event_id=audit_event_id,
    )
    notification = session.get(Notification, notification_id)
    attempt = session.get(NotificationAttempt, attempt_id)
    metadata = _callback_metadata(
        audit_event_id=audit_event_id,
        ticket_id=int(loaded.ticket.id),
        comment_id=int(loaded.comment.id),
        action=action,
    )
    if notification is None or attempt is None:
        raise PermanentDomainEffectError("crm_action_callback_correlation_missing")
    expected_notification = {
        "tenant_id": tenant_id,
        "channel": "whatsapp",
        "recipient": loaded.recipient,
        "idempotency_key": f"crm-action:{audit_event_id}",
        "message_template_registry_id": template_registry_id,
        "provider_connection_id": int(sender.provider_connection_id),
        "provider_sender_id": int(sender.id),
        "sender_binding": sender_binding,
        "content_sid": content_sid,
        "content_variables": dict(content_variables),
        "payload_digest": payload_digest,
        "metadata_json": metadata,
    }
    if any(
        getattr(notification, key) != value
        for key, value in expected_notification.items()
    ):
        raise PermanentDomainEffectError("crm_action_callback_notification_binding_invalid")
    attempt_metadata = (
        dict(attempt.metadata_json)
        if isinstance(attempt.metadata_json, Mapping)
        else {}
    )
    binding_metadata = {
        key: attempt_metadata.get(key)
        for key in metadata
    }
    operational_keys = set(attempt_metadata) - set(metadata)
    operational_metadata_valid = bool(
        operational_keys.issubset(_CALLBACK_OPERATIONAL_METADATA_KEYS)
        and (
            "provider_call_started" not in operational_keys
            or attempt_metadata.get("provider_call_started") is True
        )
    )
    if (
        attempt.notification_id != notification_id
        or _positive_int(attempt.tenant_id) != tenant_id
        or int(attempt.attempt_number) != 1
        or attempt.provider != "twilio"
        or binding_metadata != metadata
        or not operational_metadata_valid
    ):
        raise PermanentDomainEffectError("crm_action_callback_attempt_binding_invalid")
    if require_reserved and (
        notification.status != Notification.STATUS_BLOCKED
        or int(notification.attempt_count) != 0
        or notification.lease_token is not None
        or notification.leased_until is not None
        or notification.provider_status != Notification.PROVIDER_STATUS_UNKNOWN
        or attempt.status != NotificationAttempt.STATUS_BLOCKED
        or attempt.provider_status != Notification.PROVIDER_STATUS_UNKNOWN
        or attempt.provider_message_id is not None
    ):
        raise PermanentDomainEffectError("crm_action_callback_reservation_state_invalid")
    return notification, attempt


def _callback_error_digest(error_code: str) -> str:
    return hashlib.sha256(str(error_code).encode("utf-8")).hexdigest()


def _callback_error_code(exc: BaseException, *, fallback: str) -> str:
    candidate = getattr(exc, "code", None)
    rendered = str(candidate or "").strip().lower()
    if rendered and re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", rendered):
        return rendered
    return fallback


def _callback_rows_for_update(
    *,
    session,
    tenant_id: int,
    notification_id: str,
    attempt_id: str,
) -> tuple[Notification, NotificationAttempt]:
    notification = (
        session.query(Notification)
        .filter_by(id=notification_id, tenant_id=tenant_id)
        .with_for_update()
        .one_or_none()
    )
    attempt = (
        session.query(NotificationAttempt)
        .filter_by(
            id=attempt_id,
            notification_id=notification_id,
            tenant_id=tenant_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if notification is None or attempt is None:
        raise PermanentDomainEffectError("crm_action_callback_correlation_missing")
    return notification, attempt


def _notification_terminal_pair(
    notification: Notification,
    attempt: NotificationAttempt,
) -> str | None:
    if (
        notification.status == Notification.STATUS_SENT
        and attempt.status == NotificationAttempt.STATUS_SUCCESS
    ):
        return "sent"
    if (
        notification.status == Notification.STATUS_FAILED
        and attempt.status == NotificationAttempt.STATUS_FAILED
    ):
        return "failed"
    if (
        notification.status == Notification.STATUS_SEND_UNCERTAIN
        and attempt.status == NotificationAttempt.STATUS_SEND_UNCERTAIN
    ):
        return "send_uncertain"
    return None


def _record_callback_terminal_outcome(
    *,
    claim: DomainEffectClaim,
    loaded: _LoadedCrmAction,
    secret: str,
    action: str,
    sender: Any,
    sender_binding: str,
    template_registry_id: int | None,
    content_sid: str | None,
    content_variables: Mapping[str, Any],
    payload_digest: str,
    error_code: str,
    provider_call_started: bool,
    provider_ref: str | None = None,
) -> str:
    """Close a correlated SENDING reservation without permitting redispatch.

    A known pre-provider failure becomes ``failed``.  Once the provider-call
    hook was durably recorded, or a provider reference was observed, the only
    safe terminal result is ``send_uncertain``.  Existing terminal callback
    evidence is authoritative and is never downgraded.
    """

    tenant_id = int(claim.tenant_id)
    notification_id, attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=tenant_id,
        audit_event_id=int(loaded.audit_event.id),
    )
    operation_now = datetime.now(timezone.utc)
    safe_error_code = (
        error_code
        if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", str(error_code or ""))
        else "crm_action_terminal_error_redacted"
    )
    normalized_provider_ref = str(provider_ref or "").strip()[:180] or None

    with Session(bind=db.engine, expire_on_commit=False) as callback_session:
        notification, attempt = _callback_rows_for_update(
            session=callback_session,
            tenant_id=tenant_id,
            notification_id=notification_id,
            attempt_id=attempt_id,
        )
        _strict_callback_correlation(
            session=callback_session,
            loaded=loaded,
            secret=secret,
            action=action,
            sender=sender,
            sender_binding=sender_binding,
            template_registry_id=template_registry_id,
            content_sid=content_sid,
            content_variables=content_variables,
            payload_digest=payload_digest,
            require_reserved=False,
        )

        terminal = _notification_terminal_pair(notification, attempt)
        if terminal is not None:
            if notification.lease_token is not None or notification.leased_until is not None:
                raise PermanentDomainEffectError("crm_action_callback_terminal_lease_invalid")
            callback_session.rollback()
            return terminal

        if (
            notification.status != Notification.STATUS_SENDING
            or attempt.status != NotificationAttempt.STATUS_SENDING
            or int(notification.attempt_count) != 1
            or notification.lease_token is None
            or notification.leased_until is None
            or not hmac.compare_digest(
                str(notification.lease_token),
                str(claim.lease_token),
            )
        ):
            raise PermanentDomainEffectError("crm_action_callback_sending_state_invalid")

        attempt_metadata = dict(attempt.metadata_json or {})
        provider_call_was_recorded = bool(
            attempt_metadata.get("provider_call_started") is True
        )
        ambiguous = bool(
            provider_call_started
            or provider_call_was_recorded
            or normalized_provider_ref
        )

        if normalized_provider_ref:
            persisted_refs = {
                str(value)
                for value in (
                    notification.provider_message_id,
                    attempt.provider_message_id,
                )
                if value
            }
            if not persisted_refs:
                notification.provider_message_id = normalized_provider_ref
                attempt.provider_message_id = normalized_provider_ref
            elif persisted_refs == {normalized_provider_ref}:
                notification.provider_message_id = normalized_provider_ref
                attempt.provider_message_id = normalized_provider_ref
            # A conflicting provider reference is itself ambiguous. Preserve
            # the first persisted evidence and never overwrite it.

        notification.status = (
            Notification.STATUS_SEND_UNCERTAIN
            if ambiguous
            else Notification.STATUS_FAILED
        )
        attempt.status = (
            NotificationAttempt.STATUS_SEND_UNCERTAIN
            if ambiguous
            else NotificationAttempt.STATUS_FAILED
        )
        if normalized_provider_ref and not notification.provider_status == "accepted":
            notification.provider_status = "accepted"
        if normalized_provider_ref and not attempt.provider_status == "accepted":
            attempt.provider_status = "accepted"
        if not normalized_provider_ref:
            notification.provider_status = Notification.PROVIDER_STATUS_UNKNOWN
            attempt.provider_status = Notification.PROVIDER_STATUS_UNKNOWN
        notification.last_error = safe_error_code
        notification.next_retry_at = None
        notification.lease_token = None
        notification.leased_until = None
        notification.updated_at = operation_now
        attempt.error_message = safe_error_code
        attempt.error_digest = _callback_error_digest(safe_error_code)
        attempt.next_retry_at = None
        callback_session.add(notification)
        callback_session.add(attempt)
        callback_session.commit()
        return "send_uncertain" if ambiguous else "failed"


def _mark_callback_provider_call_started(
    *,
    claim: DomainEffectClaim,
    loaded: _LoadedCrmAction,
    secret: str,
    action: str,
    sender: Any,
    sender_binding: str,
    template_registry_id: int | None,
    content_sid: str | None,
    content_variables: Mapping[str, Any],
    payload_digest: str,
) -> None:
    """Durably mark the exact callback attempt immediately before provider I/O."""

    tenant_id = int(claim.tenant_id)
    notification_id, attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=tenant_id,
        audit_event_id=int(loaded.audit_event.id),
    )
    with Session(bind=db.engine, expire_on_commit=False) as callback_session:
        notification, attempt = _callback_rows_for_update(
            session=callback_session,
            tenant_id=tenant_id,
            notification_id=notification_id,
            attempt_id=attempt_id,
        )
        _strict_callback_correlation(
            session=callback_session,
            loaded=loaded,
            secret=secret,
            action=action,
            sender=sender,
            sender_binding=sender_binding,
            template_registry_id=template_registry_id,
            content_sid=content_sid,
            content_variables=content_variables,
            payload_digest=payload_digest,
            require_reserved=False,
        )
        if (
            notification.status != Notification.STATUS_SENDING
            or attempt.status != NotificationAttempt.STATUS_SENDING
            or int(notification.attempt_count) != 1
            or notification.lease_token is None
            or notification.leased_until is None
            or not hmac.compare_digest(
                str(notification.lease_token),
                str(claim.lease_token),
            )
        ):
            raise PermanentDomainEffectError("crm_action_callback_sending_state_invalid")
        metadata = dict(attempt.metadata_json or {})
        if metadata.get("provider_call_started") is True:
            raise PermanentDomainEffectError("crm_action_provider_call_already_started")
        metadata["provider_call_started"] = True
        attempt.metadata_json = metadata
        callback_session.add(attempt)
        callback_session.commit()


def _activate_callback_and_prepare(
    *,
    claim: DomainEffectClaim,
    loaded: _LoadedCrmAction,
    secret: str,
    action: str,
    sender: Any,
    sender_binding: str,
    template_registry_id: int | None,
    content_sid: str | None,
    content_variables: Mapping[str, Any],
    payload_digest: str,
    persistent_actions: tuple[str, ...] = (),
):
    """Activate the reserved callback identity after the outbox I/O fence.

    The domain-effect dispatcher calls this only from ``deliver`` after its
    own ``io_started_at`` commit.  The reservation becomes ``sending`` in an
    independent transaction before Twilio can observe the callback URL.
    """

    notification_id, attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=int(claim.tenant_id),
        audit_event_id=int(loaded.audit_event.id),
    )
    operation_now = datetime.now(timezone.utc)
    with Session(bind=db.engine, expire_on_commit=False) as callback_session:
        notification = (
            callback_session.query(Notification)
            .filter_by(id=notification_id, tenant_id=int(claim.tenant_id))
            .with_for_update()
            .one_or_none()
        )
        attempt = (
            callback_session.query(NotificationAttempt)
            .filter_by(
                id=attempt_id,
                notification_id=notification_id,
                tenant_id=int(claim.tenant_id),
            )
            .with_for_update()
            .one_or_none()
        )
        if notification is None or attempt is None:
            callback_session.rollback()
            raise PermanentDomainEffectError("crm_action_callback_correlation_missing")
        # Repeat the full immutable binding check in this independent session.
        _strict_callback_correlation(
            session=callback_session,
            loaded=loaded,
            secret=secret,
            action=action,
            sender=sender,
            sender_binding=sender_binding,
            template_registry_id=template_registry_id,
            content_sid=content_sid,
            content_variables=content_variables,
            payload_digest=payload_digest,
            require_reserved=True,
        )
        notification.status = Notification.STATUS_SENDING
        notification.attempt_count = 1
        notification.lease_token = str(claim.lease_token)
        notification.leased_until = operation_now + _CALLBACK_LEASE
        attempt.status = NotificationAttempt.STATUS_SENDING
        attempt.attempted_at = operation_now
        callback_session.flush()
        callback_session.commit()

        try:
            preflight = prepare_bound_tenant_twilio_message(
                tenant_id=int(claim.tenant_id),
                channel="whatsapp",
                expected_sender_binding=sender_binding,
                recipient=loaded.recipient,
                template_registry_id=template_registry_id,
                content_variables=(
                    dict(content_variables)
                    if template_registry_id is not None
                    else None
                ),
                persistent_actions=persistent_actions,
                notification_attempt_id=attempt_id,
                session=callback_session,
            )
            if preflight.reason_code:
                raise PermanentDomainEffectError(preflight.reason_code)
            if preflight.prepared is None:
                raise PermanentDomainEffectError(
                    "crm_action_twilio_preflight_invalid"
                )
            refreshed_attempt = callback_session.get(
                NotificationAttempt,
                attempt_id,
            )
            if (
                refreshed_attempt is None
                or refreshed_attempt.status
                != NotificationAttempt.STATUS_SENDING
            ):
                raise PermanentDomainEffectError(
                    "crm_action_callback_attempt_state_invalid"
                )
            callback_session.rollback()
            return preflight.prepared
        except Exception as exc:
            callback_session.rollback()
            terminal_error = _callback_error_code(
                exc,
                fallback="crm_action_correlated_preflight_failed",
            )
            try:
                _record_callback_terminal_outcome(
                    claim=claim,
                    loaded=loaded,
                    secret=secret,
                    action=action,
                    sender=sender,
                    sender_binding=sender_binding,
                    template_registry_id=template_registry_id,
                    content_sid=content_sid,
                    content_variables=content_variables,
                    payload_digest=payload_digest,
                    error_code=terminal_error,
                    provider_call_started=False,
                )
            except Exception as terminal_exc:
                current_app.logger.error(
                    "CRM action terminal persistence failed effect=%s "
                    "phase=correlated_preflight error_type=%s",
                    claim.effect_id,
                    type(terminal_exc).__name__,
                )
                raise AmbiguousDomainEffectError(
                    "crm_action_terminal_persistence_unknown"
                ) from terminal_exc
            if isinstance(exc, PermanentDomainEffectError):
                raise
            if isinstance(exc, TenantTwilioScopeError):
                raise PermanentDomainEffectError(exc.code) from exc
            raise PermanentDomainEffectError(terminal_error) from exc


def _record_callback_provider_acceptance(
    *,
    claim: DomainEffectClaim,
    secret: str,
    audit_event_id: int,
    provider_ref: str,
) -> None:
    notification_id, attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=int(claim.tenant_id),
        audit_event_id=audit_event_id,
    )
    operation_now = datetime.now(timezone.utc)
    with Session(bind=db.engine, expire_on_commit=False) as callback_session:
        notification = (
            callback_session.query(Notification)
            .filter_by(id=notification_id, tenant_id=int(claim.tenant_id))
            .with_for_update()
            .one_or_none()
        )
        attempt = (
            callback_session.query(NotificationAttempt)
            .filter_by(
                id=attempt_id,
                notification_id=notification_id,
                tenant_id=int(claim.tenant_id),
            )
            .with_for_update()
            .one_or_none()
        )
        if notification is None or attempt is None:
            raise PermanentDomainEffectError("crm_action_callback_correlation_missing")
        if notification.provider_message_id not in {None, provider_ref}:
            raise PermanentDomainEffectError("crm_action_callback_provider_ref_conflict")
        if attempt.provider_message_id not in {None, provider_ref}:
            raise PermanentDomainEffectError("crm_action_callback_provider_ref_conflict")
        notification.provider_message_id = provider_ref
        attempt.provider_message_id = provider_ref
        if notification.provider_status == Notification.PROVIDER_STATUS_UNKNOWN:
            notification.provider_status = "accepted"
        if attempt.provider_status == Notification.PROVIDER_STATUS_UNKNOWN:
            attempt.provider_status = "accepted"
        if notification.status == Notification.STATUS_SENDING:
            notification.status = Notification.STATUS_SENT
            notification.sent_at = operation_now
            notification.lease_token = None
            notification.leased_until = None
        if attempt.status == NotificationAttempt.STATUS_SENDING:
            attempt.status = NotificationAttempt.STATUS_SUCCESS
        callback_session.commit()


def _deliver_correlated_crm_action(
    *,
    claim: DomainEffectClaim,
    loaded: _LoadedCrmAction,
    secret: str,
    action: str,
    sender: Any,
    sender_binding: str,
    template_registry_id: int | None,
    content_sid: str | None,
    content_variables: Mapping[str, Any],
    payload_digest: str,
    prepared: Any,
) -> str:
    """Perform at most one provider call and close every SENDING outcome."""

    provider_call_started = False

    def mark_provider_call_started() -> None:
        nonlocal provider_call_started
        _mark_callback_provider_call_started(
            claim=claim,
            loaded=loaded,
            secret=secret,
            action=action,
            sender=sender,
            sender_binding=sender_binding,
            template_registry_id=template_registry_id,
            content_sid=content_sid,
            content_variables=content_variables,
            payload_digest=payload_digest,
        )
        provider_call_started = True

    try:
        provider_ref = send_prepared_tenant_twilio_message(
            prepared,
            on_provider_call_start=mark_provider_call_started,
        )
        provider_ref = str(provider_ref or "").strip()[:180] or None
    except Exception as exc:
        error_code = (
            "provider_acceptance_unknown"
            if provider_call_started
            else "crm_action_provider_call_failed_before_io"
        )
        try:
            _record_callback_terminal_outcome(
                claim=claim,
                loaded=loaded,
                secret=secret,
                action=action,
                sender=sender,
                sender_binding=sender_binding,
                template_registry_id=template_registry_id,
                content_sid=content_sid,
                content_variables=content_variables,
                payload_digest=payload_digest,
                error_code=error_code,
                provider_call_started=provider_call_started,
            )
        except Exception as terminal_exc:
            current_app.logger.error(
                "CRM action terminal persistence failed effect=%s "
                "phase=provider_call error_type=%s",
                claim.effect_id,
                type(terminal_exc).__name__,
            )
            raise AmbiguousDomainEffectError(
                "crm_action_terminal_persistence_unknown"
            ) from terminal_exc
        if provider_call_started:
            raise AmbiguousDomainEffectError(
                "provider_acceptance_unknown"
            ) from exc
        raise PermanentDomainEffectError(error_code) from exc

    if provider_ref is None:
        try:
            _record_callback_terminal_outcome(
                claim=claim,
                loaded=loaded,
                secret=secret,
                action=action,
                sender=sender,
                sender_binding=sender_binding,
                template_registry_id=template_registry_id,
                content_sid=content_sid,
                content_variables=content_variables,
                payload_digest=payload_digest,
                error_code="provider_acceptance_unknown",
                provider_call_started=True,
            )
        except Exception as terminal_exc:
            current_app.logger.error(
                "CRM action terminal persistence failed effect=%s "
                "phase=provider_reference error_type=%s",
                claim.effect_id,
                type(terminal_exc).__name__,
            )
            raise AmbiguousDomainEffectError(
                "crm_action_terminal_persistence_unknown"
            ) from terminal_exc
        raise AmbiguousDomainEffectError("provider_acceptance_unknown")

    try:
        _record_callback_provider_acceptance(
            claim=claim,
            secret=secret,
            audit_event_id=int(loaded.audit_event.id),
            provider_ref=provider_ref,
        )
    except Exception as exc:
        try:
            _record_callback_terminal_outcome(
                claim=claim,
                loaded=loaded,
                secret=secret,
                action=action,
                sender=sender,
                sender_binding=sender_binding,
                template_registry_id=template_registry_id,
                content_sid=content_sid,
                content_variables=content_variables,
                payload_digest=payload_digest,
                error_code="crm_action_provider_acceptance_persistence_unknown",
                provider_call_started=True,
                provider_ref=provider_ref,
            )
        except Exception as terminal_exc:
            current_app.logger.error(
                "CRM action terminal persistence failed effect=%s "
                "phase=provider_acceptance error_type=%s",
                claim.effect_id,
                type(terminal_exc).__name__,
            )
            raise AmbiguousDomainEffectError(
                "crm_action_terminal_persistence_unknown"
            ) from terminal_exc
        raise AmbiguousDomainEffectError(
            "provider_acceptance_persistence_unknown"
        ) from exc
    return provider_ref


def _prepare_location(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _loaded_action(claim, action="share_location")
    if not loaded.recipient:
        return SkippedDomainEffect(reason_code="requester_phone_invalid", result={})
    normalized = _normalized_location(loaded.action_payload)
    if normalized is None or normalized != loaded.action_payload:
        raise PermanentDomainEffectError("crm_action_location_payload_invalid")
    policy = resolve_domain_effect_outbox_policy(current_app.config, tenant_id=int(claim.tenant_id))
    secret = str(policy.secret or "")
    notification_id, notification_attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=int(claim.tenant_id),
        audit_event_id=int(loaded.audit_event.id),
    )
    _verify_destination_binding(
        claim=claim,
        secret=secret,
        audit_event_id=int(loaded.audit_event.id),
        ticket_id=int(loaded.ticket.id),
        recipient=loaded.recipient,
        notification_attempt_id=notification_attempt_id,
    )
    action_document = _action_document(
        tenant_id=int(claim.tenant_id),
        audit_event_id=int(loaded.audit_event.id),
        audit_actor_user_id=int(loaded.audit_event.actor_user_id),
        ticket_id=int(loaded.ticket.id),
        comment_id=int(loaded.comment.id),
        action="share_location",
        action_payload=normalized,
        recipient=loaded.recipient,
        template_registry_id=None,
        content_variables={},
        notification_id=notification_id,
        notification_attempt_id=notification_attempt_id,
    )
    expected_action_binding = _opaque_binding(secret, action_document)
    if not hmac.compare_digest(expected_action_binding, loaded.action_binding):
        raise PermanentDomainEffectError("crm_action_binding_invalid")
    snapshot = resolve_tenant_twilio_sender_snapshot(
        tenant_id=int(claim.tenant_id),
        channel="whatsapp",
    )
    if snapshot.sender is None or snapshot.reason_code:
        return SkippedDomainEffect(
            reason_code=snapshot.reason_code or "whatsapp_tenant_sender_resolution_invalid",
            result={},
        )
    if not _location_capability_enabled(snapshot.sender):
        return SkippedDomainEffect(reason_code="whatsapp_location_capability_missing", result={})
    if not _session_is_active(
        tenant_id=int(claim.tenant_id),
        recipient=loaded.recipient,
        session=db.session,
    ):
        return SkippedDomainEffect(reason_code="whatsapp_session_window_closed", result={})
    payload_digest = hashlib.sha256(_canonical(action_document)).hexdigest()
    _strict_callback_correlation(
        session=db.session,
        loaded=loaded,
        secret=secret,
        action="share_location",
        sender=snapshot.sender,
        sender_binding=str(claim.payload.get("provider_sender_binding") or ""),
        template_registry_id=None,
        content_sid=None,
        content_variables={},
        payload_digest=payload_digest,
        require_reserved=True,
    )
    persistent_action = (
        f"geo:{normalized['lat']!r},{normalized['lng']!r}|{normalized['label']}"
    )
    try:
        preflight = prepare_bound_tenant_twilio_message(
            tenant_id=int(claim.tenant_id),
            channel="whatsapp",
            expected_sender_binding=str(claim.payload.get("provider_sender_binding") or ""),
            recipient=loaded.recipient,
            persistent_actions=(persistent_action,),
        )
    except TenantTwilioScopeError as exc:
        raise PermanentDomainEffectError(exc.code) from exc
    if preflight.reason_code:
        return SkippedDomainEffect(reason_code=preflight.reason_code, result={})
    if preflight.prepared is None:
        raise PermanentDomainEffectError("crm_action_twilio_preflight_invalid")

    def deliver() -> DeliveredDomainEffect:
        prepared = _activate_callback_and_prepare(
            claim=claim,
            loaded=loaded,
            secret=secret,
            action="share_location",
            sender=snapshot.sender,
            sender_binding=str(claim.payload.get("provider_sender_binding") or ""),
            template_registry_id=None,
            content_sid=None,
            content_variables={},
            payload_digest=payload_digest,
            persistent_actions=(persistent_action,),
        )
        provider_ref = _deliver_correlated_crm_action(
            claim=claim,
            loaded=loaded,
            secret=secret,
            action="share_location",
            sender=snapshot.sender,
            sender_binding=str(claim.payload.get("provider_sender_binding") or ""),
            template_registry_id=None,
            content_sid=None,
            content_variables={},
            payload_digest=payload_digest,
            prepared=prepared,
        )
        return DeliveredDomainEffect(
            provider_ref=provider_ref,
            result={
                "delivery": "provider_accepted_callback_correlated",
                "payload_kind": "location",
            },
        )

    return PreparedDomainEffect(deliver=deliver)


def _prepare_form(
    claim: DomainEffectClaim,
) -> PreparedDomainEffect | SkippedDomainEffect:
    loaded = _loaded_action(claim, action="share_form")
    if not loaded.recipient:
        return SkippedDomainEffect(reason_code="requester_phone_invalid", result={})
    normalized = _normalized_form(loaded.action_payload)
    if normalized is None:
        raise PermanentDomainEffectError("crm_action_form_payload_invalid")
    resolved_form = resolve_tenant_public_form_action_payload(
        tenant_id=int(claim.tenant_id),
        form_slug=normalized["form_slug"],
    )
    if resolved_form is None:
        return SkippedDomainEffect(reason_code="tenant_form_not_available", result={})
    if resolved_form != loaded.action_payload:
        raise PermanentDomainEffectError("crm_action_form_publication_binding_invalid")
    template, reason = _form_template(
        tenant_id=int(claim.tenant_id),
        form_slug=normalized["form_slug"],
        session=db.session,
    )
    if template is None:
        return SkippedDomainEffect(reason_code=reason or "whatsapp_form_content_missing", result={})
    variables, variable_reason = _form_variables(
        template,
        form=normalized,
        ticket=loaded.ticket,
    )
    if variables is None:
        raise PermanentDomainEffectError(variable_reason or "whatsapp_form_variables_invalid")
    policy = resolve_domain_effect_outbox_policy(current_app.config, tenant_id=int(claim.tenant_id))
    secret = str(policy.secret or "")
    notification_id, notification_attempt_id = _callback_correlation_ids(
        secret,
        tenant_id=int(claim.tenant_id),
        audit_event_id=int(loaded.audit_event.id),
    )
    _verify_destination_binding(
        claim=claim,
        secret=secret,
        audit_event_id=int(loaded.audit_event.id),
        ticket_id=int(loaded.ticket.id),
        recipient=loaded.recipient,
        notification_attempt_id=notification_attempt_id,
    )
    action_document = _action_document(
        tenant_id=int(claim.tenant_id),
        audit_event_id=int(loaded.audit_event.id),
        audit_actor_user_id=int(loaded.audit_event.actor_user_id),
        ticket_id=int(loaded.ticket.id),
        comment_id=int(loaded.comment.id),
        action="share_form",
        action_payload=resolved_form,
        recipient=loaded.recipient,
        template_registry_id=int(template.id),
        content_variables=variables,
        notification_id=notification_id,
        notification_attempt_id=notification_attempt_id,
    )
    expected_action_binding = _opaque_binding(secret, action_document)
    if not hmac.compare_digest(expected_action_binding, loaded.action_binding):
        raise PermanentDomainEffectError("crm_action_binding_invalid")
    snapshot = resolve_tenant_twilio_sender_snapshot(
        tenant_id=int(claim.tenant_id),
        channel="whatsapp",
    )
    if snapshot.sender is None or snapshot.reason_code:
        return SkippedDomainEffect(
            reason_code=snapshot.reason_code or "whatsapp_tenant_sender_resolution_invalid",
            result={},
        )
    payload_digest = hashlib.sha256(_canonical(action_document)).hexdigest()
    _strict_callback_correlation(
        session=db.session,
        loaded=loaded,
        secret=secret,
        action="share_form",
        sender=snapshot.sender,
        sender_binding=str(claim.payload.get("provider_sender_binding") or ""),
        template_registry_id=int(template.id),
        content_sid=str(template.content_sid or "").strip(),
        content_variables=variables,
        payload_digest=payload_digest,
        require_reserved=True,
    )
    try:
        preflight = prepare_bound_tenant_twilio_message(
            tenant_id=int(claim.tenant_id),
            channel="whatsapp",
            expected_sender_binding=str(claim.payload.get("provider_sender_binding") or ""),
            recipient=loaded.recipient,
            template_registry_id=int(template.id),
            content_variables=variables,
        )
    except TenantTwilioScopeError as exc:
        raise PermanentDomainEffectError(exc.code) from exc
    if preflight.reason_code:
        return SkippedDomainEffect(reason_code=preflight.reason_code, result={})
    if preflight.prepared is None:
        raise PermanentDomainEffectError("crm_action_twilio_preflight_invalid")

    def deliver() -> DeliveredDomainEffect:
        prepared = _activate_callback_and_prepare(
            claim=claim,
            loaded=loaded,
            secret=secret,
            action="share_form",
            sender=snapshot.sender,
            sender_binding=str(claim.payload.get("provider_sender_binding") or ""),
            template_registry_id=int(template.id),
            content_sid=str(template.content_sid or "").strip(),
            content_variables=variables,
            payload_digest=payload_digest,
        )
        provider_ref = _deliver_correlated_crm_action(
            claim=claim,
            loaded=loaded,
            secret=secret,
            action="share_form",
            sender=snapshot.sender,
            sender_binding=str(claim.payload.get("provider_sender_binding") or ""),
            template_registry_id=int(template.id),
            content_sid=str(template.content_sid or "").strip(),
            content_variables=variables,
            payload_digest=payload_digest,
            prepared=prepared,
        )
        return DeliveredDomainEffect(
            provider_ref=provider_ref,
            result={
                "delivery": "provider_accepted_callback_correlated",
                "payload_kind": "form",
            },
        )

    return PreparedDomainEffect(deliver=deliver)


CRM_ACTION_DOMAIN_EFFECT_REGISTRY = DomainEffectRegistry()
CRM_ACTION_DOMAIN_EFFECT_REGISTRY.register(
    LOCATION_HANDLER,
    _prepare_location,
    payload_validator=_validate_binding_payload,
)
CRM_ACTION_DOMAIN_EFFECT_REGISTRY.register(
    FORM_HANDLER,
    _prepare_form,
    payload_validator=_validate_binding_payload,
)


def register_crm_action_handlers(registry: DomainEffectRegistry) -> None:
    """Attach the action handlers to the worker's existing ticket registry."""

    for handler_name in CRM_ACTION_DOMAIN_EFFECT_REGISTRY.handler_names:
        registration = CRM_ACTION_DOMAIN_EFFECT_REGISTRY.resolve(handler_name)
        registry.register(
            handler_name,
            registration.prepare,
            payload_validator=registration.payload_validator,
        )


__all__ = [
    "ACTION_AUDIT_CONTRACT",
    "ACTION_AUDIT_EVENT_TYPE",
    "ACTION_AUDIT_RESOURCE_TYPE",
    "CRM_ACTION_AGGREGATE",
    "CRM_ACTION_DOMAIN_EFFECT_REGISTRY",
    "CrmActionDeliveryDecision",
    "CrmActionDeliveryError",
    "DELIVERY_CONTRACT",
    "FORM_HANDLER",
    "LOCATION_HANDLER",
    "create_ticket_crm_action_audit_event",
    "register_crm_action_handlers",
    "resolve_tenant_public_form_action_payload",
    "stage_legacy_ticket_crm_action_delivery",
]
