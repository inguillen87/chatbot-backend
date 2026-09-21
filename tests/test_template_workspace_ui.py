"""Presentation contract regression: no app, DB or provider required."""
import copy
import hashlib
import json
import re
import unittest
from services.message_templates import whatsapp_template_pack_catalog, WHATSAPP_TEMPLATE_WORKSPACE_UI


class TemplateWorkspaceUITests(unittest.TestCase):
    def test_versioned_ui_is_published_in_existing_catalog(self):
        catalog = whatsapp_template_pack_catalog()
        self.assertEqual(catalog['contract_version'], 'whatsapp.template_pack.catalog.v1')
        self.assertEqual(catalog['frontend_contract']['workspace_ui'], WHATSAPP_TEMPLATE_WORKSPACE_UI)
        self.assertEqual(WHATSAPP_TEMPLATE_WORKSPACE_UI['contract_version'], 'whatsapp.template_pack.workspace_ui.v1')

    def test_original_pack_definitions_and_default_lifecycles_are_byte_equivalent(self):
        encoded = json.dumps(whatsapp_template_pack_catalog()['packs'], ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
            'c7e5f31cad112e1339dc45d88f36db728e1623c8d9064b82b6183450e51168fc')

    def test_copy_has_only_the_declared_presentation_fields(self):
        self.assertEqual(len(WHATSAPP_TEMPLATE_WORKSPACE_UI), 26)
        self.assertTrue(all(isinstance(value, str) and value.strip() and len(value) <= 1800
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
            for value in WHATSAPP_TEMPLATE_WORKSPACE_UI.values()))
        for forbidden in ['tenant', 'capabilities', 'access_token', 'provider', 'api_key']:
            self.assertNotIn(forbidden, WHATSAPP_TEMPLATE_WORKSPACE_UI)

    def test_counter_requires_exactly_two_display_placeholders(self):
        self.assertEqual(sorted(re.findall(r'\{[^{}]*\}', WHATSAPP_TEMPLATE_WORKSPACE_UI['results'])),
            ['{total}', '{visible}'])

    def test_callers_cannot_mutate_later_catalog_presentation(self):
        first = whatsapp_template_pack_catalog()
        first['frontend_contract']['workspace_ui']['confirm_action'] = 'changed'
        second = whatsapp_template_pack_catalog()
        self.assertNotEqual(second['frontend_contract']['workspace_ui']['confirm_action'], 'changed')

    def test_copy_does_not_replace_legacy_clients_contract(self):
        catalog = whatsapp_template_pack_catalog()
        self.assertIn('copy', catalog['frontend_contract'])
        self.assertIn('materialize', catalog['frontend_contract']['copy'])
        self.assertIn('approved', catalog['frontend_contract']['lifecycle_labels'])
        self.assertEqual(catalog['summary']['templates'], 15)

    def test_drafts_are_not_approved_or_sent_by_the_new_presentation(self):
        catalog = whatsapp_template_pack_catalog()
        self.assertFalse(catalog['provider_calls_performed'])
        self.assertFalse(catalog['policy']['provider_calls_allowed'])
        for pack in catalog['packs']:
            for item in pack['templates']:
                self.assertEqual(item['lifecycle']['state'], 'local_draft')
                self.assertFalse(item['lifecycle']['production_send_allowed'])
                self.assertFalse(item['materialized'])

    def test_confirmation_explains_full_pack_and_no_provider_submission(self):
        detail = WHATSAPP_TEMPLATE_WORKSPACE_UI['confirm_description']
        self.assertIn('conjunto completo', detail)
        self.assertIn('no sólo las plantillas visibles', detail)
        self.assertIn('No se enviarán mensajes', detail)
        self.assertIn('aprobación al proveedor', detail)

    def test_registry_argument_is_not_modified(self):
        registry = {'unrelated': {'status': 'local_draft'}}
        before = copy.deepcopy(registry)
        whatsapp_template_pack_catalog(registry)
        self.assertEqual(registry, before)

    def test_new_ui_does_not_raise_read_or_write_permissions(self):
        catalog = whatsapp_template_pack_catalog()
        self.assertNotIn('capabilities', catalog['frontend_contract']['workspace_ui'])
        self.assertNotIn('production_send_allowed', catalog['frontend_contract']['workspace_ui'])
        self.assertIn('no envía mensajes', catalog['frontend_contract']['workspace_ui']['provider_notice'])


if __name__ == '__main__':
    unittest.main()
