"""Actual UI/workspace producer contract, without app/HTTP/database imports."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

REVIEW_KEYS = ('import_review_current_version', 'import_review_proposed_version',
    'import_review_empty_current', 'import_review_destination',
    'import_review_declared_tenant', 'import_review_declared_sources',
    'import_review_evaluation_only', 'import_review_approval_status',
    'import_review_not_reported', 'import_review_yes', 'import_review_no',
    'import_review_notice')

def producer_functions():
    directory = Path(__file__).resolve().parents[1] / 'services'
    source = directory / 'institutional_assistant.py'
    tree = ast.parse(source.read_text(encoding='utf8'), filename=str(source))
    selected = [item for item in tree.body if (
        isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'UI' for target in item.targets)
        or isinstance(item, ast.FunctionDef) and item.name == 'workspace')]
    content = directory / 'institutional_assistant_content.py'
    constant = [item for item in ast.parse(content.read_text(encoding='utf8')).body if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'CONTRACT' for target in item.targets)]
    assert len(selected) == 2 and len(constant) == 1
    namespace = {'deepcopy': deepcopy}
    exec(compile(ast.Module(body=constant + selected, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace

def emitted_review_ui():
    producer = producer_functions()
    tenant = SimpleNamespace(id=701, slug='qa-knowledge', nombre='Institución sintética')
    payload = producer['workspace'](tenant, None)
    return {key: payload['ui'][key] for key in REVIEW_KEYS}

class InstitutionalImportReviewUITests(unittest.TestCase):
    def test_empty_workspace_supplies_complete_bounded_optional_review_copy(self):
        producer = producer_functions()
        tenant = SimpleNamespace(id=701, slug='qa-knowledge', nombre='Institución sintética')
        payload = producer['workspace'](tenant, None, editable=True)
        for key in REVIEW_KEYS:
            with self.subTest(key=key):
                value = payload['ui'][key]
                self.assertIsInstance(value, str)
                self.assertEqual(value, value.strip())
                self.assertGreater(len(value), 0)
                self.assertLessEqual(len(value), 1000)
        self.assertIsNone(payload['knowledge'])
        self.assertIsNone(payload['revision'])
        self.assertEqual(payload['visibility'], 'empty')
        self.assertEqual(payload['tenant'], {'id':701, 'slug':'qa-knowledge', 'name':'Institución sintética'})

    def test_workspace_ui_copies_are_independent_across_tenants(self):
        producer = producer_functions()
        first = producer['workspace'](SimpleNamespace(id=701, slug='qa-a', nombre='A'), None)
        second = producer['workspace'](SimpleNamespace(id=702, slug='qa-b', nombre='B'), None)
        expected = deepcopy(second['ui'])
        for key in REVIEW_KEYS:
            first['ui'][key] = 'Changed only in synthetic response A'
        self.assertEqual(second['ui'], expected)
        self.assertEqual(producer['UI'], expected)
        self.assertEqual(producer['workspace'](SimpleNamespace(id=702, slug='qa-b', nombre='B'), None)['ui'], expected)
        self.assertEqual(first['tenant']['slug'], 'qa-a')
        self.assertEqual(second['tenant']['slug'], 'qa-b')

    def test_review_ui_uses_backend_values_without_local_or_tenant_specialization(self):
        producer = producer_functions()
        for key in REVIEW_KEYS:
            producer['UI'][key] = 'SERVER COPY ' + key
        a = producer['workspace'](SimpleNamespace(id=701, slug='qa-a', nombre='A'), None)
        b = producer['workspace'](SimpleNamespace(id=702, slug='qa-b', nombre='B'), None)
        for key in REVIEW_KEYS:
            self.assertEqual(a['ui'][key], 'SERVER COPY ' + key)
            self.assertEqual(b['ui'][key], a['ui'][key])

if __name__ == '__main__':
    unittest.main()
