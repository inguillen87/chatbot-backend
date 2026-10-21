from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, Mapping, Optional

from flask import current_app
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    MessageTemplateRegistry,
    MessagingEventLedger,
    Notification,
    NotificationAttempt,
    NotificationTemplate,
    db,
)
from services.message_templates import whatsapp_template_lifecycle
from services.professional_message_preview import render_notification_template_strict
from utils.time_utils import get_local_now
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService

ALLOWED_CHANNELS = {"email", "whatsapp", "push", "in_app"}
BASE_BACKOFF_SECONDS = 60
PERMANENT_DISPATCH_FAILURES = frozenset(
    {
        "whatsapp_template_registry_required",
        "whatsapp_template_registry_mismatch",
    }
)
BLOCKED_DISPATCH_FAILURES = frozenset(
    {
        "whatsapp_template_not_approved",
        "whatsapp_transport_unavailable",
        "whatsapp_transport_tenant_not_enabled",
        "whatsapp_tenant_credentials_missing",
        "whatsapp_tenant_sender_missing",
        "whatsapp_tenant_sender_not_ready",
        "whatsapp_tenant_connection_not_ready",
        "whatsapp_status_callback_missing",
        "whatsapp_status_callback_invalid",
        "twilio_provider_network_disabled",
        "email_transport_unavailable",
        "push_transport_unavailable",
    }
)

_CONTENT_VARIABLE_KEY = re.compile(r"^[1-9][0-9]{0,2}$")
_RESERVED_NOTIFICATION_METADATA = frozenset(
    {
        "content_sid",
        "content_variables",
        "is_template",
        "provider_connection_id",
        "provider_sender_id",
        "sender_binding",
        "status_callback",
        "status_callback_url",
        "template_registry_id",
        # This fact is derived from WhatsAppContactState at dispatch. A caller
        # must not be able to extend Meta's customer-service window.
        "within_24h_window",
    }
)


class NotificationIdempotencyConflict(ValueError):
    """The same tenant/idempotency key was reused for another payload."""

    code = "notification_idempotency_payload_conflict"

    def __init__(self) -> None:
        super().__init__(self.code)


class NotificationLeaseLost(RuntimeError):
    """The dispatch worker no longer owns the durable send lease."""


@dataclass(frozen=True)
class NotificationDispatchClaim:
    notification_id: str
    attempt_id: str
    lease_token: str


def _canonical_json_digest(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("notification_payload_must_be_json") from exc
    if len(encoded) > 32_768:
        raise ValueError("notification_payload_too_large")
    return hashlib.sha256(encoded).hexdigest()


def _safe_error_code(value: Any, *, fallback: str) -> str:
    rendered = re.sub(r"[^a-z0-9_.:-]+", "_", str(value or "").strip().lower())
    return (rendered[:120] or fallback)


def _error_digest(value: Any) -> str:
    return hashlib.sha256(str(value or "unknown").encode("utf-8")).hexdigest()


def _normalize_content_variables(values: Any) -> dict[str, str]:
    if values in (None, {}):
        return {}
    if not isinstance(values, Mapping):
        raise ValueError("content_variables_must_be_object")
    if len(values) > 100:
        raise ValueError("content_variables_too_many")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key or "").strip()
        if not _CONTENT_VARIABLE_KEY.fullmatch(key):
            raise ValueError("content_variable_key_invalid")
        if isinstance(raw_value, bool):
            rendered = "true" if raw_value else "false"
        elif isinstance(raw_value, (str, int, float)) and not isinstance(
            raw_value, complex
        ):
            rendered = str(raw_value)
        else:
            raise ValueError("content_variable_value_invalid")
        if not rendered or len(rendered.encode("utf-8")) > 1024:
            raise ValueError("content_variable_value_invalid")
        normalized[key] = rendered
    _canonical_json_digest({"content_variables": normalized})
    return dict(sorted(normalized.items(), key=lambda item: int(item[0])))


def _sanitize_notification_metadata(metadata: Any) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata_must_be_object")
    safe = {
        str(key): value
        for key, value in metadata.items()
        if str(key).strip().lower() not in _RESERVED_NOTIFICATION_METADATA
    }
    _canonical_json_digest({"metadata": safe})
    return safe


_PROVIDER_DELIVERY_PROGRESS = {
    "unknown": 0,
    "accepted": 10,
    "scheduled": 15,
    "queued": 20,
    "sending": 30,
    "sent": 40,
    "delivered": 50,
    "read": 60,
}
_PROVIDER_DELIVERY_FAILURES = {"failed", "undelivered", "canceled", "cancelled"}


def _provider_transition_allowed(current: str, incoming: str) -> bool:
    if incoming not in _PROVIDER_DELIVERY_PROGRESS and incoming not in _PROVIDER_DELIVERY_FAILURES:
        return False
    if not current or current == "unknown":
        return True
    if current in _PROVIDER_DELIVERY_FAILURES:
        return incoming == current
    if incoming in _PROVIDER_DELIVERY_FAILURES:
        return _PROVIDER_DELIVERY_PROGRESS.get(current, 0) < _PROVIDER_DELIVERY_PROGRESS["delivered"]
    return _PROVIDER_DELIVERY_PROGRESS.get(incoming, 0) >= _PROVIDER_DELIVERY_PROGRESS.get(current, 0)


def reconcile_whatsapp_notification_status(
    *,
    tenant_id: int,
    notification_attempt_id: str,
    provider_message_sid: Any,
    provider_status: Any,
    provider_sender_id: Any,
    delivery_event_id: Any = None,
    error: Any = None,
    now=None,
) -> bool:
    """Apply one signed Twilio callback without permitting tenant/SID drift."""

    try:
        resolved_tenant_id = int(tenant_id)
        resolved_sender_id = int(provider_sender_id)
        canonical_attempt_id = str(uuid.UUID(str(notification_attempt_id)))
    except (TypeError, ValueError, OverflowError, AttributeError):
        return False
    if resolved_tenant_id <= 0 or resolved_sender_id <= 0:
        return False
    if canonical_attempt_id != str(notification_attempt_id or "").strip().lower():
        return False
    message_sid = str(provider_message_sid or "").strip()[:180]
    incoming_status = str(provider_status or "unknown").strip().lower()[:32]
    if not message_sid or not _provider_transition_allowed("", incoming_status):
        return False
    operation_now = now or get_local_now()

    with Session(bind=db.engine, expire_on_commit=False) as session:
        attempt_hint = session.scalar(
            select(NotificationAttempt).where(
                NotificationAttempt.id == canonical_attempt_id,
                NotificationAttempt.tenant_id == resolved_tenant_id,
            )
        )
        if attempt_hint is None:
            session.rollback()
            return False
        # Every writer in this transport locks Notification first and Attempt
        # second. The unlocked hint is used only to discover the immutable FK.
        notification = session.scalar(
            select(Notification)
            .where(
                Notification.id == attempt_hint.notification_id,
                Notification.tenant_id == resolved_tenant_id,
                Notification.channel == "whatsapp",
                Notification.provider_sender_id == resolved_sender_id,
            )
            .with_for_update()
        )
        if notification is None:
            session.rollback()
            return False
        attempt = session.scalar(
            select(NotificationAttempt)
            .where(
                NotificationAttempt.id == canonical_attempt_id,
                NotificationAttempt.notification_id == notification.id,
                NotificationAttempt.tenant_id == resolved_tenant_id,
            )
            .with_for_update()
        )
        if attempt is None or attempt.status not in {
            NotificationAttempt.STATUS_SENDING,
            NotificationAttempt.STATUS_SEND_UNCERTAIN,
            NotificationAttempt.STATUS_SUCCESS,
            NotificationAttempt.STATUS_FAILED,
        }:
            session.rollback()
            return False
        if int(attempt.attempt_number) != int(notification.attempt_count):
            session.rollback()
            return False
        for persisted_sid in (
            notification.provider_message_id,
            attempt.provider_message_id,
        ):
            if persisted_sid and not hmac.compare_digest(str(persisted_sid), message_sid):
                session.rollback()
                return False
        collision = session.scalar(
            select(Notification.id).where(
                Notification.tenant_id == resolved_tenant_id,
                Notification.provider_sender_id == resolved_sender_id,
                Notification.provider_message_id == message_sid,
                Notification.id != notification.id,
            )
        )
        if collision is not None:
            session.rollback()
            return False
        current_status = str(
            notification.provider_status or Notification.PROVIDER_STATUS_UNKNOWN
        ).lower()
        if not _provider_transition_allowed(current_status, incoming_status):
            session.rollback()
            return True

        resolved_event_id = None
        if delivery_event_id not in (None, ""):
            try:
                candidate_event_id = int(delivery_event_id)
            except (TypeError, ValueError, OverflowError):
                session.rollback()
                return False
            event = session.scalar(
                select(MessagingEventLedger).where(
                    MessagingEventLedger.id == candidate_event_id,
                    MessagingEventLedger.tenant_id == resolved_tenant_id,
                    MessagingEventLedger.provider == "twilio",
                    MessagingEventLedger.channel == "whatsapp",
                    MessagingEventLedger.provider_sender_id == resolved_sender_id,
                )
            )
            if event is None:
                session.rollback()
                return False
            resolved_event_id = int(event.id)

        notification.provider_message_id = message_sid
        notification.provider_status = incoming_status
        notification.lease_token = None
        notification.leased_until = None
        notification.next_retry_at = None
        attempt.provider_message_id = message_sid
        attempt.provider_status = incoming_status
        attempt.delivery_event_id = resolved_event_id
        attempt.next_retry_at = None
        if incoming_status in _PROVIDER_DELIVERY_FAILURES:
            safe_code = _safe_error_code(
                error or incoming_status,
                fallback=incoming_status,
            )
            notification.status = Notification.STATUS_FAILED
            notification.last_error = safe_code
            attempt.status = NotificationAttempt.STATUS_FAILED
            attempt.error_message = safe_code
            attempt.error_digest = _error_digest(error or incoming_status)
        else:
            notification.status = Notification.STATUS_SENT
            notification.sent_at = notification.sent_at or operation_now
            notification.last_error = None
            attempt.status = NotificationAttempt.STATUS_SUCCESS
            attempt.error_message = None
            attempt.error_digest = None
        notification.updated_at = operation_now
        session.add_all([notification, attempt])
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return False
    return True


class NotificationOrchestrator:
    def __init__(self, tenant_id: int):
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.tenant_id = int(tenant_id)

    def upsert_template(
        self,
        *,
        key: str,
        channel: str,
        body_template: str,
        subject_template: Optional[str] = None,
        quiet_hours_start: Optional[int] = None,
        quiet_hours_end: Optional[int] = None,
        message_template_registry_id: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> NotificationTemplate:
        channel = (channel or "").strip().lower()
        if channel not in ALLOWED_CHANNELS:
            raise ValueError("unsupported channel")
        key = (key or "").strip().lower()
        if not key:
            raise ValueError("template key is required")
        if not (body_template or "").strip():
            raise ValueError("body_template is required")
        registry_id = self._positive_id(
            message_template_registry_id,
            field="message_template_registry_id",
            allow_none=True,
        )
        if registry_id is not None:
            if channel != "whatsapp":
                raise ValueError("message_template_registry_requires_whatsapp")
            registry = MessageTemplateRegistry.query.filter_by(
                id=registry_id,
                tenant_id=self.tenant_id,
                provider="twilio",
                channel="whatsapp",
            ).one_or_none()
            if registry is None:
                raise ValueError("whatsapp_template_registry_mismatch")

        template = NotificationTemplate.query.filter_by(
            tenant_id=self.tenant_id,
            key=key,
            channel=channel,
        ).first()
        if template is None:
            template = NotificationTemplate(
                tenant_id=self.tenant_id,
                key=key,
                channel=channel,
            )
            db.session.add(template)

        template.body_template = body_template
        template.subject_template = subject_template
        template.quiet_hours_start = quiet_hours_start
        template.quiet_hours_end = quiet_hours_end
        template.message_template_registry_id = registry_id
        template.metadata_json = metadata or None
        template.is_active = True
        db.session.flush()
        return template

    def queue_notification(
        self,
        *,
        channel: str,
        recipient: str,
        idempotency_key: str,
        user_id: Optional[int] = None,
        template_key: Optional[str] = None,
        template_context: Optional[dict] = None,
        subject: Optional[str] = None,
        body: Optional[str] = None,
        max_retries: int = 3,
        template_registry_id: Optional[int] = None,
        content_variables: Optional[Mapping[str, Any]] = None,
        metadata: Optional[dict] = None,
    ) -> tuple[Notification, bool]:
        channel = (channel or "").strip().lower()
        if channel not in ALLOWED_CHANNELS:
            raise ValueError("unsupported channel")
        normalized_recipient = str(recipient or "").strip()
        if not normalized_recipient:
            raise ValueError("recipient is required")

        idem = (idempotency_key or "").strip()
        if not idem:
            raise ValueError("idempotency_key is required")

        selected_template = None
        rendered_subject = subject
        rendered_body = body
        normalized_max_retries = int(max_retries)
        if normalized_max_retries < 0 or normalized_max_retries > 10:
            raise ValueError("max_retries_out_of_range")
        safe_metadata = _sanitize_notification_metadata(metadata)
        normalized_variables = _normalize_content_variables(content_variables)
        registry_id = self._positive_id(
            template_registry_id,
            field="template_registry_id",
            allow_none=True,
        )

        if template_key:
            selected_template = NotificationTemplate.query.filter_by(
                tenant_id=self.tenant_id,
                key=str(template_key).strip().lower(),
                channel=channel,
                is_active=True,
            ).first()
            if not selected_template:
                raise ValueError("template not found")
            rendered = render_notification_template_strict(
                body_template=selected_template.body_template,
                subject_template=selected_template.subject_template,
                context=template_context or {},
                channel=channel,
            )
            rendered_body = rendered["body"]
            if selected_template.subject_template:
                rendered_subject = rendered["subject"]

            linked_registry_id = self._positive_id(
                getattr(selected_template, "message_template_registry_id", None),
                field="message_template_registry_id",
                allow_none=True,
            )
            if registry_id is not None and linked_registry_id not in (None, registry_id):
                raise ValueError("whatsapp_template_registry_mismatch")
            registry_id = registry_id or linked_registry_id

        if normalized_variables and registry_id is None:
            raise ValueError("content_variables_require_template_registry")
        if registry_id is not None and channel != "whatsapp":
            raise ValueError("template_registry_requires_whatsapp")

        registry = None
        sender_snapshot = None
        if registry_id is not None:
            registry = self._approved_whatsapp_registry(registry_id)
            # For direct provider-template queues, the provider-synced preview
            # is authoritative. A client-supplied body must not masquerade as
            # the approved content represented by ContentSid.
            if selected_template is None:
                rendered_body = str(registry.body_preview or "").strip()
            safe_metadata.update(
                {
                    "is_template": True,
                    "template_registry_id": int(registry.id),
                }
            )
        else:
            safe_metadata["is_template"] = False

        if channel == "whatsapp":
            from services.tenant_twilio_messaging import (
                resolve_tenant_twilio_sender_snapshot,
            )

            sender_snapshot = resolve_tenant_twilio_sender_snapshot(
                tenant_id=self.tenant_id,
                channel="whatsapp",
            )
            # Every queued WhatsApp notification is pinned to the exact tenant
            # sender whenever one valid sender exists. Approved templates fail
            # closed immediately; free-form traffic may remain queued so the
            # worker can expose the precise configuration blocker.
            if (
                registry_id is not None
                and (sender_snapshot.reason_code or sender_snapshot.sender is None)
            ):
                raise ValueError(
                    sender_snapshot.reason_code or "whatsapp_tenant_sender_missing"
                )

        if not (rendered_body or "").strip():
            raise ValueError("body is required")

        payload_digest = _canonical_json_digest(
            {
                "body": str(rendered_body),
                "channel": channel,
                "content_variables": normalized_variables,
                "max_retries": normalized_max_retries,
                "metadata": safe_metadata,
                "recipient": normalized_recipient,
                "subject": str(rendered_subject) if rendered_subject is not None else None,
                "template_id": selected_template.id if selected_template else None,
                "template_registry_id": int(registry.id) if registry is not None else None,
                "tenant_id": self.tenant_id,
                "user_id": int(user_id) if user_id is not None else None,
            }
        )

        existing = Notification.query.filter_by(
            tenant_id=self.tenant_id,
            idempotency_key=idem,
        ).first()
        if existing:
            self._assert_idempotent_replay(existing, payload_digest)
            return existing, False

        notification = Notification(
            tenant_id=self.tenant_id,
            user_id=user_id,
            template_id=selected_template.id if selected_template else None,
            channel=channel,
            recipient=normalized_recipient,
            subject=rendered_subject,
            body=rendered_body,
            status="queued",
            idempotency_key=idem,
            max_retries=normalized_max_retries,
            message_template_registry_id=(
                int(registry.id) if registry is not None else None
            ),
            provider_connection_id=(
                int(sender_snapshot.sender.provider_connection_id)
                if sender_snapshot is not None
                and sender_snapshot.sender is not None
                and sender_snapshot.sender.provider_connection_id is not None
                else None
            ),
            provider_sender_id=(
                int(sender_snapshot.sender.id)
                if sender_snapshot is not None and sender_snapshot.sender is not None
                else None
            ),
            sender_binding=(sender_snapshot.binding if sender_snapshot is not None else None),
            content_sid=(str(registry.content_sid) if registry is not None else None),
            content_variables=normalized_variables or None,
            payload_digest=payload_digest,
            metadata_json=safe_metadata,
        )
        try:
            with db.session.begin_nested():
                db.session.add(notification)
                db.session.flush()
        except IntegrityError:
            existing = Notification.query.filter_by(
                tenant_id=self.tenant_id,
                idempotency_key=idem,
            ).one_or_none()
            if existing is None:
                raise
            self._assert_idempotent_replay(existing, payload_digest)
            return existing, False
        return notification, True

    @staticmethod
    def _positive_id(value: Any, *, field: str, allow_none: bool = False) -> int | None:
        if value in (None, "") and allow_none:
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field}_invalid") from exc
        if normalized <= 0 or isinstance(value, bool):
            raise ValueError(f"{field}_invalid")
        return normalized

    def _approved_whatsapp_registry(self, registry_id: int) -> MessageTemplateRegistry:
        row = MessageTemplateRegistry.query.filter_by(
            id=int(registry_id),
            tenant_id=self.tenant_id,
            provider="twilio",
            channel="whatsapp",
        ).one_or_none()
        if row is None:
            raise ValueError("whatsapp_template_registry_mismatch")
        lifecycle = whatsapp_template_lifecycle(
            row.status,
            source="message_template_registry",
            provider_reference=row.content_sid or row.external_template_id,
            observed_at=row.last_sync_at,
        )
        if not lifecycle.get("production_send_allowed"):
            raise ValueError("whatsapp_template_not_approved")
        if not str(row.content_sid or "").strip():
            raise ValueError("whatsapp_template_content_sid_missing")
        return row

    @staticmethod
    def _assert_idempotent_replay(
        existing: Notification,
        expected_payload_digest: str,
    ) -> None:
        persisted = str(getattr(existing, "payload_digest", None) or "").strip().lower()
        if not persisted or not hmac.compare_digest(persisted, expected_payload_digest):
            raise NotificationIdempotencyConflict()

    def dispatch_due_notifications(self, *, now=None, limit: int = 50) -> dict:
        now = now or get_local_now()
        bounded_limit = max(1, min(int(limit), 100))
        self._recover_expired_sends(now=now, limit=bounded_limit)
        result = {
            "processed": 0,
            "sent": 0,
            "delayed": 0,
            "blocked": 0,
            "failed": 0,
            "send_uncertain": 0,
        }
        for _ in range(bounded_limit):
            claim = self._claim_next(now=now)
            if claim is None:
                break
            result["processed"] += 1
            outcome = self._dispatch_claimed(claim, now=now)
            result[outcome] += 1
        return result

    @staticmethod
    def _due_filter(now):
        return or_(
            and_(Notification.status == Notification.STATUS_QUEUED),
            and_(
                Notification.status.in_(
                    [
                        Notification.STATUS_DELAYED,
                        Notification.STATUS_RETRY_WAIT,
                        # Compatibility with rows staged before retry_wait was
                        # introduced. Terminal failed rows keep next_retry_at NULL.
                        Notification.STATUS_FAILED,
                    ]
                ),
                Notification.next_retry_at.isnot(None),
                Notification.next_retry_at <= now,
            ),
        )

    def _claim_next(self, *, now) -> NotificationDispatchClaim | None:
        lease_seconds = max(
            30,
            min(
                int(
                    current_app.config.get(
                        "NOTIFICATION_DISPATCH_LEASE_SECONDS",
                        180,
                    )
                ),
                3600,
            ),
        )
        for _ in range(8):
            token = uuid.uuid4().hex
            attempt_id = str(uuid.uuid4())
            with Session(bind=db.engine, expire_on_commit=False) as session:
                candidate_id = session.scalar(
                    select(Notification.id)
                    .where(
                        Notification.tenant_id == self.tenant_id,
                        self._due_filter(now),
                    )
                    .order_by(Notification.created_at.asc(), Notification.id.asc())
                    .limit(1)
                )
                if candidate_id is None:
                    session.rollback()
                    return None
                claimed = session.execute(
                    update(Notification)
                    .where(
                        Notification.id == candidate_id,
                        Notification.tenant_id == self.tenant_id,
                        self._due_filter(now),
                    )
                    .values(
                        status=Notification.STATUS_SENDING,
                        attempt_count=Notification.attempt_count + 1,
                        lease_token=token,
                        leased_until=now + timedelta(seconds=lease_seconds),
                        next_retry_at=None,
                        updated_at=now,
                    )
                )
                if claimed.rowcount != 1:
                    session.rollback()
                    continue
                notification = session.get(Notification, candidate_id)
                if notification is None:
                    session.rollback()
                    continue
                session.add(
                    NotificationAttempt(
                        id=attempt_id,
                        notification_id=notification.id,
                        tenant_id=self.tenant_id,
                        attempt_number=int(notification.attempt_count),
                        status=NotificationAttempt.STATUS_SENDING,
                        provider=notification.channel,
                        provider_status=Notification.PROVIDER_STATUS_UNKNOWN,
                        attempted_at=now,
                        metadata_json={
                            "provider_call_started": False,
                            "lease_token_digest": _error_digest(token),
                        },
                    )
                )
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    continue
                return NotificationDispatchClaim(
                    notification_id=str(notification.id),
                    attempt_id=attempt_id,
                    lease_token=token,
                )
        return None

    def _recover_expired_sends(self, *, now, limit: int) -> dict[str, int]:
        recovered = {"retry_wait": 0, "send_uncertain": 0, "failed": 0}
        with Session(bind=db.engine, expire_on_commit=False) as session:
            rows = session.scalars(
                select(Notification)
                .where(
                    Notification.tenant_id == self.tenant_id,
                    Notification.status == Notification.STATUS_SENDING,
                    Notification.leased_until.isnot(None),
                    Notification.leased_until <= now,
                )
                .order_by(Notification.leased_until.asc(), Notification.id.asc())
                .limit(max(1, int(limit)))
                # Serialize lease recovery with callback reconciliation and
                # provider finalization. All three paths lock Notification
                # before NotificationAttempt; skip rows another worker owns.
                .with_for_update(skip_locked=True)
            ).all()
            for notification in rows:
                attempt = session.scalar(
                    select(NotificationAttempt)
                    .where(
                        NotificationAttempt.notification_id == notification.id,
                        NotificationAttempt.tenant_id == self.tenant_id,
                        NotificationAttempt.attempt_number
                        == notification.attempt_count,
                    )
                    .order_by(NotificationAttempt.created_at.desc())
                    .limit(1)
                    .with_for_update()
                )
                attempt_metadata = (
                    dict(attempt.metadata_json or {}) if attempt is not None else {}
                )
                provider_call_started = bool(
                    attempt_metadata.get("provider_call_started")
                )
                if provider_call_started:
                    status = Notification.STATUS_SEND_UNCERTAIN
                    error_code = "notification_send_lease_expired_after_io"
                    next_retry_at = None
                    attempt_status = NotificationAttempt.STATUS_SEND_UNCERTAIN
                else:
                    can_retry = notification.attempt_count < notification.max_retries
                    status = (
                        Notification.STATUS_RETRY_WAIT
                        if can_retry
                        else Notification.STATUS_FAILED
                    )
                    error_code = "notification_send_lease_expired_before_io"
                    next_retry_at = now if can_retry else None
                    attempt_status = NotificationAttempt.STATUS_FAILED
                notification.status = status
                notification.last_error = error_code
                notification.next_retry_at = next_retry_at
                notification.lease_token = None
                notification.leased_until = None
                notification.updated_at = now
                if attempt is not None:
                    attempt.status = attempt_status
                    attempt.error_message = error_code
                    attempt.error_digest = _error_digest(error_code)
                    attempt.next_retry_at = next_retry_at
                    attempt_metadata["lease_expired_at"] = now.isoformat()
                    attempt.metadata_json = attempt_metadata
                    session.add(attempt)
                session.add(notification)
                recovered[status] += 1
            session.commit()
        return recovered

    def requeue_blocked_notifications(
        self,
        *,
        channels: Optional[set[str]] = None,
        reason_codes: Optional[set[str]] = None,
        now=None,
        limit: int = 100,
    ) -> int:
        """Explicitly requeue tenant-owned notifications after capability changes."""

        normalized_channels = {
            str(channel or "").strip().lower() for channel in (channels or set())
        }
        if normalized_channels - ALLOWED_CHANNELS:
            raise ValueError("unsupported channel")
        normalized_reasons = {
            str(reason or "").strip() for reason in (reason_codes or set())
        }
        if normalized_reasons - BLOCKED_DISPATCH_FAILURES:
            raise ValueError("unsupported blocked reason")

        query = Notification.query.filter_by(
            tenant_id=self.tenant_id,
            status="blocked",
        ).filter(Notification.provider_message_id.is_(None))
        if normalized_channels:
            query = query.filter(Notification.channel.in_(normalized_channels))
        if normalized_reasons:
            query = query.filter(Notification.last_error.in_(normalized_reasons))
        rows = query.order_by(Notification.created_at.asc()).limit(max(1, int(limit))).all()
        available_at = now or get_local_now()
        for notification in rows:
            notification.status = "queued"
            notification.next_retry_at = available_at
            notification.last_error = None
            db.session.add(notification)
        db.session.flush()
        return len(rows)

    def _dispatch_claimed(
        self,
        claim: NotificationDispatchClaim,
        *,
        now,
    ) -> str:
        notification = self._load_claimed_notification(claim)
        if notification is None:
            return "failed"

        quiet_start, quiet_end = self._quiet_hours_for_notification(notification)
        if self._is_quiet_hour(now, quiet_start, quiet_end):
            next_time = self._next_outside_quiet_hours(
                now,
                quiet_start,
                quiet_end,
            )
            self._transition_pre_io(
                claim,
                error_code="quiet_hours",
                raw_error="quiet_hours",
                now=now,
                forced_status=Notification.STATUS_DELAYED,
                next_retry_at=next_time,
            )
            return "delayed"

        metadata = (
            dict(notification.metadata_json or {})
            if isinstance(notification.metadata_json, dict)
            else {}
        )
        if metadata.get("force_fail"):
            return self._transition_pre_io(
                claim,
                error_code="forced_failure",
                raw_error="forced_failure",
                now=now,
            )

        if notification.channel != "whatsapp":
            accepted, provider_id, error_code = self._send_stub(notification)
            if accepted:
                self._mark_provider_accepted(
                    claim,
                    provider_message_id=provider_id,
                    provider_status="accepted",
                    now=now,
                    provider_call_started=False,
                )
                return "sent"
            return self._transition_pre_io(
                claim,
                error_code=error_code or "notification_transport_failed",
                raw_error=error_code or "notification_transport_failed",
                now=now,
            )

        policy_metadata = {
            **metadata,
            "is_template": notification.message_template_registry_id is not None,
            "recipient": notification.recipient,
            # The customer-service window is bound to the exact sender that
            # received the inbound message, never to the tenant globally.
            "provider_sender_id": notification.provider_sender_id,
        }
        policy_body = "\n".join(
            [
                str(notification.body or ""),
                *[
                    str(value)
                    for _key, value in sorted(
                        (notification.content_variables or {}).items()
                    )
                ],
            ]
        )
        try:
            policy_allowed, policy_error = WhatsAppEnterpriseRulesService(
                self.tenant_id
            ).evaluate_outbound(
                body=policy_body,
                metadata=policy_metadata,
            )
        except Exception as exc:
            return self._transition_pre_io(
                claim,
                error_code="whatsapp_policy_evaluation_failed",
                raw_error=type(exc).__name__,
                now=now,
            )
        if not policy_allowed:
            return self._transition_pre_io(
                claim,
                error_code=policy_error or "whatsapp_policy_blocked",
                raw_error=policy_error or "whatsapp_policy_blocked",
                now=now,
            )

        if current_app.config.get("WHATSAPP_NOTIFICATION_TRANSPORT_ENABLED") is not True:
            return self._transition_pre_io(
                claim,
                error_code="whatsapp_transport_unavailable",
                raw_error="whatsapp_transport_unavailable",
                now=now,
            )
        if not self._whatsapp_transport_tenant_enabled():
            return self._transition_pre_io(
                claim,
                error_code="whatsapp_transport_tenant_not_enabled",
                raw_error="whatsapp_transport_tenant_not_enabled",
                now=now,
            )
        if notification.message_template_registry_id is None:
            # This first durable cutover only permits server-approved Content
            # templates. Free-form conversations stay on the separately
            # fenced inbound-turn transport.
            return self._transition_pre_io(
                claim,
                error_code="whatsapp_template_registry_required",
                raw_error="whatsapp_template_registry_required",
                now=now,
            )

        registry_error = self._durable_whatsapp_registry_error(notification)
        if registry_error:
            return self._transition_pre_io(
                claim,
                error_code=registry_error,
                raw_error=registry_error,
                now=now,
            )

        try:
            from services.tenant_twilio_messaging import (
                prepare_bound_tenant_twilio_message,
                send_prepared_tenant_twilio_message,
            )

            preflight = prepare_bound_tenant_twilio_message(
                tenant_id=self.tenant_id,
                channel="whatsapp",
                expected_sender_binding=str(notification.sender_binding or ""),
                recipient=notification.recipient,
                body=None,
                template_registry_id=notification.message_template_registry_id,
                content_variables=notification.content_variables or {},
                notification_attempt_id=claim.attempt_id,
            )
        except Exception as exc:
            return self._transition_pre_io(
                claim,
                error_code=self._provider_exception_code(exc),
                raw_error=type(exc).__name__,
                now=now,
            )
        if preflight.reason_code or preflight.prepared is None:
            reason = preflight.reason_code or "whatsapp_preflight_failed"
            return self._transition_pre_io(
                claim,
                error_code=reason,
                raw_error=reason,
                now=now,
            )

        # Reserve rate-limit capacity only after every local/provider-binding
        # preflight passes. A stale sender, invalid callback or malformed
        # ContentSid never reached Twilio and must not consume delivery quota.
        try:
            allowed, policy_error = WhatsAppEnterpriseRulesService(
                self.tenant_id
            ).reserve_outbound(
                body=policy_body,
                reservation_key=f"notification:{claim.attempt_id}",
                metadata=policy_metadata,
                provider="twilio",
                provider_connection_id=notification.provider_connection_id,
                provider_sender_id=notification.provider_sender_id,
                source="notification_orchestrator",
            )
        except Exception as exc:
            return self._transition_pre_io(
                claim,
                error_code="whatsapp_policy_reservation_failed",
                raw_error=type(exc).__name__,
                now=now,
            )
        if not allowed:
            return self._transition_pre_io(
                claim,
                error_code=policy_error or "whatsapp_policy_blocked",
                raw_error=policy_error or "whatsapp_policy_blocked",
                now=now,
            )

        call_state = {"started": False}

        def _before_provider_call() -> None:
            self._mark_provider_call_started(claim, now=get_local_now())
            call_state["started"] = True

        try:
            provider_message_id = send_prepared_tenant_twilio_message(
                preflight.prepared,
                on_provider_call_start=_before_provider_call,
            )
        except Exception as exc:
            if call_state["started"]:
                return self._mark_send_uncertain(
                    claim,
                    error_code=self._provider_exception_code(exc),
                    raw_error=type(exc).__name__,
                    now=get_local_now(),
                )
            return self._transition_pre_io(
                claim,
                error_code=self._provider_exception_code(exc),
                raw_error=type(exc).__name__,
                now=get_local_now(),
            )

        if not str(provider_message_id or "").strip():
            return self._mark_send_uncertain(
                claim,
                error_code="twilio_acknowledgement_missing",
                raw_error="twilio_acknowledgement_missing",
                now=get_local_now(),
            )
        return self._mark_provider_accepted(
            claim,
            provider_message_id=str(provider_message_id),
            provider_status="accepted",
            now=get_local_now(),
            provider_call_started=True,
        )

    def _load_claimed_notification(
        self,
        claim: NotificationDispatchClaim,
    ) -> Notification | None:
        with Session(bind=db.engine, expire_on_commit=False) as session:
            notification = session.scalar(
                select(Notification).where(
                    Notification.id == claim.notification_id,
                    Notification.tenant_id == self.tenant_id,
                    Notification.status == Notification.STATUS_SENDING,
                    Notification.lease_token == claim.lease_token,
                )
            )
            if notification is not None:
                session.expunge(notification)
            return notification

    @staticmethod
    def _provider_exception_code(exc: Exception) -> str:
        scoped_code = str(
            getattr(exc, "code", None)
            or getattr(exc, "reason_code", None)
            or ""
        ).strip()
        if scoped_code:
            return _safe_error_code(scoped_code, fallback="twilio_provider_error")
        name = type(exc).__name__
        if name == "ProviderNetworkDisabledError":
            return "twilio_provider_network_disabled"
        if isinstance(exc, NotificationLeaseLost):
            return "notification_lease_lost_before_io"
        return f"twilio_{_safe_error_code(name, fallback='provider_error')}"

    def _whatsapp_transport_tenant_enabled(self) -> bool:
        raw = str(
            current_app.config.get(
                "WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS",
                "",
            )
            or ""
        )
        allowed: set[int] = set()
        for token in raw.split(","):
            try:
                tenant_id = int(token.strip())
            except (TypeError, ValueError, OverflowError):
                continue
            if tenant_id > 0:
                allowed.add(tenant_id)
        return self.tenant_id in allowed

    @staticmethod
    def _blocked_dispatch_error(error_code: str) -> bool:
        return bool(
            error_code in BLOCKED_DISPATCH_FAILURES
            or error_code == "twilio_provider_network_disabled"
            or error_code.endswith("_credentials_missing")
            or error_code.endswith("_sender_missing")
            or error_code.endswith("_sender_not_ready")
            or error_code.endswith("_connection_not_ready")
        )

    def _transition_pre_io(
        self,
        claim: NotificationDispatchClaim,
        *,
        error_code: str,
        raw_error: Any,
        now,
        forced_status: str | None = None,
        next_retry_at=None,
    ) -> str:
        safe_code = _safe_error_code(error_code, fallback="notification_failed")
        with Session(bind=db.engine, expire_on_commit=False) as session:
            notification, attempt = self._claim_rows(session, claim)
            if notification is None or attempt is None:
                raise NotificationLeaseLost("notification_lease_lost_before_io")

            if forced_status == Notification.STATUS_DELAYED:
                status = Notification.STATUS_DELAYED
                attempt_status = NotificationAttempt.STATUS_DELAYED
                outcome = "delayed"
            elif self._blocked_dispatch_error(safe_code):
                status = Notification.STATUS_BLOCKED
                attempt_status = NotificationAttempt.STATUS_BLOCKED
                next_retry_at = None
                outcome = "blocked"
            else:
                remaining = (
                    safe_code not in PERMANENT_DISPATCH_FAILURES
                    and notification.attempt_count < notification.max_retries
                )
                status = (
                    Notification.STATUS_RETRY_WAIT
                    if remaining
                    else Notification.STATUS_FAILED
                )
                attempt_status = NotificationAttempt.STATUS_FAILED
                if remaining and next_retry_at is None:
                    exponent = max(0, int(notification.attempt_count) - 1)
                    next_retry_at = now + timedelta(
                        seconds=BASE_BACKOFF_SECONDS * (2**exponent)
                    )
                if not remaining:
                    next_retry_at = None
                outcome = "failed"

            notification.status = status
            notification.last_error = safe_code
            notification.next_retry_at = next_retry_at
            notification.lease_token = None
            notification.leased_until = None
            notification.updated_at = now
            attempt.status = attempt_status
            attempt.error_message = safe_code
            attempt.error_digest = _error_digest(raw_error)
            attempt.next_retry_at = next_retry_at
            session.add_all([notification, attempt])
            session.commit()

        self._emit_notification_event(
            f"notification.{status}",
            notification,
            now=now,
            error_message=safe_code,
        )
        return outcome

    def _claim_rows(self, session: Session, claim: NotificationDispatchClaim):
        notification = session.scalar(
            select(Notification)
            .where(
                Notification.id == claim.notification_id,
                Notification.tenant_id == self.tenant_id,
                Notification.status == Notification.STATUS_SENDING,
                Notification.lease_token == claim.lease_token,
            )
            .with_for_update()
        )
        if notification is None:
            return None, None
        attempt = session.scalar(
            select(NotificationAttempt)
            .where(
                NotificationAttempt.id == claim.attempt_id,
                NotificationAttempt.notification_id == notification.id,
                NotificationAttempt.tenant_id == self.tenant_id,
                NotificationAttempt.status == NotificationAttempt.STATUS_SENDING,
            )
            .with_for_update()
        )
        return notification, attempt

    def _terminal_callback_outcome(
        self,
        session: Session,
        claim: NotificationDispatchClaim,
        *,
        provider_message_id: str | None = None,
    ) -> tuple[str, Notification] | None:
        """Recognize a signed callback that won the post-I/O race."""

        notification = session.scalar(
            select(Notification)
            .where(
                Notification.id == claim.notification_id,
                Notification.tenant_id == self.tenant_id,
            )
            .with_for_update()
        )
        if notification is None:
            return None
        attempt = session.scalar(
            select(NotificationAttempt)
            .where(
                NotificationAttempt.id == claim.attempt_id,
                NotificationAttempt.notification_id == notification.id,
                NotificationAttempt.tenant_id == self.tenant_id,
            )
            .with_for_update()
        )
        if (
            attempt is None
            or int(attempt.attempt_number) != int(notification.attempt_count)
        ):
            return None
        notification_sid = str(notification.provider_message_id or "").strip()
        attempt_sid = str(attempt.provider_message_id or "").strip()
        if not notification_sid or not hmac.compare_digest(
            notification_sid,
            attempt_sid,
        ):
            return None
        expected_sid = str(provider_message_id or "").strip()
        if expected_sid and not hmac.compare_digest(notification_sid, expected_sid):
            return None
        provider_status = str(notification.provider_status or "").lower()
        if (
            notification.status == Notification.STATUS_SENT
            and attempt.status == NotificationAttempt.STATUS_SUCCESS
            and _PROVIDER_DELIVERY_PROGRESS.get(provider_status, 0)
            >= _PROVIDER_DELIVERY_PROGRESS["accepted"]
        ):
            return "sent", notification
        if (
            notification.status == Notification.STATUS_FAILED
            and attempt.status == NotificationAttempt.STATUS_FAILED
            and provider_status in _PROVIDER_DELIVERY_FAILURES
        ):
            return "failed", notification
        return None

    def _mark_provider_call_started(
        self,
        claim: NotificationDispatchClaim,
        *,
        now,
    ) -> None:
        with Session(bind=db.engine, expire_on_commit=False) as session:
            notification, attempt = self._claim_rows(session, claim)
            if notification is None or attempt is None:
                session.rollback()
                raise NotificationLeaseLost("notification_lease_lost_before_io")
            metadata = dict(attempt.metadata_json or {})
            metadata["provider_call_started"] = True
            metadata["provider_call_started_at"] = now.isoformat()
            attempt.metadata_json = metadata
            session.add(attempt)
            session.commit()

    def _mark_provider_accepted(
        self,
        claim: NotificationDispatchClaim,
        *,
        provider_message_id: str | None,
        provider_status: str,
        now,
        provider_call_started: bool,
    ) -> str:
        provider_id = str(provider_message_id or "").strip()[:180] or None
        if provider_call_started and provider_id is None:
            raise ValueError("provider_message_id_required_after_io")
        with Session(bind=db.engine, expire_on_commit=False) as session:
            notification, attempt = self._claim_rows(session, claim)
            if notification is None or attempt is None:
                terminal = self._terminal_callback_outcome(
                    session,
                    claim,
                    provider_message_id=provider_id,
                )
                if terminal is not None:
                    session.rollback()
                    return terminal[0]
                session.rollback()
                raise NotificationLeaseLost("notification_lease_lost_after_io")
            notification.status = Notification.STATUS_SENT
            notification.provider_message_id = provider_id
            notification.provider_status = provider_status
            notification.sent_at = now
            notification.last_error = None
            notification.next_retry_at = None
            notification.lease_token = None
            notification.leased_until = None
            notification.updated_at = now
            attempt.status = NotificationAttempt.STATUS_SUCCESS
            attempt.provider_message_id = provider_id
            attempt.provider_status = provider_status
            attempt.error_message = None
            attempt.error_digest = None
            session.add_all([notification, attempt])
            session.commit()
        self._emit_notification_event(
            "notification.sent",
            notification,
            now=now,
            provider_message_id=provider_id,
        )
        return "sent"

    def _mark_send_uncertain(
        self,
        claim: NotificationDispatchClaim,
        *,
        error_code: str,
        raw_error: Any,
        now,
    ) -> str:
        safe_code = _safe_error_code(error_code, fallback="provider_outcome_unknown")
        with Session(bind=db.engine, expire_on_commit=False) as session:
            notification, attempt = self._claim_rows(session, claim)
            if notification is None or attempt is None:
                terminal = self._terminal_callback_outcome(session, claim)
                if terminal is not None:
                    session.rollback()
                    return terminal[0]
                session.rollback()
                raise NotificationLeaseLost("notification_lease_lost_after_io")
            notification.status = Notification.STATUS_SEND_UNCERTAIN
            notification.provider_status = Notification.PROVIDER_STATUS_UNKNOWN
            notification.last_error = safe_code
            notification.next_retry_at = None
            notification.lease_token = None
            notification.leased_until = None
            notification.updated_at = now
            attempt.status = NotificationAttempt.STATUS_SEND_UNCERTAIN
            attempt.provider_status = Notification.PROVIDER_STATUS_UNKNOWN
            attempt.error_message = safe_code
            attempt.error_digest = _error_digest(raw_error)
            session.add_all([notification, attempt])
            session.commit()
        self._emit_notification_event(
            "notification.send_uncertain",
            notification,
            now=now,
            error_message=safe_code,
        )
        return "send_uncertain"

    def _durable_whatsapp_registry_error(
        self,
        notification: Notification,
    ) -> str | None:
        registry_id = self._positive_id(
            notification.message_template_registry_id,
            field="template_registry_id",
        )
        row = MessageTemplateRegistry.query.filter_by(
            id=registry_id,
            tenant_id=self.tenant_id,
            provider="twilio",
            channel="whatsapp",
        ).one_or_none()
        if row is None:
            return "whatsapp_template_registry_mismatch"
        if not hmac.compare_digest(
            str(row.content_sid or ""),
            str(notification.content_sid or ""),
        ):
            return "whatsapp_template_registry_mismatch"
        lifecycle = whatsapp_template_lifecycle(
            row.status,
            source="message_template_registry",
            provider_reference=row.content_sid or row.external_template_id,
            observed_at=row.last_sync_at,
        )
        if not lifecycle.get("production_send_allowed"):
            return "whatsapp_template_not_approved"
        return None

    def _emit_notification_event(
        self,
        event_name: str,
        notif: Notification,
        *,
        now,
        provider_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        try:
            from socket_service import emit_notification_status_changed

            emit_notification_status_changed(
                {
                    "event": event_name,
                    "tenant_id": self.tenant_id,
                    "notification_id": notif.id,
                    "channel": notif.channel,
                    "status": notif.status,
                    "attempt_count": notif.attempt_count,
                    "max_retries": notif.max_retries,
                    "provider_status": getattr(notif, "provider_status", None),
                    "occurred_at": now.isoformat() if hasattr(now, "isoformat") else str(now),
                    "provider_message_id": provider_message_id,
                    "error_message": error_message,
                }
            )
        except Exception:
            # Events are non-blocking and should not fail dispatch flow.
            pass

    def _quiet_hours_for_notification(self, notif: Notification) -> tuple[Optional[int], Optional[int]]:
        if notif.template_id:
            tpl = NotificationTemplate.query.filter_by(id=notif.template_id, tenant_id=self.tenant_id).first()
            if tpl:
                return tpl.quiet_hours_start, tpl.quiet_hours_end
        meta = notif.metadata_json or {}
        return meta.get("quiet_hours_start"), meta.get("quiet_hours_end")

    @staticmethod
    def _is_quiet_hour(now, start: Optional[int], end: Optional[int]) -> bool:
        if start is None or end is None:
            return False
        hour = (now.astimezone(timezone.utc).hour if getattr(now, "tzinfo", None) else now.hour)
        start = int(start)
        end = int(end)
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    @staticmethod
    def _next_outside_quiet_hours(now, start: Optional[int], end: Optional[int]):
        if start is None or end is None:
            return now
        probe = now
        for _ in range(48):
            probe = probe + timedelta(hours=1)
            if not NotificationOrchestrator._is_quiet_hour(probe, start, end):
                return probe
        return now + timedelta(hours=1)

    @staticmethod
    def _send_stub(notif: Notification) -> tuple[bool, Optional[str], Optional[str]]:
        """Dispatch local channels only; external compatibility paths fail closed."""

        # MVP worker stub: consider metadata.force_fail to exercise retries in tests.
        if (notif.metadata_json or {}).get("force_fail"):
            return False, None, "forced_failure"
        if notif.channel == "whatsapp":
            metadata = notif.metadata_json if isinstance(notif.metadata_json, dict) else {}
            metadata = {**metadata, "recipient": notif.recipient}
            allowed, reason = WhatsAppEnterpriseRulesService(notif.tenant_id).evaluate_outbound(
                body=notif.body,
                metadata=metadata,
            )
            if not allowed:
                return False, None, reason
            if metadata.get("is_template"):
                registry_error = NotificationOrchestrator._whatsapp_registry_error(
                    notif,
                    metadata,
                )
                if registry_error:
                    return False, None, registry_error
            # The old worker fabricated a provider SID and marked the row sent.
            # No tenant-bound Twilio adapter is wired here, so provider
            # acceptance cannot be claimed even inside the customer window.
            return False, None, "whatsapp_transport_unavailable"
        if notif.channel == "in_app":
            provider_id = f"in_app:{notif.id}:{notif.attempt_count + 1}"
            return True, provider_id, None
        if notif.channel in {"email", "push"}:
            return False, None, f"{notif.channel}_transport_unavailable"
        return False, None, "unsupported_channel"

    @staticmethod
    def _whatsapp_registry_error(
        notif: Notification,
        metadata: dict,
    ) -> str | None:
        """Require fresh tenant-owned provider approval for template claims."""

        raw_registry_id = metadata.get("template_registry_id")
        try:
            registry_id = int(raw_registry_id)
        except (TypeError, ValueError, OverflowError):
            registry_id = None
        if registry_id is not None and registry_id <= 0:
            registry_id = None
        content_sid = str(metadata.get("content_sid") or "").strip()
        if registry_id is None and not content_sid:
            return "whatsapp_template_registry_required"

        query = MessageTemplateRegistry.query.filter_by(
            tenant_id=int(notif.tenant_id),
            provider="twilio",
            channel="whatsapp",
        )
        if registry_id is not None:
            row = query.filter_by(id=registry_id).one_or_none()
        else:
            matches = query.filter_by(content_sid=content_sid).limit(2).all()
            row = matches[0] if len(matches) == 1 else None
        if row is None:
            return "whatsapp_template_registry_mismatch"
        if content_sid and str(row.content_sid or "").strip() != content_sid:
            return "whatsapp_template_registry_mismatch"

        lifecycle = whatsapp_template_lifecycle(
            row.status,
            source="message_template_registry",
            provider_reference=row.content_sid or row.external_template_id,
            observed_at=row.last_sync_at,
        )
        if not lifecycle.get("production_send_allowed"):
            return "whatsapp_template_not_approved"
        return None
