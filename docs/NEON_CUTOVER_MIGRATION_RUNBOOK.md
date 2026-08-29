# Neon cutover migration runner

`scripts/apply_neon_cutover_migrations.py` is a deliberately narrow gate for
the four revisions approved for the Render-to-Neon cutover:

1. `20260825_legacy_municipio_ticket_scope_repair_v1`
2. `20260825_chat_idempotency_v1`
3. `20260829_inbound_fifo_v2`
4. `20260829_global_writer_authority_v1`

It accepts only the exact initial revision
`20260825_demo_survey_participation_v1` or one of those four allowlisted
intermediate revisions, and requires an unbranched local graph ending at the
fourth revision. Canonical SHA-256 hashes pin the reviewed source of all
migration files. It applies only the remaining exact revisions and never
upgrades to `head` or `heads`.

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
- All upgrades and every post-check run in one outer transaction. A deviation
  after either step aborts the transaction; no first-step partial commit is
  allowed.
- Before writing, the runner validates the complete contract for the current
  allowlisted revision. At the initial revision this requires all three
  evidence-backed ticket rows to still be scoped to `almacen`, the demo survey
  schema contract to be intact, and the idempotency table to be absent.
- After every exact step, the runner verifies that `alembic_version` contains
  only that revision and runs the corresponding post-check described below.
- Standard output is one compact redacted JSON object. It contains only
  aggregate checks and fingerprints, never the DSN, credentials, raw host,
  raw project ID, raw branch ID, evidence IDs, or database rows.

Neon documents that pooled endpoint names add the `-pooler` suffix and that
connections require TLS: <https://neon.com/docs/connect/connection-pooling>
and <https://neon.com/docs/connect/query-with-psql-editor>.

## Transactional post-checks

Every applicable post-check runs before the outer transaction can commit:

1. After `20260825_legacy_municipio_ticket_scope_repair_v1`, the demo survey
   contract remains intact, the idempotency table is still absent, and the
   three reviewed legacy ticket rows are present and scoped to `junin`.
2. After `20260825_chat_idempotency_v1`, the prior checks still pass and
   `municipio_chat_idempotency` has the reviewed columns, index, constraints,
   and zero initial rows.
3. After `20260829_inbound_fifo_v2`, the prior checks still pass and
   `ix_whatsapp_inbound_turn_stream_fifo` is a valid, ready, non-unique,
   unfiltered plain-column index in the exact order
   `(tenant_id, stream_key, received_at, id)`, with no included columns.
4. After `20260829_global_writer_authority_v1`, all prior checks still pass and
   `cutover_global_writer_authority` has the reviewed columns and constraints
   plus exactly the safe bootstrap singleton: `authority_key='primary'`, no
   owner, `epoch=0`, and both Render and Vercel fenced.

Any failed post-check aborts the outer transaction, so no earlier revision in
the same invocation is committed by itself.

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
