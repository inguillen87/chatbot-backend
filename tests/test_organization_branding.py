"""Brand persistence with source-model columns and real SQL transactions.

Membership/entitlement callbacks are synthetic here; full HTTP tests cover the
actual controls. PostgreSQL concurrency uses the existing disposable CI service.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import unittest
from sqlalchemy.exc import SQLAlchemyError
from tests import test_organization_profile_settings as storage
from services.organization_branding import build_branding,save_branding,BrandingError,KEY,color_pair
from services.public_tenant_config import sanitize_public_tenant_config
S,T,U,A=storage.Session,storage.Tenant,storage.User,storage.Audit

class BrandingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): storage.OrganizationProfileSettingsTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): storage.OrganizationProfileSettingsTests.tearDownClass.__func__(cls)
    def setUp(self): storage.OrganizationProfileSettingsTests.setUp(self)
    def tearDown(self): S.remove()
    def read(self,tenant_id=1): return build_branding(S.get(T,tenant_id),can_edit=True,entitled=True)
    def save(self,values=None,*,expected=None,actor=2,tenant_id=1,restore=None,entitled=True):
        operation={'operation':'publish','values':values or {'enabled':True,'primary_color':'#6D28D9','accent_color':'#C2410C'}}
        if restore is not None: operation={'operation':'restore','version':restore}
        return save_branding(S,T,U,A,tenant_id=tenant_id,actor_id=actor,
            data={'expected_revision':expected or self.initial['revision'],'organization_branding':operation},
            authorize=storage.allowed,entitlement=lambda tenant:entitled)
    def test_publish_is_persisted_with_audit_and_does_not_change_identity(self):
        before=(S.get(T,1).nombre,S.get(U,1).name);result=self.save()
        self.assertTrue(result['saved']);self.assertEqual(result['brand']['version'],1)
        self.assertEqual(self.read()['values']['primary_color'],'#6D28D9')
        self.assertEqual((S.get(T,1).nombre,S.get(U,1).name),before)
        self.assertTrue(S.get(T,1).configuracion['keep']);self.assertEqual(S.query(A).count(),1)
    def test_restore_creates_a_new_version_without_aba_revision(self):
        published=self.save()['brand'];restored=self.save(expected=published['revision'],restore=0)['brand']
        self.assertEqual(restored['values'],self.initial['values']);self.assertEqual(restored['version'],2)
        self.assertNotEqual(restored['revision'],self.initial['revision']);self.assertEqual(S.query(A).count(),2)
    def test_noop_adds_no_version_or_audit(self):
        result=self.save(self.initial['values']);self.assertFalse(result['saved'])
        self.assertEqual(result['brand']['version'],0);self.assertEqual(S.query(A).count(),0)
    def test_stale_revision_does_not_overwrite_another_publication(self):
        self.save()
        with self.assertRaises(BrandingError) as caught:self.save({'enabled':True,'primary_color':'#FFFFFF','accent_color':'#000000'})
        self.assertEqual(caught.exception.status,412);self.assertEqual(S.query(A).count(),1)
    def test_entitlement_and_membership_are_independent(self):
        for kwargs in ({'entitled':False},{'actor':3},{'actor':999}):
            with self.subTest(kwargs=kwargs),self.assertRaises(BrandingError):self.save(**kwargs)
        self.assertEqual(self.read()['version'],0);self.assertEqual(S.query(A).count(),0)
    def test_unrelated_capabilities_cannot_bypass_explicit_entitlement_callback(self):
        row=S.get(T,1);row.configuracion={'capabilities':['*','integrations.full_access']};S.commit()
        with self.assertRaises(BrandingError):self.save(entitled=False)
        self.assertEqual(self.read()['version'],0)
    def test_invalid_css_and_unknown_fields_never_partially_write(self):
        for invalid in ('red','url(https://external.example)','var(--x)','#FFF','#123456;display:none'):
            with self.subTest(invalid=invalid),self.assertRaises(BrandingError):
                self.save({'enabled':True,'primary_color':invalid,'accent_color':'#000000'})
        with self.assertRaises(BrandingError):self.save({'enabled':True,'primary_color':'#123456','accent_color':'#654321','plan':'full'})
        self.assertEqual(S.query(A).count(),0)
    def test_history_is_bounded_and_private(self):
        latest=self.initial
        for index in range(14): latest=self.save({'enabled':True,'primary_color':f'#{index:06X}','accent_color':'#334455'},expected=latest['revision'])['brand']
        self.assertEqual(len(latest['history']),10);self.assertEqual(latest['version'],14)
        public=sanitize_public_tenant_config(S.get(T,1).configuracion)
        self.assertNotIn(KEY,public);self.assertTrue(public['keep'])
        with self.assertRaises(BrandingError):self.save(expected=latest['revision'],restore=0)
    def test_commit_failure_reverts_palette_and_audit(self):
        with patch.object(S.session_factory.class_,'commit',side_effect=SQLAlchemyError('fixture')):
            with self.assertRaises(BrandingError):self.save()
        self.assertEqual(self.read()['version'],0);self.assertEqual(S.query(A).count(),0)
    def test_downgrade_suppresses_appearance_without_destroying_palette(self):
        self.save();row=S.get(T,1);current=build_branding(row,can_edit=True,entitled=False)
        self.assertFalse(current['appearance']['active']);self.assertFalse(current['can_edit'])
        self.assertEqual(current['values']['primary_color'],'#6D28D9');self.assertEqual(current['version'],1)
    def test_malformed_storage_is_not_repaired_silently(self):
        row=S.get(T,1);row.configuracion={KEY:{'unexpected':'secret'}};S.commit()
        self.assertIsNone(self.read())
        with self.assertRaises(BrandingError):self.save()
        self.assertEqual(S.get(T,1).configuracion,{KEY:{'unexpected':'secret'}})
    def test_contrast_chooses_readable_text_for_color_samples(self):
        for red in range(0,256,17):
            for green in range(0,256,17):
                for blue in range(0,256,17):
                    self.assertGreaterEqual(color_pair(f'#{red:02X}{green:02X}{blue:02X}')['contrast'],4.5)
    def test_history_from_another_tenant_cannot_be_restored(self):
        self.save()
        target=self.read(2)
        with self.assertRaises(BrandingError):self.save(tenant_id=2,actor=3,expected=target['revision'],restore=0)
        self.assertEqual(self.read(2)['version'],0)
    @unittest.skipUnless(storage.POSTGRES,'Real row-lock race requires disposable PostgreSQL')
    def test_simultaneous_publish_has_one_commit_and_one_conflict(self):
        barrier=Barrier(2)
        def publish(color):
            try:
                barrier.wait(timeout=5)
                return self.save({'enabled':True,'primary_color':color,'accent_color':'#334455'})['brand']['version']
            except BrandingError as error:return error.status
            finally:S.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(publish,('#123456','#654321')))
        self.assertEqual(sorted(results),[1,412]);self.assertEqual(S.query(A).count(),1)

if __name__=='__main__':unittest.main()
