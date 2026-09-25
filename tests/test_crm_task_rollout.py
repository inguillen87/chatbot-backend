import unittest
from services.crm_task_rollout import selected_tenants, task_rollout_decision


class TaskRolloutTests(unittest.TestCase):
    def test_default_and_global_switch_alone_enable_nobody(self):
        for config in ({}, {'CRM_TASKS_ENABLED':True}, {'CRM_TASKS_ENABLED':'true','CRM_TASKS_TENANT_IDS':''}):
            with self.subTest(config=config):
                self.assertFalse(task_rollout_decision(config,1)['eligible'])

    def test_only_explicit_ids_are_selected(self):
        config={'CRM_TASKS_ENABLED':True,'CRM_TASKS_TENANT_IDS':'7, 42'}
        self.assertTrue(task_rollout_decision(config,7)['eligible'])
        self.assertTrue(task_rollout_decision(config,42)['eligible'])
        self.assertFalse(task_rollout_decision(config,8)['eligible'])
        self.assertNotIn('tenant_ids',task_rollout_decision(config,7))

    def test_invalid_config_does_not_partially_enable_valid_prefix(self):
        for raw in ('7,*','7,all','7,','7,,42','07','+7','7.0','7e0','0','-7','2147483648',True,[7],{'7':True},'7,'*3000):
            with self.subTest(raw=str(raw)[:30]):
                result=task_rollout_decision({'CRM_TASKS_ENABLED':True,'CRM_TASKS_TENANT_IDS':raw},7)
                self.assertFalse(result['eligible']);self.assertEqual(result['reason_code'],'invalid_rollout_config')

    def test_kill_switch_overrides_selection(self):
        for flag in (False,None,0,1,'1','yes','false','enabled'):
            with self.subTest(flag=flag):
                self.assertFalse(task_rollout_decision({'CRM_TASKS_ENABLED':flag,'CRM_TASKS_TENANT_IDS':'7'},7)['eligible'])

    def test_invalid_identity_cannot_match_by_string_or_bool_coercion(self):
        for identity in (True,'7',7.0,None,0,-1):
            with self.subTest(identity=identity):
                result=task_rollout_decision({'CRM_TASKS_ENABLED':True,'CRM_TASKS_TENANT_IDS':'7,1'},identity)
                self.assertFalse(result['eligible']);self.assertEqual(result['reason_code'],'invalid_tenant')

    def test_supported_switch_values_are_exact(self):
        for flag in (True,'true','TRUE',' true '):
            with self.subTest(flag=flag):
                self.assertTrue(task_rollout_decision({'CRM_TASKS_ENABLED':flag,'CRM_TASKS_TENANT_IDS':'7'},7)['eligible'])

    def test_empty_selection_is_valid_but_disabled(self):
        self.assertEqual(selected_tenants(' '),(frozenset(),True))
        self.assertFalse(task_rollout_decision({'CRM_TASKS_ENABLED':True,'CRM_TASKS_TENANT_IDS':' '},7)['eligible'])

if __name__=='__main__':unittest.main(verbosity=2)
