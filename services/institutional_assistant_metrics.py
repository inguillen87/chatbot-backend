"""Anonymous outcome counts for typed questions to the institutional assistant.

Only canonical identifiers are kept: the outcome, the displayed node, the
selected nodes and the knowledge revision prefix. The question, any transcript
and every caller identity (user, session, address, phone) are never recorded.
"""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import logging
import re

logger = logging.getLogger(__name__)

CONTRACT = 'chatboc.institutional_assistant_metrics.v1'
EVENT_NAME = 'institutional_assistant.question'
FEEDBACK_EVENT = 'institutional_assistant.feedback'
OUTCOMES = ('answered', 'uncovered', 'typed_navigation', 'sources', 'unavailable')
CHANNELS = ('web_reader',)
FEEDBACK_CHANNELS = ('web_reader', 'whatsapp')
DEFAULT_DAYS, MAX_DAYS, MAX_EVENTS, MAX_RANKING = 30, 90, 20000, 20
MAX_UTC_OFFSET_MINUTES = 14 * 60
# Wording of the administration panel. The client renders it as received.
UI = {
    'title': 'Consultas escritas',
    'description': 'Resultado de las consultas que las personas escribieron en el lector público.',
    'period': 'Últimos {days} días',
    'answered': 'Con respuesta', 'uncovered': 'Sin respuesta en las fuentes',
    'typed_navigation': 'Navegación escrita', 'sources': 'Pedidos de fuentes',
    'unavailable': 'No se pudieron interpretar',
    'uncovered_rate': 'Consultas sin respuesta',
    'resolution_rate': 'Tasa de resolución',
    'resolution_detail': '{answered} de {interpreted} consultas tuvieron respuesta en las fuentes.',
    'uncovered_by_node': 'Temas donde faltó información',
    'answered_by_node': 'Temas más consultados',
    'node': 'Tema', 'count': 'Consultas',
    'retired_node': 'Tema de una versión anterior',
    'empty': 'Todavía no hay consultas escritas en este período.',
    'privacy': ('Se registra el resultado y el tema de cada consulta. '
                'No se guarda el texto ni datos de quien consulta.'),
    'truncated': 'Hay más consultas de las que se muestran; se usaron las más recientes.',
    'feedback_title': 'Valoración de las respuestas',
    'feedback_description': 'Lo que marcaron las personas al leer una respuesta en el lector público.',
    'feedback_helpful': 'Les sirvió', 'feedback_unhelpful': 'No les sirvió',
    'feedback_rate': 'Respuestas que sirvieron',
    'feedback_by_node': 'Temas con más respuestas que no sirvieron',
    'feedback_empty': 'Todavía nadie valoró una respuesta en este período.',
}
_NODE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}')
_REVISION_PREFIX = re.compile(r'[a-f0-9]{16}')
_observed = ContextVar('institutional_question_outcomes', default=None)


@contextmanager
def observe_question_outcomes():
    """Collect, in memory only, the outcomes produced inside this block."""
    outcomes = []
    token = _observed.set(outcomes)
    try:
        yield outcomes
    finally:
        _observed.reset(token)


def note_question_outcome(revision, node_id, outcome, selected=()):
    """Remember one typed-question outcome for the enclosing observer.

    Performs no I/O and never raises: an answer must not depend on a metric.
    """
    outcomes = _observed.get()
    if outcomes is None:
        return
    try:
        outcomes.append({'outcome': outcome, 'node': node_id,
                         'selected': list(selected)[:3], 'revision': revision[:16]})
    except Exception:  # pragma: no cover - defensive, see docstring
        pass


def _payload(item):
    """Rebuild the stored payload from validated canonical identifiers only."""
    if not isinstance(item, dict) or item.get('outcome') not in OUTCOMES:
        return None
    node, selected, revision = item.get('node'), item.get('selected'), item.get('revision')
    if (not isinstance(node, str) or not _NODE_ID.fullmatch(node)
            or not isinstance(revision, str) or not _REVISION_PREFIX.fullmatch(revision)
            or not isinstance(selected, list) or len(selected) > 3
            or not all(isinstance(value, str) and _NODE_ID.fullmatch(value) for value in selected)):
        return None
    return {'outcome': item['outcome'], 'node': node, 'selected': list(selected), 'revision': revision,
            'selection_performed': item['outcome'] in ('answered', 'uncovered', 'unavailable')}


def record_question_outcomes(tenant_id, tenant_type, channel, outcomes):
    """Persist the collected outcomes. Best effort: failures are logged, never raised."""
    try:
        if not outcomes or channel not in CHANNELS or type(tenant_id) is not int or tenant_id < 1:
            return 0
        from flask import current_app
        from cutover_writer_fence import cutover_writer_fence_enabled
        if cutover_writer_fence_enabled(current_app.config):
            return 0
        from services.analytics.ingestor import analytics_ingestor
        stored = 0
        for item in outcomes:
            payload = _payload(item)
            if payload is None:
                continue
            if analytics_ingestor.track(tenant_id, EVENT_NAME, payload, channel=channel,
                                        tenant_type=tenant_type) is not None:
                stored += 1
        return stored
    except Exception:
        logger.warning('institutional_question_metric_not_recorded channel=%s', channel, exc_info=True)
        try:
            from models import db
            db.session.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        return 0


def record_answer_feedback(tenant_id, tenant_type, channel, revision, node_id, helpful):
    """Persist one anonymous «did this help» mark. Best effort, never raises.

    Only the topic, the knowledge revision prefix and yes/no are kept: no
    comment, no text and no caller identity exist in this record."""
    try:
        if (channel not in FEEDBACK_CHANNELS or type(tenant_id) is not int or tenant_id < 1 or type(helpful) is not bool
                or not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id)
                or not isinstance(revision, str) or not _REVISION_PREFIX.fullmatch(revision[:16])):
            return False
        from flask import current_app
        from cutover_writer_fence import cutover_writer_fence_enabled
        if cutover_writer_fence_enabled(current_app.config):
            return False
        from services.analytics.ingestor import analytics_ingestor
        return analytics_ingestor.track(tenant_id, FEEDBACK_EVENT, {
            'node': node_id, 'revision': revision[:16], 'helpful': helpful},
            channel=channel, tenant_type=tenant_type) is not None
    except Exception:
        logger.warning('institutional_answer_feedback_not_recorded channel=%s', channel, exc_info=True)
        try:
            from models import db
            db.session.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        return False


def _feedback(tenant, since, by_day_keys, offset, ranking):
    from models import db, AnalyticsEventV2
    rows = (db.session.query(AnalyticsEventV2.ts, AnalyticsEventV2.channel, AnalyticsEventV2.metadata_payload)
            .filter(AnalyticsEventV2.tenant_id == tenant.id, AnalyticsEventV2.event_name == FEEDBACK_EVENT,
                    AnalyticsEventV2.ts >= since)
            .order_by(AnalyticsEventV2.ts.desc()).limit(MAX_EVENTS).all())
    helpful, unhelpful, by_node = 0, 0, Counter()
    for ts, channel, stored in rows:
        if (channel not in FEEDBACK_CHANNELS or ts is None or not isinstance(stored, dict)
                or type(stored.get('helpful')) is not bool or not isinstance(stored.get('node'), str)
                or not _NODE_ID.fullmatch(stored['node'])):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if (ts.astimezone(timezone.utc) + offset).date() not in by_day_keys:
            continue
        if stored['helpful']:
            helpful += 1
        else:
            unhelpful += 1
            by_node[stored['node']] += 1
    total = helpful + unhelpful
    return {'helpful': helpful, 'unhelpful': unhelpful,
            'helpful_rate': round(helpful / total, 4) if total else None,
            'unhelpful_by_node': ranking(by_node)}


def question_metrics(tenant, state, *, days=DEFAULT_DAYS, utc_offset_minutes=0, now=None):
    """Read-only aggregates of the recorded outcomes for one tenant."""
    from models import db, AnalyticsEventV2
    offset = timedelta(minutes=utc_offset_minutes)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    last_day = (current + offset).date()
    first_day = last_day - timedelta(days=days - 1)
    since = datetime(first_day.year, first_day.month, first_day.day, tzinfo=timezone.utc) - offset
    rows = (db.session.query(AnalyticsEventV2.ts, AnalyticsEventV2.channel, AnalyticsEventV2.metadata_payload)
            .filter(AnalyticsEventV2.tenant_id == tenant.id, AnalyticsEventV2.event_name == EVENT_NAME,
                    AnalyticsEventV2.ts >= since)
            .order_by(AnalyticsEventV2.ts.desc()).limit(MAX_EVENTS + 1).all())
    truncated = len(rows) > MAX_EVENTS
    by_day = {first_day + timedelta(days=index): Counter() for index in range(days)}
    totals, by_channel = Counter(), {channel: Counter() for channel in CHANNELS}
    uncovered, asked = Counter(), Counter()
    for ts, channel, stored in rows[:MAX_EVENTS]:
        payload = _payload(stored)
        if payload is None or channel not in by_channel or ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        day = (ts.astimezone(timezone.utc) + offset).date()
        if day not in by_day:
            continue
        outcome = payload['outcome']
        totals[outcome] += 1
        by_day[day][outcome] += 1
        by_channel[channel][outcome] += 1
        if outcome == 'uncovered':
            uncovered[payload['node']] += 1
        elif outcome == 'answered':
            asked.update(set(payload['selected']))
    nodes = state['bundle']['nodes'] if state else {}
    start = state['bundle']['start'] if state else None

    def counts(counter):
        return {outcome: counter.get(outcome, 0) for outcome in OUTCOMES}

    def ranking(counter):
        ordered = sorted(counter.items(), key=lambda entry: (-entry[1], entry[0]))[:MAX_RANKING]
        return [{'node_id': node_id, 'count': count,
                 'title': nodes[node_id]['title'] if node_id in nodes else None,
                 'is_start': node_id == start} for node_id, count in ordered]

    interpreted = totals['answered'] + totals['uncovered']
    return {
        'contract_version': CONTRACT, 'tenant': {'id': tenant.id, 'slug': tenant.slug},
        'revision': state['revision'] if state else None,
        'days': days, 'utc_offset_minutes': utc_offset_minutes,
        'from': first_day.isoformat(), 'to': last_day.isoformat(), 'truncated': truncated,
        'totals': counts(totals),
        'uncovered_rate': round(totals['uncovered'] / interpreted, 4) if interpreted else None,
        # Share of the interpreted questions that the published sources answered.
        'resolution_rate': round(totals['answered'] / interpreted, 4) if interpreted else None,
        'interpreted': interpreted,
        'by_channel': {channel: counts(counter) for channel, counter in by_channel.items()},
        'by_day': [{'date': day.isoformat(), **counts(counter)} for day, counter in sorted(by_day.items())],
        'uncovered_by_node': ranking(uncovered), 'answered_by_node': ranking(asked),
        'feedback': _feedback(tenant, since, by_day, offset, ranking),
        'ui': dict(UI),
    }
