"""Pure presentation contract tests; no providers, database or customer writes."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from services.organization_branding_ui import CONFIG_KEY, CONTRACT, TEXTS, build_brand_workflow_ui
from services.organization_branding import build_branding
from services.public_tenant_config import sanitize_public_tenant_config

def tenant(overrides=None):
    return SimpleNamespace(id=1,slug='copy-test',is_active=True,configuracion={CONFIG_KEY:overrides or {}})

class BrandWorkflowUITests(unittest.TestCase):
    def test_contract_contains_complete_server_vocabulary(self):
        result=build_brand_workflow_ui(tenant())
        self.assertEqual(result['contract_version'],CONTRACT)
        self.assertEqual(result['texts'],TEXTS)
        self.assertEqual(len(result['texts']),70)
    def test_tenant_overrides_are_scoped_and_cannot_grant_rights(self):
        first=tenant({'discard_action':'Descartar identidad local','can_edit':True})
        brand=build_branding(first,can_edit=False,entitled=False)
        self.assertEqual(brand['workflow_ui']['texts']['discard_action'],'Descartar identidad local')
        self.assertFalse(brand['can_edit'])
        self.assertNotIn('can_edit',brand['workflow_ui']['texts'])
        self.assertEqual(build_brand_workflow_ui(tenant())['texts']['discard_action'],TEXTS['discard_action'])
    def test_invalid_overrides_use_server_defaults(self):
        for value in ('', 'x'*601, '<b>Invalid</b>', chr(0), 5, {'text':'invalid'}, 'Unknown {token}'):
            with self.subTest(value=value):
                self.assertEqual(build_brand_workflow_ui(tenant({'discard_action':value}))['texts']['discard_action'],TEXTS['discard_action'])
    def test_required_placeholders_survive_customization(self):
        valid=build_brand_workflow_ui(tenant({'restore_title':'Restore snapshot {version}'}))
        self.assertEqual(valid['texts']['restore_title'],'Restore snapshot {version}')
        for invalid in ('Restore','Restore {tenant}','Restore {version} {other}'):
            self.assertEqual(build_brand_workflow_ui(tenant({'restore_title':invalid}))['texts']['restore_title'],TEXTS['restore_title'])
    def test_returned_dictionary_is_detached(self):
        first=build_brand_workflow_ui(tenant())
        first['texts']['discard_action']='Modified response'
        self.assertEqual(build_brand_workflow_ui(tenant())['texts']['discard_action'],TEXTS['discard_action'])
    def test_administration_copy_never_leaks_in_public_config(self):
        public=sanitize_public_tenant_config({'keep':'public',CONFIG_KEY:{'discard_action':'Internal'},'nested':{CONFIG_KEY:{'secret':'private'}}})
        self.assertEqual(public,{'keep':'public','nested':{}})
    def test_read_does_not_modify_storage_or_palette_revision(self):
        row=tenant();before=deepcopy(row.configuracion)
        first=build_branding(row,can_edit=True,entitled=True)
        self.assertEqual(row.configuracion,before)
        row.configuracion[CONFIG_KEY]={'publish_action':'Publish organization palette'}
        second=build_branding(row,can_edit=True,entitled=True)
        self.assertEqual(first['revision'],second['revision'])
        self.assertEqual(first['values'],second['values'])
        self.assertEqual(second['workflow_ui']['texts']['publish_action'],'Publish organization palette')
    def test_non_dictionary_configuration_never_supplies_ui_values(self):
        row=tenant();row.configuracion=[]
        self.assertEqual(build_brand_workflow_ui(row)['texts'],TEXTS)

if __name__=='__main__':unittest.main()
