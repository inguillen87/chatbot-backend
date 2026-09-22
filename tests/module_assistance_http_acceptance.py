"""Run the existing full HTTP suite plus new assistance contract scenarios."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__':
    prepare_process()

import unittest
from tests import profile_http_acceptance as original
from services.organization_modules import SELECTION_ASSISTANCE

class AssistedModuleHTTPTests(original.InstitutionalProfileHttpTests):
    def test_assistance_is_returned_by_authenticated_settings_and_save_receipt(self):
        with self.brand_plan():
            browser = self.login()
            endpoint = '/api/admin/tenants/acceptance-a/config'
            status, body, _ = browser.request('GET', endpoint)
            self.assertEqual(status, 200)
            snapshot = body['organization_modules']
            self.assertEqual(snapshot['selection_assistance'], SELECTION_ASSISTANCE)
            status, result, _ = browser.request('PUT', endpoint, {
                'expected_revision':snapshot['revision'],
                'organization_modules':{'selected':['catalog','payments']}})
            self.assertEqual(status, 200)
            self.assertEqual(result['selection']['selection_assistance'], SELECTION_ASSISTANCE)
            self.assertFalse(result['changes_runtime_access'])

    def test_helpful_copy_never_bypasses_a_server_dependency(self):
        with self.brand_plan():
            browser = self.login()
            endpoint = '/api/admin/tenants/acceptance-a/config'
            _, before, _ = browser.request('GET', endpoint)
            snapshot = before['organization_modules']
            status, _, _ = browser.request('PUT', endpoint, {
                'expected_revision':snapshot['revision'],
                'organization_modules':{'selected':['payments']}})
            self.assertEqual(status, 400)
            _, after, _ = browser.request('GET', endpoint)
            self.assertEqual(after['organization_modules']['revision'], snapshot['revision'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
