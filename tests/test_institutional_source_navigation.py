"""Real ORM/routes/shared channel adapter; identities and selector are synthetic.

No provider delivery, source-byte availability or browser acceptance is asserted.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from models import TenantConfig, TenantProfile, User, db
from services.auth_session_lifecycle import issue_token
from services.institutional_assistant_content import digest, normalize_bundle
from tests.test_institutional_assistant_content import sample


QUESTION = '¿Dónde puedo ver los documentos que respaldan esta información?'


@pytest.fixture
def navigation_context(client):
    assert str(db.engine.url) == 'sqlite:///:memory:'
    records = []
    for suffix in ('a', 'b'):
        owner = User(name='Synthetic source owner ' + suffix,
                     email='source-owner-' + suffix + '@example.invalid',
                     rol='admin', tipo_chat='municipio')
        owner.set_password('synthetic-source-navigation-only')
        db.session.add(owner); db.session.flush()
        tenant = TenantProfile(slug='source-navigation-' + suffix, nombre='Synthetic source ' + suffix,
                               tipo='municipio', municipio_id=owner.id, is_active=True)
        db.session.add(tenant); db.session.flush()
        owner.tenant_id, owner.tenant_slug, owner.municipio_id = tenant.id, tenant.slug, owner.id
        raw = sample(tenant.id, tenant.slug)
        raw['sources']['a'].update(document_visibility='private',
            official_url='https://example.invalid/private-document.pdf',
            origin_url='https://drive.google.com/file/d/private-fixture/view')
        raw['sources']['b'] = {'title': 'Public source ' + suffix, 'sha256': 'b' * 64,
            'page_count': 1, 'document_visibility': 'public',
            'official_url': 'https://example.invalid/public-source-' + suffix + '.pdf'}
        bundle = normalize_bundle(raw, tenant.id, tenant.slug)
        state = {'bundle': bundle, 'bundle_hash': digest(bundle), 'generation': 1, 'visibility': 'public'}
        state['revision'] = digest({key: state[key] for key in ('bundle_hash', 'generation', 'visibility')})
        row = TenantConfig(tenant_id=tenant.id, key='institutional_assistant', channel='knowledge', json_value=state)
        db.session.add(row)
        records.append({'owner': owner, 'tenant': tenant, 'state': state, 'row': row})
    db.session.commit()
    return records


def _url(record, public=True):
    return '/api/' + ('public' if public else 'admin') + '/tenants/' + record['tenant'].slug + '/institutional-assistant'


def _headers(record):
    actor = record['owner']
    token = issue_token({'user_id': actor.id, 'rol': actor.rol, 'tipo_chat': actor.tipo_chat,
                         'exp': datetime.now(timezone.utc) + timedelta(minutes=5)})
    return {'Authorization': 'Bearer ' + (token.decode() if isinstance(token, bytes) else token)}


def test_explicit_source_navigation_uses_authorized_catalog_without_selection(client, navigation_context, monkeypatch):
    own, _ = navigation_context
    selected = []
    def selector(*args):
        selected.append(args)
        return {'node_ids': []}
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', selector)
    workspace = client.get(_url(own)).get_json()
    response = client.post(_url(own) + '/answer', json={'revision': own['state']['revision'], 'question': QUESTION})
    assert response.status_code == 200
    data = response.get_json()
    assert data.get('result_type') == 'source_navigation', data
    assert data['nodes'] == [] and data['sources'] == workspace['knowledge']['sources']
    assert data['tenant'] == {'id': own['tenant'].id, 'slug': own['tenant'].slug}
    assert data['revision'] == workspace['revision']
    assert data['selection_performed'] is False and data['business_writes_performed'] is False
    assert selected == []
    assert 'origin_url' not in str(data) and 'private-document.pdf' not in str(data)
    assert all('pages' not in source and 'excerpts' not in source for source in data['sources'])


@pytest.mark.parametrize('question', ['Ver fuentes', 'Mostrame las fuentes de respaldo',
    '¿Cómo puedo consultar los documentos de respaldo?', 'Documentos y fuentes',
    'FUENTES', 'documentos de respaldo'])
def test_explicit_navigation_aliases_do_not_invoke_the_selector(client, navigation_context, monkeypatch, question):
    own, _ = navigation_context
    def forbidden(*args):
        raise AssertionError('Explicit catalog navigation must not select or invent nodes')
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', forbidden)
    response = client.post(_url(own) + '/answer', json={'revision': own['state']['revision'], 'question': question})
    assert response.status_code == 200 and response.get_json()['result_type'] == 'source_navigation'


def test_procedure_requirements_still_use_canonical_selection(client, navigation_context, monkeypatch):
    own, _ = navigation_context
    selected = []
    def selector(instructions, request):
        selected.append(request)
        return {'node_ids': ['requirements']}
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', selector)
    question = '¿Qué documentos tengo que presentar para el CUD?'
    response = client.post(_url(own) + '/answer', json={'revision': own['state']['revision'], 'question': question})
    data = response.get_json()
    assert response.status_code == 200 and len(selected) == 1 and question in selected[0]
    assert 'result_type' not in data and 'sources' not in data
    assert data['selection_performed'] is True and data['nodes'][0]['id'] == 'requirements'
    assert data['nodes'][0]['sources'][0]['pages'] == [2]


def test_uncovered_question_and_canonical_get_keep_their_existing_contract(client, navigation_context, monkeypatch):
    from services.institutional_assistant import UI
    own, _ = navigation_context
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', lambda *args: {'node_ids': []})
    unknown = client.post(_url(own) + '/answer', json={'revision': own['state']['revision'], 'question': 'Tema inexistente'}).get_json()
    assert unknown['nodes'] == [] and unknown['text'] == UI['unknown'] and unknown['selection_performed'] is True
    assert 'result_type' not in unknown and 'sources' not in unknown
    canonical = client.get(_url(own) + '/nodes/requirements', query_string={'revision': own['state']['revision']})
    assert canonical.status_code == 200 and canonical.get_json()['nodes'][0]['id'] == 'requirements'
    assert canonical.get_json()['selection_performed'] is False and 'result_type' not in canonical.get_json()


def test_private_catalog_preserves_authenticated_projection_and_scope(client, navigation_context):
    own, foreign = navigation_context
    headers = _headers(own)
    workspace = client.get(_url(own, False), headers=headers).get_json()
    response = client.post(_url(own, False) + '/answer', headers=headers,
        json={'revision': own['state']['revision'], 'question': QUESTION})
    assert response.status_code == 200
    assert response.get_json()['sources'] == workspace['knowledge']['sources']
    # This authenticated projection is the pre-existing management contract;
    # public/WhatsApp never receive its private original or private URL.
    assert response.get_json()['sources'][0]['origin_url'].startswith('https://drive.google.com/')
    assert client.post(_url(own, False) + '/answer', headers=_headers(foreign),
        json={'revision': own['state']['revision'], 'question': QUESTION}).status_code == 403
    assert client.post(_url(own) + '/answer', headers={'X-Tenant-Slug': foreign['tenant'].slug},
        json={'revision': own['state']['revision'], 'question': QUESTION}).status_code == 400
    assert client.post(_url(own) + '/answer',
        json={'revision': foreign['state']['revision'], 'question': QUESTION}).status_code == 412


def test_source_navigation_does_not_publish_a_private_corpus(client, navigation_context):
    own, _ = navigation_context
    state = deepcopy(own['state']); state.update(visibility='private', generation=2)
    state['revision'] = digest({key: state[key] for key in ('bundle_hash', 'generation', 'visibility')})
    own['row'].json_value = state; db.session.commit()
    response = client.post(_url(own) + '/answer', json={'revision': state['revision'], 'question': QUESTION})
    assert response.status_code == 404 and response.get_json() == {'reason_code': 'knowledge_not_available'}


@pytest.mark.parametrize('change', ['revision', 'visibility', 'inactive'])
def test_source_navigation_revalidates_persisted_state_before_return(client, navigation_context, monkeypatch, change):
    import services.institutional_assistant as shared
    own, _ = navigation_context
    original = shared.is_source_navigation
    def retire_during_command(question):
        matched = original(question)
        if change == 'inactive':
            own['tenant'].is_active = False
        else:
            state = deepcopy(own['state']); state['generation'] += 1
            if change == 'visibility': state['visibility'] = 'private'
            state['revision'] = digest({key: state[key] for key in ('bundle_hash', 'generation', 'visibility')})
            own['row'].json_value = state
        db.session.commit()
        return matched
    monkeypatch.setattr(shared, 'is_source_navigation', retire_during_command)
    response = client.post(_url(own) + '/answer', json={'revision': own['state']['revision'], 'question': QUESTION})
    assert response.status_code == (412 if change == 'revision' else 404)
    assert 'sources' not in response.get_json()


@pytest.mark.parametrize('change', ['role', 'disabled', 'session'])
def test_private_navigation_revalidates_actor_and_session(client, navigation_context, monkeypatch, change):
    import services.institutional_assistant as shared
    from utils.auth_helpers import bump_auth_session_version
    own, _ = navigation_context
    headers = _headers(own)
    original = shared.is_source_navigation
    def revoke_during_command(question):
        matched = original(question)
        if change == 'role': own['owner'].rol = 'ciudadano'
        elif change == 'disabled': own['owner'].accesibilidad = {'auth': {'disabled': True}}
        else: bump_auth_session_version(own['owner'])
        db.session.commit()
        return matched
    monkeypatch.setattr(shared, 'is_source_navigation', revoke_during_command)
    response = client.post(_url(own, False) + '/answer', headers=headers,
        json={'revision': own['state']['revision'], 'question': QUESTION})
    assert response.status_code == 403 and response.get_json() == {'reason_code': 'knowledge_forbidden'}


def test_whatsapp_adapter_and_formatter_use_catalog_without_fake_nodes(client, navigation_context, monkeypatch):
    from types import SimpleNamespace
    from services.institutional_assistant import maybe_handle_institutional_question, UI
    from services.response_formatter import build_interactive_response
    own, _ = navigation_context
    before = {'institutional_knowledge': {'revision': own['state']['revision'], 'node_id': 'requirements',
        'reply_node_id': 'requirements', 'reply_choices': {'9': 'start'}}}
    session = SimpleNamespace(tenant_id=own['tenant'].id, context_data=deepcopy(before))
    def forbidden(*args): raise AssertionError('Navigation must not select nodes')
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', forbidden)
    result = maybe_handle_institutional_question(QUESTION, own['owner'], session)
    assert result['fuente'] == 'institutional_knowledge_sources'
    assert result['knowledge_tenant'] == {'id': own['tenant'].id, 'slug': own['tenant'].slug}
    assert result['context_revision'] == own['state']['revision']
    assert result['knowledge_sources'] == client.get(_url(own)).get_json()['knowledge']['sources']
    assert result['knowledge_ui'] == {key: UI[key] for key in ('sources', 'source_details', 'large_text')}
    assert 'knowledge_nodes' not in result and 'knowledge_audio_reading' not in result
    assert session.context_data == before
    assert 'private-document.pdf' not in str(result) and 'origin_url' not in str(result)
    formatted = build_interactive_response(options=[], body_text=result['message_body'], channel='whatsapp',
        message_type='text', original_bot_response=deepcopy(result))
    assert formatted['type'] == 'text' and result['message_body'] in formatted['text']['body']
    assert formatted['contexto_actualizado']['last_options_scope'] == 'institutional_knowledge'


def test_navigation_does_not_intercept_an_active_operation_or_foreign_session(client, navigation_context, monkeypatch):
    from types import SimpleNamespace
    from services.constants import CONTEXTO_MUNICIPIO
    from services.institutional_assistant import maybe_handle_institutional_question
    own, foreign = navigation_context
    def forbidden(*args): raise AssertionError('An active operation must keep control')
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', forbidden)
    for context in ({CONTEXTO_MUNICIPIO: {'stage': 'location'}}, {'active_ticket_id': 99},
                    {'human_chat_in_progress': True}, {'contexto_pyme_v2': {'step': 'delivery'}}):
        session = SimpleNamespace(tenant_id=own['tenant'].id, context_data=deepcopy(context))
        assert maybe_handle_institutional_question(QUESTION, own['owner'], session) is None
        assert session.context_data == context
    foreign_session = SimpleNamespace(tenant_id=foreign['tenant'].id, context_data={})
    assert maybe_handle_institutional_question(QUESTION, own['owner'], foreign_session) is None


def test_large_whatsapp_catalog_has_bounded_text_and_full_authorized_metadata(client, navigation_context):
    from services.institutional_assistant import maybe_handle_institutional_question
    own, _ = navigation_context
    state = deepcopy(own['state'])
    raw = sample(own['tenant'].id, own['tenant'].slug)
    raw['sources'] = {('a' if index == 0 else 'source' + str(index)): {
        'sha256': 'b' * 64, 'page_count': 3, 'document_visibility': 'public',
        'title': ('Synthetic source ' + str(index)).ljust(250, 'x'),
        'official_url': 'https://example.invalid/' + 'a' * 1600} for index in range(64)}
    state['bundle'] = normalize_bundle(raw, own['tenant'].id, own['tenant'].slug)
    state['bundle_hash'] = digest(state['bundle'])
    state['revision'] = digest({key: state[key] for key in ('bundle_hash', 'generation', 'visibility')})
    own['row'].json_value = state; db.session.commit()
    result = maybe_handle_institutional_question(QUESTION, own['owner'])
    assert result['fuente'] == 'institutional_knowledge_sources'
    assert len(result['knowledge_sources']) == 64 and len(result['message_body']) <= 3500
    assert '56 fuentes más' in result['message_body'] and 'knowledge_nodes' not in result
