"""Execute only the new migration on an isolated in-memory database."""
import importlib.util
from pathlib import Path
import unittest
import shutil
import tempfile
from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from alembic.migration import MigrationContext
from alembic.operations import Operations


class MethodologyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine=sa.create_engine('sqlite://')
        self.connection=self.engine.connect()
        self.connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        self.connection.exec_driver_sql('CREATE TABLE user (id INTEGER PRIMARY KEY)')
        self.connection.exec_driver_sql('CREATE TABLE enc_encuesta (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, UNIQUE(tenant_id,id))')
        self.connection.exec_driver_sql('INSERT INTO user(id) VALUES(1)')
        self.connection.exec_driver_sql('INSERT INTO enc_encuesta(id,tenant_id) VALUES(301,7)')
        path=Path(__file__).resolve().parents[1]/'migrations/pending/20260922_add_survey_methodology_v1.py'
        spec=importlib.util.spec_from_file_location('methodology_migration_under_test',path)
        self.migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.migration)
        self.migration.op=Operations(MigrationContext.configure(self.connection))
        self.migration.upgrade();self.connection.commit()
    def tearDown(self):
        self.connection.rollback();self.connection.close();self.engine.dispose()
    def insert(self,tenant=7,revision=1,actor=1):
        sql=("INSERT INTO survey_methodology_revision "
          "(tenant_id,survey_id,revision,instrument_revision,fields,change_reason,actor_user_id,created_at,digest,previous_digest,operation_digest) "
          "VALUES (:tenant,301,:revision,1,'{}','Migration fixture',:actor,'2026-09-22T00:00:00',:digest,NULL,:digest)")
        self.connection.execute(sa.text(sql),{'tenant':tenant,'revision':revision,'actor':actor,'digest':'a'*64})
    def test_new_table_has_compound_scope_and_version_constraints(self):
        info=sa.inspect(self.connection)
        self.assertIn('survey_methodology_revision',info.get_table_names())
        self.assertIn('uq_methodology_revision',{x['name'] for x in info.get_unique_constraints('survey_methodology_revision')})
        self.assertIn('ix_methodology_scope_revision',{x['name'] for x in info.get_indexes('survey_methodology_revision')})
        self.insert();self.connection.commit()
    def test_database_rejects_cross_tenant_survey_reference(self):
        with self.assertRaises(IntegrityError):self.insert(tenant=8)
    def test_database_rejects_duplicate_revisions(self):
        self.insert();self.connection.commit()
        with self.assertRaises(IntegrityError):self.insert()
    def test_database_rejects_invalid_revision_and_actor(self):
        with self.assertRaises(IntegrityError):self.insert(revision=0)
        self.connection.rollback()
        with self.assertRaises(IntegrityError):self.insert(actor=99)
    def test_survey_delete_cannot_cascade_away_methodology(self):
        self.insert();self.connection.commit()
        with self.assertRaises(IntegrityError):self.connection.exec_driver_sql('DELETE FROM enc_encuesta WHERE id=301')
    def test_downgrade_refuses_to_destroy_history(self):
        self.insert();self.connection.commit()
        with self.assertRaisesRegex(RuntimeError,'refusing destructive'):self.migration.downgrade()
        self.assertEqual(self.connection.exec_driver_sql('SELECT COUNT(*) FROM survey_methodology_revision').scalar_one(),1)
    def test_empty_table_can_be_downgraded_without_dropping_parent_records(self):
        self.migration.downgrade()
        self.assertNotIn('survey_methodology_revision',sa.inspect(self.connection).get_table_names())
        self.assertEqual(self.connection.exec_driver_sql('SELECT COUNT(*) FROM enc_encuesta').scalar_one(),1)


class MethodologyRolloutBoundaryTests(unittest.TestCase):
    def test_pending_migration_does_not_advance_the_reviewed_cutover(self):
        from scripts.preflight_neon_cutover import REVIEWED_MIGRATION_HEAD, _single_migration_head
        root=Path(__file__).resolve().parents[1]
        config=Config(str(root/'alembic.ini'))
        config.set_main_option('script_location',str(root/'migrations'))
        graph=ScriptDirectory.from_config(config)
        self.assertEqual(_single_migration_head(graph),REVIEWED_MIGRATION_HEAD)
        self.assertNotIn('20260922_methodology_v1',[entry.revision for entry in graph.walk_revisions()])
        self.assertTrue((root/'migrations/pending/20260922_add_survey_methodology_v1.py').is_file())

    def test_promoting_without_review_still_fails_closed(self):
        from scripts.preflight_neon_cutover import PreflightFailure, _single_migration_head
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='methodology-graph-') as directory:
            location=Path(directory)/'migrations'
            shutil.copytree(root/'migrations',location,ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(location/'pending/20260922_add_survey_methodology_v1.py',location/'versions/20260922_add_survey_methodology_v1.py')
            config=Config(str(root/'alembic.ini'))
            config.set_main_option('script_location',str(location))
            with self.assertRaises(PreflightFailure) as raised:
                _single_migration_head(ScriptDirectory.from_config(config))
            self.assertEqual(raised.exception.reason_code,'local_migration_head_not_allowlisted')


if __name__=='__main__':unittest.main(verbosity=2)
