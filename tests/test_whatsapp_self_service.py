from copy import deepcopy
from types import SimpleNamespace
import json
import unittest
from services.whatsapp_self_service import build_whatsapp_self_service


class SelfServiceTests(unittest.TestCase):
    def tenant(self, **values):
        return SimpleNamespace(**({'id': 17, 'slug': 'sample-town', 'tipo': 'municipio', 'whatsapp_sender_id': None} | values))

    def test_existing_sender_resumes_without_new_registration(self):
        result = build_whatsapp_self_service(self.tenant(), {'sender_sid': 'XE-example'})
        self.assertEqual(result['mode'], 'existing_connection')
        self.assertTrue(result['existing_connection'])
        self.assertFalse(result['writes_performed'])
        self.assertFalse(result['provider_calls_performed'])

    def test_legacy_sender_on_tenant_is_preserved(self):
        result = build_whatsapp_self_service(self.tenant(whatsapp_sender_id='whatsapp:+15555550100'), {})
        self.assertTrue(result['existing_connection'])

    def test_only_requested_number_does_not_claim_existing_connection(self):
        result = build_whatsapp_self_service(self.tenant(), {'requested_phone_number': '+15555550100'})
        self.assertEqual(result['mode'], 'new_connection')
        self.assertFalse(result['existing_connection'])

    def test_inactive_provider_still_preserves_existing_registration(self):
        result = build_whatsapp_self_service(self.tenant(), {'sender_sid': 'XE-example', 'sender_status': 'OFFLINE'})
        self.assertTrue(result['existing_connection'])
        self.assertNotIn('production_ready', result)

    def test_each_vertical_has_its_own_generic_profile_label(self):
        labels = {'municipio': 'Perfil del municipio', 'gobierno': 'Perfil institucional',
                  'colegio': 'Perfil del colegio', 'empresa': 'Perfil de la empresa', 'pyme': 'Perfil de la pyme'}
        for kind, label in labels.items():
            with self.subTest(kind=kind):
                result = build_whatsapp_self_service(self.tenant(tipo=kind), {})
                self.assertEqual(result['sections'][0]['label'], label)

    def test_unknown_vertical_is_not_guessed(self):
        result = build_whatsapp_self_service(self.tenant(tipo='other'), {})
        self.assertEqual(result['organization_label'], 'Organización')

    def test_routes_are_scoped_to_the_current_organization(self):
        result = build_whatsapp_self_service(self.tenant(slug='second-tenant'), {})
        self.assertEqual(len(result['sections']), 4)
        self.assertTrue(all(item['href'].startswith('/t/second-tenant/') for item in result['sections']))
        self.assertEqual(result['tenant'], {'id': 17, 'slug': 'second-tenant'})

    def test_invalid_slug_does_not_produce_links(self):
        for slug in ('', None, '../other', 'a/b', 'a?tenant=other', 'a%2fother'):
            with self.subTest(slug=slug):
                self.assertIsNone(build_whatsapp_self_service(self.tenant(slug=slug), {}))

    def test_malformed_state_does_not_become_a_connection(self):
        self.assertFalse(build_whatsapp_self_service(self.tenant(), [])['existing_connection'])

    def test_secret_state_is_not_exposed(self):
        result = build_whatsapp_self_service(self.tenant(), {'sender_id': 'whatsapp:+15555550100', 'token': 'private-value'})
        serialized = json.dumps(result)
        self.assertNotIn('private-value', serialized)
        self.assertNotIn('+15555550100', serialized)
        self.assertNotIn('full', serialized.lower())

    def test_input_and_saved_configuration_are_untouched(self):
        state = {'sender_sid': 'XE-example', 'settings': {'color': 'existing'}}
        original = deepcopy(state)
        tenant = self.tenant(configuracion={'brand': 'existing-brand'})
        before = deepcopy(vars(tenant))
        build_whatsapp_self_service(tenant, state)
        self.assertEqual(state, original)
        self.assertEqual(vars(tenant), before)

    def test_same_input_is_deterministic(self):
        self.assertEqual(build_whatsapp_self_service(self.tenant(), {}), build_whatsapp_self_service(self.tenant(), {}))

if __name__ == '__main__':
    unittest.main()
