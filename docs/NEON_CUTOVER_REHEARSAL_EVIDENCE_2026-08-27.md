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
source, rerun this exact audit, target the demo revision and only then apply
the two dedicated cutover migrations.

## Managed export, empty-database restore and full migration rehearsal - 2026-08-29

The provider-managed Render logical export path was then exercised without a
writer freeze. This was deliberately a rehearsal: Render remained the public
writer and no production URL, webhook, environment variable or Neon main data
was changed.

| Check | Result |
| --- | --- |
| Render export | completed; PostgreSQL directory-format archive |
| Archive size / SHA-256 | 2,603,637 bytes / `0c28672fc660552bd1ca8c0c8caa7486e37434cc0fbc2b558de2c07a83131d7e` |
| Dump producer / restore client | PostgreSQL 18.6 / PostgreSQL 18.6 |
| Archive safety | 174 archive paths inspected; no absolute or traversal path |
| Restore target | empty `render_rehearsal_20260829` database on the isolated rehearsal branch |
| Restore result | success in one `pg_restore --single-transaction`; 170 tables |
| Restored revision | `20260820_survey_content_jurisdiction_v1` |

The strict auditor then compared live Render to the restored database in two
repeatable-read, read-only transactions. Because writers were not fenced, the
auditor correctly refused to issue a production certificate, but exact parity
itself passed:

| Metric | Result |
| --- | --- |
| Aggregate rows | 52,863 on both sides |
| Source tables audited | 169 (`alembic_version` excluded by policy) |
| Tables matching | 169 |
| Common cells checked / mismatched | 518,770 / 0 |
| Source rows missing / destination rows extra | 0 / 0 |
| Rehearsal status | `writer_fence_attestation_required` |
| Evidence SHA-256 | `b81cffe98c0b5b3a2e322105412964c4158459cfba32fe05a583b476c189c7fb` |

This closes the earlier 112-row gap for the tested export/restore path. It does
not replace the final frozen export: a new source write after the audit would
invalidate this rehearsal evidence.

The local archive and parity artifact were moved out of the temporary
directory into an ACL-restricted evidence directory outside the repository,
protected with Windows CurrentUser DPAPI and round-trip hash verified. The
temporary plaintext copies were sent to the Windows recycle bin. This protects
the rehearsal artifact locally; the final cutover still requires an approved
durable evidence-retention destination and restore check.

The first targeted Alembic attempt also exposed a pre-existing URL handling
defect: `migrations/env.py` used SQLAlchemy's redacted string representation as
the live connection URL, replacing a real password with `***`. Authentication
failed before any migration SQL ran; a post-failure check proved the revision
and schema were unchanged. The runner now preserves percent-encoded
credentials for the engine, redacts all credentials and query parameters from
diagnostics, and is covered by three offline regression tests.

After that correction, the complete path from the actual Render source
revision passed on the isolated restored database:

1. exact Alembic target `20260825_demo_survey_participation_v1`;
2. dedicated dry-run for the two cutover revisions;
3. atomic apply of
   `20260825_legacy_municipio_ticket_scope_repair_v1` and
   `20260825_chat_idempotency_v1` under an advisory lock;
4. read-only postflight at the exact single head.

The postflight reports 172 tables, 52,863 aggregate rows, all three legacy
tickets scoped to Junin, both new ledger tables present and empty, and no
pending revisions. `content_parity_certified=false` remains intentional: final
certification still requires a real Render writer fence tied to the final
export.

A post-migration Neon backup branch was then created from the exact rehearsal
LSN: `cutover-rehearsal-20260829-postmigration` /
`br-purple-mode-acfyj7js`. A direct read-only check on the copied
`render_rehearsal_20260829` database returned the expected single revision
`20260825_chat_idempotency_v1` and 172 public tables.

A sender reconciliation dry-run against this restored result failed closed
with `provider_connection_exactly_one_required`. The only enterprise Twilio
connection is still a sandbox connection, so no `provider_sender` was created
or promoted and no provider message was sent. The official Junin sender gate
therefore remains open.

The local durable-inbound canary now also covers the cutover retry shape: a
signed inbound receives a no-store `503` while fenced, then the same
`MessageSid` is retried after the fence with distinct
`I-Twilio-Idempotency-Token` values. The queue stores one row, enqueues once,
runs the responder once, creates one governed outbound intent and performs no
provider call. This is useful regression evidence, but it is sequential and
mocked; PostgreSQL concurrency and Twilio live retry behavior remain mandatory
provider canaries.

The R2 smoke gate was also completed at code level. It now accepts only the
approved non-personal fixture hash, creates a GUID-scoped canary, proves
PUT/GET, discards only through the same signed intent, verifies database,
temporary-object and final-object absence, requires a post-delete signed GET
to return 404, replays delete idempotently and performs fail-closed emergency
attachment cleanup. This removes the earlier R2/attachment artifact leak; it
does not claim to undo an explicitly permitted demo-auth mutation on a
disposable rehearsal database. Production R2 still
requires a real controlled canary before traffic moves. That write canary
cannot run behind the global writer fence: it must use an existing scoped
canary identity on a separate no-alias deployment backed by a disposable Neon
database with every cron/provider side effect disabled, or run immediately
after the controlled Vercel un-fence. Demo authentication is fail-closed unless
the operator explicitly allows its isolated rehearsal mutation.

The Vercel container preflight also found tracked legacy runtime uploads under
`data/archivos`, `data/archivos_tickets` and `data/catalogos`. Those paths are
now excluded from both the Vercel upload bundle and Docker build context. A
post-change `vercel deploy --dry --json` inventory returned zero matching
runtime-media paths; governed demo fixtures and configuration under the other
`data` subdirectories remain available to the application.

## Fenced Vercel candidate over the restored database - 2026-08-29

Commit `d3067af02a3bc157c7adba4d7652599c097516ed` was pushed and deployed as a
Production-target candidate with `--skip-domain`. It has no public/custom
alias and does not receive public traffic; Vercel still assigns its normal
technical project alias:

| Check | Result |
| --- | --- |
| Deployment | `dpl_GAqPTuiFEapWUqti3NRTU6VkzaVH` |
| URL | `chatboc-backend-aeut7oqti-marcelos-projects-c26aa499.vercel.app` |
| Revision telemetry | `/api/version` returns the exact `d3067af02...` revision |
| Runtime target | pooled connection to isolated `render_rehearsal_20260829`; migrations remain direct |
| Database schema | `20260825_chat_idempotency_v1`; 172 public tables |
| Health | `/health=ok`; `/api/health/=db connected`; `/health/ready=ready` with database and Redis `ok` |
| Junin read surface | public tenant profile returns `junin` / `Municipalidad de Junin` |
| Write protection | unsafe POST returns no-store `503`, retryable, `Retry-After: 60` |
| Background protection | outbox cron route returns no-store `503`, `cutover_writer_fence_enabled` |
| Domain state | no public/custom alias; `api.chatboc.ar` remains on Render revision `8ced9216...` |

The first no-alias build was rejected as release evidence because its public
version endpoint inherited a stale manual version variable. It never received
an alias. The replacement sets the deployment-scoped immutable revision and
binds runtime evidence to the pushed source SHA.

This candidate proves container startup, the restored Neon schema, public
Junin reads, readiness and both request/background fences. It does not certify
authenticated CRM operations, R2 writes, the official WhatsApp sender, live
Twilio callbacks, cron ownership, a rollback drill or the 24-hour soak. Those
gates remain mandatory before any domain or webhook moves.

### Latest fenced revision revalidation

The subsequently hardened source revision
`eba5599ffc9b58a8f986a2733aa2d9063abf9233` was deployed as
`dpl_6GiWK8ehTBRe3K6hPEoVdJuKPMdQ` at the isolated technical URL
`chatboc-backend-33996czy5-marcelos-projects-c26aa499.vercel.app`. It remained
fenced and received no public/custom domain or provider traffic.

| Check | Result |
| --- | --- |
| Deployment state | `READY`; source telemetry matches exact `eba5599...` SHA |
| Runtime health | `/health=200`; database health `200`; readiness reports database and Redis `ok` |
| Junin read surface | public tenant profile returns `200`, tenant `junin` |
| Unsafe HTTP | `503`, `Cache-Control: no-store`, `Retry-After: 60` |
| Four internal cron routes | all return fenced no-store `503`; none executed |
| Public production | `api.chatboc.ar` still returns Render revision `8ced9216...` |
| Vercel scheduler registry | all four local definitions report `not deployed`; ownership remains pending |

This revalidation proves the latest code and restored Neon runtime can remain
available read-only while writes/jobs are fenced. It deliberately does not
claim scheduler ownership, live provider delivery, rollback readiness or a
Production cutover.

### Render standby verify-only rehearsal

The hardened Render predeploy entrypoint was exercised against the isolated
Neon rehearsal branch while the writer fence remained active. It required the
explicit `verify-only` action, a direct Neon connection and pinned project and
branch identities. The first database statement was `SET TRANSACTION READ
ONLY`; the verifier then confirmed Neon identity, exact revision
`20260829_inbound_fifo_v2`, the local migration graph and the demo,
idempotency and inbound FIFO structural contracts. It always rolled back and
reported zero write attempts and zero ownership acquisition.

This proves the rollback compute can validate the already-migrated database
without running Alembic. It does not prove the live Render service has been
rebound to Neon or that a remote rollback has been executed; both remain
maintenance-window gates.

## Isolated write canary and ownership audit - 2026-08-29

A second Production-target deployment, `dpl_HNHRkZJCTF94Me7XxPhvqXMKXXG3`,
was created without a public/custom alias against only the disposable restored
Neon database. Its global fence was disabled solely for isolated application
canaries; every cron, durable worker, WhatsApp notification transport, Twilio
auto-provisioning and provider live-smoke flag remained disabled. It never
received `api.chatboc.ar` traffic and no WhatsApp/Twilio message was sent.

The approved 70-byte non-personal R2 fixture completed the full governed
lifecycle: direct-upload prepare, exact CORS for `chatboc.ar`, `www.chatboc.ar`
and the Preview origin, malicious-origin rejection, signed PUT, idempotent
complete, signed GET `200`, raw public probe blocked, signed-intent discard,
database/temporary/final-object absence, deleted signed GET `404` and
idempotent delete replay. The initial attempt failed safely before upload
because the smoke session ID exceeded the backend's 36-character contract;
the client now keeps the full GUID in a 35-character ID and its regression
test passes.

An authenticated Junin administrator canary then proved the restored
application surface without provider effects:

| Check | Result |
| --- | --- |
| Admin identity | tenant `junin`, role `admin` |
| Legacy ticket list | 12 items returned on page 1 |
| Operational inbox | contract `backoffice.inbox_summary.v1`, 25 items |
| Workflow metadata | contract `tickets.workflow.v1` |
| Synthetic CRM case | one isolated `TenantTicket`, category `Luminarias` |
| Assignment | actor took the ticket; assignee matches authenticated operator |
| Workflow/conversation | status `in_progress`; one internal comment visible |
| External effects | all providers disabled; no send attempted |

The public demo identity was separately denied access to the ticket and
backoffice surfaces with `demo_scope_denied`, which is the expected RBAC
result. These canaries certify the isolated API path, not the final database,
live sender, public frontend, or delivery callbacks.

The Vercel scheduler audit found another mandatory gate: the project currently
has an empty cron definition registry still associated with an older
deployment. The two candidates contain four definitions in their build, but
`--skip-domain` did not transfer scheduler ownership. Before cutover, the
approved fenced deployment must register exactly those four definitions while
all cron enable flags remain false, and project ownership must be re-audited.

Finally, the inbound queue now has a reviewed FIFO correction: original
provider `received_at` is preserved for delayed replay, head-of-stream is
ordered by `(received_at, id)`, and the supporting index is introduced by
`20260829_inbound_fifo_v2`. The exact revision was then applied incrementally
to the same disposable rehearsal database from the approved predecessor
`20260825_chat_idempotency_v1`. Postflight returned the new single head, 172
public tables and the exact non-unique index columns
`(tenant_id, stream_key, received_at, id)`.

The attempt to create an additional post-FIFO Neon backup branch was refused
by the provider because the project branch limit is already reached. No branch
was deleted to make room. The existing pre-FIFO postmigration branch remains
the rollback reference for this isolated rehearsal, while final cutover still
requires explicit backup capacity and a new post-migration branch before
traffic can move.

## Shared writer authority and inbound continuity - 2026-08-29

The cutover gate now uses a PostgreSQL control connection that is explicit and
common to Render and Vercel. It never falls back to either runtime's
application `DATABASE_URL`; this is required because the source Render
PostgreSQL and destination Neon databases are different before the cut. Once
enabled, an absent/invalid control DSN, runtime identity, singleton row or
database response fails closed for unsafe HTTP, internal crons, Celery and
permanent workers. Ownership transitions use a singleton, CAS epoch and a
database constraint that prevents both runtimes from being active writers.
The control query has bounded connect, pool, statement, lock and idle
timeouts.

The exact incremental migrator dry-ran successfully from the already-applied
`20260829_inbound_fifo_v2` revision, then applied only
`20260829_global_writer_authority_v1` under its advisory lock. The migration
source SHA-256 was
`e5e1801f1d26cc7e596c8dd33418df2122cce4cd52cfb6e83ef2aabc4369950f`.
Postcheck and a separate read-only status command proved one singleton with
owner `NULL`, epoch `0`, `render_fenced=true` and `vercel_fenced=true`; no
runtime owns writes yet. A fresh preflight returned the exact new head, 173
public tables, 52,876 aggregate rows, all critical schema checks true and a
read-only transaction. Aggregate count is destination inventory evidence,
not renewed Production content parity.

Render standby verify-only was repeated against that exact authority revision.
It pinned the same Neon project/branch, verified the demo, idempotency, FIFO
and authority contracts in a read-only transaction, rolled back, and reported
no writes or ownership acquisition. The project still has ten branches, so a
post-authority backup could not be created without deleting an existing
branch. The existing `br-purple-mode-acfyj7js` branch remains the pre-authority
rollback point; capacity for a new final backup is still a NO-GO gate.

An independent inbound buffer was implemented for the maintenance interval.
It accepts only correctly signed Twilio forms for the pinned Junin recipient,
persists before ACK into a separate required database, encrypts the complete
payload with AES-256-GCM, applies a separate envelope HMAC, deduplicates by
`MessageSid`, preserves original `received_at`, and replays FIFO through the
existing durable intake. Replay requires the same stream-secret fingerprint
and an allowed global writer decision before it can take a lease.

The final adversarial corrections close three deployment blockers. An
environment-driven ingress runtime now rejects SQLite, direct PostgreSQL and
PostgreSQL without TLS before it can construct an ACK-capable application;
SQLite remains available only through an explicitly injected test factory.
Startup verifies the complete known table contract, including columns, types,
nullability, primary key, checks, index order/uniqueness/partial predicate and
the absence of unexpected foreign keys. Removing the partial unique stream
lease index now makes both migration verification and startup fail closed. The
survey-effect CLI and common dispatcher also evaluate the local fence and
shared writer authority before any application query, claim or effect, so a
manual/direct invocation cannot bypass cutover ownership.

The buffer is not deployed: it still needs a separate Neon project/database,
secret loading, retention/metrics, a remote signed-webhook drill and a
controlled webhook switch. The final disjoint recertification groups passed
496 focused tests: 87 buffer/inbound, 112 authority/survey effects, 151 exact
migration/preflight/cron/sender/rollback, and 146 worker/service integration
tests. Python compilation and `git diff --check` also passed. The Vercel cron
registry was queried again and still reports all four definitions as `not
deployed`; their enable flags remain off and scheduler ownership has not moved.

Finally, the Junin sender audit ran read-only against the restored Neon
database with the required official ending `3718`. It failed closed at
`tenant_profile_sender_mismatch`; the existing dry-run reconciler then stopped
at `provider_connection_exactly_one_required`. No row or provider resource was
changed and no message was sent. A fresh provider read snapshot and an exact
production connection/sender binding are mandatory before reconciliation or
any live canary.

## Post-review fenced candidate and provider read - 2026-08-29

The reviewed revision `e8373ae1056aa5d5293aa40b9481bcc347d51347` was
published and deployed as `dpl_CK7KpRAWqDFhR5vkRnV8mc3fw11H` with
`--skip-domain`. It targets only the isolated rehearsal database, has the local
writer fence enabled, leaves shared authority disabled, and explicitly keeps
all Vercel cron, WhatsApp notification, inbound wakeup, durable-worker,
Twilio provisioning and provider-live flags disabled.

| Check | Result |
| --- | --- |
| Deployment | `READY`; exact backend revision returned by `/api/version` |
| Runtime | `/health=200`; database health `200`; Neon and Redis readiness `ok` |
| Junin read | public tenant profile `200`, slug `junin`, `Municipalidad de Junin` |
| Unsafe HTTP | `POST /api/tickets` returned no-store retryable `503` from the writer fence |
| Internal schedules | all four routes returned no-store `503`, `cutover_writer_fence_enabled` |
| Aliases | only the Vercel technical project alias; no public/custom alias |
| Scheduler registry | four definitions still report `not deployed`; ownership did not move |
| Public Production | `api.chatboc.ar` still returned Render revision `8ced9216...` with Render headers |

A fresh Twilio CLI provider read, using the already configured active account,
listed four Messaging Services and exactly one WhatsApp Channel Sender ending
`3718`. That sender belongs to one Messaging Service; both its inbound request
and status callback use host `api.chatboc.ar`, and no fallback URL is
configured. Only redacted identifier suffixes and URL-host/path hashes were
emitted. This read performed no provider mutation and sent no message. It
proves the official provider resource exists, but the restored Neon database
still lacks the exact tenant-owned production connection/sender binding, so
the sender gate remains NO-GO until a reviewed, idempotent reconciliation is
applied and the full audit certifies exact account, SID, URL and tenant
ownership.

A second independent GET used Twilio's WhatsApp Senders v2 resource, which
exposes registration status rather than merely Messaging-Service association.
It returned four account senders: the official ending `3718` is `ONLINE`, the
sandbox ending `8886` is `OFFLINE`, one unrelated sender is `ONLINE`, and one
other sender is `OFFLINE`. Exactly one v2 sender ends `3718`; its inbound and
status callbacks resolve to `api.chatboc.ar` at `/webhook/whatsapp` and
`/twilio/whatsapp/status`, with no fallback URL. This remains read-only provider
evidence: it does not yet prove that the destination Vercel credential belongs
to the same account, and it did not send a message.

## Isolated provider-binding dry-run and ingress package - 2026-08-29

A new fail-closed provider-connection reconciler was exercised in its default
read-only mode against `render_rehearsal_20260829` on branch
`br-floral-unit-acgqawl6`. It found exactly one active Junin tenant (`id=22`),
one unique owner, no scoped Twilio/WhatsApp/production connection and no
cross-tenant account conflict. The proposed action is one create, with zero
deletes, zero provider calls and zero messages. Its plan digest is
`44e357613dd48e66840644d9c088a2b48b7c85a391980727d01a0199cf50e543`.
The account and database are represented only by hashes and identifier suffixes
in the command output. No row was changed. Apply remains blocked pending an
independent review of the provider-read evidence and readiness status.

That independent review rejected this first plan as P1: it would have promoted
the new connection to `online` using operator-supplied evidence labels that
were not cryptographically bound to the tenant, provider snapshot, callback,
credential reference or deployment revision. The digest above is therefore
invalidated and MUST NOT be approved or applied. The replacement workflow must
first persist a non-ready connection and only promote it from a fresh canonical
provider snapshot whose digest is part of the approved plan. The isolated
database remains unchanged.

The replacement reconciler was subsequently reviewed and exercised only on
the isolated rehearsal database. Its approved dry-run digest was
`73813fb273f96e5b3e856075a16187f56310cfca696ebe6eb47baee25bb6fdd7`.
The exact apply created one Junin connection in
`pending_provider_verification`; a post-apply dry-run returned `noop`. It did
not set the connection online, call Twilio, send a message, change a callback
or touch Production. This supersedes the earlier statement that the isolated
database had no row change; the invalidated plan above remains invalid.

The provider gate now also has reviewed evidence producers: an allowlisted
GET-only Twilio collector and a bearer-protected destination-runtime attestor.
They cryptographically bind the official sender snapshot and the exact Vercel
project, deployment, revision, TLS PostgreSQL identity, nonce, cutover window
and tenant-scoped credential without emitting raw secrets. Their integrated
focal suite passed 71 tests. This is code readiness only: neither producer has
yet emitted the fresh paired envelopes for a final deployment, and no online
promotion is authorized.

The cutover ingress also gained a dedicated Vercel container package inside
`cutover_ingress/`. Its deny-first build context copies only the six ingress
modules, uses a non-root user, registers no cron or backend rewrite, exposes no
LLM or outbound-provider client, requires the Neon pooled endpoint at runtime
and delegates reusable connection management to Neon while SQLAlchemy uses
`NullPool`. Migrations require the direct sibling URL for the exact same branch
and database. Its health contract
continues to declare `mode=buffer_only` and `providers_enabled=false`.
Ingress/package tests cover official Twilio signature handling for the default
HTTPS port, Unicode, reserved and empty form values. Duplicate form keys are an
intentional fail-closed exception and return `422` before persistence. The
suite also covers persistence-before-ACK, schema contract, WSGI isolation,
minimal dependencies and pooled-runtime/direct-migration separation. A remote
container build and signed webhook smoke were still required at that checkpoint;
the following section records their isolated completion.

## Isolated ingress deployment and encrypted canary - 2026-08-29

The dedicated database `cutover_ingress_rehearsal_20260829` was initialized
through its direct Neon endpoint and then independently verified through the
pooled runtime endpoint. Verification checked the exact schema and executed a
valid INSERT followed by rollback through the runtime role; no probe row
remained. Canonical `postgresql://` Neon URLs are normalized to the bundled
`postgresql+psycopg` driver, while host, effective port and exact database name
must match between the pooled and direct sibling URLs. Target-changing
connection options are rejected.

The package was linked from `cutover_ingress/` to the dedicated Vercel project
`chatboc-cutover-ingress`. Production deployment
`dpl_G3oYffPGtPD1uesdsdX3rYbTShpT` has only the two Vercel technical aliases;
it has no Chatboc custom domain and received no provider traffic. The build log
certifies the container path, explicit ingress module allowlist, non-root user,
Gunicorn `$PORT` binding and absence of the Twilio SDK/outbound REST client.
The image contains a minimal inbound-only signature verifier whose parity is
tested against the official SDK.

Runtime configuration used a random synthetic Twilio account/token that cannot
address the real account. `/health` returned `database=reachable`,
`mode=buffer_only` and `providers_enabled=false`. A signed remote canary then
proved `403` for an invalid signature, `200` for creation, `200` for the exact
duplicate and `409` for the same SID with changed content. Neon contained one
encrypted row, no plaintext body, and the cleanup deleted exactly that row.
The canary made no Twilio call and attempted no replay into Chatboc. The real
Twilio webhook, `api.chatboc.ar`, Render writers and public DNS remain
unchanged.

The final reviewed ingress source was then committed as
`6884985709f1b5f2977f0e9022f62c0b4ab72785`, pushed to the rehearsal branch and
redeployed as `dpl_7fpVPU68wypDo4qWoEx83u6BvPqb`. `/health` exposes the
runtime-computed source fingerprint
`fc806576686a21614c704c32da26332e04517d74bfbe073d0b69c5c4abb5e71f`, returned
`status=buffer_ready`, and caches the database probe for at most five seconds
to prevent health polling from multiplying pooled connections. The complete
signed/encrypted canary passed again against this exact image and cleaned its
single row.

An isolated alias rollback drill moved only
`chatboc-cutover-ingress.vercel.app` to the preceding deployment and then back
to `dpl_7fpVPU68wypDo4qWoEx83u6BvPqb`. Deployment-ID inspection and a
cache-busted health request proved the alias again serves the exact source
fingerprint above. The first HTTP-only restore check was attempted too soon and
observed stale alias content; no Chatboc or provider endpoint was involved.

## Exact fenced backend and restored-database correction - 2026-08-29

The first deployment of revision `8f4d80e65f5ca6f268e4e26214d3c47ba5019b59`
proved the request and background fences but failed deep readiness with
`required_schema_missing`. A read-only Neon inspection showed that the Vercel
runtime had inherited the primary `main/neondb`, whose migration head does not
contain `municipio_chat_idempotency_receipt`; the independently restored
`render_rehearsal_20260829` database on branch
`br-floral-unit-acgqawl6` does contain the required schema. No migration was
applied to Neon main and no Render database or service was changed.

Production-scoped Vercel runtime and migration URLs were then rebound through
Neon CLI to the pooled and direct endpoints of that exact restored database.
The local writer fence stayed enabled and shared writer authority stayed
disabled. A separate operational defect was also fixed: the authenticated
runtime attestor is an explicitly marked read-only POST and can now emit
evidence during a writer-fenced window; contradictory read-only/writer markers
fail closed. The focal evidence/fence suite passed 83 tests.

Revision `21e77ca02be9ab0f0875b65622b898b16885f087` was pushed and deployed
with `--skip-domain` as `dpl_HWoecwQtVvuxNwr3x5nddSatF4zj` at
`chatboc-backend-ompj6a90y-marcelos-projects-c26aa499.vercel.app`.

| Check | Result |
| --- | --- |
| Revision | `/api/version` returns exact `21e77ca02be9ab0f0875b65622b898b16885f087` |
| Deep readiness | `/health/ready=200`; database and Redis both `ok` |
| Request fence | municipal chat POST returns no-store retryable `503` |
| Background fence | all four cron paths return no-store `503`, zero execution |
| Runtime attestor boundary | unauthenticated POST reaches the read-only handler and returns no-store `401`, not the writer-fence `503` |
| Vercel scheduler | definitions are present in the deployment but `vercel crons list` still reports all four `not deployed`; ownership has not moved |
| Preview backend | `api-preview.chatboc.ar` remains on fenced revision `84021daf003291aa479e9f72d4f836c2bfe16e04` |
| Public Production | `api.chatboc.ar` remains Render/Gunicorn revision `8ced9216ff134951a0e3cb050c473e364c28be5c` |

This closes the current candidate's schema/readiness regression and proves the
read-only attestation boundary. It does not provide the fresh paired provider
envelopes because the destination still lacks the required independent HMAC,
bearer and tenant-scoped credential configuration. It also does not deploy or
activate scheduler ownership, fence Render, certify a final frozen export,
move a webhook/domain, perform a real provider canary or authorize Render
retirement.
