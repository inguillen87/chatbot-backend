# Neon cutover migration runner

`scripts/apply_neon_cutover_migrations.py` is a deliberately narrow gate for
the two revisions rehearsed for the Render-to-Neon cutover:

1. `20260825_legacy_municipio_ticket_scope_repair_v1`
2. `20260825_chat_idempotency_v1`

It accepts only the exact initial revision
`20260825_demo_survey_participation_v1` and an unbranched local graph ending at
the second revision. Canonical SHA-256 hashes pin the reviewed source of both
migration files. It never upgrades to `head` or `heads`.

## Safety contract

- Dry-run is the default and opens one serializable, read-only transaction.
- The DSN is read only from the environment variable explicitly named with
  `--environment-variable`. There is no fallback to `DATABASE_URL`, Flask, or
  `alembic.ini`; the DSN must also contain its password and cannot delegate
  connection identity or credentials to libpq service/passfile parameters.
- The target must be a direct (non-`-pooler`) Neon PostgreSQL endpoint with
  TLS enabled. SHA-256 fingerprints of the normalized host, Neon project ID,
  and Neon branch ID must all match operator-supplied expected values.
- Apply mode requires three distinct approved evidence IDs: writer fence,
  snapshot, and final parity. The runner validates and binds the IDs to its
  output, but approval/authenticity remains an external human release gate.
- Apply mode holds a transaction-scoped advisory lock and uses fixed statement,
  lock, connect, and idle-transaction timeouts plus a fixed `public,pg_catalog`
  search path.
- Both upgrades and every post-check run in one outer transaction. A deviation
  after either step aborts the transaction; no first-step partial commit is
  allowed.
- Before writing, the runner requires all three evidence-backed ticket rows to
  still be scoped to `almacen`, the demo survey schema contract to be intact,
  and the idempotency table to be absent. It then verifies the single Alembic
  row after every step, all three repairs to `junin`, and the new table,
  columns, index, constraints, and empty initial row count.
- Standard output is one compact redacted JSON object. It contains only
  aggregate checks and fingerprints, never the DSN, credentials, raw host,
  raw project ID, raw branch ID, evidence IDs, or database rows.

Neon documents that pooled endpoint names add the `-pooler` suffix and that
connections require TLS: <https://neon.com/docs/connect/connection-pooling>
and <https://neon.com/docs/connect/query-with-psql-editor>.

## Dry-run

Compute the three expected SHA-256 fingerprints from the already approved,
normalized lowercase host/project/branch values. Set a purpose-specific
environment variable to the **direct** Neon URL, then run without `--apply`:

```powershell
$env:CHATBOC_CUTOVER_NEON_DIRECT_URL = '<direct Neon PostgreSQL URL>'

python scripts/apply_neon_cutover_migrations.py `
  --environment-variable CHATBOC_CUTOVER_NEON_DIRECT_URL `
  --expected-host-fingerprint-sha256 '<64 lowercase hex>' `
  --expected-project-fingerprint-sha256 '<64 lowercase hex>' `
  --expected-branch-fingerprint-sha256 '<64 lowercase hex>'
```

Success returns exit code `0`, `status=ready_to_apply`, `mode=dry_run`, and
`database_commit_confirmed=false`. Any mismatch returns exit code `2` and a
stable redacted `reason_code`.

## Apply in the approved maintenance window

Run only after the writer fence, snapshot, and final parity artifacts have
been reviewed and approved. Re-run the same command with `--apply` and the
three distinct evidence IDs:

```powershell
python scripts/apply_neon_cutover_migrations.py `
  --environment-variable CHATBOC_CUTOVER_NEON_DIRECT_URL `
  --expected-host-fingerprint-sha256 '<64 lowercase hex>' `
  --expected-project-fingerprint-sha256 '<64 lowercase hex>' `
  --expected-branch-fingerprint-sha256 '<64 lowercase hex>' `
  --approved-writer-fence-evidence-id '<approved opaque ID>' `
  --approved-snapshot-evidence-id '<approved opaque ID>' `
  --approved-parity-evidence-id '<approved opaque ID>' `
  --apply
```

Archive the JSON output with the maintenance record. `status=applied` and
`database_commit_confirmed=true` certify only this atomic schema/data step;
they do not certify application canaries, WhatsApp/Twilio webhooks, DNS,
soak, Render retirement, or rollback readiness.
