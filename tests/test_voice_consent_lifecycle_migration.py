import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260730_add_voice_consent_lifecycle_v1.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("voice_consent_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run(module, connection, name):
    context = MigrationContext.configure(connection)
    operations = Operations(context)
    original_op = module.op
    module.op = operations
    try:
        getattr(module, name)()
    finally:
        module.op = original_op


def _bootstrap_tenant(connection):
    connection.execute(
        sa.text(
            "CREATE TABLE tenant_profile (id INTEGER PRIMARY KEY, slug VARCHAR(255) NOT NULL)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO tenant_profile (id, slug) VALUES "
            "(1, 'tenant-a'), (2, 'tenant-b')"
        )
    )


def test_voice_consent_migration_has_single_current_head_and_remains_ancestor():
    migration = _load_migration()
    assert migration.revision == "20260730_voice_consent_v1"
    assert migration.down_revision == "20260730_survey_governance_v1"

    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    revisions = {}
    revision_parents = {}
    down_revisions = set()
    for path in versions.glob("*.py"):
        if path.name.startswith("__"):
            continue
        spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        revision = getattr(module, "revision", None)
        if revision:
            revisions[revision] = path
        down = getattr(module, "down_revision", None)
        revision_parents[revision] = down
        if isinstance(down, (tuple, list, set)):
            down_revisions.update(item for item in down if item)
        elif down:
            down_revisions.add(down)
    heads = sorted(set(revisions) - down_revisions)
    assert len(heads) == 1
    lineage = set()
    pending = [heads[0]]
    while pending:
        cursor = pending.pop()
        if not cursor or cursor in lineage:
            continue
        lineage.add(cursor)
        parent = revision_parents.get(cursor)
        if isinstance(parent, (tuple, list, set)):
            pending.extend(item for item in parent if item)
        elif parent:
            pending.append(parent)
    assert "20260730_voice_consent_v1" in lineage


def test_voice_lifecycle_db_guards_recording_terminal_state_and_audit_events():
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    now = "2026-07-30 22:00:00"
    with engine.begin() as connection:
        _bootstrap_tenant(connection)
        _run(migration, connection, "upgrade")

        connection.execute(
            sa.text(
                """
                INSERT INTO voice_call_lifecycle (
                    id, tenant_id, provider, provider_call_sid, direction, state,
                    consent_status, consent_policy_version, ai_processing_allowed,
                    recording_allowed, recording_enabled, created_at, updated_at, terminal_at
                ) VALUES (
                    10, 1, 'twilio', 'CA-migration-001', 'inbound', 'consent_pending',
                    'required', 'voice.consent.v1', 0, 0, 0, :now, :now, NULL
                )
                """
            ),
            {"now": now},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO voice_call_lifecycle (
                            id, tenant_id, provider, provider_call_sid, direction, state,
                            consent_status, consent_policy_version, ai_processing_allowed,
                            recording_allowed, recording_enabled, created_at, updated_at, terminal_at
                        ) VALUES (
                            11, 2, 'twilio', 'CA-migration-001', 'inbound', 'consent_pending',
                            'required', 'voice.consent.v1', 0, 0, 0, :now, :now, NULL
                        )
                        """
                    ),
                    {"now": now},
                )
        connection.execute(
            sa.text(
                """
                INSERT INTO voice_call_lifecycle_event (
                    id, tenant_id, lifecycle_id, event_key, state, reason_code,
                    provider_status, policy_version, created_at
                ) VALUES (
                    20, 1, 10, 'consent:required', 'consent_pending',
                    'explicit_consent_required', NULL, 'voice.consent.v1', :now
                )
                """
            ),
            {"now": now},
        )

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE voice_call_lifecycle SET recording_enabled = 1 WHERE id = 10"
                    )
                )

        connection.execute(
            sa.text(
                """
                UPDATE voice_call_lifecycle
                SET state = 'completed', terminal_at = :now, last_provider_status = 'completed'
                WHERE id = 10
                """
            ),
            {"now": now},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE voice_call_lifecycle SET state = 'stream_authorized', terminal_at = NULL WHERE id = 10"
                    )
                )

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        "UPDATE voice_call_lifecycle_event SET reason_code = 'tampered' WHERE id = 20"
                    )
                )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.text("DELETE FROM voice_call_lifecycle_event WHERE id = 20")
                )

        lifecycle = connection.execute(
            sa.text(
                "SELECT state, recording_allowed, recording_enabled FROM voice_call_lifecycle WHERE id = 10"
            )
        ).mappings().one()
        assert lifecycle == {
            "state": "completed",
            "recording_allowed": 0,
            "recording_enabled": 0,
        }
        event = connection.execute(
            sa.text("SELECT reason_code FROM voice_call_lifecycle_event WHERE id = 20")
        ).scalar_one()
        assert event == "explicit_consent_required"

        _run(migration, connection, "downgrade")
        tables = set(sa.inspect(connection).get_table_names())
        assert "voice_call_lifecycle" not in tables
        assert "voice_call_lifecycle_event" not in tables
