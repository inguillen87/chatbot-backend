from utils.response_utils import normalize_response_payload


def test_institutional_metadata_survives_button_normalization_and_replay():
    from copy import deepcopy
    metadata = {'knowledge_tenant': {'id': 701, 'slug': 'qa-knowledge'},
                'knowledge_nodes': [{'id': 'start', 'title': 'Inicio', 'text': 'Elegí una consulta.',
                    'actions': [{'code': '1', 'label': 'Requisitos', 'target': 'requirements'}],
                    'sources': [{'id': 'source-a', 'title': 'Documento', 'document_visibility': 'private'}]}],
                'knowledge_sources': [{'id': 'source-a', 'title': 'Documento', 'document_visibility': 'private'}]}
    payload = {'fuente': 'institutional_knowledge', 'context_revision': 'a' * 64,
               'message_body': 'Elegí una consulta.', **deepcopy(metadata),
               'botones': [{'texto': 'Requisitos', 'action_id': 'knowledge:' + 'a' * 16 + ':requirements'}]}
    for _ in range(2):
        normalize_response_payload(payload)
        for key, expected in metadata.items():
            assert payload[key] == expected
        assert payload['botones'][0]['id'] == 'knowledge:' + 'a' * 16 + ':requirements'
        assert payload['botones'][0]['id_accion'] == payload['botones'][0]['action_id']
        assert payload['messages'] == [{'role': 'assistant', 'content': 'Elegí una consulta.'}]


def test_other_responses_keep_existing_recursive_button_compatibility():
    payload = {'fuente': 'operational', 'knowledge_nodes': [{'id': 'existing-option', 'label': 'Opción'}]}
    normalize_response_payload(payload)
    assert payload['knowledge_nodes'][0]['action_id'] == 'existing-option'
    assert payload['knowledge_nodes'][0]['texto'] == 'Opción'


def test_normalize_response_payload_maps_legacy_fields():
    payload = {
        "message_to_user": "Hola",
        "botones": [{"texto": "Opción"}],
        "pedir_info": "email",
        "success": False,
    }

    normalized = normalize_response_payload(payload)

    assert normalized["message_body"] == "Hola"
    assert normalized["options_list"][0]["texto"] == "Opción"
    assert normalized["message_type"] == "interactive_buttons"
    assert normalized["success"] is True
