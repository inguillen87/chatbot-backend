from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import unittest
from sqlalchemy.exc import SQLAlchemyError
from tests import test_organization_profile_settings as storage
from services.organization_modules import build_module_selection,save_module_selection,ModuleSelectionError,KEY
from services.organization_setup_journey import build_organization_setup_journey
from services.public_tenant_config import sanitize_public_tenant_config
from tests.test_organization_setup_journey import channels
S,T,U,A=storage.Session,storage.Tenant,storage.User,storage.Audit

class ModuleSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): storage.OrganizationProfileSettingsTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): storage.OrganizationProfileSettingsTests.tearDownClass.__func__(cls)
    def setUp(self): storage.OrganizationProfileSettingsTests.setUp(self)
    def tearDown(self): S.remove()
    def read(self,tenant_id=1): return build_module_selection(S.get(T,tenant_id),can_edit=True,entitled=True)
    def save(self,selected=None,*,revision=None,actor=2,tenant_id=1,entitled=True):
        return save_module_selection(S,T,U,A,tenant_id=tenant_id,actor_id=actor,
            data={'expected_revision':revision or self.initial['revision'],'organization_modules':{'selected':selected if selected is not None else ['catalog','payments']}},
            authorize=storage.allowed,entitlement=lambda tenant:entitled)

    def test_publish_persists_only_setup_preferences_and_audit(self):
        before = deepcopy(S.get(T, 1).configuracion)
        result = self.save()
        self.assertEqual(result['selection']['selected'], ['catalog', 'payments'])
        self.assertEqual(result['selection']['version'], 1)
        self.assertTrue(result['saved'])
        self.assertTrue(S.get(T, 1).configuracion['keep'])
        self.assertEqual(S.query(A).count(), 1)
        self.assertEqual({k: v for k, v in S.get(T, 1).configuracion.items() if k != KEY}, before)
        self.assertFalse(result['changes_runtime_access'])
        self.assertFalse(result['provider_calls_performed'])

    def test_defaults_are_read_only_until_explicit_first_save(self):
        first = self.read()
        self.assertEqual(first['source'], 'defaults')
        self.assertEqual(S.query(A).count(), 0)
        saved = self.save(first['selected'])
        self.assertEqual(saved['selection']['source'], 'saved')
        self.assertTrue(saved['saved'])

    def test_saved_noop_does_not_add_another_version(self):
        first = self.save()['selection']
        again = self.save(revision=first['revision'])
        self.assertFalse(again['saved'])
        self.assertEqual(again['selection']['version'], 1)
        self.assertEqual(S.query(A).count(), 1)

    def test_changed_selection_changes_journey_but_not_legacy_channels(self):
        source = channels()
        copy = deepcopy(source)
        self.save([])
        journey = build_organization_setup_journey(S.get(T, 1), source)
        self.assertEqual(journey['stages'][1]['source_ids'], ['widget', 'live_chat'])
        self.assertEqual(journey['stages'][2]['source_ids'], ['knowledge_content'])
        self.assertIn('identity_auth', journey['stages'][-1]['source_ids'])
        self.assertEqual(source, copy)

    def test_stale_revision_is_rejected_and_reread_allows_explicit_write(self):
        current = self.save()['selection']
        with self.assertRaises(ModuleSelectionError) as e:
            self.save([])
        self.assertEqual(e.exception.status, 412)
        updated = self.save([], revision=current['revision'])['selection']
        self.assertEqual(updated['version'], 2)
        self.assertNotEqual(updated['revision'], self.initial['revision'])

    def test_dependencies_unknown_ids_and_duplicates_do_not_write(self):
        for value in (['payments'], ['whatsapp', 'whatsapp'], ['superadmin'], ['catalog', None], 'catalog'):
            with self.subTest(value=value), self.assertRaises(ModuleSelectionError):
                self.save(value)
        self.assertEqual(S.query(A).count(), 0)
        self.assertEqual(self.read()['version'], 0)

    def test_free_foreign_inactive_and_missing_actors_cannot_write(self):
        for kwargs in ({'entitled': False}, {'actor': 3}, {'actor': 999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ModuleSelectionError):
                self.save(**kwargs)
        row = S.get(T, 1)
        row.is_active = False
        S.commit()
        with self.assertRaises(ModuleSelectionError):
            self.save()
        self.assertEqual(S.query(A).count(), 0)

    def test_downgrade_does_not_destroy_saved_preferences(self):
        self.save()
        snapshot = build_module_selection(S.get(T, 1), can_edit=True, entitled=False)
        self.assertFalse(snapshot['can_edit'])
        self.assertEqual(snapshot['selected'], ['catalog', 'payments'])
        self.assertEqual(snapshot['reason_code'], 'full')

    def test_maintenance_never_claims_permission_to_write(self):
        value = build_module_selection(S.get(T, 1), can_edit=True, entitled=True, writes_blocked=True)
        self.assertFalse(value['can_edit'])
        self.assertEqual(value['reason_code'], 'maintenance')

    def test_corrupted_storage_does_not_reset_to_defaults(self):
        row = S.get(T, 1)
        row.configuracion = {KEY: {'unknown': 'private'}}
        S.commit()
        self.assertIsNone(self.read())
        with self.assertRaises(ModuleSelectionError):
            self.save()
        self.assertIsNone(build_organization_setup_journey(S.get(T, 1), channels()))

    def test_public_configuration_does_not_expose_internal_selection(self):
        self.save()
        public = sanitize_public_tenant_config(S.get(T, 1).configuracion)
        self.assertNotIn(KEY, public)
        self.assertTrue(public['keep'])

    def test_failed_commit_restores_selection_and_audit(self):
        with patch.object(S.session_factory.class_, 'commit', side_effect=SQLAlchemyError('fixture')):
            with self.assertRaises(ModuleSelectionError) as e:
                self.save()
        self.assertEqual(e.exception.status, 503)
        self.assertEqual(self.read()['version'], 0)
        self.assertEqual(S.query(A).count(), 0)

    def test_catalog_revision_is_bound_to_tenant_and_sector(self):
        other = self.read(2)
        self.assertNotEqual(self.initial['revision'], other['revision'])
        row = S.get(T, 1)
        row.tipo = 'empresa'
        S.commit()
        self.assertNotEqual(self.read()['revision'], self.initial['revision'])
        self.assertNotIn('territory', [x['id'] for x in self.read()['catalog']])

    def test_wrong_revision_and_mixed_update_are_rejected(self):
        for payload in ({'organization_modules': {'selected': []}}, {'organization_modules': {'selected': []}, 'expected_revision': self.initial['revision'], 'tenant': {'plan': 'full'}}):
            with self.assertRaises(ModuleSelectionError):
                save_module_selection(S, T, U, A, tenant_id=1, actor_id=2, data=payload, authorize=storage.allowed, entitlement=lambda t: True)
        self.assertEqual(S.query(A).count(), 0)

    @unittest.skipUnless(storage.POSTGRES, 'Concurrent row locking requires disposable PostgreSQL')
    def test_concurrent_changes_have_one_saved_selection_and_one_conflict(self):
        barrier = Barrier(2)

        def write(selected):
            try:
                barrier.wait(timeout=5)
                return self.save(selected)['selection']['version']
            except ModuleSelectionError as error:
                return error.status
            finally:
                S.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, (['whatsapp'], ['catalog'])))
        self.assertEqual(sorted(results), [1, 412])
        self.assertEqual(S.query(A).count(), 1)

if __name__=='__main__':unittest.main()
