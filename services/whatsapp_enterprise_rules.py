from __future__ import annotations

from datetime import timedelta

from models import (
    Notification,
    WhatsAppContactState,
    WhatsAppEnterpriseRule,
    WhatsAppFlowInteraction,
    db,
)
from utils.time_utils import get_local_now


def _to_utc_naive(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is not None:
        return value.astimezone().replace(tzinfo=None)
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
            # Serialize real sends for tenants with an hourly cap. PostgreSQL holds
            # this row lock until the caller commits its durable send reservation.
            rule_query = rule_query.with_for_update()
        rule = rule_query.first()
        if not rule:
            return True, None
        metadata = metadata if isinstance(metadata, dict) else {}

        body_l = (body or "").lower()
        for word in (rule.blocked_keywords or []):
            if str(word).strip().lower() and str(word).strip().lower() in body_l:
                return False, "blocked_keyword"

        if rule.max_outbound_per_hour:
            since = get_local_now() - timedelta(hours=1)
            notification_count = (
                Notification.query.filter_by(tenant_id=self.tenant_id, channel="whatsapp")
                .filter(Notification.created_at >= since)
                .count()
            )
            flow_count = (
                WhatsAppFlowInteraction.query.filter_by(tenant_id=self.tenant_id)
                .filter(WhatsAppFlowInteraction.created_at >= since)
                .count()
            )
            sent_last_hour = notification_count + flow_count
            if sent_last_hour >= int(rule.max_outbound_per_hour):
                return False, "rate_limited"

        within_24h_window = bool(metadata.get("within_24h_window", False))
        if not within_24h_window and metadata.get("recipient"):
            state = self.get_contact_state(recipient=metadata.get("recipient"))
            if state and state.last_inbound_at:
                now_naive = _to_utc_naive(get_local_now())
                inbound_naive = _to_utc_naive(state.last_inbound_at)
                if now_naive and inbound_naive:
                    within_24h_window = (now_naive - inbound_naive) <= timedelta(hours=24)

        if rule.enforce_template_outside_24h and not metadata.get("is_template"):
            if not within_24h_window:
                return False, "template_required"

        return True, None

    def get_contact_state(self, *, recipient: str | None) -> WhatsAppContactState | None:
        normalized = self._normalize_recipient(recipient)
        if not normalized:
            return None
        return WhatsAppContactState.query.filter_by(
            tenant_id=self.tenant_id,
            recipient=normalized,
        ).first()

    def register_inbound_activity(self, *, recipient: str | None, at=None) -> WhatsAppContactState | None:
        normalized = self._normalize_recipient(recipient)
        if not normalized:
            return None
        state = self.get_contact_state(recipient=normalized)
        if not state:
            state = WhatsAppContactState(tenant_id=self.tenant_id, recipient=normalized)
            db.session.add(state)
        state.last_inbound_at = at or get_local_now()
        db.session.flush()
        return state

    @staticmethod
    def _normalize_recipient(value: str | None) -> str:
        if not value:
            return ""
        return str(value).strip().replace("whatsapp:", "")
