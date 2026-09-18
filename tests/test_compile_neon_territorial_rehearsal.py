import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import compile_neon_territorial_rehearsal as compiler

ROOT = Path(__file__).resolve().parents[1]
TARGET = dict(project_id='project-test', branch_id='br-child',
    source_branch_id='br-parent', database_name='render_rehearsal_test')


class TerritorialRehearsalCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = compiler.compile_rehearsal(ROOT, **TARGET)
        cls.statements = cls.report['sql_statements']
        cls.sql = '\n'.join(cls.statements)

    def test_exact_reviewed_three_steps(self):
        self.assertEqual(self.report['revisions'], list(compiler.REVISIONS))
        self.assertEqual(len(self.report['source_sha256']), 3)
        self.assertEqual(len(self.report['graph_sha256']), 64)

    def test_compilation_is_deterministic(self):
        self.assertEqual(self.report, compiler.compile_rehearsal(ROOT, **TARGET))

    def test_source_branch_cannot_be_target(self):
        with self.assertRaisesRegex(ValueError, 'isolated'):
            compiler.compile_rehearsal(ROOT, **{**TARGET, 'branch_id':'br-parent'})

    def test_production_database_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'isolated'):
            compiler.compile_rehearsal(ROOT, **{**TARGET, 'database_name':'neondb'})

    def test_sql_injection_characters_are_rejected(self):
        for field in TARGET:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'invalid'):
                compiler.compile_rehearsal(ROOT, **{**TARGET, field:"x'; DROP TABLE y;--"})

    def test_identity_and_revision_guards_precede_mutations(self):
        self.assertIn('rehearsal_identity_mismatch', self.statements[6])
        self.assertIn('rehearsal_revision_mismatch', self.statements[6])
        self.assertIn('pg_try_advisory_xact_lock', self.statements[6])
        self.assertLess(self.sql.index('rehearsal_identity_mismatch'), self.sql.index('CREATE TABLE territorial'))

    def test_all_migration_writes_are_inside_savepoint(self):
        start = self.statements.index('SAVEPOINT reviewed_territorial_rehearsal')
        end = self.statements.index('ROLLBACK TO SAVEPOINT reviewed_territorial_rehearsal')
        for i, sql in enumerate(self.statements):
            if sql.startswith(('CREATE TABLE territorial', 'CREATE INDEX', 'UPDATE public.')):
                self.assertTrue(start < i < end)
        self.assertEqual(sum(s.startswith('CREATE TABLE territorial') for s in self.statements), 4)

    def test_has_no_apply_or_commit_statement(self):
        self.assertFalse(self.report['cutover_authorized'])
        self.assertTrue(self.report['rollback_required'])
        self.assertFalse(any(s.startswith(('COMMIT', 'DROP ', 'TRUNCATE ', 'DELETE ')) for s in self.statements))

    def test_dynamic_sql_concatenation_survives_whitespace_normalization(self):
        snapshots = [s for s in self.statements if 'EXECUTE format' in s]
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all("' ||" in s for s in snapshots))

    def test_rollback_checks_every_original_table_with_content_hashes(self):
        self.assertIn('row_to_json(r)::text', self.sql)
        self.assertIn('rollback_data_changed', self.sql)
        self.assertIn('rollback_tables_changed', self.sql)
        self.assertIn('sha256', self.sql)
        self.assertIn('false AS schema_committed', self.sql)

    def test_required_schema_and_index_checks_are_present(self):
        self.assertEqual(sum('territorial_columns_mismatch' in s for s in self.statements), 4)
        self.assertEqual(sum('territorial_indexes_mismatch' in s for s in self.statements), 4)
        self.assertEqual(sum('territorial_index_not_ready' in s for s in self.statements), 4)

    def test_fingerprint_failure_is_not_suppressed(self):
        with patch.object(compiler.gate, '_load_exact_migration_plan',
                side_effect=ValueError('source_fingerprint_changed')):
            with self.assertRaisesRegex(ValueError, 'source_fingerprint_changed'):
                compiler.compile_rehearsal(ROOT, **TARGET)

if __name__ == '__main__':
    unittest.main()
