import importlib.util
from pathlib import Path
import unittest

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
import sqlalchemy as sa

from models import MetaDirectSignupSession
from scripts.preflight_neon_cutover import _load_migration_directory, _single_migration_head


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "pending" / "20261121_add_meta_direct_signup_session.py"
TABLE = MetaDirectSignupSession.__table__


class MetaDirectSignupMigrationTests(unittest.TestCase):
    def setUp(self):
        engine = sa.create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        self.connection = engine.connect()
        self.addCleanup(self.connection.close)
        self.connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        for name in ("tenant_profile", "user", "provider_connection", "provider_sender"):
            self.connection.exec_driver_sql(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY)')
            self.connection.exec_driver_sql(f'INSERT INTO "{name}" (id) VALUES (1), (2)')
        spec = importlib.util.spec_from_file_location("meta_signup_migration", MIGRATION)
        self.migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.migration)
        self.migration.op = Operations(MigrationContext.configure(self.connection))
        self.migration.upgrade()

    def insert(self, serial, **values):
        row = dict(state_hash=f"{serial:064x}", tenant_id=1, actor_user_id=1,
                   created_at_epoch=10, status="issued")
        self.connection.execute(TABLE.insert().values(**(row | values)))

    def test_candidate_descends_from_current_reviewed_head_without_changing_it(self):
        graph = _load_migration_directory(ROOT)
        self.assertEqual(self.migration.down_revision, _single_migration_head(graph))
        self.assertNotIn(self.migration.revision, {item.revision for item in graph.walk_revisions()})

    def test_schema_matches_model_and_rejects_invalid_references_and_state(self):
        schema = sa.inspect(self.connection)
        self.assertEqual(
            {col["name"]: (str(col["type"]), col["nullable"]) for col in schema.get_columns(TABLE.name)},
            {col.name: (str(col.type), col.nullable) for col in TABLE.columns},
        )
        self.assertEqual(schema.get_pk_constraint(TABLE.name)["constrained_columns"], ["state_hash"])
        self.assertEqual(
            {tuple(key["column_names"]) for key in schema.get_unique_constraints(TABLE.name)},
            {("provider_session_hash",), ("code_hash",)},
        )
        for column in ("tenant_id", "actor_user_id", "connection_id", "sender_id"):
            with self.assertRaises(sa.exc.IntegrityError, msg=column):
                self.insert(1, **{column: 99})
        with self.assertRaises(sa.exc.IntegrityError):
            self.insert(1, status="unknown")
        for serial, status in enumerate(("issued", "exchanging", "pending", "connected", "rejected")):
            self.insert(serial, status=status)

    def test_both_replay_digests_are_global_and_nullable_at_issuance(self):
        self.insert(1)
        self.insert(2, tenant_id=2)
        self.insert(3, provider_session_hash="a" * 64, code_hash="b" * 64)
        for key, digest in (("provider_session_hash", "a" * 64), ("code_hash", "b" * 64)):
            with self.assertRaises(sa.exc.IntegrityError, msg=key):
                self.insert(4, tenant_id=2, **{key: digest})

    def test_rollback_preserves_replay_history_and_allows_reverting_an_empty_table(self):
        self.connection.commit()
        self.insert(1)
        with self.assertRaisesRegex(RuntimeError, "populated"):
            self.migration.downgrade()
        self.assertEqual(self.connection.scalar(sa.select(sa.func.count()).select_from(TABLE)), 1)
        self.connection.rollback()
        self.migration.downgrade()
        self.assertNotIn(TABLE.name, sa.inspect(self.connection).get_table_names())
