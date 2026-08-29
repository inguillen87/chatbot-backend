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

## HTTP writer fence prepared (not activated)

The application now exposes an explicit, disabled-by-default
`CUTOVER_WRITER_FENCE_ENABLED` control. When deliberately enabled for the
maintenance window, every unsafe HTTP method (`POST`, `PUT`, `PATCH`, and
`DELETE`) returns a no-store `503` contract with `Retry-After`, before auth,
tenant resolution, uploads, webhook handlers, or route code can mutate state.
`GET`, `HEAD`, and `OPTIONS` remain available for health/readiness and CORS
checks.

This only fences HTTP writers. It does **not** prove quiescence on its own:
Render workers, scheduled jobs, Vercel cron/effect ownership, and any external
database writer must be stopped or independently fenced before taking the final
snapshot. The flag has not been enabled on Render or Vercel Production.

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

## Revalidation - 2026-08-29

The primary Neon branch was rechecked through the authorized Neon CLI using a
direct, TLS-required connection and the repository preflight. The operation was
read-only and returned the expected `migration_required` NO-GO contract:

| Check | Result |
| --- | --- |
| Project / branch | `nameless-rain-94060889` / `br-dark-silence-acmnikpq` |
| Public tables / exact rows | 171 / 52,751 |
| Inventory fingerprint | `56cb88b08b89aeb7b71c66ad0b404d537fdc0f84caf65f7a71abb24516ce3f21` |
| Current / expected migration | `20260825_demo_survey_participation_v1` / `20260825_chat_idempotency_v1` |
| Pending revisions | 2 |
| Legacy Junin repair | 0 of 3 scoped |
| Chat idempotency table | absent |
| WAL | `0/4DAB7D0` from the primary |
| Content parity | not certified |

Preview was also refreshed without changing Production:

- backend deployment `dpl_4fJR3NapEiakx5nkukdDwARgV8K7`, exposed only at
  `api-preview.chatboc.ar`;
- frontend deployment `dpl_2ZZsk82X4WpmEFmH3jiFsJtSQDGi`, exposed only at
  `chatboc-r2-preview.vercel.app`;
- backend health returned `200`, database connected; runtime readiness returned
  `ready=true` for PostgreSQL and Redis;
- the frontend build compiled eight audited Preview rewrites, zero Production
  backend references, same-origin browser API traffic and the direct Preview
  Socket.IO origin;
- an earlier ordinary Preview build was rejected by the routing guard because
  the canonical configuration targets `api.chatboc.ar`. It was not promoted;
  the successful deployment used the generated safe Preview configuration;
- `CUTOVER_WRITER_FENCE_ENABLED` and
  `VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED` exist in the Vercel Production scope
  and remain disabled. No Production deployment was issued.

`api.chatboc.ar` continued returning `200` through Cloudflare with a Render
`gunicorn` origin after the Preview refresh. No Render service, Production DNS,
provider webhook or Production database writer was changed. The remaining
gates above therefore remain mandatory.
