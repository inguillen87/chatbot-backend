"""Easy-read answers: bounded command, verified rewrite, cache and public route.

Real ORM and routes with an in-memory database; the rewriter is synthetic and
no provider is called.
"""
import pytest

from models import TenantConfig, TenantProfile, User, db
from services import institutional_assistant_easy_read as easy
from services.institutional_assistant_content import digest, normalize_bundle
from tests.test_institutional_assistant_content import sample

SOURCE_TEXT = ('Para iniciar el trámite del CUD tenés que presentar DNI y estudios médicos '
               'en la oficina de Ushuaia, Gobernador Paz 2360, de lunes a viernes de 9 a 15. '
               'Teléfono 2901-588449. La información es orientativa: confirmá los requisitos '
               'antes de ir. Más datos: https://example.invalid/cud.')
FAITHFUL = ('• Llevá tu DNI y tus estudios médicos.\n'
            '• Andá a la oficina de Ushuaia, en Gobernador Paz 2360.\n'
            '• Atiende de lunes a viernes, de 9 a 15.\n'
            '• Teléfono: 2901-588449.\n'
            'Es una orientación. Confirmá los requisitos del CUD antes de ir. '
            'Más datos: https://example.invalid/cud.')


@pytest.fixture
def published(client, monkeypatch):
    assert str(db.engine.url) == 'sqlite:///:memory:'
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-easy-read-key')
    owner = User(name='Synthetic easy-read owner', email='easy-read-owner@example.invalid',
                 rol='admin', tipo_chat='municipio')
    owner.set_password('synthetic-easy-read-only')
    db.session.add(owner); db.session.flush()
    tenant = TenantProfile(slug='easy-read-org', nombre='Synthetic easy-read org',
                           tipo='municipio', municipio_id=owner.id, is_active=True)
    db.session.add(tenant); db.session.flush()
    raw = sample(tenant.id, tenant.slug)
    raw['nodes']['requirements']['text'] = SOURCE_TEXT
    bundle = normalize_bundle(raw, tenant.id, tenant.slug)
    state = {'bundle': bundle, 'bundle_hash': digest(bundle), 'generation': 1, 'visibility': 'public'}
    state['revision'] = digest({key: state[key] for key in ('bundle_hash', 'generation', 'visibility')})
    db.session.add(TenantConfig(tenant_id=tenant.id, key='institutional_assistant', channel='knowledge', json_value=state))
    db.session.commit()
    easy._cache.clear()
    from extensions import limiter
    limiter.reset()
    return {'tenant': tenant, 'state': state, 'owner': owner}


def _url(record):
    return '/api/public/tenants/' + record['tenant'].slug + '/institutional-assistant'


@pytest.mark.parametrize('text', [
    'Explicámelo más simple', 'explicame mas simple por favor', '¿Me lo podés explicar más simple?',
    'Más simple', 'más fácil', 'lectura fácil', 'en palabras simples', 'Decilo más fácil'])
def test_easy_read_commands(text):
    assert easy.is_easy_read_command(text)


@pytest.mark.parametrize('text', [
    '¿Qué trámite es más simple, el CUD o el CMO?', 'necesito el CUD', 'simple', 'volver al menú',
    'explicame cómo sacar el CUD', '', 'x' * 200])
def test_other_questions_are_not_easy_read_commands(text):
    assert not easy.is_easy_read_command(text)


def test_a_faithful_rewrite_is_accepted():
    source = 'Requisitos\n\n' + SOURCE_TEXT
    assert easy.verified_easy_read(source, {'text': FAITHFUL}) == FAITHFUL


@pytest.mark.parametrize('rewrite', [
    FAITHFUL.replace('2901-588449', '2901-588450'),  # a number not in the source
    FAITHFUL + ' Escribí a ayuda@example.invalid.',  # an invented contact
    FAITHFUL.replace('Ushuaia', 'Tolhuin'),  # a place not in the source
    FAITHFUL + ' Pedí el formulario ANSES.',  # an acronym not in the source
    FAITHFUL.replace('Es una orientación. Confirmá los requisitos del CUD antes de ir. ', ''),  # caveat dropped
    '**' + FAITHFUL + '**',  # markdown
    'Corto.',  # not an answer
    FAITHFUL * 4,  # far longer than the source
])
def test_an_unfaithful_rewrite_is_rejected(rewrite):
    assert easy.verified_easy_read('Requisitos\n\n' + SOURCE_TEXT, {'text': rewrite}) is None


@pytest.mark.parametrize('result', [None, 'texto', {'text': 1}, {'text': FAITHFUL, 'extra': True}])
def test_malformed_rewriter_output_is_rejected(result):
    assert easy.verified_easy_read(SOURCE_TEXT, result) is None


def test_public_route_returns_a_verified_version_once_and_caches_it(client, published, monkeypatch):
    calls = []
    def rewriter(instructions, request):
        calls.append(request)
        return {'text': FAITHFUL}
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', rewriter)
    workspace = client.get(_url(published)).get_json()
    assert workspace['easy_read']['action'] == 'Explicámelo más simple'
    command = {'revision': published['state']['revision'], 'node_id': 'requirements'}
    first = client.post(_url(published) + '/easy-read', json=command)
    second = client.post(_url(published) + '/easy-read', json=command)
    assert first.status_code == 200, first.get_json()
    data = first.get_json()
    assert data['text'] == FAITHFUL and data['node_id'] == 'requirements'
    assert data['contract_version'] == 'chatboc.institutional_easy_read.v1'
    assert data['disclosure'] == easy.EASY_READ['disclosure']
    assert second.get_json() == data and len(calls) == 1
    # Only the canonical title and text reach the rewriter: never sources or evidence.
    assert SOURCE_TEXT in calls[0] and 'Documento de prueba' not in calls[0] and 'PRIVATE' not in calls[0]


def test_unverified_rewrite_is_never_returned(client, published, monkeypatch):
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado',
                        lambda *args: {'text': FAITHFUL.replace('2360', '9999')})
    response = client.post(_url(published) + '/easy-read',
                           json={'revision': published['state']['revision'], 'node_id': 'requirements'})
    assert response.status_code == 422
    assert response.get_json() == {'reason_code': 'knowledge_easy_read_unverified'}


@pytest.mark.parametrize('command, status', [
    ({'revision': 'f' * 64, 'node_id': 'requirements'}, 412),
    ({'node_id': 'requirements'}, 400),
    ({'revision': 'f' * 64, 'node_id': 'requirements', 'question': 'x'}, 400),
])
def test_invalid_or_stale_commands_do_not_call_the_rewriter(client, published, monkeypatch, command, status):
    def forbidden(*args):
        raise AssertionError('no rewrite for an invalid command')
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', forbidden)
    assert client.post(_url(published) + '/easy-read', json=command).status_code == status


def test_unknown_node_is_rejected(client, published, monkeypatch):
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado',
                        lambda *args: pytest.fail('no rewrite for an unknown node'))
    response = client.post(_url(published) + '/easy-read',
                           json={'revision': published['state']['revision'], 'node_id': 'missing'})
    assert response.status_code == 400


def test_capability_requires_the_provider_key(client, published, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY')
    assert 'easy_read' not in client.get(_url(published)).get_json()


def test_typed_command_rereads_the_current_answer_without_selection(client, published, monkeypatch):
    def forbidden(*args):
        raise AssertionError('a reading command must not select other nodes')
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', forbidden)
    response = client.post(_url(published) + '/answer', json={
        'revision': published['state']['revision'], 'node_id': 'requirements', 'question': 'Explicámelo más simple'})
    data = response.get_json()
    assert response.status_code == 200, data
    assert [node['id'] for node in data['nodes']] == ['requirements']
    assert data['easy_read_requested'] is True and data['selection_performed'] is False


def test_chat_channel_answers_the_typed_command_with_the_verified_version(client, published, monkeypatch):
    from services.institutional_assistant import maybe_handle_institutional_question

    class Session:
        tenant_id = published['tenant'].id
        context_data = {'institutional_knowledge': {
            'revision': published['state']['revision'], 'node_id': 'requirements',
            'reply_node_id': 'requirements', 'reply_choices': {'9': 'start'}}}

    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', lambda *args: {'text': FAITHFUL})
    reply = maybe_handle_institutional_question('explicámelo más simple', published['owner'], Session())
    assert reply['message_body'].startswith(FAITHFUL)
    assert easy.EASY_READ['disclosure'] in reply['message_body']
    assert reply['knowledge_easy_read_text'] == FAITHFUL
    assert [button['texto'] for button in reply['botones']] == ['Volver']

    easy._cache.clear()
    monkeypatch.setattr('services.llm_utils.llamar_llm_para_json_estructurado', lambda *args: {'text': 'Corto.'})
    fallback = maybe_handle_institutional_question('más simple', published['owner'], Session())
    assert fallback['message_body'].startswith(easy.EASY_READ['error'])
    assert SOURCE_TEXT in fallback['message_body'] and 'knowledge_easy_read_text' not in fallback
