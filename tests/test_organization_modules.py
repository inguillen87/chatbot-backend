from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import unittest
from sqlalchemy.exc import SQLAlchemyError
from tests import test_organization_profile_settings as storage
from services.organization_modules import build_modules, save_modules, ModuleSelectionError, KEY, apply_setup_selection
from services.public_tenant_config import sanitize_public_tenant_config
from services.tenant_implementation_journey import _STAGES
S,T,U,A = storage.Session, storage.Tenant, storage.User, storage.Audit

class ModuleSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): storage.OrganizationProfileSettingsTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): storage.OrganizationProfileSettingsTests.tearDownClass.__func__(cls)
    def setUp(self): storage.OrganizationProfileSettingsTests.setUp(self)
    def tearDown(self): S.remove()
    def read(self,tenant_id=1): return build_modules(S.get(T,tenant_id),can_edit=True,entitled=True)
    def save(self, selected=None, *, expected=None, actor=2, tenant=1, entitled=True):
        return save_modules(S,T,U,A,tenant_id=tenant,actor_id=actor,
            data={'expected_revision':expected or self.initial['revision'],
                  'organization_modules':{'selected':selected if selected is not None else ['catalog','payments']}},
            authorize=storage.allowed,entitlement=lambda tenant:entitled)
    def test_persist_and_reread_without_changing_other_settings(self):
        saved=self.save()['modules'];S.remove()
        self.assertEqual(self.read()['selected'],['catalog','payments'])
        self.assertEqual(saved['version'],1);self.assertEqual(S.query(A).count(),1)
        self.assertTrue(S.get(T,1).configuracion['keep']);self.assertEqual(S.get(U,2).name,'Operator')
    def test_dependencies_and_unknown_modules_reject_without_writes(self):
        for selected in (['payments'],['unknown'],['catalog','catalog'],['superadmin'],[True]):
            with self.subTest(selected=selected), self.assertRaises(ModuleSelectionError): self.save(selected)
        self.assertEqual(S.query(A).count(),0);self.assertEqual(self.read()['version'],0)
    def test_full_membership_and_tenant_active_rechecked(self):
        for options in ({'entitled':False},{'actor':3},{'actor':999}):
            with self.subTest(options=options),self.assertRaises(ModuleSelectionError):self.save(**options)
        row=S.get(T,1);row.is_active=False;S.commit()
        with self.assertRaises(ModuleSelectionError):self.save()
        self.assertEqual(S.query(A).count(),0)
    def test_shared_key_is_not_public(self):
        self.save();self.assertNotIn(KEY,sanitize_public_tenant_config(S.get(T,1).configuracion))
    def test_stale_version_rejected_without_losing_original(self):
        self.save()
        with self.assertRaises(ModuleSelectionError) as caught:self.save([])
        self.assertEqual(caught.exception.status,412);self.assertEqual(self.read()['selected'],['catalog','payments'])
    def test_noop_does_not_create_an_audit(self):
        saved=self.save()['modules'];result=self.save(saved['selected'],expected=saved['revision'])
        self.assertFalse(result['saved']);self.assertEqual(S.query(A).count(),1)
    def test_empty_optional_selection_preserves_core_checks(self):
        self.save([]);steps=apply_setup_selection(S.get(T,1),_STAGES)
        self.assertEqual(steps[1]['source_ids'],('widget','live_chat'))
        self.assertEqual(steps[2]['source_ids'],('knowledge_content',))
        self.assertIn('identity_auth',steps[-1]['source_ids'])
        self.assertNotIn('territorial_intelligence',steps[-1]['source_ids'])
    def test_company_cannot_publish_government_territory(self):
        current=self.read(2)
        with self.assertRaises(ModuleSelectionError):self.save(['territory'],actor=3,tenant=2,expected=current['revision'])
    def test_corrupted_storage_does_not_silently_reset(self):
        row=S.get(T,1);row.configuracion={KEY:{'bad':'data'}};S.commit()
        self.assertIsNone(self.read())
        with self.assertRaises(ModuleSelectionError):self.save()
        self.assertEqual(S.get(T,1).configuracion,{KEY:{'bad':'data'}})
    def test_commit_failure_rolls_back_configuration_and_audit(self):
        with patch.object(S.session_factory.class_,'commit',side_effect=SQLAlchemyError('fixture')):
            with self.assertRaises(ModuleSelectionError):self.save()
        self.assertEqual(self.read()['version'],0);self.assertEqual(S.query(A).count(),0)
    def test_downgrade_does_not_erase_selection(self):
        self.save();readonly=build_modules(S.get(T,1),can_edit=True,entitled=False)
        self.assertFalse(readonly['can_edit']);self.assertEqual(readonly['selected'],['catalog','payments'])
    @unittest.skipUnless(storage.POSTGRES,'Requires disposable PostgreSQL row locks')
    def test_concurrent_saves_have_one_commit_and_one_conflict(self):
        barrier=Barrier(2)
        def publish(selected):
            try:
                barrier.wait(timeout=5);return self.save(selected)['modules']['version']
            except ModuleSelectionError as error:return error.status
            finally:S.remove()
        with ThreadPoolExecutor(max_workers=2) as executor:
            result=list(executor.map(publish,(['catalog'],['surveys'])))
        self.assertEqual(sorted(result),[1,412]);self.assertEqual(S.query(A).count(),1)
