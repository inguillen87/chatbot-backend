"""Identify the existing cron routes without interpreting request credentials.

These routes own their CRON_SECRET and activation gates. Generic account,
contact and tenant discovery must not perform database work before those gates.
This is only a middleware dispatch boundary, never authorization to run a job.
"""
from flask import Request


_REGISTERED_CRON_PATHS = {
    'internal_cron.outbox_reconciliation': '/api/internal/cron/outbox-reconciliation',
    'internal_cron.whatsapp_payload_retention': '/api/internal/cron/whatsapp-payload-retention',
    'internal_cron.survey_privacy_retention': '/api/internal/cron/survey-privacy-retention',
    'internal_cron.weekly_analytics_report': '/api/internal/cron/weekly-analytics-report',
}
_CRON_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


def is_registered_internal_cron_request(req: Request) -> bool:
    """Match exact registered endpoint, canonical rule/path and allowed method.

    An unmatched path, neighboring route, alternate rule for the same endpoint,
    or future endpoint is deliberately not exempt. Query/header tenant hints
    and credentials do not participate in this decision.
    """
    canonical_path = _REGISTERED_CRON_PATHS.get(req.endpoint)
    rule = req.url_rule
    return bool(
        canonical_path is not None
        and rule is not None
        and rule.rule == canonical_path
        and req.path == canonical_path
        and req.method in _CRON_METHODS
        and req.method in (rule.methods or ())
    )
