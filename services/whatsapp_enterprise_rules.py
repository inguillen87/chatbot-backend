from __future__ import annotations

from datetime import timedelta, timezone
import hashlib
import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    MessagingEventLedger,
    Notification,
    NotificationAttempt,
    WhatsAppContactState,
    WhatsAppEnterpriseRule,
    WhatsAppFlowInteraction,
    db,
)
from utils.time_utils import get_local_now


RATE_LIMIT_RESERVATION_EVENT = "whatsapp_outbound_rate_limit_reserved"
_RESERVATION_KEY = re.compile(r"^[A-Za-z0-9_.:\-]{8,120}$")


def _to_utc_naive(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class WhatsAppEnterpriseRulesService:
    def __init__(self, tenant_id: int):
        self.tenant_id = int(tenant_id)

    def get_or_create(self) -> WhatsAppEnterpriseRule:
        rule = WhatsAppEnterpriseRule.query.filter_by(tenant_id=self.tenant_id).first()
        if not rule:
            rule = WhatsAppEnterpriseRule(tenant_id=self.tenant_id)
            from models import db

            db.session.add(rule)
            db.session.flush()
        return rule

    def evaluate_outbound(
        self,
        *,
        body: str,
        metadata: dict | None = None,
        lock_rate_limit: bool = False,
    ) -> tuple[bool, str | None]:
        rule_query = WhatsAppEnterpriseRule.query.filter_by(tenant_id=self.tenant_id)
        if lock_rate_limit:
            rule_query = rule_query.with_for_update()
        rule = rule_query.first()
        if not rule:
            return True, None
        metadata = metadata if isinstance(metadata, dict) else {}

        return self._evaluate_rule(
            rule,
            body=body,
            metadata=metadata,
            session=db.session,
        )

    def reserve_outbound(
        self,
        *,
        body: str,
        reservation_key: str,
        metadata: dict | None = None,
        provider: str | None = None,
        provider_connection_id: int | None = None,
        provider_sender_id: int | None = None,
        source: str | None = None,
    ) -> tuple[bool, str | None]:
        """Atomically enforce policy and reserve one provider send attempt."""

        safe_key = str(reservation_key or "").strip()
        if not _RESERVATION_KEY.fullmatch(safe_key):
            raise ValueError("invalid_rate_limit_reservation_key")
        safe_metadata = metadata if isinstance(metadata, dict) else {}

        # Keep this commit independent from request state: the reservation must
        # be durable before an external provider can accept the message.
        with Session(db.engine) as session:
            with session.begin():
                rule = (
                    session.query(WhatsAppEnterpriseRule)
                    .filter_by(tenant_id=self.tenant_id)
                    .with_for_update()
                    .first()
                )
                if not rule:
                    return True, None

                allowed, reason = self._evaluate_rule(
                    rule,
                    body=body,
                    metadata=safe_metadata,
                    session=session,
                    reservation_key=safe_key,
                )
                if not allowed:
                    return False, reason
                if not rule.max_outbound_per_hour:
                    return True, None
                if self._reservation_exists(
                    session,
                    safe_key,
                    since=get_local_now() - timedelta(hours=1),
                ):
                    return True, None

                recipient = str(safe_metadata.get("recipient") or "")
                digits = "".join(character for character in recipient if character.isdigit())
                recipient_hint = f"***{digits[-4:]}" if digits else None
                session.add(
                    MessagingEventLedger(
                        tenant_id=self.tenant_id,
                        provider_connection_id=provider_connection_id,
                        provider_sender_id=provider_sender_id,
                        channel="whatsapp",
                        direction="outbound",
                        event_type=RATE_LIMIT_RESERVATION_EVENT,
                        provider=str(provider or "").strip()[:50] or None,
                        external_status="reserved",
                        recipient=recipient_hint,
                        request_id=safe_key,
                        payload={"source": str(source or "unknown").strip()[:80]},
                        occurred_at=get_local_now(),
                    )
                )
        return True, None

    def _evaluate_rule(
        self,
        rule: WhatsAppEnterpriseRule,
        *,
        body: str,
        metadata: dict,
        session,
        reservation_key: str | None = None,
    ) -> tuple[bool, str | None]:

        body_l = (body or "").lower()
        for word in (rule.blocked_keywords or []):
            if str(word).strip().lower() and str(word).strip().lower() in body_l:
                return False, "blocked_keyword"

        quiet_start = rule.quiet_hours_start
        quiet_end = rule.quiet_hours_end
        if quiet_start is not None and quiet_end is not None:
            # Rule hours are explicitly UTC until a governed tenant timezone
            # becomes part of this contract.  Hidden server-local conversion
            # would make the same policy behave differently after migration.
            current_hour = int(get_local_now().hour)
            in_quiet_hours = (
                int(quiet_start) <= current_hour < int(quiet_end)
                if int(quiet_start) < int(quiet_end)
                else current_hour >= int(quiet_start)
                or current_hour < int(quiet_end)
            )
            if in_quiet_hours:
                return False, "quiet_hours"

        if rule.max_outbound_per_hour:
            since = get_local_now() - timedelta(hours=1)
            reservation_exists = bool(reservation_key) and self._reservation_exists(
                session,
                str(reservation_key),
                since=since,
            )
            if not reservation_exists:
                sent_last_hour = self._hourly_outbound_count(session, since=since)
                if sent_last_hour >= int(rule.max_outbound_per_hour):
                    return False, "rate_limited"

        within_24h_window = bool(metadata.get("within_24h_window", False))
        if not within_24h_window and metadata.get("recipient"):
            normalized = self._normalize_recipient(metadata.get("recipient"))
            state = None
            try:
                provider_sender_id = int(metadata.get("provider_sender_id") or 0)
            except (TypeError, ValueError, OverflowError):
                provider_sender_id = 0
            if normalized and provider_sender_id > 0:
                state = (
                    session.query(WhatsAppContactState)
                    .filter_by(
                        tenant_id=self.tenant_id,
                        provider_sender_id=provider_sender_id,
                        recipient=normalized,
                    )
                    .first()
                )
            if state and state.last_inbound_at:
                now_naive = _to_utc_naive(get_local_now())
                inbound_naive = _to_utc_naive(state.last_inbound_at)
                if now_naive and inbound_naive:
                    within_24h_window = (now_naive - inbound_naive) <= timedelta(hours=24)

        if rule.enforce_template_outside_24h and not metadata.get("is_template"):
            if not within_24h_window:
                return False, "template_required"

        return True, None

    def _reservation_exists(self, session, reservation_key: str, *, since=None) -> bool:
        query = session.query(MessagingEventLedger.id).filter_by(
            tenant_id=self.tenant_id,
            channel="whatsapp",
            event_type=RATE_LIMIT_RESERVATION_EVENT,
            request_id=reservation_key,
        )
        if since is not None:
            query = query.filter(MessagingEventLedger.occurred_at >= since)
        return bool(query.first())

    def _hourly_outbound_count(self, session, *, since) -> int:
        reservations = (
            session.query(MessagingEventLedger.request_id)
            .filter_by(
                tenant_id=self.tenant_id,
                channel="whatsapp",
                event_type=RATE_LIMIT_RESERVATION_EVENT,
            )
            .filter(MessagingEventLedger.occurred_at >= since)
            .all()
        )
        reservation_keys = {row[0] for row in reservations if row[0]}
        recent_notification_attempts = (
            session.query(Notification.id, NotificationAttempt.id)
            .outerjoin(
                NotificationAttempt,
                (NotificationAttempt.notification_id == Notification.id)
                & (NotificationAttempt.tenant_id == Notification.tenant_id)
                & (
                    NotificationAttempt.status.in_(
                        [
                            NotificationAttempt.STATUS_SUCCESS,
                            NotificationAttempt.STATUS_SEND_UNCERTAIN,
                        ]
                    )
                ),
            )
            .filter(
                Notification.tenant_id == self.tenant_id,
                Notification.channel == "whatsapp",
            )
            .filter(Notification.created_at >= since)
            .filter(
                Notification.status.in_(
                    [Notification.STATUS_SENT, Notification.STATUS_SEND_UNCERTAIN]
                )
            )
            .all()
        )
        notification_ids = {row[0] for row in recent_notification_attempts}
        notification_ids_with_reservation = {
            row[0]
            for row in recent_notification_attempts
            if row[1] and f"notification:{row[1]}" in reservation_keys
        }
        legacy_notification_count = len(
            notification_ids - notification_ids_with_reservation
        )
        recent_flow_keys = (
            session.query(WhatsAppFlowInteraction.idempotency_key)
            .filter_by(tenant_id=self.tenant_id)
            .filter(WhatsAppFlowInteraction.created_at >= since)
            .all()
        )
        legacy_flow_count = sum(
            1
            for row in recent_flow_keys
            if whatsapp_flow_rate_limit_reservation_key(row[0]) not in reservation_keys
        )
        return len(reservation_keys) + legacy_notification_count + legacy_flow_count

    def get_contact_state(
        self,
        *,
        recipient: str | None,
        provider_sender_id: int | None,
    ) -> WhatsAppContactState | None:
        normalized = self._normalize_recipient(recipient)
        try:
            normalized_sender_id = int(provider_sender_id or 0)
        except (TypeError, ValueError, OverflowError):
            normalized_sender_id = 0
        if not normalized or normalized_sender_id <= 0:
            return None
        return WhatsAppContactState.query.filter_by(
            tenant_id=self.tenant_id,
            provider_sender_id=normalized_sender_id,
            recipient=normalized,
        ).first()

    def register_inbound_activity(
        self,
        *,
        recipient: str | None,
        provider_sender_id: int | None,
        at=None,
    ) -> WhatsAppContactState | None:
        normalized = self._normalize_recipient(recipient)
        try:
            normalized_sender_id = int(provider_sender_id or 0)
        except (TypeError, ValueError, OverflowError):
            normalized_sender_id = 0
        if not normalized or normalized_sender_id <= 0:
            return None
        state = self.get_contact_state(
            recipient=normalized,
            provider_sender_id=normalized_sender_id,
        )
        observed_at = at or get_local_now()
        if state:
            state.last_inbound_at = observed_at
            db.session.flush()
            return state

        try:
            with db.session.begin_nested():
                candidate = WhatsAppContactState(
                    tenant_id=self.tenant_id,
                    provider_sender_id=normalized_sender_id,
                    recipient=normalized,
                    last_inbound_at=observed_at,
                )
                db.session.add(candidate)
                db.session.flush()
            state = candidate
        except IntegrityError:
            # Another inbound for this exact sender/contact may win the first
            # insert.  The savepoint keeps the webhook transaction usable.
            state = self.get_contact_state(
                recipient=normalized,
                provider_sender_id=normalized_sender_id,
            )
            if state is None:
                raise
            current_at = _to_utc_naive(state.last_inbound_at)
            candidate_at = _to_utc_naive(observed_at)
            if current_at is None or (
                candidate_at is not None and candidate_at >= current_at
            ):
                state.last_inbound_at = observed_at
        db.session.flush()
        return state

    @staticmethod
    def _normalize_recipient(value: str | None) -> str:
        if not value:
            return ""
        return str(value).strip().replace("whatsapp:", "")


def whatsapp_flow_rate_limit_reservation_key(idempotency_key: str) -> str:
    digest = hashlib.sha256(str(idempotency_key or "").encode("utf-8")).hexdigest()
    return f"flow:{digest}"
