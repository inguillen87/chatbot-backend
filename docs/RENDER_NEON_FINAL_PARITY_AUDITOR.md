# Render to Neon final parity auditor

`scripts/audit_render_neon_parity.py` turns the manual pre-cutover comparison
into reproducible, machine-readable evidence. It is a **read-only auditor**, not
a migration or cutover command.

## Source modes and what they prove

- `--source-sqlite` is the legacy/offline mode. The input must be a standalone
  file with no WAL/journal sidecar, must stay byte
  stable during the run, and is opened with `mode=ro`, `immutable=1`, and
  `PRAGMA query_only=ON`.
- `--source-database-environment-variable` is the live PostgreSQL snapshot
  mode. The source must be a non-Neon PostgreSQL URL with TLS enabled. Its
  normalized hostname must match the separately approved
  `EXPECTED_SOURCE_HOST_FINGERPRINT_SHA256`; a mismatch fails closed. The
  source is read inside one bounded `REPEATABLE READ, READ ONLY` transaction.
- The destination is the explicitly expected direct Neon project/branch over
  TLS, read inside a separate bounded `REPEATABLE READ, READ ONLY`
  transaction. A source and destination resolving to the same host/database
  identity are rejected before connection.
- Every non-excluded source table exists in Neon.
- The default `--comparison-mode source-inclusion` proves that Neon contains at
  least every source row. `--comparison-mode exact` instead requires identical
  row/key multisets and matching governed content in every common audited
  table. Extra destination keys/rows are reported only as counts and aggregate
  HMAC fingerprints.
- In exact mode, a destination-only table may contain rows only when it appears
  in the fingerprinted policy's `allowed_destination_only_tables` map with a
  non-empty rationale. Empty destination-only tables are acceptable. Any
  destination-authoritative column exclusion is rejected.
- Stable common cells match after a versioned cross-database normalization.
  Only reviewed exceptions in
  `config/render_neon_parity_policy.v1.toml` are allowed.

The JSON contains counts, schema names, mismatch counts, HMAC-SHA256
fingerprints, source and Neon WAL positions (in PostgreSQL mode), policy
SHA-256, and an `evidence_sha256`. PostgreSQL source identity is emitted only
as a SHA-256 fingerprint; the artifact contains no source DSN, host, database
name, path, key, PK value, raw row, or cell value. `evidence_sha256` is an
integrity digest, **not a signature**; the canonical stdout artifact still
needs to be signed/archived by the release process.

## Final-window invocation for the current PostgreSQL source

The production source must be frozen first. Capture the immutable operational
snapshot/fence evidence ID out of band; this auditor neither activates nor
proves the fence. Compute and approve the SHA-256 of the normalized Render
source hostname separately, then expose only the variable name on the command
line. Never paste either DSN into command history.

```powershell
$env:RENDER_SOURCE_DATABASE_URL = '<direct Render PostgreSQL URL with sslmode=require>'
$env:EXPECTED_SOURCE_HOST_FINGERPRINT_SHA256 = '<approved sha256 of normalized source hostname>'
$env:MIGRATIONS_DATABASE_URL = '<direct Neon URL, not pooler>'
$env:EXPECTED_NEON_PROJECT_ID = '<expected project id>'
$env:EXPECTED_NEON_BRANCH_ID = '<expected branch id>'
$env:PARITY_AUDIT_HMAC_KEY = '<dedicated secret, minimum 32 UTF-8 bytes>'

python scripts/audit_render_neon_parity.py `
  --source-database-environment-variable RENDER_SOURCE_DATABASE_URL `
  --comparison-mode exact `
  --policy config/render_neon_final_cutover_policy.v1.toml `
  --source-snapshot-id 'render-pg-final-20260829-001' `
  --writers-fenced `
  --writer-fence-evidence-id 'render-fence-20260829-001' `
  --fingerprint-key-id 'parity-hmac-2026-08' `
  > render-neon-final-parity.json
```

This performs comparison only. It does not dump, copy, reconcile, migrate, or
write either database. A parity mismatch must be handled by the separately
reviewed full-dump or delta migration procedure and then audited again.
The strict final policy does not treat `municipio_ticket.estado` as
destination-authoritative: Render PostgreSQL and Neon must agree at the frozen
cutover boundary. It excludes only `alembic_version` (validated by the separate
migration-head gate) and permits the retired source-only `archivo_url` only
when every source value is null. Its destination-only allowlist starts empty;
additions require an explicit reviewed rationale.

## Legacy SQLite invocation

Create the SQLite snapshot only after the Render writer fence is active and
record the fence/snapshot evidence IDs. Do not point `--source-sqlite` at a live
mutable file. This mode does **not** certify a production service whose current
source of truth is PostgreSQL.

```powershell
$env:MIGRATIONS_DATABASE_URL = '<direct Neon URL, not pooler>'
$env:EXPECTED_NEON_PROJECT_ID = '<expected project id>'
$env:EXPECTED_NEON_BRANCH_ID = '<expected branch id>'
$env:PARITY_AUDIT_HMAC_KEY = '<dedicated secret, minimum 32 UTF-8 bytes>'

python scripts/audit_render_neon_parity.py `
  --source-sqlite '<fenced snapshot path>' `
  --source-snapshot-id 'render-final-20260829-001' `
  --writers-fenced `
  --writer-fence-evidence-id 'render-fence-20260829-001' `
  --fingerprint-key-id 'parity-hmac-2026-08' `
  > render-neon-final-parity.json
```

Keep the HMAC key outside the artifact and secret manager logs. Use the same
key/key ID when an independently repeated run must reproduce fingerprints.

## Exit contract

| Code | Status | Meaning |
| ---: | --- | --- |
| `0` | `certified` | Parity passed and a writer-fence evidence ID was attested. |
| `2` | `blocked` | Invalid configuration, unsafe snapshot, wrong Neon identity, row cap, or runtime failure. |
| `3` | `parity_mismatch` | A table, PK/row, schema contract, or stable cell is missing/different. |
| `4` | `writer_fence_attestation_required` | Parity passed, but the run is rehearsal-only because the writer fence was not attested. |

## Deliberate limitations

- The tool verifies the supplied writer-fence evidence ID syntactically; it
  cannot activate or independently prove the fence. Operational evidence must
  bind the ID to the deployed fence and maintenance window.
- PostgreSQL snapshots are transactionally consistent within each database,
  but they are not a distributed snapshot. The writer fence is what prevents
  source drift between the two independent snapshots.
- Source inclusion allows destination-only growth. It does not assert that
  Neon has no additional rows or newer product tables. Use exact mode and the
  strict final-cutover policy for the production cutover decision.
- The current policy excludes `alembic_version`, delegates migration-head
  validation to `preflight_neon_cutover.py`, treats
  `municipio_ticket.estado` as destination-authoritative, and permits the
  retired `archivo_url` only when all legacy values are null.
- No mutable `user` columns are silently ignored. If final evidence finds
  expected post-migration changes, data ownership must be decided and the
  policy reviewed/versioned before certification.
- This gate does not validate application E2E, workers/crons, webhooks,
  canaries, DNS, rollback, or soak. Those remain separate cutover gates.
