"""Receipt campaign settings on the existing profile transaction and route."""

import ast
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from flask import Flask, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from services.organization_profile_settings import (
    ProfileSettingsError, RECEIPT_HEADER_KEY, save_profile_settings, validate_receipt_header,
)
from tests import test_organization_profile_settings as fixture
from services.whatsapp_receipt_branding import resolve_whatsapp_receipt_header


IMAGE = 'https://brand.example.test/campaign.png'


class OrganizationReceiptHeaderSettingsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.OrganizationProfileSettingsTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.OrganizationProfileSettingsTests.tearDownClass.__func__(cls)

    setUp = fixture.OrganizationProfileSettingsTests.setUp
    tearDown = fixture.OrganizationProfileSettingsTests.tearDown
    read = fixture.OrganizationProfileSettingsTests.read
    save = fixture.OrganizationProfileSettingsTests.save

    def seed_header(self, value):
        tenant = fixture.Session.get(fixture.Tenant, 1)
        tenant.configuracion = {**tenant.configuracion, RECEIPT_HEADER_KEY: deepcopy(value),
                                'promo_image_url': 'https://brand.example.test/legacy.jpg',
                                'unknown_config': {'keep': [1, 2]}}
        fixture.Session.commit()
        fixture.Session.remove()
        return self.read()

    def test_absent_is_inheritance_and_read_does_not_write(self):
        self.assertIsNone(self.initial['values'][RECEIPT_HEADER_KEY])
        self.assertNotIn(RECEIPT_HEADER_KEY, fixture.Session.get(fixture.Tenant, 1).configuracion)
        self.assertEqual(fixture.Session.query(fixture.Audit).count(), 0)

    def test_enabled_campaign_persists_with_canonical_ack_and_isolation(self):
        other = deepcopy(self.read(2))
        change = {'enabled': True, 'image_url': IMAGE}
        result = self.save({RECEIPT_HEADER_KEY: change})
        self.assertTrue(result['saved'])
        self.assertEqual(result['profile']['values'][RECEIPT_HEADER_KEY], change)
        change['image_url'] = 'https://brand.example.test/mutated.png'
        result['profile']['values'][RECEIPT_HEADER_KEY]['image_url'] = 'https://brand.example.test/ack-mutated.png'
        self.assertEqual(fixture.Session.get(fixture.Tenant, 1).configuracion[RECEIPT_HEADER_KEY],
                         {'enabled': True, 'image_url': IMAGE})
        self.assertIsNone(fixture.Session.get(fixture.Tenant, 1).logo_url)
        self.assertIsNone(fixture.Session.get(fixture.User, 1).logo_url)
        fixture.Session.remove()
        self.assertEqual(self.read(2), other)
        self.assertEqual(self.read()['values'][RECEIPT_HEADER_KEY], {'enabled': True, 'image_url': IMAGE})
        event = fixture.Session.query(fixture.Audit).one()
        self.assertEqual(event.details['changed_fields'], [RECEIPT_HEADER_KEY])
        self.assertNotIn(IMAGE, str(event.details))

    def test_false_is_explicit_and_preserves_campaign_for_later_activation(self):
        result = self.save({RECEIPT_HEADER_KEY: {'enabled': False, 'image_url': IMAGE}})
        self.assertEqual(result['profile']['values'][RECEIPT_HEADER_KEY], {'enabled': False, 'image_url': IMAGE})
        self.assertIs(fixture.Session.get(fixture.Tenant, 1).configuracion[RECEIPT_HEADER_KEY]['enabled'], False)

    def test_false_with_empty_image_is_valid_and_distinct_from_inheritance(self):
        result = self.save({RECEIPT_HEADER_KEY: {'enabled': False, 'image_url': ''}})
        self.assertEqual(result['profile']['values'][RECEIPT_HEADER_KEY], {'enabled': False, 'image_url': ''})
        self.assertNotEqual(result['profile']['revision'], self.initial['revision'])

    def test_null_removes_only_explicit_header_and_restores_inheritance(self):
        current = self.seed_header({'enabled': True, 'image_url': IMAGE})
        result = self.save({RECEIPT_HEADER_KEY: None}, revision=current['revision'])
        config = fixture.Session.get(fixture.Tenant, 1).configuracion
        self.assertNotIn(RECEIPT_HEADER_KEY, config)
        self.assertEqual(config['promo_image_url'], 'https://brand.example.test/legacy.jpg')
        self.assertEqual(config['unknown_config'], {'keep': [1, 2]})
        self.assertIsNone(result['profile']['values'][RECEIPT_HEADER_KEY])

    def test_omitted_header_preserves_unknown_and_legacy_fields(self):
        stored = {'image_url': IMAGE, 'legacy_metadata': {'keep': True}}
        current = self.seed_header(stored)
        self.save({'ciudad': 'Ciudad ficticia'}, revision=current['revision'])
        self.assertEqual(fixture.Session.get(fixture.Tenant, 1).configuracion[RECEIPT_HEADER_KEY], stored)
        self.assertEqual(self.read()['values'][RECEIPT_HEADER_KEY], {'enabled': False, 'image_url': ''})
        self.assertIsNone(resolve_whatsapp_receipt_header(fixture.Session.get(fixture.Tenant, 1)))

    def test_legacy_object_without_enabled_has_compatible_read_and_resolution(self):
        current = self.seed_header({'image_url': IMAGE})
        self.assertEqual(current['values'][RECEIPT_HEADER_KEY], {'enabled': True, 'image_url': IMAGE})
        self.assertEqual(resolve_whatsapp_receipt_header(fixture.Session.get(fixture.Tenant, 1)), IMAGE)

    def test_stale_revision_cannot_replace_a_new_campaign(self):
        self.save({RECEIPT_HEADER_KEY: {'enabled': True, 'image_url': IMAGE}})
        with self.assertRaises(ProfileSettingsError) as error:
            self.save({RECEIPT_HEADER_KEY: {'enabled': False, 'image_url': ''}})
        self.assertEqual(error.exception.status, 412)
        self.assertEqual(self.read()['values'][RECEIPT_HEADER_KEY], {'enabled': True, 'image_url': IMAGE})

    def test_raw_config_drift_invalidates_revision_without_exposing_bad_values(self):
        first = self.seed_header({'enabled': 'bad-one', 'image_url': 'javascript:private-marker-one'})
        self.assertEqual(first['values'][RECEIPT_HEADER_KEY], {'enabled': False, 'image_url': ''})
        second = self.seed_header({'enabled': 'bad-two', 'image_url': 'javascript:private-marker-two'})
        self.assertEqual(first['values'], second['values'])
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertNotIn('private-marker-two', str(second))
        with self.assertRaises(ProfileSettingsError) as error:
            self.save({'ciudad': 'No sobrescribir'}, revision=first['revision'])
        self.assertEqual(error.exception.status, 412)

    def test_explicit_null_stored_key_can_be_removed_even_when_value_is_none(self):
        current = self.seed_header(None)
        self.assertIsNone(current['values'][RECEIPT_HEADER_KEY])
        result = self.save({RECEIPT_HEADER_KEY: None}, revision=current['revision'])
        self.assertTrue(result['saved'])
        self.assertNotIn(RECEIPT_HEADER_KEY, fixture.Session.get(fixture.Tenant, 1).configuracion)
        self.assertNotEqual(result['profile']['revision'], current['revision'])

    def test_unchanged_explicit_header_has_no_audit_or_revision_change(self):
        current = self.seed_header({'enabled': True, 'image_url': IMAGE})
        result = self.save({RECEIPT_HEADER_KEY: {'enabled': True, 'image_url': IMAGE}}, revision=current['revision'])
        self.assertFalse(result['saved'])
        self.assertEqual(result['profile']['revision'], current['revision'])
        self.assertEqual(fixture.Session.query(fixture.Audit).count(), 0)

    def test_foreign_actor_cannot_set_campaign(self):
        with self.assertRaises(ProfileSettingsError) as error:
            self.save({RECEIPT_HEADER_KEY: {'enabled': True, 'image_url': IMAGE}}, actor=3)
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.read()['values'], self.initial['values'])

    def test_invalid_header_is_atomic(self):
        invalid = [False, True, '', [], {}, {'image_url': IMAGE}, {'enabled': True},
                   {'enabled': 'false', 'image_url': IMAGE}, {'enabled': True, 'image_url': ''},
                   {'enabled': True, 'image_url': IMAGE, 'unknown': True}]
        urls = ['http://brand.example.test/a.png', 'https://user:pass@brand.example.test/a.jpg',
                'https://@brand.example.test/a.png', 'https://localhost/a.png',
                'https://127.0.0.1/a.png', 'https://10.0.0.1/a.jpg', 'https://site.local/a.png',
                'https://brand.example.test/a.svg', 'https://brand.example.test/a.js',
                'https://brand.example.test/a.gif', 'https://brand.example.test/a.png?signature=synthetic',
                'https://brand.example.test/a.png#private', 'data:image/png;base64,synthetic',
                '/static/campaign.png', 'https://brand.example.test/a/../b.png',
                'https://brand.example.test/a/%2e%2e/b.png', 'https://brand.example.test/a%00.png',
                'https://brand.example.test/a.png\n', 'https://brand.example.test/a.png:443',
                'https://brand.example.test:8443/a.png']
        invalid.extend({'enabled': True, 'image_url': url} for url in urls)
        for header in invalid:
            with self.subTest(header=header), self.assertRaises(ProfileSettingsError):
                self.save({'ciudad': 'No guardar', RECEIPT_HEADER_KEY: header})
        self.assertEqual(self.read()['values'], self.initial['values'])
        self.assertEqual(fixture.Session.query(fixture.Audit).count(), 0)

    def test_supported_formats_and_disabled_url_are_valid(self):
        for suffix in ('png', 'jpg', 'jpeg', 'webp', 'PNG'):
            with self.subTest(suffix=suffix):
                current = self.read()
                result = self.save({RECEIPT_HEADER_KEY: {'enabled': False, 'image_url': 'https://brand.example.test/header.' + suffix}}, revision=current['revision'])
                self.assertIs(result['profile']['values'][RECEIPT_HEADER_KEY]['enabled'], False)

    def test_noncanonical_numeric_hosts_cannot_bypass_public_ip_check(self):
        for host in ('127.1', '127.000.000.001', '0x7f.0.0.1'):
            with self.subTest(host=host), self.assertRaises(ProfileSettingsError):
                validate_receipt_header({'enabled': True, 'image_url': 'https://' + host + '/campaign.png'})
        # Only URL shape is tested; there is no request to this public IP.
        value = {'enabled': True, 'image_url': 'https://8.8.8.8/campaign.png'}
        self.assertEqual(validate_receipt_header(value), value)

    def test_commit_failure_rolls_back_campaign_and_profile(self):
        with patch.object(fixture.Session.session_factory.class_, 'commit', side_effect=SQLAlchemyError('synthetic')):
            with self.assertRaises(ProfileSettingsError) as error:
                self.save({'ciudad': 'No guardar', RECEIPT_HEADER_KEY: {'enabled': True, 'image_url': IMAGE}})
        self.assertEqual(error.exception.code, 'profile_save_unconfirmed')
        fixture.Session.remove()
        self.assertEqual(self.read()['values'], self.initial['values'])
        self.assertEqual(fixture.Session.query(fixture.Audit).count(), 0)

    def test_existing_config_endpoint_returns_verified_header_ack(self):
        source = ast.parse((Path(fixture.ROOT) / 'routes/admin_tenant.py').read_text(encoding='utf-8'))
        handler = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'update_tenant_config_bundle')
        handler.decorator_list = []
        context = {'request': request, 'jsonify': jsonify, 'save_profile_settings': save_profile_settings,
                   'ProfileSettingsError': ProfileSettingsError, 'db': fixture.db,
                   'TenantProfile': fixture.Tenant, 'User': fixture.User, 'AuditEvent': fixture.Audit,
                   '_resolve_admin_tenant': lambda user, slug: fixture.Session.query(fixture.Tenant).filter_by(slug=slug).one_or_none(),
                   '_is_authorized_for_tenant': fixture.allowed, 'can_manage_tenant_control_plane': fixture.allowed}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'receipt-header-config-route', 'exec'), context)
        app = Flask('receipt-header-config-route-fixture')
        with app.test_request_context('/api/admin/tenants/tenant-a/config', method='PUT', json={
                'organization_profile': {RECEIPT_HEADER_KEY: {'enabled': True, 'image_url': IMAGE}},
                'expected_revision': self.initial['revision']}):
            response, status = context['update_tenant_config_bundle'](fixture.Session.get(fixture.User, 2), 'tenant-a')
        self.assertEqual(status, 200)
        self.assertTrue(response.json['ok'])
        self.assertEqual(response.json['profile']['values'][RECEIPT_HEADER_KEY], {'enabled': True, 'image_url': IMAGE})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_saved_settings_drive_real_resolver_inherit_enable_disable_reset(self):
        current = self.seed_header(None)
        tenant = fixture.Session.get(fixture.Tenant, 1)
        self.assertEqual(resolve_whatsapp_receipt_header(tenant), 'https://brand.example.test/legacy.jpg')
        enabled = self.save({RECEIPT_HEADER_KEY: {'enabled': True, 'image_url': IMAGE}}, revision=current['revision'])
        self.assertEqual(resolve_whatsapp_receipt_header(tenant), IMAGE)
        disabled = self.save({RECEIPT_HEADER_KEY: {'enabled': False, 'image_url': ''}}, revision=enabled['profile']['revision'])
        self.assertIsNone(resolve_whatsapp_receipt_header(tenant))
        restored = self.save({RECEIPT_HEADER_KEY: None}, revision=disabled['profile']['revision'])
        self.assertIsNone(restored['profile']['values'][RECEIPT_HEADER_KEY])
        self.assertEqual(resolve_whatsapp_receipt_header(tenant), 'https://brand.example.test/legacy.jpg')
        self.assertIsNone(resolve_whatsapp_receipt_header(fixture.Session.get(fixture.Tenant, 2)))


if __name__ == '__main__':
    unittest.main()
