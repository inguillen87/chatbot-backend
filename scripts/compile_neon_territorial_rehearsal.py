"""Compile a credential-free, rollback-only territorial rehearsal for Neon.

Execute the emitted statements in ONE transaction on an isolated child branch.
The production migration runner and its approval gates are unchanged.
"""
from __future__ import annotations
import argparse
import io
import json
import re
from pathlib import Path
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from scripts import apply_neon_cutover_migrations as gate

REVISIONS = (gate.TERRITORIAL_GEOCODING_REVISION,
    gate.TERRITORIAL_GEOCODING_REVIEW_REVISION, gate.TERRITORIAL_GEOCODING_SYNC_REVISION)
CONTRACT = 'chatboc.territorial_rehearsal_sql.v1'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def compile_rehearsal(root, *, project_id, branch_id, source_branch_id, database_name):
    if not all(re.fullmatch(r'[a-z0-9_-]{3,80}', v) for v in
            (project_id, branch_id, source_branch_id, database_name)):
        raise ValueError('invalid_target_identity')
    if branch_id == source_branch_id or not database_name.startswith('render_rehearsal_'):
        raise ValueError('isolated_rehearsal_target_required')
    plan = gate._load_exact_migration_plan(Path(root))
    steps = ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
        "SET LOCAL statement_timeout='120s'", "SET LOCAL lock_timeout='5s'",
        "SET LOCAL idle_in_transaction_session_timeout='60s'", "SET LOCAL TIME ZONE 'UTC'",
        "SET LOCAL search_path=public,pg_catalog"]
    steps.append(f"""DO $$ BEGIN
      IF current_database() <> {literal(database_name)} OR
        current_setting('neon.project_id',true) IS DISTINCT FROM {literal(project_id)} OR
        current_setting('neon.branch_id',true) IS DISTINCT FROM {literal(branch_id)} THEN
        RAISE EXCEPTION 'rehearsal_identity_mismatch'; END IF;
      IF (SELECT array_agg(version_num::text) FROM public.alembic_version)
        IS DISTINCT FROM ARRAY[{literal(gate.GLOBAL_WRITER_AUTHORITY_REVISION)}] THEN
        RAISE EXCEPTION 'rehearsal_revision_mismatch'; END IF;
      IF NOT pg_try_advisory_xact_lock({gate.ADVISORY_LOCK_KEY}) THEN
        RAISE EXCEPTION 'rehearsal_lock_unavailable'; END IF;
    END $$""")
    steps.append("CREATE TEMP TABLE rehearsal_before (name text PRIMARY KEY, rows bigint, digest text) ON COMMIT DROP")
    steps.append("""DO $$ DECLARE t record; BEGIN
      FOR t IN SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' LOOP
        EXECUTE format('INSERT INTO rehearsal_before SELECT %L, count(*), ' ||
          'encode(sha256(convert_to(COALESCE(string_agg(d, %L ORDER BY d), %L), %L)), %L) ' ||
          'FROM (SELECT encode(sha256(convert_to(row_to_json(r)::text, %L)), %L) d ' ||
          'FROM public.%I r) h', t.tablename, '', '', 'UTF8', 'hex', 'UTF8', 'hex', t.tablename);
      END LOOP;
    END $$""")
    steps.append("SAVEPOINT reviewed_territorial_rehearsal")
    previous = gate.GLOBAL_WRITER_AUTHORITY_REVISION
    for revision in REVISIONS:
        buffer = io.StringIO()
        context = MigrationContext.configure(dialect_name='postgresql',
            opts={'as_sql': True, 'output_buffer': buffer})
        script = plan.script.get_revision(revision)
        if script.down_revision != previous:
            raise ValueError('migration_chain_mismatch')
        with Operations.context(context):
            script.module.upgrade()
        for statement in buffer.getvalue().strip().split(';\n'):
            statement = statement.strip().rstrip(';')
            if statement:
                if not statement.startswith(('CREATE TABLE ', 'CREATE INDEX ')):
                    raise ValueError('unreviewed_statement_kind')
                steps.append(statement)
        steps.append(f"UPDATE public.alembic_version SET version_num={literal(revision)} "
            f"WHERE version_num={literal(previous)}")
        steps.append(f"SELECT {literal(revision)} AS rehearsed_revision, "
            "(SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public') AS tables_inside_transaction")
        previous = revision
    for table, spec in gate.EXPECTED_TERRITORIAL_SCHEMA.items():
        columns = ','.join(literal(x) for x in sorted(spec['columns']))
        steps.append(f"""DO $$ BEGIN
          IF (SELECT array_agg(column_name::text ORDER BY column_name) FROM information_schema.columns
            WHERE table_schema='public' AND table_name={literal(table)}) IS DISTINCT FROM ARRAY[{columns}]
            THEN RAISE EXCEPTION 'territorial_columns_mismatch'; END IF;
        END $$""")
        constraints = ','.join(literal(x) for x in sorted(spec['constraints']))
        indexes = ','.join(literal(x) for x in sorted(spec['indexes']))
        steps.append(f"""DO $$ BEGIN
          IF NOT (ARRAY[{constraints}] <@ (SELECT array_agg(conname::text) FROM pg_constraint
              WHERE conrelid={literal('public.' + table)}::regclass)) THEN
            RAISE EXCEPTION 'territorial_constraints_mismatch'; END IF;
          IF (SELECT array_agg(indexname::text ORDER BY indexname) FROM pg_indexes
              WHERE schemaname='public' AND tablename={literal(table)}) IS DISTINCT FROM ARRAY[{indexes}] THEN
            RAISE EXCEPTION 'territorial_indexes_mismatch'; END IF;
          IF EXISTS (SELECT 1 FROM pg_index WHERE indrelid={literal('public.' + table)}::regclass
              AND (NOT indisvalid OR NOT indisready)) THEN
            RAISE EXCEPTION 'territorial_index_not_ready'; END IF;
        END $$""")
    steps.append("SELECT version_num AS tested_revision, false AS cutover_authorized FROM public.alembic_version")
    steps.append("ROLLBACK TO SAVEPOINT reviewed_territorial_rehearsal")
    steps.append("""DO $$ DECLARE t record; n bigint; h text; BEGIN
      IF (SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public') <>
          (SELECT count(*) FROM rehearsal_before) THEN RAISE EXCEPTION 'rollback_tables_changed'; END IF;
      FOR t IN SELECT * FROM rehearsal_before LOOP
        EXECUTE format('SELECT count(*), encode(sha256(convert_to(COALESCE(string_agg(d, %L ORDER BY d), %L), %L)), %L) ' ||
          'FROM (SELECT encode(sha256(convert_to(row_to_json(r)::text, %L)), %L) d FROM public.%I r) h',
          '', '', 'UTF8', 'hex', 'UTF8', 'hex', t.name) INTO n,h;
        IF n IS DISTINCT FROM t.rows OR h IS DISTINCT FROM t.digest THEN
          RAISE EXCEPTION 'rollback_data_changed'; END IF;
      END LOOP;
    END $$""")
    steps.append(f"SELECT {literal(CONTRACT)} AS contract_version, true AS rollback_verified, "
        "false AS schema_committed, false AS cutover_authorized, count(*) AS tables_verified, "
        "sum(rows) AS rows_verified, encode(sha256(convert_to(string_agg(name||':'||rows::text||':'||digest, "
        "'' ORDER BY name), 'UTF8')), 'hex') AS data_fingerprint_sha256, "
        "(SELECT version_num FROM public.alembic_version) AS restored_revision FROM rehearsal_before")
    steps.append("RELEASE SAVEPOINT reviewed_territorial_rehearsal")
    return {'contract_version': CONTRACT, 'sql_statements': steps,
        'revisions': list(REVISIONS), 'graph_sha256': plan.graph_fingerprint_sha256,
        'source_sha256': {r:plan.source_fingerprints_sha256[r] for r in REVISIONS},
        'cutover_authorized': False, 'rollback_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project-id', 'branch-id', 'source-branch-id', 'database-name'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = compile_rehearsal(Path(__file__).resolve().parents[1], project_id=args.project_id,
        branch_id=args.branch_id, source_branch_id=args.source_branch_id, database_name=args.database_name)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'contract_version': CONTRACT, 'statements': len(report['sql_statements']),
        'revisions': report['revisions'], 'executed': False}))

if __name__ == '__main__':
    main()
