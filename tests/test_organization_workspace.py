from copy import deepcopy
from types import SimpleNamespace
import unittest
from services.organization_workspace import build_organization_workspace, CONTRACT_VERSION


def tenant(**values):
    return SimpleNamespace(**({'id': 7, 'slug': 'org-demo', 'is_active': True,
        'tipo': 'municipio', 'whatsapp_sender_id': None} | values))


class OrganizationWorkspaceTests(unittest.TestCase):
    def test_explicit_verticals_have_distinct_identity(self):
        for kind, label in [('municipio','Municipio'),('gobierno','Gobierno'),
                ('colegio','Colegio'),('empresa','Empresa'),('pyme','Pyme')]:
            with self.subTest(kind=kind):
                result = build_organization_workspace(tenant(tipo=kind))
                self.assertEqual(result['organization_type'], kind)
                self.assertEqual(result['organization_label'], label)
                self.assertEqual(result['tenant'], {'id': 7, 'slug': 'org-demo'})

    def test_aliases_are_explicit_not_inferred_from_name(self):
        self.assertEqual(build_organization_workspace(tenant(tipo=' SCHOOL '))['organization_type'],'colegio')
        self.assertEqual(build_organization_workspace(tenant(tipo='government'))['organization_type'],'gobierno')
        result = build_organization_workspace(tenant(tipo='unknown', nombre='Colegio Gobierno Junin'))
        self.assertEqual(result['organization_type'],'organizacion')

    def test_invalid_scope_is_not_published(self):
        for values in ({'id':None},{'id':True},{'id':0},{'slug':'../other'},
                {'slug':'a/b'},{'slug':'a?tenant=b'},{'slug':''},{'is_active':False}):
            with self.subTest(values=values):
                self.assertIsNone(build_organization_workspace(tenant(**values)))
        self.assertIsNone(build_organization_workspace(None))

    def test_existing_sender_is_preserved_without_claiming_delivery(self):
        result = build_organization_workspace(tenant(whatsapp_sender_id='sender-private'))
        self.assertTrue(result['continuity']['whatsapp_registration_present'])
        self.assertTrue(result['continuity']['preserve_existing_account'])
        self.assertNotIn('sender-private',str(result))
        self.assertNotIn('ready',result)

    def test_a_contact_phone_does_not_become_a_sender(self):
        result = build_organization_workspace(tenant(telefono='+540000000'))
        self.assertFalse(result['continuity']['whatsapp_registration_present'])

    def test_new_connection_is_not_automatically_provisioned(self):
        result=build_organization_workspace(tenant(whatsapp_sender_id=' '))
        self.assertFalse(result['writes_performed'])
        self.assertFalse(result['provider_calls_performed'])
        self.assertNotIn('token',result)
        self.assertNotIn('permissions',result)
        self.assertNotIn('full',result)

    def test_payloads_are_independent_and_input_is_untouched(self):
        model=tenant(secret='never-publish',configuracion={'domain':'legacy'})
        before=deepcopy(vars(model))
        first=build_organization_workspace(model)
        first['sections'].clear()
        second=build_organization_workspace(model)
        self.assertEqual(vars(model),before)
        self.assertEqual(len(second['sections']),6)
        self.assertNotIn('never-publish',str(second))

    def test_contract_does_not_offer_writes_or_external_urls(self):
        result=build_organization_workspace(tenant())
        self.assertEqual(result['contract_version'],CONTRACT_VERSION)
        self.assertEqual([s['id'] for s in result['sections']],
            ['general','identity','location','hours','channels','plan-security'])
        self.assertNotIn('href',str(result))
        self.assertIn('no cambia el dominio',result['domain_note'])

if __name__ == '__main__':
    unittest.main()
