"""Focal tests of real manifest loading/validation/projection, without app boot.

Only ORM import boundaries are substituted; these are not RBAC/database tests.
Any database access fails rather than silently accepting an application.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PRESET = 'government-disability-support'


class NoDatabase:
    def __getattr__(self, name):
        raise AssertionError('Preset preview must not touch a database')


def load_subject():
    boundaries = {}
    for name, fields in {
        'extensions': {'db': NoDatabase()},
        'models': {'TenantProfile': types.SimpleNamespace},
        'models_tenant_blueprints': {'TenantBlueprintApplication': NoDatabase()},
    }.items():
        module = types.ModuleType(name)
        module.__dict__.update(fields)
        boundaries[name] = module
    spec = importlib.util.spec_from_file_location('blueprint_focal_subject', ROOT/'services/tenant_blueprints.py')
    subject = importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules', boundaries):
        spec.loader.exec_module(subject)
    return subject


class GovernmentSupportPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.subject = load_subject()

    def tenant(self, **overrides):
        values = dict(id=11, slug='evaluation-a', tipo='gobierno', is_active=True, configuracion={})
        return types.SimpleNamespace(**(values | overrides))

    def test_existing_default_remains_first_and_unchanged(self):
        catalog = self.subject.list_blueprints()
        self.assertEqual([item['id'] for item in catalog['blueprints']], ['government-core', PRESET])
        core, _ = self.subject.load_blueprint('government-core')
        self.assertEqual(core['version'], '1.0.0')
        self.assertIn('Baches y calzada', core['configuration_defaults']['ticket_categories'])

    def test_five_areas_and_eight_shared_modules(self):
        manifest, digest = self.subject.load_blueprint(PRESET)
        self.assertEqual(len(manifest['configuration_defaults']['ticket_categories']), 5)
        self.assertEqual(len(manifest['modules']), 8)
        self.assertEqual(len(digest), 64)
        self.assertIn('Pensión y licencias', manifest['configuration_defaults']['ticket_categories'])

    def test_preset_has_no_regional_identity_or_credentials(self):
        manifest, _ = self.subject.load_blueprint(PRESET)
        encoded = json.dumps(manifest, ensure_ascii=False).lower()
        for forbidden in ('tdf', 'faro', 'rubino', '@', 'https://', 'tierra del fuego'):
            self.assertNotIn(forbidden, encoded)

    def test_loading_is_deterministic_and_returns_an_independent_copy(self):
        original, digest = self.subject.load_blueprint(PRESET)
        original['configuration_defaults']['ticket_categories'].clear()
        second, second_digest = self.subject.load_blueprint(PRESET)
        self.assertEqual(digest, second_digest)
        self.assertEqual(len(second['configuration_defaults']['ticket_categories']), 5)

    def test_preset_cannot_enable_channels_or_certify_accessibility(self):
        manifest, _ = self.subject.load_blueprint(PRESET)
        for channel in manifest['configuration_defaults']['channels'].values():
            self.assertIs(channel['enabled'], False)
        self.assertEqual(manifest['configuration_defaults']['accessibility_policy']['status'], 'validation_required')
        self.assertEqual(manifest['configuration_defaults']['employee_routing']['default_ticket_categories'], [])

    def test_preview_scopes_by_tenant_without_mutation(self):
        tenant = self.tenant(configuracion={'private': {'secret': 'not-for-preview'}})
        before = deepcopy(tenant.configuracion)
        result = self.subject.preview_blueprint(tenant, PRESET)
        self.assertEqual(result['tenant']['slug'], 'evaluation-a')
        self.assertEqual(result['changes']['namespace'], 'government_disability_support')
        self.assertFalse(result['write_performed'])
        self.assertFalse(result['runtime_activation_performed'])
        self.assertNotIn('not-for-preview', json.dumps(result))
        self.assertEqual(tenant.configuracion, before)

    def test_other_configuration_and_existing_values_are_preserved(self):
        source = {'government_core': {'custom': True}, 'government_disability_support': {'ticket_categories': ['Approved local taxonomy']}}
        tenant = self.tenant(configuracion=deepcopy(source))
        manifest, digest = self.subject.load_blueprint(PRESET)
        result = self.subject._build_projection(tenant, manifest, digest)
        self.assertEqual(result['merged_config']['government_core'], source['government_core'])
        self.assertEqual(result['merged_config']['government_disability_support']['ticket_categories'], ['Approved local taxonomy'])
        self.assertEqual(tenant.configuracion, source)

    def test_second_projection_has_no_additional_changes(self):
        tenant = self.tenant()
        manifest, digest = self.subject.load_blueprint(PRESET)
        projected = self.subject._build_projection(tenant, manifest, digest)
        result = self.subject.preview_blueprint(self.tenant(configuracion=projected['merged_config']), PRESET)
        self.assertEqual(result['changes']['apply_count'], 0)

    def test_inactive_and_business_tenants_are_rejected(self):
        for tenant in (self.tenant(is_active=False), self.tenant(tipo='pyme')):
            with self.subTest(tenant=tenant.tipo), self.assertRaises(self.subject.TenantBlueprintError):
                self.subject.preview_blueprint(tenant, PRESET)

    def test_existing_invalid_namespace_is_not_overwritten(self):
        with self.assertRaises(self.subject.TenantBlueprintError):
            self.subject.preview_blueprint(self.tenant(configuracion={'government_disability_support': 'legacy'}), PRESET)

    def test_unsafe_extensions_fail_closed(self):
        manifest, _ = self.subject.load_blueprint(PRESET)
        for key in ('password', 'capabilities', 'domain', 'sender_id'):
            bad = deepcopy(manifest)
            bad['configuration_defaults'][key] = 'prohibited'
            with self.subTest(key=key), self.assertRaises(self.subject.TenantBlueprintError):
                self.subject._validate_manifest(bad, expected_blueprint_id=PRESET)

    def test_manifest_registry_cannot_read_arbitrary_paths(self):
        for identifier in ('../government-core', 'missing', '/tmp/preset'):
            with self.subTest(identifier=identifier), self.assertRaises(self.subject.TenantBlueprintError):
                self.subject.load_blueprint(identifier)

if __name__ == '__main__':
    unittest.main()
