# Migration runtime sprint — 2026-09-18

## Scope and source lineage

This cut is based on `e4bd9407564ef5f3996963b03aaca440e652b4c3`, the migration
Preview revision, not the August main branch or the September Render release.
It does not authorize replacing the public API, restoring a database, acquiring
writer authority, moving WhatsApp callbacks, or retiring Render.

## Reproduced observation

The existing Preview is not permanently stuck: its startup logs report canonical
initialization completing in approximately 3.9–4.1 seconds. A bounded read-only
probe at 2026-09-18T21:02:54Z observed:

- first `/api/version`: HTTP 503, typed retryable bootstrap, 5343 ms;
- explicit retry: HTTP 200 with the expected revision, 2110 ms;
- `/health/ready`: HTTP 200, exact `runtime.readiness.v1`, PostgreSQL and Redis
  required and healthy, 1328 ms;
- final `/api/version`: same revision, HTTP 200, 219 ms.

This proves eventual readiness for that observation, not a cold-start SLA,
source/destination data parity, runtime credential binding, or production readiness.

## Implemented

- A Vercel-only Gunicorn `post_worker_init` hook schedules the existing delayed,
  single-flight loader before the first HTTP request. No blocking load is added
  to the master process; non-Vercel workers do not start this warmup.
- `scripts/verify_migration_runtime.py` checks the exact revision, required
  PostgreSQL/Redis readiness, and the same revision again. It retries only the
  explicitly retryable bootstrap response, with bounded attempts and timeouts.
- The probe rejects Production, credentials in URLs, redirects and unapproved
  hosts. Evidence includes only status, duration, reason and release identity;
  response bodies, credentials and exception text are never copied.
- `first_attempt_ready` stays false after a recovered cold response. Every
  report has `cutover_authorized=false`; this does not replace the strict
  release manifest, cold-start, parity, writer-ownership or provider gates.
- A Linux CI gate exercises real Gunicorn and proves initialization begins
  before the first HTTP request, using an isolated WSGI fixture with no
  database or provider credentials.

## Validation boundary

Local regression: 24 tests passed, including the existing bootstrap tests,
new lifecycle tests and probe contract tests. `git diff --check` passed.
The Linux Gunicorn fixture and a new immutable Vercel candidate still require
separate execution; a local test is not evidence of a successful deployment.

## Neon state read without mutations

On project `nameless-rain-94060889`, branch `br-floral-unit-acgqawl6`, database
`render_rehearsal_20260829`, a read-only transaction confirmed 173 public tables
and revision `20260829_global_writer_authority_v1`. The territorial queue,
review and sync tables are not yet present. The next exact migrations remain
`20260830_territorial_geocoding_v1`, `20260830_geo_review_v1` and
`20260830_geo_sync_v1`, subject to the established rehearsal runbook.

No database was restored, resized, migrated, promoted or deleted in this cut.
Render remains the public source of truth. R2 parity, WhatsApp ingress replay,
cron ownership, the final source snapshot and the rollback/soak evidence remain
separate mandatory gates. Existing rehearsals must not be advertised as final
Production parity.

## Operator command

```sh
python scripts/verify_migration_runtime.py \
  --expected-revision <exact-40-character-revision> \
  --output <new-evidence-file.json>
```

For a strict first-attempt check add `--require-first-attempt`. Exit 3 means
readiness was eventually achieved but a first attempt failed. This small probe
has neither a cloud control-plane credential nor a write/cutover operation.
Use `RENDER_NEON_PRODUCTION_CUTOVER_RUNBOOK_2026-08-29.md` and the existing
`audit_cutover_release_manifest.py` for the remaining migration decisions.
