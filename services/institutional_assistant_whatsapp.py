"""Bounded WhatsApp presentation; canonical app/widget responses stay unchanged."""
from copy import deepcopy
import re

from services.institutional_assistant import UI, answer, read_state
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


def _acronyms(nodes):
    # Acronyms come already resolved from the published version's own
    # definitions (materialize_node); the channel only lists them once.
    values, seen = [], set()
    for node in nodes:
        for item in node.get('acronyms') or []:
            acronym, expansion = item.get('acronym'), item.get('expansion')
            if (isinstance(acronym, str) and isinstance(expansion, str)
                    and acronym.strip() and expansion.strip() and acronym not in seen):
                values.append(acronym.strip() + ': ' + ' '.join(expansion.split()))
                seen.add(acronym)
    return values


def acronyms_block(nodes):
    values = _acronyms(nodes)
    return (UI['acronyms'] + ':\n' + '\n'.join('• ' + value for value in values)) if values else ''


def present_answer_parts(result, context, *, tenant_id, tenant_slug):
    """Separate bounded-channel notices from text, without parsing free prose."""
    value = clear_source_scope(context)
    nodes, revision = result.get('knowledge_nodes'), result.get('context_revision')
    tenant = {'id': tenant_id, 'slug': tenant_slug}
    # An uncovered question that kept the person in the same single topic keeps
    # that topic's reference scope, so FUENTES still lists its public sources.
    scope, current = context.get(SOURCE_SCOPE), context.get('institutional_knowledge')
    if (result.get('fuente') == 'institutional_knowledge_unknown_question'
            and isinstance(scope, dict) and isinstance(current, dict)
            and scope.get('tenant') == tenant and scope.get('revision') == revision
            and current.get('revision') == revision and isinstance(current.get('node_id'), str)
            and scope.get('node_ids') == [current['node_id']] == [current.get('reply_node_id')]):
        value[SOURCE_SCOPE] = deepcopy(scope)
    if (not isinstance(nodes, list) or not 1 <= len(nodes) <= 3
            or result.get('knowledge_tenant') != tenant
            or not isinstance(revision, str) or not _REVISION.fullmatch(revision)):
        return {'text': result['message_body'], 'links': [], 'notices': '', 'acronyms': ''}, value
    value[SOURCE_SCOPE] = {'tenant': tenant, 'revision': revision,
                          'node_ids': [node['id'] for node in nodes]}
    notices = []
    notice = _review_notice(_public_sources(nodes))
    if notice:
        notices.append(notice)
    notices.append('Para consultar las referencias de esta orientación, escribí FUENTES.')
    return {'text': '\n\n'.join(node['text'] for node in nodes), 'links': _links(nodes),
            'notices': '\n\n'.join(notices), 'acronyms': acronyms_block(nodes)}, value


def present_answer(result, context, *, tenant_id, tenant_slug):
    """Remove only automatic source metadata, preserving canonical text/links."""
    parts, value = present_answer_parts(result, context, tenant_id=tenant_id, tenant_slug=tenant_slug)
    return '\n\n'.join(part for part in [parts['text'], parts['acronyms'], *parts['links'], parts['notices']] if part), value


def _source_entries(nodes):
    entries = []
    for source in _public_sources(nodes):
        title = ' '.join(source['title'].split())
        suffix = ''
        notice = _review_notice([source])
        if notice:
            suffix += '\n' + notice
        # Only the normalized official URL of an explicitly public document.
        # delivery metadata alone never promises a downloadable stored file.
        if source.get('url'):
            suffix += '\n' + source['url']
        if not source.get('pages'):
            entries.append(title + suffix)
            continue
        # A document can collect evidence from three nodes. Split only its
        # page list; every group retains the complete title, notice and URL.
        prefix, current = title + ' · páginas ', []
        for page in source['pages']:
            page_text = str(page)
            candidate = ', '.join(current + [page_text])
            if len(prefix + candidate + suffix) > _PAGE_BODY_LIMIT:
                if not current:
                    raise ContentError('knowledge_reference_too_large', 400)
                entries.append(prefix + ', '.join(current) + suffix)
                current = []
                if len(prefix + page_text + suffix) > _PAGE_BODY_LIMIT:
                    raise ContentError('knowledge_reference_too_large', 400)
            current.append(page_text)
        entries.append(prefix + ', '.join(current) + suffix)
    entries.extend(_links(nodes))
    return entries


def _source_pages(entries):
    pages, current = [], []
    for entry in entries:
        if len(entry) > _PAGE_BODY_LIMIT:
            raise ContentError('knowledge_reference_too_large', 400)
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
        pages = _source_pages(_source_entries(nodes))
    except ContentError:
        return _UNAVAILABLE, clear_source_scope(context), None
    if not pages:
        return 'No hay referencias públicas disponibles para esta orientación.', context, scope['revision']
    if not 1 <= page <= len(pages):
        return 'Ese grupo de referencias no está disponible. Escribí FUENTES para ver el primero.', context, scope['revision']
    body = 'Referencias de esta orientación (' + str(page) + '/' + str(len(pages)) + '):\n\n' + pages[page - 1]
    if page < len(pages):
        body += '\n\nPara ver más, escribí FUENTES ' + str(page + 1) + '.'
    return body, context, scope['revision']


# One short symbol before the words (a pictogram, an arrow), never a run of them.
_LEADING_PICTOGRAM = re.compile(r'^[^\w\s¿¡(«"\']{1,4}\s+(?=\S)', re.UNICODE)

def row_title(prefix, label, limit=24):
    """Title of a WhatsApp list row, which holds very few characters.

    A leading pictogram is kept when the whole label fits. When it does not,
    the words matter more than the picture: the title drops the pictogram
    before it drops words. The complete label stays in the row description."""
    text = ' '.join(label.split())
    room = limit - len(prefix)
    if len(text) > room:
        words = _LEADING_PICTOGRAM.sub('', text, count=1)
        if words and words != text:
            text = words
    return prefix + short_label(text, room)

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
