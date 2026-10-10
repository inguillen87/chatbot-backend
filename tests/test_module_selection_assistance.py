"""Additive copy contract; existing storage/authorization remains authoritative."""
import json
from types import SimpleNamespace
import unittest
from services.organization_modules import (
    SELECTION_ASSISTANCE, KEY, build_module_selection, normalized_selection, ModuleSelectionError,
)

class SelectionAssistanceTests(unittest.TestCase):
    def tenant(self, kind='municipio'):
        return SimpleNamespace(id=1, slug='tenant-a', tipo=kind, is_active=True, configuracion={})

    def test_copy_is_scoped_to_the_snapshot_without_materializing_defaults(self):
        tenant = self.tenant()
        result = build_module_selection(tenant, can_edit=True, entitled=True)
        self.assertEqual(result['selection_assistance'], SELECTION_ASSISTANCE)
        self.assertEqual(result['version'], 0)
        self.assertEqual(tenant.configuracion, {})
        self.assertFalse(result['provider_calls_performed'])
        self.assertFalse(result['changes_runtime_access'])

    def test_assistance_is_not_edit_permission(self):
        for options in ({}, {'can_edit':True}, {'entitled':True},
                        {'can_edit':True,'entitled':True,'writes_blocked':True}):
            with self.subTest(options=options):
                result = build_module_selection(self.tenant(), **options)
                self.assertFalse(result['can_edit'])
                self.assertEqual(result['selection_assistance'], SELECTION_ASSISTANCE)

    def test_response_copy_mutation_cannot_affect_other_organizations(self):
        first = build_module_selection(self.tenant(), can_edit=True, entitled=True)
        first['selection_assistance']['detail'] = 'changed by consumer'
        second = build_module_selection(self.tenant('empresa'), can_edit=True, entitled=True)
        self.assertEqual(second['selection_assistance'], SELECTION_ASSISTANCE)
        self.assertNotIn('territory', [module['id'] for module in second['catalog']])

    def test_incomplete_selection_remains_rejected_on_server(self):
        result = build_module_selection(self.tenant(), can_edit=True, entitled=True)
        with self.assertRaises(ModuleSelectionError):
            normalized_selection(['payments'], result['catalog'])
        self.assertEqual(normalized_selection(['payments','catalog'], result['catalog']), ['catalog','payments'])

    def test_copy_does_not_change_persisted_record_or_revision(self):
        tenant = self.tenant()
        tenant.configuracion = {KEY:{'catalog_version':1,'version':4,'selected':['catalog','payments']}}
        before = json.dumps(tenant.configuracion, sort_keys=True)
        first = build_module_selection(tenant, can_edit=True, entitled=True)
        readonly = build_module_selection(tenant, can_edit=False, entitled=False)
        self.assertEqual(first['revision'], readonly['revision'])
        self.assertEqual(json.dumps(tenant.configuracion, sort_keys=True), before)
        self.assertEqual(first['selected'], ['catalog','payments'])

    def test_all_published_copy_is_bounded_and_serializable(self):
        self.assertEqual(SELECTION_ASSISTANCE['contract_version'], 'organization.setup_module_assistance.v1')
        for value in SELECTION_ASSISTANCE.values():
            self.assertIsInstance(value, str)
            self.assertTrue(value.strip())
            self.assertLessEqual(len(value), 1600)
            self.assertFalse(any(ord(c)<32 or 127<=ord(c)<=159 for c in value))
        json.dumps(SELECTION_ASSISTANCE, allow_nan=False)

if __name__ == '__main__':
    unittest.main()
