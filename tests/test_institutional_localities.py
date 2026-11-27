import json
import pathlib

import pytest

from services.institutional_assistant_content import (
    ContentError, MAX_QUESTION_CHARS, locality_question, normalize_bundle, overview)

PACK = pathlib.Path(__file__).parent / 'fixtures'


def _raw():
    source = {'id': 'doc', 'sha256': 'a' * 64, 'title': 'Guía', 'page_count': 1}
    def node(key, actions=()):
        return {'id': key, 'title': key.title(), 'text': 'Texto de ' + key, 'actions': list(actions)}
    return {
        'contract_version': None, 'tenant': {'id': 7, 'slug': 'demo'}, 'version': 'v1', 'start': 'inicio',
        'sources': {'doc': source},
        'nodes': {'inicio': node('inicio', [{'code': '1', 'label': 'Norte', 'target': 'norte'},
                                            {'code': '2', 'label': 'Sur', 'target': 'sur'}]),
                  'norte': node('norte'), 'sur': node('sur')},
        'node_evidence': {k: [{'source_id': 'doc', 'page': 1}] for k in ('inicio', 'norte', 'sur')},
        'policy': {k: False for k in ('accepts_personal_data', 'creates_real_cases', 'queries_official_records',
                                      'sends_notifications', 'stores_feedback')},
    }


def _bundle(**extra):
    from services.institutional_assistant_content import BUNDLE_CONTRACT
    raw = {**_raw(), 'contract_version': BUNDLE_CONTRACT, **extra}
    return normalize_bundle(raw, 7, 'demo')


def test_bundle_without_localities_publishes_an_empty_list():
    bundle = _bundle()
    assert 'localities' not in bundle
    assert overview(bundle, public=True)['localities'] == []


def test_localities_are_published_with_their_topic():
    places = [{'label': 'Norte', 'target': 'norte'}, {'label': 'Sur', 'target': 'sur'}]
    assert overview(_bundle(localities=places), public=True)['localities'] == places


@pytest.mark.parametrize('places', [
    [{'label': 'Norte', 'target': 'norte'}],
    [{'label': 'Norte', 'target': 'norte'}, {'label': 'norte', 'target': 'sur'}],
    [{'label': 'Norte', 'target': 'norte'}, {'label': 'Sur', 'target': 'no-existe'}],
    'Norte',
])
def test_invalid_localities_are_rejected(places):
    with pytest.raises(ContentError):
        _bundle(localities=places)


def test_selector_question_names_the_chosen_place_only_when_published():
    bundle = _bundle(localities=[{'label': 'Norte', 'target': 'norte'}, {'label': 'Sur', 'target': 'sur'}])
    assert locality_question(bundle, '¿Dónde hago el trámite?', 'sur') == '¿Dónde hago el trámite? (consulta desde Sur)'
    assert locality_question(bundle, '¿Dónde hago el trámite?', 'Oeste') == '¿Dónde hago el trámite?'
    assert locality_question(bundle, '¿Dónde hago el trámite?', None) == '¿Dónde hago el trámite?'
    assert locality_question(bundle, 'Trámite en el sur', 'Sur') == 'Trámite en el sur'
    long = 'a' * MAX_QUESTION_CHARS
    assert locality_question(bundle, long, 'Sur') == long
    assert locality_question(_bundle(), 'Hola', 'Sur') == 'Hola'


def test_conversation_locality_prefers_message_then_opened_topic_then_memory():
    from services.institutional_assistant_content import conversation_locality
    bundle = _bundle(localities=[{'label': 'Norte', 'target': 'norte'}, {'label': 'Río Sur', 'target': 'sur'}])
    assert conversation_locality(bundle) is None
    assert conversation_locality(bundle, 'Norte') == 'Norte'
    assert conversation_locality(bundle, 'Oeste') is None
    assert conversation_locality(bundle, 'Norte', text='vivo en rio sur') == 'Río Sur'
    assert conversation_locality(bundle, 'Norte', text='norte o río sur') == 'Norte'
    assert conversation_locality(bundle, None, text='el nortecito') is None
    assert conversation_locality(bundle, 'Norte', node_ids=['sur']) == 'Río Sur'
    assert conversation_locality(bundle, 'Norte', node_ids=['inicio']) == 'Norte'
    assert conversation_locality(_bundle(), 'Norte', text='norte') is None
