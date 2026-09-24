"""PostgreSQL checks for guard drift and strictly read-only rollout inspection."""
from sqlalchemy import text


class PostgresReadinessTests:
    def test_08_noop_guard_with_expected_name_is_not_ready(self):
        with self.app.app_context(),self.db.engine.connect() as connection:
            transaction=connection.begin()
            try:
                connection.execute(text("CREATE OR REPLACE FUNCTION crm_task_event_immutable() RETURNS trigger AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql"))
                self.assertFalse(self.readiness.inspect_task_schema(connection))
            finally:transaction.rollback()
        with self.app.app_context():self.assertTrue(self.readiness.task_schema_ready())

    def test_09_disabled_guard_is_not_ready(self):
        with self.app.app_context(),self.db.engine.connect() as connection:
            transaction=connection.begin()
            try:
                connection.execute(text('ALTER TABLE crm_task_event DISABLE TRIGGER crm_task_event_immutable'))
                self.assertFalse(self.readiness.inspect_task_schema(connection))
            finally:transaction.rollback()

    def test_10_missing_delete_event_is_not_ready(self):
        with self.app.app_context(),self.db.engine.connect() as connection:
            transaction=connection.begin()
            try:
                connection.execute(text('DROP TRIGGER crm_task_event_immutable ON crm_task_event'))
                connection.execute(text('CREATE TRIGGER crm_task_event_immutable BEFORE UPDATE ON crm_task_event FOR EACH ROW EXECUTE FUNCTION crm_task_event_immutable()'))
                self.assertFalse(self.readiness.inspect_task_schema(connection))
            finally:transaction.rollback()

    def test_11_preflight_reads_verified_schema_and_revision_without_writes(self):
        from scripts.preflight_crm_tasks import database_report, PARENT
        with self.app.app_context(),self.db.engine.begin() as connection:
            connection.execute(text('CREATE TABLE IF NOT EXISTS alembic_version(version_num varchar(128) PRIMARY KEY)'))
            connection.execute(text('DELETE FROM alembic_version'))
            connection.execute(text('INSERT INTO alembic_version(version_num) VALUES (:version)'),{'version':PARENT})
        with self.app.app_context(),self.db.engine.connect() as connection:
            with connection.begin():
                connection.execute(text('SET TRANSACTION READ ONLY'))
                before=connection.execute(text('SELECT count(*) FROM crm_task_event')).scalar()
                report=database_report(connection,PARENT,frozenset({1}))
                self.assertTrue(report['expected_revision_matches']);self.assertTrue(report['task_schema_ready'])
                self.assertTrue(report['all_requested_tenants_exist'])
                self.assertEqual(connection.execute(text('SHOW transaction_read_only')).scalar(),'on')
                self.assertEqual(connection.execute(text('SELECT count(*) FROM crm_task_event')).scalar(),before)

    def test_12_preflight_exposes_wrong_revision_and_missing_tenant(self):
        from scripts.preflight_crm_tasks import database_report
        with self.app.app_context(),self.db.engine.connect() as connection:
            with connection.begin():
                connection.execute(text('SET TRANSACTION READ ONLY'))
                report=database_report(connection,'uninstalled_revision',frozenset({9999}))
                self.assertFalse(report['expected_revision_matches']);self.assertFalse(report['all_requested_tenants_exist'])

    def test_13_replica_session_cannot_certify_ordinary_guards(self):
        with self.app.app_context(),self.db.engine.connect() as connection:
            transaction=connection.begin()
            try:
                connection.execute(text('SET LOCAL session_replication_role=replica'))
                self.assertFalse(self.readiness.inspect_task_schema(connection))
            finally:transaction.rollback()
