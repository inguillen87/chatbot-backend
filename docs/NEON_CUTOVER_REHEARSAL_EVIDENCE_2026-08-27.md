# Neon cutover backup and rehearsal evidence - 2026-08-27

Status: **rehearsal passed; Production cutover remains NO-GO**.

This record contains only branch identities, aggregate counts, migration
revisions and WAL positions. Database URLs, credentials, application rows and
PII are intentionally excluded.

## Scope and safety

- Neon organization access was authorized through OAuth and the CLI was scoped
  to project `nameless-rain-94060889` (`chatboc-postgres`).
- Primary branch: `br-dark-silence-acmnikpq` (`main`). All primary checks ran
  inside transactions that explicitly set `transaction_read_only=on`.
- No Vercel Production variables, DNS, Render service, worker, cron, webhook or
  public alias was changed.
- The branch-creation response initially returned a generated branch credential.
  That credential was immediately rotated on the backup branch through a
  temporary write endpoint. The temporary endpoint was then deleted, only the
  read-only endpoint remains, and a post-rotation preflight reproduced the same
  table count, row count and inventory fingerprint.
- The plan rejected protected-branch creation because its protected-branch
  allowance is exhausted. The backup therefore remains unprotected and must
  not be treated as an account-enforced immutable snapshot.

## Fresh backup

- Name: `codex-cutover-backup-20260827-r2`
- Branch: `br-royal-violet-acrq1pzm`
- Parent branch: `br-dark-silence-acmnikpq`
- Parent LSN: `0/4C2D790`
- Endpoint mode: read-only
- State after creation: ready

The source primary and backup produced the same destination-inventory evidence:

| Check | Primary | Backup |
| --- | ---: | ---: |
| Public tables | 171 | 171 |
| Exact aggregate rows | 52,751 | 52,751 |
| Inventory fingerprint | `56cb88b08b89aeb7b71c66ad0b404d537fdc0f84caf65f7a71abb24516ce3f21` | same |
| Alembic revision | `20260825_demo_survey_participation_v1` | same |
| Pending revisions | 2 | 2 |
| Legacy tickets scoped to Junin | 0/3 | 0/3 |
| Chat idempotency table | absent | absent |

The backup reported replay LSN `0/4C2D790`, proving that the check executed on
the read replica rather than silently falling back to the writer.

## Isolated migration rehearsal

- Name: `codex-cutover-rehearsal-20260827-r2`
- Branch: `br-holy-pine-ac1epxcc`
- Parent branch: `br-dark-silence-acmnikpq`
- Parent LSN: `0/4C2DEE8`
- Expiration: `2026-08-30T03:00:00Z`

The pre-migration inventory matched the primary: 171 tables, 52,751 aggregate
rows, the same inventory fingerprint and exactly these two pending revisions:

1. `20260825_legacy_municipio_ticket_scope_repair_v1`
2. `20260825_chat_idempotency_v1`

The repository migration runner applied only those revisions. The post-run
preflight returned `status=ready` and exit code `0` with:

- Alembic at `20260825_chat_idempotency_v1` and no pending revisions;
- 172 public tables and the same 52,751 aggregate rows;
- all three evidence-backed legacy tickets scoped to Junin;
- the durable municipal chat idempotency table and index present;
- survey immutability trigger and indexes present;
- every critical schema check true.

The primary was queried again after the rehearsal and remained at 171 tables,
52,751 aggregate rows, the original inventory fingerprint, 0/3 repaired tickets
and both pending revisions. This proves that the rehearsal changes were
isolated from `main`.

## Certification-tool correction

`scripts/preflight_neon_cutover.py` now detects PostgreSQL recovery mode. It
uses `pg_current_wal_lsn()` on a primary and `pg_last_wal_replay_lsn()` on a
read replica, failing closed if no WAL position is available. This lets the
same redacted preflight certify a read-only Neon backup without requiring a
write-capable endpoint.

Validation: 24 focused tests passed, including primary/read-replica WAL
selection, missing-WAL failure, migration graph validation and bounded legacy
ticket repair.

## Remaining cutover gates

The preflight deliberately reports `content_parity_certified=false`. Render
must remain active until all of the following are complete:

1. Fence source writers and repeat the signed Render-to-Neon content inventory
   against the final snapshot.
2. Take a final restorable backup at the cutover boundary and resolve the
   protected-branch plan limitation or document an equivalent retention guard.
3. Apply the two rehearsed migrations to `main` only inside the approved
   maintenance window, then repeat the full preflight.
4. Certify Vercel Production variable targets, workers, four cron paths, Redis,
   R2/storage and rollback without exposing secret values.
5. Run authenticated UI-to-API-to-database canaries for tenant isolation,
   tickets/chat, surveys, analytics, uploads and realtime behavior.
6. Validate external WhatsApp/Twilio webhook delivery and outbound effects.
7. Move DNS only after those gates pass, observe a soak window and execute a
   tested rollback before cancelling Render.

Neither a successful rehearsal nor a `Ready` deployment is sufficient evidence
to retire Render.
