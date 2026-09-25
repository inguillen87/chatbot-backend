import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import preflight_crm_tasks as preflight


class TaskPreflightTests(unittest.TestCase):
    def test_live_source_has_the_expected_parent_without_importing_migrations(self):
        graph=preflight.source_graph()
        self.assertEqual(graph['active_heads'],[preflight.PARENT])
        self.assertEqual(graph['missing_parents'],[])
        self.assertFalse(graph['task_migration_active']);self.assertTrue(graph['task_parent_in_active_graph'])

    def test_wrong_host_is_rejected_before_connecting(self):
        with patch.object(preflight,'create_engine') as engine:
            with self.assertRaises(ValueError):
                preflight.inspect_database('postgresql://user:private@other.invalid/database','verified.invalid',preflight.PARENT,frozenset())
            engine.assert_not_called()

    def test_wrong_database_type_is_rejected_before_connecting(self):
        with patch.object(preflight,'create_engine') as engine:
            with self.assertRaises(ValueError):preflight.inspect_database('sqlite:///private.db','localhost',preflight.PARENT,frozenset())
            engine.assert_not_called()

    def run_cli(self,*flags):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        out=Path(directory.name)/'preflight.json'
        with patch('sys.argv',['preflight','--output',str(out),*flags]),contextlib.redirect_stdout(io.StringIO()):code=preflight.main()
        return code,json.loads(out.read_text(encoding='utf-8'))

    def test_offline_plan_never_connects_or_authorizes_activation(self):
        with patch.object(preflight,'inspect_database') as inspect:
            code,report=self.run_cli()
            self.assertEqual(code,0);inspect.assert_not_called()
            self.assertFalse(report['database_checked']);self.assertFalse(report['activation_authorized'])
            self.assertFalse(report['write_authorized'])

    def test_common_database_env_is_not_used_implicitly(self):
        with patch.dict('os.environ',{'DATABASE_URL':'postgresql://private@live.invalid/live','CRM_TASKS_PREFLIGHT_DATABASE_URL':''}),patch.object(preflight,'inspect_database') as inspect:
            code,report=self.run_cli('--inspect-database','--expected-database-host','live.invalid')
            self.assertEqual(code,1);self.assertEqual(report['error_code'],'explicit_database_url_required');inspect.assert_not_called()
            self.assertNotIn('private',json.dumps(report))

    def test_private_connection_failure_is_redacted(self):
        with patch.dict('os.environ',{'CRM_TASKS_PREFLIGHT_DATABASE_URL':'postgresql://user:private@live.invalid/live'}),patch.object(preflight,'inspect_database',side_effect=RuntimeError('private password and host')):
            code,report=self.run_cli('--inspect-database','--expected-database-host','live.invalid')
            self.assertEqual(code,1);self.assertEqual(report['error_code'],'preflight_failed')
            self.assertNotIn('password',json.dumps(report));self.assertNotIn('live.invalid',json.dumps(report))

    def test_invalid_tenant_list_fails_before_database_access(self):
        with patch.object(preflight,'inspect_database') as inspect:
            code,report=self.run_cli('--inspect-database','--tenant-ids','1,*')
            self.assertEqual(code,1);self.assertEqual(report['error_code'],'invalid_tenant_selection');inspect.assert_not_called()


class TaskPreflightLineageTests(unittest.TestCase):
    run_cli = TaskPreflightTests.run_cli
    def test_other_release_migration_graph_is_not_accepted(self):
        with patch.object(preflight,'source_graph',return_value={'active_heads':['unrelated_release_head'],'missing_parents':[]}):
            code,report=self.run_cli()
            self.assertEqual(code,1);self.assertEqual(report['error_code'],'unexpected_migration_graph')

    def test_missing_migration_ancestor_is_not_accepted(self):
        with patch.object(preflight,'source_graph',return_value={'active_heads':[preflight.PARENT],'missing_parents':['missing']}):
            code,report=self.run_cli()
            self.assertEqual(code,1);self.assertEqual(report['error_code'],'unexpected_migration_graph')

if __name__=='__main__':unittest.main(verbosity=2)
