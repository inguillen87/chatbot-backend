from __future__ import annotations

from datetime import timedelta, timezone
from string import Template
from typing import Optional

from models import Notification, NotificationAttempt, NotificationTemplate, db
from utils.time_utils import get_local_now

ALLOWED_CHANNELS = {"email", "whatsapp", "push", "in_app"}
BASE_BACKOFF_SECONDS = 60


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
        metadata: Optional[dict] = None,
    ) -> tuple[Notification, bool]:
        channel = (channel or "").strip().lower()
        if channel not in ALLOWED_CHANNELS:
            raise ValueError("unsupported channel")
        if not (recipient or "").strip():
            raise ValueError("recipient is required")

        idem = (idempotency_key or "").strip()
        if not idem:
            raise ValueError("idempotency_key is required")

        existing = Notification.query.filter_by(
            tenant_id=self.tenant_id,
            idempotency_key=idem,
        ).first()
        if existing:
            return existing, False

        selected_template = None
        rendered_subject = subject
        rendered_body = body

        if template_key:
            selected_template = NotificationTemplate.query.filter_by(
                tenant_id=self.tenant_id,
                key=str(template_key).strip().lower(),
                channel=channel,
                is_active=True,
            ).first()
            if not selected_template:
                raise ValueError("template not found")
            context = template_context or {}
            rendered_body = Template(selected_template.body_template).safe_substitute(context)
            if selected_template.subject_template:
                rendered_subject = Template(selected_template.subject_template).safe_substitute(context)

        if not (rendered_body or "").strip():
            raise ValueError("body is required")

        notification = Notification(
            tenant_id=self.tenant_id,
            user_id=user_id,
            template_id=selected_template.id if selected_template else None,
            channel=channel,
            recipient=recipient,
            subject=rendered_subject,
            body=rendered_body,
            status="queued",
            idempotency_key=idem,
            max_retries=max(0, int(max_retries)),
            metadata_json=metadata or {},
        )
        db.session.add(notification)
        db.session.flush()
        return notification, True

    def dispatch_due_notifications(self, *, now=None, limit: int = 50) -> dict:
        now = now or get_local_now()
        due = (
            Notification.query.filter(Notification.tenant_id == self.tenant_id)
            .filter(Notification.status.in_(["queued", "failed", "delayed"]))
            .filter((Notification.next_retry_at.is_(None)) | (Notification.next_retry_at <= now))
            .order_by(Notification.created_at.asc())
            .limit(limit)
            .all()
        )

        result = {"processed": 0, "sent": 0, "delayed": 0, "failed": 0}
        for notif in due:
            result["processed"] += 1
            outcome = self._dispatch_one(notif, now=now)
            result[outcome] += 1

        db.session.flush()
        return result

    def _dispatch_one(self, notif: Notification, *, now):
        quiet_start, quiet_end = self._quiet_hours_for_notification(notif)
        if self._is_quiet_hour(now, quiet_start, quiet_end):
            next_time = self._next_outside_quiet_hours(now, quiet_start, quiet_end)
            notif.status = "delayed"
            notif.next_retry_at = next_time
            self._create_attempt(
                notif,
                "delayed",
                error_message="quiet_hours",
                next_retry_at=next_time,
                attempt_number=notif.attempt_count + 1,
            )
            return "delayed"

        ok, provider_id, error_msg = self._send_stub(notif)
        notif.attempt_count += 1
        if ok:
            notif.status = "sent"
            notif.sent_at = now
            notif.last_error = None
            notif.next_retry_at = None
            self._create_attempt(notif, "success", provider_message_id=provider_id)
            return "sent"

        remaining = notif.attempt_count <= notif.max_retries
        backoff = timedelta(seconds=BASE_BACKOFF_SECONDS * (2 ** max(0, notif.attempt_count - 1)))
        next_retry = now + backoff if remaining else None
        notif.status = "failed"
        notif.last_error = error_msg
        notif.next_retry_at = next_retry
        self._create_attempt(notif, "failed", error_message=error_msg, next_retry_at=next_retry)
        return "failed"

    def _create_attempt(
        self,
        notif: Notification,
        status: str,
        provider_message_id: str | None = None,
        error_message: str | None = None,
        next_retry_at=None,
        attempt_number: int | None = None,
    ):
        attempt = NotificationAttempt(
            notification_id=notif.id,
            tenant_id=self.tenant_id,
            attempt_number=attempt_number if attempt_number is not None else notif.attempt_count,
            status=status,
            provider=notif.channel,
            provider_message_id=provider_message_id,
            error_message=error_message,
            attempted_at=get_local_now(),
            next_retry_at=next_retry_at,
        )
        db.session.add(attempt)

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
        # MVP worker stub: consider metadata.force_fail to exercise retries in tests.
        if (notif.metadata_json or {}).get("force_fail"):
            return False, None, "forced_failure"
        provider_id = f"{notif.channel}:{notif.id}:{notif.attempt_count + 1}"
        return True, provider_id, None
