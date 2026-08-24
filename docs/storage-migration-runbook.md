# Persistent storage migration runbook

Status: **NO-GO for detaching `/data`**. This runbook gathers and validates evidence; it does not move, delete, upload, rewrite, or detach production data.

## Security and truth boundary

- Run the inventory inside the Linux service runtime that owns the mounted disk. Descriptor-relative traversal (`openat` semantics), `O_NOFOLLOW`, same-filesystem enforcement, two consistency passes, and directory/file identity checks are mandatory for a reviewable manifest.
- The focused `.github/workflows/storage-inventory-security.yml` job must pass on Ubuntu 24.04/Python 3.12.14 with its hash-locked wheel set before merge. Merely adding the workflow is not Linux evidence; record the green GitHub Actions run URL and commit SHA in the change record.
- Windows dry runs are aggregate evidence only and return `unsafe`; the tool refuses manifests because Python cannot prove equivalent traversal and owner-only ACL properties there.
- A mutable tree can return `unsafe` even when no malicious activity exists. Quiesce writers and rerun; never reinterpret that result as a pass.
- Public stdout is one aggregate JSON object. It never contains a plaintext path, encrypted path token, content hash, key, or file content.
- `go_for_cutover` is always `false`: this inventory can pass its own gate but cannot authorize a storage cutover by itself. `inventory_gate_passed=true` only advances to the next phase in this runbook.
- Secret-classified files are never opened for content hashing. This includes `.env.*`, `google_service_key.json`, service-account names, credentials, private keys, and token-like filenames such as `cache/auth-token.json`.
- A secret-like filename below a media/upload directory is classified `secret_review_required`: it is never content-hashed and it still requires operator review. A plausible media extension never downgrades the secret policy.
- `--skip-content-hashes` is permitted only for aggregate dry-run diagnostics. A private manifest without non-secret checksums is refused.
- `unknown`, `media_review_required`, and `secret_review_required` return exit code `3`, set `go_for_cutover=false`, and require operator review. Filesystem or consistency issues return `2` and refuse a manifest.
- A manifest is written only outside the inventory root, into an existing owner-owned `0700` directory with no symlink/reparse ancestor. The file is atomically published without replacement as `0600`, and the file and directory are `fsync`ed.
- Private output replacement is deliberately unsupported. Use a new timestamped name so a reviewed artifact cannot be silently replaced.
- A root-identity failure after atomic publication returns failure and emits no success/MAC. The tool deliberately does not unlink a published name because POSIX has no atomic `unlink-if-inode-matches`; automatic cleanup could delete a same-UID concurrent replacement. Treat any artifact from a failed command as unapproved quarantine, never source an expected MAC from it, and remove it only through the approved operator cleanup process.
- Every manifest is bound inside its authenticated payload to an operator-approved logical scope plus the mounted root's device/inode identity. Verification and review also require the exact manifest MAC captured independently when the manifest was generated; never source that expected MAC from the envelope being checked.

## Key custody, identity, and rotation

The manifest key is a 32-byte CSPRNG value encoded as unpadded base64url and stored in the secret manager as:

```text
base64url:<43 base64url characters>
```

Do not derive it from a password, UUID, tenant name, date, or API key. Generate it directly in an approved secret manager or HSM workflow that does not echo the value to logs or shell history. The tool rejects malformed, low-diversity, repeated, and simple sequential values, but this check cannot prove randomness; custody evidence remains an operator responsibility.

Set a non-secret rotation identifier such as `inventory-2026-08-r1` in `--key-id`. HKDF derives independent keys for path identifiers, path encryption, and manifest integrity; changing the key ID also changes all derived keys.

Set a stable, non-secret scope that names the service, environment, and logical disk, for example `chatboc:production:backend:disk-data-v1`. The scope is an operator/change-management identifier, not a filesystem path, and must not be copied from an untrusted manifest. Reuse it only for the same logical storage scope.

Rotation procedure:

1. Create a new CSPRNG master key and a new key ID. Never reuse an ID with different key material.
2. Write a new timestamped manifest; do not overwrite the prior one.
3. Verify and review the new manifest with the new key.
4. Retain the old key under the approved retention policy until its manifests and review artifacts expire.
5. Record key ID, secret-manager version, manifest MAC, operator, and timestamps in the change record. Never record key material.
6. Revoke the old key only after rollback and audit retention no longer depend on it.

## Phase 1: quiesced aggregate dry run

Identify every writer first: web workers, background jobs, upload handlers, SQLite clients, tenant-config writers, and maintenance tasks. Put them into the approved read-only/quiesced state. A process restart is not proof of quiescence.

Run inside the service runtime:

```bash
python scripts/inventory_persistent_data.py \
  --root /data \
  --dry-run \
  --redact-paths
```

Expected stdout is one `storage.inventory.summary.v2` JSON object. Interpret it as follows:

| Exit | Status | Meaning | Action |
| --- | --- | --- | --- |
| `0` | `complete` | Stable, supported traversal and no review category | `inventory_gate_passed=true`; eligible for the next evidence gate only |
| `3` | `review_required` | Stable scan, but media, secret-like media, or unknown files exist | Generate a private manifest and review |
| `2` | `unsafe` or `failed` | Mutation, link/reparse, boundary, permission, or I/O issue | Remain NO-GO; remediate and rerun |

Any non-empty `issues`, any `review_required_files`, or `go_for_cutover=false` blocks cutover. Two matching scans are necessary evidence, not a proof that application writers are quiesced.

## Phase 2: signed, reviewable private manifest

Create a dedicated output directory outside `/data`. The example uses an owner-only ephemeral directory; use an approved encrypted operational volume when the artifact must survive the shell session:

```bash
STORAGE_REVIEW_DIR="$(mktemp -d /tmp/chatboc-storage-review.XXXXXX)"
chmod 0700 "$STORAGE_REVIEW_DIR"
```

Inject `STORAGE_INVENTORY_MASTER_KEY` from the secret manager without printing it, with shell tracing disabled. Then run:

```bash
STORAGE_SCOPE_ID="chatboc:production:backend:disk-data-v1"
python scripts/inventory_persistent_data.py \
  --root /data \
  --manifest-out "$STORAGE_REVIEW_DIR/inventory-20260824T000000Z.json" \
  --key-id inventory-2026-08-r1 \
  --scope-id "$STORAGE_SCOPE_ID" \
  --redact-paths
```

The manifest contains only a MAC-authenticated canonical payload. Relative paths are AES-GCM encrypted and also represented by keyed HMAC identifiers. Non-secret content hashes are SHA-256; secret hashes remain absent. The MAC covers the contract, key ID, integrity algorithm, logical scope, root device/inode, and complete canonical inventory payload.

The generation command prints one redacted aggregate JSON object containing `manifest_mac`. Record that MAC in an independent protected change record or approved operator channel together with the scope, key ID, operator, and timestamp. Do not derive or copy the expected value from the manifest envelope during verification; doing so would remove the replay/substitution check.

```bash
STORAGE_EXPECTED_MANIFEST_MAC="<64-hex MAC copied from the independent change record>"
```

Verify integrity before every use:

```bash
python scripts/inventory_persistent_data.py \
  --root /data \
  --verify-manifest "$STORAGE_REVIEW_DIR/inventory-20260824T000000Z.json" \
  --key-id inventory-2026-08-r1 \
  --scope-id "$STORAGE_SCOPE_ID" \
  --expected-manifest-mac "$STORAGE_EXPECTED_MANIFEST_MAC"
```

Verification also compares the current mounted root's device/inode with the signed identity. A legitimate unmount/remount or disk replacement can change that identity; the mismatch remains a hard failure. Re-quiesce the intended disk, confirm its operator-approved scope, and generate a new manifest/MAC instead of suppressing or editing the mismatch.

Generate an operator-only plaintext review map only when necessary. It is a separate `0600` file, never stdout, and must be deleted through the approved secure-retention process after sign-off:

```bash
python scripts/inventory_persistent_data.py \
  --root /data \
  --review-manifest "$STORAGE_REVIEW_DIR/inventory-20260824T000000Z.json" \
  --review-out "$STORAGE_REVIEW_DIR/review-20260824T000000Z.json" \
  --key-id inventory-2026-08-r1 \
  --scope-id "$STORAGE_SCOPE_ID" \
  --expected-manifest-mac "$STORAGE_EXPECTED_MANIFEST_MAC"
```

Verification returning `3` remains a valid signature check but is still a migration NO-GO because the inventory requires review.

## Phase 3: classification and destination mapping

An accountable human must approve every `unknown`, `media_review_required`, and `secret_review_required` entry using the operator review file. Record the approved classification beside its `path_id`; do not put citizen names or plaintext paths in tickets or chat.

| Source class | Approved destination | Required controls | Explicit prohibition |
| --- | --- | --- | --- |
| SQLite database and WAL/SHM | Transactional database target or encrypted database backup vault | Consistent snapshot, integrity check, restore drill, access audit | Never upload a live `.db` alone while WAL writes continue |
| Tenant mutable configuration | Versioned database/config store keyed by tenant | Tenant RBAC, schema validation, audit trail, optimistic concurrency | No shared public bucket and no cross-tenant prefix |
| Public media | Private object bucket with an explicit public delivery policy | Immutable object key, checksum, content type, malware policy, CDN rules | Filename alone never proves public status |
| Private/PII media | Private encrypted object storage | Tenant-scoped authorization, short-lived signed access, audit, retention/deletion policy | No public ACL or permanent URL |
| Secrets | Secret manager | Least privilege, rotation, access logging | Never object storage, manifest hash, database dump, or repository |
| Cache/temp | Rebuild or discard only after owner approval | Prove it is non-authoritative and has no pending work | Never assume a directory named `cache` is disposable |
| Unknown | No destination | Human classification or private quarantine | Cannot proceed while unknown remains |

The object key must be generated independently from the original filename and scoped by tenant. Store the source `path_id`, destination object ID, source size/hash, destination size/hash, encryption class, tenant, migration attempt, and verification state in a private migration ledger.

## Phase 4: SQLite consistent snapshot and restore proof

For every SQLite database:

1. Identify all processes and connection strings that can write it, including fallback paths used when `DATABASE_URL` is absent.
2. Stop or fence writers. Confirm no new WAL frames or file metadata changes during the observation window.
3. Use the SQLite online backup API (or `.backup`) from a live connection to create the snapshot; do not copy only the main file while WAL mode is active.
4. Run `PRAGMA wal_checkpoint` under the database owner's approved procedure. Preserve WAL/SHM separately for incident evidence when policy requires it; do not improvise deletion.
5. On the snapshot, run `PRAGMA quick_check` and `PRAGMA integrity_check`; both must return `ok`.
6. Capture schema version, table list, row counts, and a deterministic sample/checksum plan that contains no PII in logs.
7. Restore into an isolated environment with production egress and notifications disabled.
8. Start the application against the restored copy, execute read and write smoke tests, and reconcile row counts and business invariants.
9. Record snapshot ID, checksums, tool versions, timestamps, operator, verification results, and restore duration in the change record.

The fallback SQLite path must be removed or made fail-closed before disk detachment; otherwise a missing database URL can silently create a fresh local database.

## Phase 5: transfer, verification, and soak

Use idempotent copy jobs. A retry must address the same destination object or transaction and must not create duplicate citizen data. For every copied object:

1. Compare source and destination byte counts.
2. Compare a destination-side cryptographic checksum to the signed source manifest.
3. Verify tenant ownership and authorization using a negative cross-tenant test.
4. Verify encryption at rest, TLS in transit, key ownership, bucket/database ACLs, access logging, retention, lifecycle, and deletion behavior.
5. Verify dangling references in both directions: no database row without an object and no object without an authorized owning record.

Introduce storage adapters behind a feature flag, then dual-write with idempotency keys. During soak, measure:

- local-only writes (must be zero);
- destination write failures and retry age;
- checksum mismatches (must be zero);
- legacy read fallback count and oldest fallback;
- tenant authorization failures and cross-tenant denials;
- p50/p95/p99 latency and error rate versus the accepted baseline;
- backup completion and a scheduled restore drill.

Soak duration and traffic coverage are release decisions. They must be written and approved before the test; “a few days” or “looks stable” is not acceptance evidence.

## RPO, RTO, and cutover acceptance

Do not invent service objectives. Product, operations, and data owners must fill and approve these fields before migration:

| Objective | Approved value | Measurement evidence | Owner |
| --- | --- | --- | --- |
| RPO: maximum acceptable committed-data loss | **TBD — blocks cutover** | Last durable copy and reconciliation timestamp | TBD |
| RTO: maximum acceptable restore/cutback time | **TBD — blocks cutover** | Timed restore and rollback drills | TBD |
| Soak duration and minimum representative traffic | **TBD — blocks cutover** | Observability report | TBD |
| Legacy fallback retirement threshold | Zero sustained fallbacks for approved window | Adapter metrics | TBD |

A cutover rehearsal must demonstrate both RPO and RTO with clocks and durable evidence. A theoretical estimate is insufficient.

## Rollback and disk-detach gate

Before cutover, take a final verified snapshot and preserve the Render disk unchanged. The rollback flag must switch reads and writes back consistently; disabling only reads can split authoritative state.

Read-only Render audit evidence captured on 2026-08-24 showed a 1 GB persistent disk mounted at `/data`, daily snapshots dated August 17 through August 23, and a seven-day retention window. It also showed no Redis/Key Value service, no environment groups, and no `RATELIMIT_STORAGE_URI`. These are point-in-time observations, not restore proof or approval to detach the disk. Because retention rolls forward, re-check snapshot IDs, timestamps, health, and expiry immediately before the change; create the approved final snapshot and complete a separate isolated restore drill. Do not restore, delete, or mutate existing snapshots as part of inventory collection.

The absence of external Redis/Key Value and `RATELIMIT_STORAGE_URI` means the change owner must explicitly prove that any local cache, queue, session, or rate-limit state is either non-authoritative/reconstructible or migrated to an approved durable service. The absence of environment groups also requires an environment-variable name/inheritance audit at the service level without exporting secret values into logs or manifests.

Rollback snapshot preflight:

1. Confirm the `/data` disk identity and logical scope match the manifest-generation change record.
2. Record the final snapshot ID and completion timestamp in the protected change record; confirm it falls within the approved RPO.
3. Confirm retention will cover the full soak and rollback window; seven-day historical availability alone is insufficient if the approved window is longer.
4. Complete an isolated restore and timed application smoke test without overwriting the mounted production disk.
5. Keep the original disk and all required snapshots immutable through the approved rollback window.

Rollback triggers include any checksum mismatch, authorization regression, lost/delayed write beyond approved RPO, restore failure, sustained error/latency breach, unexpected legacy fallback, or reconciliation drift.

Rollback procedure:

1. Freeze new writes or route them through the approved single authoritative path.
2. Disable destination cutover flags as one audited change.
3. Reconcile writes accepted since cutover using idempotency keys; never blind-copy over newer data.
4. Re-enable the original disk-backed adapter and verify health plus tenant isolation.
5. Preserve failed-state logs and manifests, redact paths from public incident channels, and open an incident/change record.
6. Rerun reconciliation and a restore check before declaring service recovered.

Detaching `/data` is allowed only after all of these are proven:

1. Every file has an approved destination or documented, approved deletion policy.
2. Signed manifest verification and operator classification are complete.
3. SQLite snapshot, integrity checks, isolated restore, and application smoke pass.
4. Object/database copies pass size, checksum, ownership, ACL, and dangling-reference verification.
5. Dual-read/dual-write soak meets the pre-approved gates with zero local-only writes.
6. RPO and RTO drills meet approved objectives.
7. Rollback has been rehearsed and the original disk remains recoverable through the rollback window.
8. Security, data owner, and operations sign-offs are recorded.

The Eventlet/Gunicorn migration is a separate change. Do not combine runtime replacement, storage cutover, and disk detachment in one release.
