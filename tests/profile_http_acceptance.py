"""Run with python -m tests.profile_http_acceptance in a disposable process.
Full create_app, password login, cookies, tenant middleware and HTTP handlers.
Not acceptance of deployed QA, Clerk, email verification or physical devices.
"""
from tests.profile_acceptance_runtime import prepare_process, create_disposable_app

if __name__ == '__main__':
    prepare_process()

import json
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor, Request
from urllib.error import HTTPError


class BrowserSession:
    def __init__(self, origin):
        self.origin = origin
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
    def request(self, method, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        req = Request(self.origin + path, data=body, method=method,
            headers={'Content-Type': 'application/json', **(headers or {})})
        try: response = self.opener.open(req, timeout=15)
        except HTTPError as error: response = error
        with response:
            return response.status, json.loads(response.read()), response.headers

class InstitutionalProfileHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='chatboc-profile-http-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)
        from werkzeug.serving import make_server
        cls.server = make_server('127.0.0.1', 0, cls.app, threaded=True)
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.thread.join(timeout=5)
        from database import db
        with cls.app.app_context():
            db.session.remove(); db.engine.dispose()
        cls.directory.cleanup()
    def login(self, account='acceptance-a'):
        browser = BrowserSession(self.origin)
        status, body, _ = browser.request('POST', '/auth/login',
            {'email':self.accounts[account]['email'], 'password':self.password})
        self.assertEqual(status, 200, 'Real password login did not succeed')
        self.assertTrue(body.get('token'), 'Login must issue a signed session')
        return browser
    def profile(self, browser, slug='acceptance-a'):
        status, body, _ = browser.request('GET', f'/api/admin/tenants/{slug}/config')
        self.assertEqual(status, 200, f'Institutional read rejected with {status}')
        return body['organization_profile']
    def save(self, browser, profile, changes):
        return browser.request('PUT', profile['save_endpoint'],
            {'expected_revision':profile['revision'], 'organization_profile':changes})
    def test_password_login_save_and_independent_reread(self):
        first, second = self.login(), self.login('second')
        before = self.profile(first)
        status, body, headers = self.save(first, before, {'ciudad':'Ciudad de aceptación HTTP'})
        self.assertEqual(status, 200)
        self.assertTrue(body['saved'])
        self.assertEqual(headers.get('Cache-Control'), 'no-store')
        reread = self.profile(second)
        self.assertEqual(reread['values']['ciudad'], 'Ciudad de aceptación HTTP')
        self.assertEqual(reread['revision'], body['profile']['revision'])
    def test_two_authenticated_sessions_preserve_the_first_writer(self):
        first, second = self.login(), self.login('second')
        before = self.profile(first)
        other = self.profile(second)
        status, saved, _ = self.save(first, before, {'direccion':'Primera edición HTTP'})
        self.assertEqual(status, 200)
        status, conflict, _ = self.save(second, other, {'direccion':'Segunda edición HTTP'})
        self.assertEqual(status, 412)
        self.assertEqual(conflict['reason_code'], 'profile_revision_conflict')
        self.assertEqual(self.profile(second)['values']['direccion'], 'Primera edición HTTP')
    def test_other_organization_cannot_read_or_write(self):
        foreign = self.login('acceptance-b')
        profile = self.profile(self.login())
        status, _, _ = foreign.request('GET', profile['save_endpoint'])
        self.assertIn(status, (403, 404))
        status, _, _ = self.save(foreign, profile, {'ciudad':'Forbidden'})
        self.assertIn(status, (403, 404))
    def test_employee_cannot_modify_institutional_settings(self):
        viewer = self.login('viewer')
        profile = self.profile(viewer)
        self.assertFalse(profile['can_edit'])
        status, _, _ = self.save(viewer, profile, {'ciudad':'Not allowed'})
        self.assertEqual(status, 403)
    def test_anonymous_and_wrong_password_are_rejected(self):
        browser = BrowserSession(self.origin)
        status, _, _ = browser.request('GET', '/api/admin/tenants/acceptance-a/config')
        self.assertEqual(status, 401)
        status, _, _ = browser.request('POST', '/auth/login',
            {'email':self.accounts['acceptance-a']['email'], 'password':'incorrect-password'})
        self.assertEqual(status, 401)
    def test_writer_fence_blocks_existing_session_without_losing_data(self):
        browser = self.login()
        before = self.profile(browser)
        self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = True
        try:
            readonly = self.profile(browser)
            self.assertFalse(readonly['can_edit'])
            self.assertEqual(readonly['editability']['reason_code'], 'maintenance')
            status, _, _ = self.save(browser, before, {'ciudad':'Do not persist'})
            self.assertEqual(status, 503)
            self.assertEqual(self.profile(browser)['values'], before['values'])
        finally:
            self.app.config['CUTOVER_WRITER_FENCE_ENABLED'] = False

    def test_scoped_admin_grant_and_revocation_are_rechecked_with_same_session(self):
        from database import db
        from models import UserRole
        browser = self.login('delegated')
        before = self.profile(browser)
        self.assertTrue(before['can_edit'])
        status, _, _ = self.save(browser, before, {'provincia':'Delegación autorizada'})
        self.assertEqual(status, 200)
        with self.app.app_context():
            grant = UserRole.query.filter_by(user_id=self.accounts['delegated']['id']).one()
            role_id, tenant_id = grant.role_id, grant.tenant_id
            db.session.delete(grant); db.session.commit()
        try:
            current = self.profile(browser)
            self.assertFalse(current['can_edit'])
            self.assertEqual(current['editability']['reason_code'], 'tenant_admin_required')
            status, _, _ = self.save(browser, current, {'provincia':'Permiso revocado'})
            self.assertEqual(status, 403)
            self.assertEqual(self.profile(self.login())['values']['provincia'], 'Delegación autorizada')
        finally:
            with self.app.app_context():
                db.session.add(UserRole(user_id=self.accounts['delegated']['id'], role_id=role_id, tenant_id=tenant_id))
                db.session.commit()

    def test_api_me_and_admin_read_share_profile_identity(self):
        browser = self.login()
        profile = self.profile(browser)
        status, me, headers = browser.request('GET', '/api/me?tenant_slug=acceptance-a')
        self.assertEqual(status, 200)
        self.assertEqual(me['organization_profile'], profile)

    def test_invalid_fields_never_change_operator_or_institution(self):
        browser = self.login()
        before = self.profile(browser)
        status, _, _ = self.save(browser, before, {'ciudad':'Forbidden', 'rol':'superadmin'})
        self.assertEqual(status, 400)
        self.assertEqual(self.profile(browser)['values'], before['values'])

    def test_inactive_organization_rejects_existing_and_new_sessions(self):
        from database import db
        from models import TenantProfile
        browser = self.login('acceptance-b')
        before = self.profile(browser, 'acceptance-b')
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, self.accounts['acceptance-b']['tenant_id'])
            tenant.is_active = False; db.session.commit()
        try:
            status, _, _ = browser.request('GET', before['save_endpoint'])
            self.assertIn(status, (401, 403, 404))
            status, _, _ = self.save(browser, before, {'ciudad':'Inactive'})
            self.assertIn(status, (401, 403, 404))
            anonymous = BrowserSession(self.origin)
            status, _, _ = anonymous.request('POST', '/auth/login',
                {'email':self.accounts['acceptance-b']['email'], 'password':self.password})
            self.assertEqual(status, 401)
        finally:
            with self.app.app_context():
                db.session.get(TenantProfile, self.accounts['acceptance-b']['tenant_id']).is_active = True
                db.session.commit()

    def test_setup_journey_is_bound_to_authorized_organization(self):
        browser=self.login()
        status,body,headers=browser.request('GET','/api/v2/tenants/acceptance-a/activation/channels')
        self.assertEqual(status,200)
        setup=body['organization_setup']
        self.assertEqual(setup['tenant']['slug'],'acceptance-a')
        self.assertEqual(setup['organization_type'],'municipio')
        self.assertTrue(setup['government_setup'])
        self.assertEqual(body['implementation_journey']['contract_version'],'tenant.implementation_journey.v1')
        self.assertFalse(setup['writes_performed'])
        status,_,_=self.login('acceptance-b').request('GET','/api/v2/tenants/acceptance-a/activation/channels')
        self.assertIn(status,(403,404))

    def test_company_setup_preserves_legacy_and_excludes_government_requirements(self):
        from database import db
        from models import TenantProfile
        with self.app.app_context():
            entity=db.session.get(TenantProfile,self.accounts['acceptance-b']['tenant_id'])
            old=entity.tipo;entity.tipo='empresa';db.session.commit()
        try:
            status,body,_=self.login('acceptance-b').request('GET','/api/v2/tenants/acceptance-b/activation/channels')
            self.assertEqual(status,200)
            setup=body['organization_setup'];self.assertEqual(setup['organization_type'],'empresa')
            self.assertFalse(setup['government_setup'])
            self.assertIn('payments_checkout',setup['stages'][2]['source_ids'])
            self.assertNotIn('territorial_intelligence',setup['stages'][-1]['source_ids'])
        finally:
            with self.app.app_context():
                db.session.get(TenantProfile,self.accounts['acceptance-b']['tenant_id']).tipo=old;db.session.commit()

    def brand_plan(self,plan='full'):
        from contextlib import contextmanager
        @contextmanager
        def setup():
            from copy import deepcopy
            from database import db
            from models import TenantProfile
            from services.organization_branding import KEY
            with self.app.app_context():
                tenant=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
                previous=(tenant.plan,deepcopy(tenant.configuracion))
                tenant.plan=plan;config=deepcopy(tenant.configuracion or {});config.pop(KEY,None)
                tenant.configuracion=config;db.session.commit()
            try:yield
            finally:
                with self.app.app_context():
                    tenant=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
                    tenant.plan,tenant.configuracion=previous;db.session.commit()
        return setup()
    def read_brand(self,browser):
        status,body,_=browser.request('GET','/api/admin/tenants/acceptance-a/config')
        self.assertEqual(status,200);return body['organization_branding']
    def post_brand(self,browser,brand,*,restore=None):
        operation={'operation':'publish','values':{'enabled':True,'primary_color':'#6D28D9','accent_color':'#C2410C'}}
        if restore is not None:operation={'operation':'restore','version':restore}
        return browser.request('PUT',brand['save_endpoint'],{'expected_revision':brand['revision'],'organization_branding':operation})
    def test_branding_full_publish_read_through_me_and_restore(self):
        with self.brand_plan():
            first=self.login();second=self.login('second');before=self.read_brand(first)
            self.assertTrue(before['can_edit'])
            status,saved,headers=self.post_brand(first,before);self.assertEqual(status,200)
            self.assertTrue(saved['saved']);self.assertEqual(headers['Cache-Control'],'no-store')
            reread=self.read_brand(second);self.assertEqual(reread['version'],1)
            status,me,_=second.request('GET','/api/me?tenant_slug=acceptance-a');self.assertEqual(status,200)
            self.assertEqual(me['workspace_appearance']['appearance']['primary']['background'],'#6D28D9')
            self.assertTrue(me['workspace_appearance']['appearance']['active'])
            status,restored,_=self.post_brand(second,reread,restore=0);self.assertEqual(status,200)
            self.assertEqual(restored['brand']['version'],2);self.assertFalse(restored['brand']['appearance']['active'])
    def test_branding_free_plan_and_arbitrary_capabilities_do_not_unlock(self):
        with self.brand_plan('free'):
            from database import db
            from models import TenantProfile
            with self.app.app_context():
                row=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
                row.configuracion={**(row.configuracion or {}),'capabilities':['*','integrations.full_access']};db.session.commit()
            browser=self.login();brand=self.read_brand(browser);self.assertFalse(brand['can_edit'])
            self.assertEqual(brand['reason_code'],'full_plan_required')
            self.assertEqual(self.post_brand(browser,brand)[0],403)
            self.assertEqual(self.read_brand(browser)['version'],0)
    def test_branding_cross_tenant_and_employee_mutations_are_rejected(self):
        with self.brand_plan():
            owner=self.login();brand=self.read_brand(owner)
            for who in ('acceptance-b','viewer'):
                self.assertEqual(self.post_brand(self.login(who),brand)[0],403)
            self.assertEqual(self.read_brand(owner)['version'],0)
    def test_branding_two_real_sessions_require_latest_revision(self):
        with self.brand_plan():
            first,second=self.login(),self.login('second');before=self.read_brand(first)
            self.assertEqual(self.post_brand(first,before)[0],200)
            status,body,_=self.post_brand(second,before);self.assertEqual(status,412)
            self.assertEqual(body['reason_code'],'branding_revision_conflict')
            self.assertEqual(self.read_brand(second)['version'],1)
    def test_branding_writer_fence_prevents_publish_without_changing_history(self):
        with self.brand_plan():
            browser=self.login();brand=self.read_brand(browser);self.app.config['CUTOVER_WRITER_FENCE_ENABLED']=True
            try:
                self.assertEqual(self.read_brand(browser)['reason_code'],'maintenance')
                self.assertEqual(self.post_brand(browser,brand)[0],503)
            finally:self.app.config['CUTOVER_WRITER_FENCE_ENABLED']=False
            self.assertEqual(self.read_brand(browser)['version'],0)
    def test_branding_generic_integration_grant_does_not_override_trial(self):
        with self.brand_plan():
            from database import db
            from models import TenantProfile
            with self.app.app_context():
                row=db.session.get(TenantProfile,self.accounts['acceptance-a']['tenant_id'])
                row.configuracion={**(row.configuracion or {}),'demo_mode':True};db.session.commit()
            browser=self.login();brand=self.read_brand(browser);self.assertFalse(brand['can_edit'])
            self.assertEqual(self.post_brand(browser,brand)[0],403)

    def test_modules_full_save_reread_and_journey_consumes_selection(self):
        with self.brand_plan():
            browser=self.login();other=self.login('second')
            status,config,_=browser.request('GET','/api/admin/tenants/acceptance-a/config');self.assertEqual(status,200)
            before=config['organization_modules'];self.assertTrue(before['can_edit'])
            status,saved,_=browser.request('PUT',before['save_endpoint'],{'expected_revision':before['revision'],'organization_modules':{'selected':['catalog','payments']}})
            self.assertEqual(status,200);self.assertEqual(saved['selection']['version'],1)
            status,again,_=other.request('GET',before['save_endpoint']);self.assertEqual(again['organization_modules']['selected'],['catalog','payments'])
            status,channels,_=other.request('GET','/api/v2/tenants/acceptance-a/activation/channels');self.assertEqual(status,200)
            self.assertEqual(channels['organization_setup']['selected_modules'],['catalog','payments'])
            self.assertNotIn('whatsapp',channels['organization_setup']['stages'][1]['source_ids'])
            self.assertIn('whatsapp',[c['id'] for c in channels['channels']])
    def test_modules_free_foreign_and_viewer_cannot_publish(self):
        for plan in ('free','full'):
            with self.brand_plan(plan):
                status,body,_=self.login().request('GET','/api/admin/tenants/acceptance-a/config');snapshot=body['organization_modules']
                for who in (['acceptance-a'] if plan=='free' else ['acceptance-b','viewer']):
                    result=self.login(who).request('PUT',snapshot['save_endpoint'],{'expected_revision':snapshot['revision'],'organization_modules':{'selected':[]}})
                    self.assertEqual(result[0],403)
    def test_modules_conflict_dependency_and_maintenance_are_enforced(self):
        with self.brand_plan():
            browser=self.login();_,body,_=browser.request('GET','/api/admin/tenants/acceptance-a/config');initial=body['organization_modules']
            def save(selected):return browser.request('PUT',initial['save_endpoint'],{'expected_revision':initial['revision'],'organization_modules':{'selected':selected}})
            self.assertEqual(save(['payments'])[0],400);self.assertEqual(save(['catalog'])[0],200);self.assertEqual(save([])[0],412)
            self.app.config['CUTOVER_WRITER_FENCE_ENABLED']=True
            try:self.assertEqual(save([])[0],503)
            finally:self.app.config['CUTOVER_WRITER_FENCE_ENABLED']=False


if __name__ == '__main__':
    unittest.main(verbosity=2)
