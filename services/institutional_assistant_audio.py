"""On-demand reading of published knowledge, bounded before any provider call."""
from copy import deepcopy
import os
import re

from flask import current_app
from flask_limiter.util import get_remote_address
from limits import parse
from limits.storage import MemoryStorage

from extensions import limiter
from services.institutional_assistant_content import ContentError


MAX_AUDIO_COMMAND_BYTES = 2048
MAX_AUDIO_TEXT_CHARS = 4000
MAX_AUDIO_BYTES = 4 * 1024 * 1024
AUDIO_TIMEOUT_SECONDS = 20.0
AUDIO_READING = {
    'contract_version': 'chatboc.institutional_audio.v1',
    'listen': 'Escuchar', 'pause': 'Pausar', 'resume': 'Continuar audio',
    'stop': 'Detener', 'loading': 'Preparando audio…',
    'error': 'No se pudo reproducir el audio. La información sigue disponible en texto.',
    'disclosure': 'Voz generada por IA. Se lee la información publicada de esta respuesta.',
}
_NODE_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}')
_REVISION = re.compile(r'[a-f0-9]{64}')


def _shared_rate_storage_ready():
    """Vercel/production must never silently fall back to per-process budgets."""
    production = bool(os.getenv('VERCEL') or os.getenv('VERCEL_ENV')
        or str(current_app.config.get('ENV') or '').lower() in ('prod', 'production'))
    if not production:
        return True
    try:
        # The active strategy also identifies an in-memory outage fallback.
        return not isinstance(limiter.limiter.storage, MemoryStorage)
    except Exception:
        return False


def audio_reading_capability():
    if not os.getenv('OPENAI_API_KEY', '').strip() or not _shared_rate_storage_ready():
        return None
    return deepcopy(AUDIO_READING)


def _validate_audio_command(command):
    if not isinstance(command, dict) or set(command) != {'revision', 'node_ids'}:
        raise ContentError('knowledge_command_invalid', 400)
    revision, ids = command['revision'], command['node_ids']
    if (not isinstance(revision, str) or not _REVISION.fullmatch(revision)
        or not isinstance(ids, list) or not 1 <= len(ids) <= 3
        or any(not isinstance(node, str) or not _NODE_ID.fullmatch(node) for node in ids)
        or len(set(ids)) != len(ids)):
        raise ContentError('knowledge_command_invalid', 400)
    return revision, ids


def _consume_audio_budget(tenant_id):
    if not _shared_rate_storage_ready():
        raise ContentError('knowledge_audio_rate_limit_unavailable', 503)
    # The existing shared limiter supplies atomic cross-worker counters. Limits
    # include tenant and installation budgets, so rotating IPs cannot remove
    # the aggregate ceiling. Invalid/private/stale requests never consume TTS.
    budgets = (
        ('6 per minute', 'ip-minute', get_remote_address()),
        ('30 per hour', 'ip-hour', get_remote_address()),
        ('60 per hour', 'tenant-hour', str(tenant_id)),
        ('180 per hour', 'installation-hour', 'all'),
    )
    for limit, scope, key in budgets:
        try:
            strategy = limiter.limiter
            allowed = strategy.hit(parse(limit), 'institutional-audio-v1', scope, key)
        except Exception as error:
            # No exception body, key, text or URL enters the response or log.
            current_app.logger.warning('Institutional audio capacity unavailable')
            raise ContentError('knowledge_audio_rate_limit_unavailable', 503) from error
        if not allowed:
            raise ContentError('knowledge_audio_rate_limited', 429)


def synthesize_public_nodes(tenant, command):
    from models import db
    from services.institutional_assistant import read_state

    revision, ids = _validate_audio_command(command)
    state = read_state(tenant, public=True)
    if state['revision'] != revision:
        raise ContentError('knowledge_revision_conflict', 412)
    nodes = state['bundle']['nodes']
    if any(node_id not in nodes for node_id in ids):
        raise ContentError('knowledge_node_not_found', 400)
    # Read only the canonical public answer and its menu. Never send source
    # excerpts, private originals, provenance links, questions or caller text.
    lines = []
    for node_id in ids:
        node = nodes[node_id]
        lines.extend((node['title'], node['text']))
        if node['actions']:
            lines.append('Opciones disponibles:')
            lines.extend(f"Opción {choice['code']}: {choice['label']}." for choice in node['actions'])
    text = '\n\n'.join(lines)
    if len(text) > MAX_AUDIO_TEXT_CHARS:
        raise ContentError('knowledge_audio_text_too_large', 413)
    if not os.getenv('OPENAI_API_KEY', '').strip():
        raise ContentError('knowledge_audio_unavailable', 503)
    _consume_audio_budget(tenant.id)
    from services.openai_tts_bridge import synthesize_mp3_bytes
    audio = synthesize_mp3_bytes(text, max_bytes=MAX_AUDIO_BYTES,
        timeout_seconds=AUDIO_TIMEOUT_SECONDS)
    # Retiring/replacing the version or disabling the tenant while the remote
    # provider runs prevents release of all bytes, including a partial stream.
    db.session.expire_all()
    latest = read_state(tenant, public=True)
    if latest['revision'] != revision:
        raise ContentError('knowledge_revision_conflict', 412)
    return audio, revision
