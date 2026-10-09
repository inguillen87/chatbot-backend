"""Producer contract tests without app bootstrap, .env, database or providers.

Compile the actual presentation functions with in-memory persistence boundaries.
This tests returned payloads without treating database fixtures as UI evidence.
"""
import ast
from copy import deepcopy
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from services.institutional_assistant_content import (
    CONTRACT, ContentError, materialize_node, normalize_bundle, overview,
)
from tests.test_institutional_assistant_content import sample


def producer_functions():
    path = Path(__file__).resolve().parents[1] / 'services' / 'institutional_assistant.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    constants = {'UI', '_CHANNEL_UI_KEYS'}
    functions = {'workspace', '_channel_source_label', 'maybe_handle_institutional_question'}
    selected = [item for item in tree.body if (
        isinstance(item, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants for target in item.targets)
        or isinstance(item, ast.FunctionDef) and item.name in functions)]
    namespace = dict(deepcopy=deepcopy, CONTRACT=CONTRACT, ContentError=ContentError,
                     materialize_node=materialize_node, overview=overview,
                     _active_operational_context=lambda context: False)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class InstitutionalAssistantUIContractTests(unittest.TestCase):
    def setUp(self):
        self.service = producer_functions()
        self.tenant = SimpleNamespace(id=701, slug='qa-knowledge', nombre='Institución de prueba',
                                      tipo='municipio', municipio_id=11, pyme_id=None)
        self.owner = SimpleNamespace(id=11, tenant_id=701)
        data = sample()
        data['sources']['a']['document_visibility'] = 'private'
        data['sources']['a']['origin_url'] = 'https://example.invalid/reserved-original'
        self.bundle = normalize_bundle(data, self.tenant.id, self.tenant.slug)
        self.state = dict(bundle=self.bundle, revision='b' * 64, visibility='public')
        self.service['read_state'] = Mock(return_value=self.state)
        self.service['db'] = SimpleNamespace(session=SimpleNamespace(get=Mock(return_value=self.tenant)))
        self.service['TenantProfile'] = object()

    def respond(self, nodes, question='menú'):
        self.service['answer'] = Mock(return_value={
            'tenant': {'id': self.tenant.id, 'slug': self.tenant.slug},
            'nodes': deepcopy(nodes), 'text': '\n\n'.join(node['text'] for node in nodes)})
        return self.service['maybe_handle_institutional_question'](question, self.owner)

    def test_workspace_supplies_navigation_without_changing_knowledge_or_revision(self):
        before = deepcopy(self.state)
        for public in (False, True):
            with self.subTest(public=public):
                result = self.service['workspace'](self.tenant, self.state, public=public)
                for key in self.service['_CHANNEL_UI_KEYS']:
                    self.assertEqual(result['ui'][key], self.service['UI'][key])
                self.assertEqual(result['knowledge']['initial'], materialize_node(self.bundle['nodes']['start'], public=public))
                self.assertEqual(result['revision'], self.state['revision'])
        self.assertEqual(self.state, before)
        self.service['db'].session.get.assert_not_called()

    def test_empty_workspace_still_supplies_ui_without_creating_knowledge(self):
        result = self.service['workspace'](self.tenant, None)
        self.assertEqual(result['visibility'], 'empty')
        self.assertIsNone(result['knowledge'])
        self.assertIsNone(result['revision'])
        self.assertEqual(result['ui']['more_options'], 'Más opciones')

    def test_labels_are_bounded_nonempty_and_page_has_only_current_and_total(self):
        for key in self.service['_CHANNEL_UI_KEYS']:
            with self.subTest(key=key):
                value = self.service['UI'][key]
                self.assertIsInstance(value, str)
                self.assertEqual(value, value.strip())
                self.assertGreater(len(value), 0)
                self.assertLessEqual(len(value), 200)
        page = self.service['UI']['options_page']
        fields = [field for _, field, _, _ in Formatter().parse(page) if field is not None]
        self.assertCountEqual(fields, ['current', 'total'])
        self.assertEqual(page.format(current=2, total=5), 'Opciones: grupo 2 de 5')

    def test_widget_gets_only_safe_ui_and_preserves_canonical_choices_and_private_sources(self):
        node = materialize_node(self.bundle['nodes']['start'], public=True)
        before = deepcopy(node)
        result = self.respond([node])
        self.assertEqual(set(result['knowledge_ui']), set(self.service['_CHANNEL_UI_KEYS']))
        self.assertEqual(result['knowledge_ui']['large_text'], self.service['UI']['large_text'])
        self.assertEqual(result['knowledge_ui']['source_details'], self.service['UI']['source_details'])
        self.assertNotIn('import', result['knowledge_ui'])
        self.assertEqual(result['knowledge_nodes'], [node])
        self.assertEqual(result['knowledge_sources'], node['sources'])
        self.assertNotIn('reserved-original', str(result))
        self.assertEqual(result['botones'], [{'texto': 'Requisitos', 'action_id': 'knowledge:' + 'b' * 16 + ':requirements', 'reply_code': '1'}])
        self.assertEqual(result['context_revision'], self.state['revision'])
        self.assertEqual(node, before)

    def test_multinode_response_retains_all_choices_and_evidence_for_old_clients(self):
        nodes = []
        for index in range(3):
            node = materialize_node(self.bundle['nodes']['start'], public=True)
            node.update(id=f'node-{index}', text=f'Texto original {index}.')
            node['actions'] = [dict(code=str(choice + 1), label=f'Opción {index}-{choice}',
                                    target=f'destination-{index}-{choice}') for choice in range(6)]
            nodes.append(node)
        before = deepcopy(nodes)
        result = self.respond(nodes, question='consulta de varios temas')
        self.assertEqual(len(result['botones']), 18)
        self.assertTrue(all('reply_code' not in choice for choice in result['botones']))
        self.assertEqual([choice['texto'] for choice in result['botones']], [choice['label'] for node in nodes for choice in node['actions']])
        self.assertEqual(result['knowledge_nodes'], before)
        self.assertEqual(result['knowledge_sources'], [source for node in before for source in node['sources']])
        self.assertTrue(result['message_body'].startswith('\n\n'.join(node['text'] for node in nodes)))
        self.assertEqual(nodes, before)

    def test_each_response_ui_is_independent_and_noninstitutional_fallback_is_unchanged(self):
        node = materialize_node(self.bundle['nodes']['start'], public=True)
        first = self.respond([node])
        first['knowledge_ui']['more_options'] = 'Changed only in client data'
        next_result = self.respond([node])
        self.assertEqual(next_result['knowledge_ui']['more_options'], 'Más opciones')
        workspace = self.service['workspace'](self.tenant, self.state)
        workspace['ui']['more_options'] = 'Changed only in client data'
        self.assertEqual(self.service['UI']['more_options'], 'Más opciones')
        self.assertIsNone(self.service['maybe_handle_institutional_question']({'action_id': 'iniciar_reclamo'}, self.owner))
        self.service['answer'].assert_called_once()


if __name__ == '__main__':
    unittest.main()
