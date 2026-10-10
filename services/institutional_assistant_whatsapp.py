"""Bounded WhatsApp presentation; canonical app/widget responses stay unchanged."""
from copy import deepcopy
import re

from services.institutional_assistant import answer, read_state
from services.institutional_assistant_content import ContentError

SOURCE_SCOPE = 'institutional_whatsapp_sources'
_REVISION = re.compile(r'^[0-9a-f]{64}$')
_SOURCES_COMMAND = re.compile(r'fuentes(?: ([1-9][0-9]{0,2}))?', re.IGNORECASE)
_PAGE_BODY_LIMIT = 2800
_UNAVAILABLE = 'Estas referencias no están disponibles en este turno. Escribí MENÚ para elegir un tema nuevamente.'


def sources_page(question):
    """An explicit UI command, never a classifier or a user-supplied node ID."""
    match = _SOURCES_COMMAND.fullmatch(question.strip()) if isinstance(question, str) else None
    return int(match.group(1) or '1') if match else None


def clear_source_scope(context):
    value = deepcopy(context) if isinstance(context, dict) else {}
    value.pop(SOURCE_SCOPE, None)
    return value


def _public_sources(nodes):
    # A public answer may cite a private original. It does not publish that
    # original's title, review metadata, provenance, bytes or delivery URL.
    result, seen = [], {}
    for node in nodes:
        for source in node.get('sources', []):
            if source.get('document_visibility') != 'public':
                continue
            if source['id'] not in seen:
                value = deepcopy(source)
                seen[source['id']] = value
                result.append(value)
            else:
                value = seen[source['id']]
                value['pages'] = sorted(set(value.get('pages', [])) | set(source.get('pages', [])))
    return result


def _review_notice(sources):
    statuses = {source.get('review_status') for source in sources}
    if 'conflict' in statuses:
        return 'Hay diferencias entre algunas fuentes que todavía deben revisarse.'
    if statuses & {'unreviewed', 'needs_review'}:
        return 'Parte de la documentación todavía tiene revisión pendiente.'
    return ''


def _links(nodes):
    values, seen = [], set()
    for node in nodes:
        for link in node.get('links', []):
            item = link['label'] + ': ' + link['url']
            if item not in seen:
                values.append(item)
                seen.add(item)
    return values


def present_answer(result, context, *, tenant_id, tenant_slug):
    """Remove only automatic source metadata, preserving canonical text/links."""
    value = clear_source_scope(context)
    nodes, revision = result.get('knowledge_nodes'), result.get('context_revision')
    tenant = {'id': tenant_id, 'slug': tenant_slug}
    if (not isinstance(nodes, list) or not 1 <= len(nodes) <= 3
            or result.get('knowledge_tenant') != tenant
            or not isinstance(revision, str) or not _REVISION.fullmatch(revision)):
        return result['message_body'], value
    value[SOURCE_SCOPE] = {'tenant': tenant, 'revision': revision,
                          'node_ids': [node['id'] for node in nodes]}
    parts = [node['text'] for node in nodes] + _links(nodes)
    notice = _review_notice(_public_sources(nodes))
    if notice:
        parts.append(notice)
    parts.append('Para consultar las referencias de esta orientación, escribí FUENTES.')
    return '\n\n'.join(parts), value


def _source_entries(nodes):
    entries = []
    for source in _public_sources(nodes):
        label = ' '.join(source['title'].split())
        if source.get('pages'):
            label += ' · páginas ' + ', '.join(str(page) for page in source['pages'])
        notice = _review_notice([source])
        if notice:
            label += '\n' + notice
        # Only the normalized official URL of an explicitly public document.
        # delivery metadata alone never promises a downloadable stored file.
        if source.get('url'):
            label += '\n' + source['url']
        entries.append(label)
    entries.extend(_links(nodes))
    return entries


def _source_pages(entries):
    pages, current = [], []
    for entry in entries:
        if current and len('\n\n'.join(current + [entry])) > _PAGE_BODY_LIMIT:
            pages.append('\n\n'.join(current))
            current = []
        current.append(entry)
    if current:
        pages.append('\n\n'.join(current))
    return pages


def sources_answer(tenant, context, page):
    """Re-read exactly the last canonical nodes, with no selector or private I/O."""
    scope = context.get(SOURCE_SCOPE)
    previous = context.get('institutional_knowledge', {})
    try:
        state = read_state(tenant, public=True)
        if (not isinstance(scope, dict) or set(scope) != {'tenant', 'revision', 'node_ids'}
                or scope['tenant'] != {'id': tenant.id, 'slug': tenant.slug}
                or scope['revision'] != state['revision']
                or not isinstance(previous, dict) or previous.get('revision') != scope['revision']):
            raise ContentError('knowledge_revision_conflict', 412)
        ids = scope['node_ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 3
                or not all(isinstance(node_id, str) and node_id in state['bundle']['nodes'] for node_id in ids)
                or len(set(ids)) != len(ids) or previous.get('node_id') != ids[-1]):
            raise ContentError('knowledge_node_not_found', 400)
        nodes = []
        for node_id in ids:
            nodes.extend(answer(tenant, {'revision': scope['revision'], 'node_id': node_id}, public=True)['nodes'])
        # answer() re-reads each result; this also fences a multi-node response.
        if read_state(tenant, public=True)['revision'] != scope['revision']:
            raise ContentError('knowledge_revision_conflict', 412)
    except ContentError:
        return _UNAVAILABLE, clear_source_scope(context), None
    pages = _source_pages(_source_entries(nodes))
    if not pages:
        return 'No hay referencias públicas disponibles para esta orientación.', context, scope['revision']
    if not 1 <= page <= len(pages):
        return 'Ese grupo de referencias no está disponible. Escribí FUENTES para ver el primero.', context, scope['revision']
    body = 'Referencias de esta orientación (' + str(page) + '/' + str(len(pages)) + '):\n\n' + pages[page - 1]
    if page < len(pages):
        body += '\n\nPara ver más, escribí FUENTES ' + str(page + 1) + '.'
    return body, context, scope['revision']


def short_label(value, limit):
    """Whole-word elision avoids cutting Unicode combining/emoji sequences."""
    text = ' '.join(value.split())
    if len(text) <= limit:
        return text
    words = []
    for word in text.split(' '):
        candidate = ' '.join(words + [word])
        if len(candidate) + 1 > limit:
            break
        words.append(word)
    return (' '.join(words).rstrip() + '…') if words else '…'
