from __future__ import annotations

from datetime import timedelta

from models import Notification, WhatsAppEnterpriseRule
from utils.time_utils import get_local_now


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

    def evaluate_outbound(self, *, body: str, metadata: dict | None = None) -> tuple[bool, str | None]:
        rule = WhatsAppEnterpriseRule.query.filter_by(tenant_id=self.tenant_id).first()
        if not rule:
            return True, None

        body_l = (body or "").lower()
        for word in (rule.blocked_keywords or []):
            if str(word).strip().lower() and str(word).strip().lower() in body_l:
                return False, "blocked_keyword"

        if rule.max_outbound_per_hour:
            since = get_local_now() - timedelta(hours=1)
            sent_last_hour = (
                Notification.query.filter_by(tenant_id=self.tenant_id, channel="whatsapp")
                .filter(Notification.created_at >= since)
                .count()
            )
            if sent_last_hour >= int(rule.max_outbound_per_hour):
                return False, "rate_limited"

        if rule.enforce_template_outside_24h and not (metadata or {}).get("is_template"):
            if not (metadata or {}).get("within_24h_window", False):
                return False, "template_required"

        return True, None
