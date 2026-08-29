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

## Full application writer fence prepared (not activated)

The application now exposes an explicit, disabled-by-default
`CUTOVER_WRITER_FENCE_ENABLED` control. When deliberately enabled for the
maintenance window, every unsafe HTTP method (`POST`, `PUT`, `PATCH`, and
`DELETE`) returns a no-store `503` contract with `Retry-After`, before auth,
tenant resolution, uploads, webhook handlers, or route code can mutate state.
Truly read-only `GET`/`HEAD` routes and `OPTIONS` remain available for
health/readiness and CORS checks. GET endpoints that create defaults, persist
read-audit events, update verification state, poll a provider or otherwise
flush/commit are explicitly marked as writers and return the same `503` before
authentication, ORM access or provider setup. The four mutating Vercel internal
cron paths are independently fenced before bearer validation or service import.

The same flag is propagated from the Render web service to the permanent
WhatsApp, domain-effect and survey-effect workers, plus WhatsApp retention,
survey retention and weekly analytics crons. A fenced permanent worker performs
one signal-aware wait until shutdown, without database polling, broker wakeups
or provider construction; `--once`/health invocations emit only a redacted,
zero-count `fenced` report and exit successfully. Direct Celery entrypoints for
the three durable effect pipelines also return zero-work fenced contracts.
The two retention entrypoints, weekly analytics command and manual bounded
survey-effect drain are fenced before their mutating service import/query. The
generic campaign, image-analysis, notification, SLA, legacy survey-effect and
file-analysis Celery entrypoints fence both enqueue helpers and task execution,
including work that was already present in the broker.

The flag is a **process-start snapshot**, not a dynamic distributed lock.
Changing the Render web-service value or a Vercel variable does not prove that
older processes loaded it, and `fromService` propagation alone is not an
attestation. Every web, worker and cron generation must be redeployed/restarted;
each permanent worker must emit the payload-free startup line
`cutover_writer_fence_active` with the expected contract, component and
`status=fenced`, and old generations must be shown terminated. Before the final
snapshot, drain and attest zero in-flight transactions/provider calls. Freeze
deployments and explicitly block migrations, manual seed commands, direct SQL
sessions and any external writer. Render's declared `preDeployCommand` now runs
`python -m scripts.run_predeploy_migrations`: it emits the same payload-free
fence attestation and skips `flask db upgrade` when the flag is active or
unrecognized; with an explicit false value it preserves the migration.

This application fence still does **not** prove global quiescence on its own.
Direct SQL sessions, migration/pre-deploy commands, provider-side retries and
any writer outside these declared services remain operator-owned and must be
inventoried and attested before taking the final snapshot. Do not deploy or run
`flask db upgrade` while using the flag as the source-freeze boundary. The flag
has not been enabled on Render or on the public backend. It is enabled only on
the isolated Vercel Production candidate documented below; that candidate has
not received `api.chatboc.ar` traffic.

## Remaining cutover gates

The preflight deliberately reports `content_parity_certified=false`. Render
must remain active until all of the following are complete:

1. Enable the shared application writer fence, redeploy/restart and attest every
   process generation from its payload-free startup log, terminate old
   generations, separately quiesce every out-of-band/direct writer, drain and
   attest zero in-flight work, and repeat the signed Render-to-Neon content
   inventory against the final snapshot.
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

- backend deployment `dpl_6ca33mfeK18ijsjbCh1HCm4J1kbA`, built from immutable
  revision `84021daf003291aa479e9f72d4f836c2bfe16e04` and exposed only at
  `api-preview.chatboc.ar` with the writer fence explicitly disabled;
- frontend deployment `dpl_2xQVQL1QVNJJ5TbZjoTsSm1KNjMY`, built from revision
  `8ab9a96d` and exposed only at `chatboc-r2-preview.vercel.app`;
- backend health returned `200`, database connected; runtime readiness returned
  `ready=true` for PostgreSQL and Redis; `/api/version` returned the exact
  immutable backend revision above;
- all four internal cron routes returned `401` without authorization, while a
  marked mutating GET and an unsafe POST followed their normal unfenced
  validation paths (`400`, never the maintenance `503`);
- 161 focused local tests covering the HTTP/background fence, workers, queues,
  Render blueprints and Vercel cron contracts passed independently before the
  deployment; Python compilation and `git diff --check` also passed;
- the frontend build compiled eight audited Preview rewrites, zero Production
  backend references, same-origin browser API traffic and the direct Preview
  Socket.IO origin;
- an earlier ordinary Preview build was rejected by the routing guard because
  the canonical configuration targets `api.chatboc.ar`. It was not promoted;
  the successful deployment used the generated safe Preview configuration;
- `CUTOVER_WRITER_FENCE_ENABLED` and
  `VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED` exist in the Vercel Production scope
  and remained disabled. No Production deployment was issued during that
  Preview revalidation phase; the later fenced candidate is recorded below.

`api.chatboc.ar` continued returning `200` through Cloudflare with a Render
`gunicorn` origin after the Preview refresh. No Render service, Production DNS,
provider webhook or Production database writer was changed. The remaining
gates above therefore remain mandatory.

## Fenced Vercel candidate - 2026-08-29

An isolated Vercel Production candidate was deployed to validate the final
application fence without moving the public API hostname or enabling any
background writer:

| Check | Result |
| --- | --- |
| Deployment | `dpl_HfbQSLvJ2GMjh7tofAf5EMPonj7d` |
| Direct URL | `chatboc-backend-kappyz0d3-marcelos-projects-c26aa499.vercel.app` |
| Immutable revision | `8c70a47768e5945da93e4d33bfc788d2dacdb47a` |
| Deployment state | `READY` |
| `/health` | `200`, `status=ok` |
| `/health/ready` | `503`, `reason_code=required_schema_missing` |
| Writer fence | enabled on this candidate only |
| Vercel writer crons | all four disabled and returning `503` |
| Public API | unchanged Render origin, revision `8ced9216ff134951a0e3cb050c473e364c28be5c` |
| Preview API | unchanged Vercel Preview revision `84021daf003291aa479e9f72d4f836c2bfe16e04` |

The HTTP matrix returned the fenced `503` contract for unsafe authentication,
catalog/cart compatibility aliases, ticket enrichment, executive analytics,
provider smoke checks, category bootstrap, tenant administration, WhatsApp
provider status, widget configuration, survey analytics, production-smoke,
kits, points and reward rules. The four Vercel cron paths independently
returned `503`. This demonstrates that the candidate cannot become an
accidental concurrent writer while Neon remains incomplete.

The same revision hardens the declared Render standby path without activating
it: standby now requires a direct TLS Neon migration URL, refuses SQLite or a
non-Neon database, skips predeploy migrations whenever the fence is active,
propagates the standby flag to permanent background services, and gives the web
and worker processes a graceful shutdown window. Municipal flyer uploads also
use the shared object-storage path and fail closed in Production when required
R2 storage is unavailable. The corresponding Render flags remain `false`; no
Render blueprint or environment value was applied.

The Vercel Production variables and Neon CLI correlate to project
`nameless-rain-94060889`, branch `br-dark-silence-acmnikpq` (`main`) and endpoint
`ep-withered-union-acn1hovx`. The read-only preflight still reports exactly two
pending revisions, no chat-idempotency receipt table and
`content_parity_certified=false`. Therefore this candidate remains **NO-GO**
for public traffic, migrations, webhook reassignment or Render retirement.

The next safe boundary is not another public deployment. It is an approved
maintenance window that fences and restarts every Render writer, drains
in-flight work, captures the immutable final source snapshot, certifies signed
content parity and only then applies the two rehearsed Neon migrations. Render
must remain recoverable through canaries, provider validation, soak and a
tested rollback.

## Live Render source correction - 2026-08-29

A read-only inspection of the authenticated Render control plane and a
read-only transaction executed from the live web instance invalidated one
earlier cutover assumption: the current Production writer source is Render
PostgreSQL, not the persistent SQLite file.

| Check | Live result |
| --- | --- |
| Public Render web service | `chatbot-backend` / `srv-d0rq2rp5pdvs738t3bhg` |
| Deployed revision | `8ced9216ff134951a0e3cb050c473e364c28be5c` |
| Tracked Git branch / auto-deploy | `main` / off |
| Compute | Starter, 512 MB |
| Recent stability signal | repeated 512 MB out-of-memory instance failures followed by recovery |
| Runtime database | internal Render PostgreSQL over TLS |
| Source migration revision | `20260820_survey_content_jurisdiction_v1` |
| Source public tables / exact aggregate rows | 170 / 52,863 |
| Source inventory fingerprint | `8be4669ab0dfa4829e470ce71db7d82c4c6a1e15779425881e9ecaf6efc6fb13` |
| Render PostgreSQL service | `chatboc-postgres-28/11/2025` / `dpg-d4lusfali9vc73egvnq0-a` |
| PostgreSQL recovery | three-day point-in-time recovery plus provider logical export |
| Legacy disk artifact | `/data/database.db`, 1,933,312 bytes, WAL sidecar present |
| Writer fence / standby flags | absent from the live web-service environment |

The live service configuration also still uses the older direct
`scripts/apply_migrations.py` predeploy command and an eventlet Gunicorn start
command. It is not controlled by the newly hardened Blueprint declaration.
Deploying the cutover revision without first replacing the live predeploy
command could run an unbounded Alembic head upgrade against the Render source.

The current Neon main inventory is 171 tables and 52,751 aggregate rows at
`20260825_demo_survey_participation_v1`; its inventory fingerprint is
`56cb88b08b89aeb7b71c66ad0b404d537fdc0f84caf65f7a71abb24516ce3f21`.
The difference in revisions, table count, row count and fingerprints proves
that schema-only migration is insufficient. It does not identify which source
rows are missing, which destination-only rows must be preserved or how
conflicting mutable records should be resolved.

Consequently, the SQLite final-parity command remains valid only for legacy
inclusion evidence and must not certify the Production cutover. The new P0 gate
is a read-only PostgreSQL-to-PostgreSQL auditor plus an idempotent reconciliation
plan rehearsed on an isolated Neon branch. Until that gate passes, do not set
the Render fence, create the final logical export, migrate Neon main, move
webhooks/DNS or retire Render.

## Live WhatsApp and background-runtime correction - 2026-08-29

The same read-only control-plane inspection found no active Render background
worker or cron service. Production currently has one web process and no
configured Redis/Celery or Socket.IO message-queue ownership. The workers and
crons declared in the repository Blueprint are therefore desired-state code,
not evidence of live execution. Their ownership must be established explicitly
on Vercel; they cannot be described as a migration of an existing Render
worker.

Twilio reports two online production-capable senders, including the official
Junin number ending `3718`. The current Render environment nevertheless uses
an offline sandbox sender ending `8886` as
`TWILIO_WHATSAPP_NUMBER`, while `TWILIO_PHONE_NUMBER` ends in `3718`.
The public inbound webhook remains
`https://api.chatboc.ar/webhook/whatsapp`. No webhook or message was changed
or sent during this audit.

A read-only ownership query against both Render PostgreSQL and Neon main
returned the same structural result:

- tenant `junin` is active and has one active legacy mapping for the official
  `3718` sender;
- it also has a second active legacy mapping ending `5678`, which remains
  unclassified and must not be deleted implicitly;
- the tenant has one Twilio provider connection with a credential reference,
  but that connection is still marked `sandbox:provisioning_plan_ready`;
- there is no `provider_sender` row and no tenant-profile sender bound to the
  official number;
- the legacy `municipio` tenant is also active but has no corresponding
  provider connection or sender.

This explains why the current webhook can still resolve Junin through the
legacy mapping while the enterprise provider model is incomplete. The cutover
must first reconcile one verified production `provider_sender` for tenant
`junin`, retain or classify the `5678` mapping explicitly, and prove that
the sandbox sender is not the outbound default. Copying the existing Render
Twilio variables to Vercel would reproduce the defect.

The latest inspected Twilio message page contained successful inbound and
outbound traffic but also seven attachment failures with provider code
`63019`. Text transport and media transport are separate gates: R2 object
availability and Twilio media retrieval must pass a controlled canary before
Render can be retired.

## PostgreSQL parity and exact-migration rehearsal - 2026-08-29

An isolated Neon read/write branch was created from main solely for cutover
rehearsal. No application, Preview deployment, DNS record or webhook points to
this branch.

| Check | Result |
| --- | --- |
| Rehearsal branch | `cutover-rehearsal-20260829-a` / `br-floral-unit-acgqawl6` |
| Rehearsal endpoint | `ep-billowing-star-acaw9usk` |
| Baseline dry-run | `ready_to_apply`; no writes |
| Applied revision 1 | `20260825_legacy_municipio_ticket_scope_repair_v1` |
| Applied revision 2 | `20260825_chat_idempotency_v1` |
| Transaction | one atomic transaction with advisory lock |
| Legacy ticket postcheck | 3 present, 0 in source tenant, 3 in Junin |
| Idempotency postcheck | table, columns, constraints and index present; 0 rows |
| Final preflight | `ready`; exact head; 172 tables; 52,751 rows |
| Content parity | deliberately not certified |

The migration runner therefore passed a real Neon apply and postcheck without
touching main. This proves the exact schema path; it does not prove that the
existing Neon data contains all current Render records.

A separate strict PostgreSQL-to-PostgreSQL audit compared the live Render
source to this rehearsal branch in repeatable-read, read-only transactions.
Because Render was not writer-fenced, this is diagnostic evidence only:

| Metric | Result |
| --- | --- |
| Source / destination tables | 170 / 172 |
| Source / destination aggregate rows | 52,863 / 52,751 |
| Source tables audited | 169 (`alembic_version` excluded by policy) |
| Exact matches | 157 |
| Mismatching tables | 12 |
| Source rows missing in Neon | 112 |
| Destination extra rows | 0 |
| Mismatching common cells | 812 |
| Diagnostic evidence SHA-256 | `42fda9165d4b698c88cacdc3566b06ce966b05d3553db6322b288dd0a677f92e` |

The 112 missing source rows are distributed across `admin_audit_log` (1),
`analytics_events_v2` (28), `channel_session` (3),
`chat_session_context` (3), `contact` (3), `contact_snapshot` (3),
`conversation` (3), `flask_sessions` (48), `interaction_event` (6),
`message` (12) and `ticket_realtime_state` (2). `municipio_ticket` has no
missing or extra primary keys; its only three differences are the intentionally
rehearsed tenant-scope repair. The two destination-only tables are empty and
were not classified as data conflicts.

This result is strong evidence that Neon is a lagging snapshot rather than an
independent writer: there are no destination-extra rows, while recent session,
conversation, contact, message and analytics records exist only in Render.
It remains unsafe to switch traffic or run only Alembic. The final window must
fence Render, capture a managed logical export, restore/reconcile that frozen
source, rerun this exact audit and only then apply the two migrations.
