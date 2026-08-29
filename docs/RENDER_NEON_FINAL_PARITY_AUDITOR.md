# Render to Neon final parity auditor

`scripts/audit_render_neon_parity.py` turns the manual pre-cutover comparison
into reproducible, machine-readable evidence. It is a **read-only auditor**, not
a migration or cutover command.

## What it proves

- The SQLite input is a standalone file with no WAL/journal sidecar, stays byte
  stable during the run, and is opened with `mode=ro`, `immutable=1`, and
  `PRAGMA query_only=ON`.
- The destination is the explicitly expected direct Neon project/branch over
  TLS, read inside one `REPEATABLE READ, READ ONLY` transaction.
- Every non-excluded source table exists in Neon.
- Neon contains at least every source row. Primary-key tables use PK inclusion;
  unkeyed tables use duplicate-aware row-multiset inclusion.
- Stable common cells match after a versioned cross-database normalization.
  Only reviewed exceptions in
  `config/render_neon_parity_policy.v1.toml` are allowed.

The JSON contains counts, schema names, mismatch counts, HMAC-SHA256
fingerprints, snapshot SHA-256, Neon WAL position, policy SHA-256, and an
`evidence_sha256`. It contains no DSN, host, path, key, PK value, raw row, or
cell value. `evidence_sha256` is an integrity digest, **not a signature**; the
canonical stdout artifact still needs to be signed/archived by the release
process.

## Final-window invocation

Create the SQLite snapshot only after the Render writer fence is active and
record the fence/snapshot evidence IDs. Do not point `--source-sqlite` at a live
mutable file.

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
- Source inclusion allows destination-only growth. It does not assert that
  Neon has no additional rows or newer product tables.
- The current policy excludes `alembic_version`, delegates migration-head
  validation to `preflight_neon_cutover.py`, treats
  `municipio_ticket.estado` as destination-authoritative, and permits the
  retired `archivo_url` only when all legacy values are null.
- No mutable `user` columns are silently ignored. If final evidence finds
  expected post-migration changes, data ownership must be decided and the
  policy reviewed/versioned before certification.
- This gate does not validate application E2E, workers/crons, webhooks,
  canaries, DNS, rollback, or soak. Those remain separate cutover gates.
