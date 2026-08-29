# Render to Neon and Vercel Production cutover

Status: **NO-GO until every mandatory gate below is certified.**

This runbook is the operational source of truth for moving `api.chatboc.ar`
from the current Render web service and Render PostgreSQL to the fenced Vercel
candidate and Neon. It intentionally separates preparation from the disruptive
maintenance window. No step authorizes deleting Render or its database.

## Confirmed live baseline

| Surface | Confirmed state |
| --- | --- |
| Public API | Render web service, revision `8ced9216ff134951a0e3cb050c473e364c28be5c` |
| Source of truth | Render PostgreSQL 18, not `/data/database.db` |
| Source schema / inventory | `20260820_survey_content_jurisdiction_v1`; 170 public tables; 52,863 aggregate rows |
| Neon main | `20260825_demo_survey_participation_v1`; 171 public tables; 52,751 aggregate rows |
| Fenced Vercel candidate | `dpl_GAqPTuiFEapWUqti3NRTU6VkzaVH`, revision `d3067af02a3bc157c7adba4d7652599c097516ed`, no aliases |
| Junin WhatsApp sender | official sender ending `3718` |
| Wrong default found in Render | sandbox sender ending `8886`, offline |
| Junin ownership model | official legacy mapping present; production `provider_sender` absent |
| Secondary Junin mapping | active legacy mapping ending `5678`; classification pending |
| Background processing | no active Render workers or cron services were found |
| Public traffic | unchanged; Render is still the only public writer |

The SQLite file on the Render disk is a legacy artifact and must not be used as
the final migration source. Current Render and Neon row counts, table counts,
schema revisions and fingerprints differ. Applying only Alembic migrations
would lose or preserve the wrong data.

## Non-negotiable safety rules

1. Render stays running until the cutover, rollback drill and soak complete.
2. Do not run `flask db upgrade`, `alembic upgrade head`,
   `scripts/apply_migrations.py`, a seed command, or direct mutation SQL
   during the freeze.
3. Never restore with `pg_restore --clean` into Neon main.
4. Never copy the offline Twilio sandbox sender into Vercel.
5. Exactly one compute environment may own writes, WhatsApp processing and
   outbox/cron execution at a time.
6. Preview deployments that point to Neon main are writers too. They must be
   fenced or moved to an isolated Neon branch before final parity evidence.
7. A `READY` deployment, a green health route or matching aggregate counts do
   not prove content parity or application correctness.
8. Evidence must contain no database URL, password, token, raw row, phone,
   message body or other personal data.

## Gate 0 - release identity and owners

- [ ] Approve one immutable backend revision and one immutable frontend
      revision.
- [ ] Record the maintenance start, maximum read-only interval, rollback
      deadline, operator and decision owner.
- [ ] Record the Render service ID, Render PostgreSQL ID, Vercel deployment ID,
      Neon project/branch/endpoint identities and current DNS TTL.
- [ ] Pause unrelated deploys, seeds, admin scripts and direct SQL sessions.
- [ ] Preserve the existing Twilio webhook and status-callback URLs for
      rollback.
- [ ] Approve and test an inbound-continuity path that can durably buffer
      signed WhatsApp webhooks outside the frozen source database. Twilio
      connection retries alone are not a maintenance-window buffer: callback
      overrides allow at most five retries and a 15-second total timeout.

Exit evidence: `cutover-window-*.json`, signed or archived outside the
application repository.

## Gate 1 - destination rehearsal

- [x] Create a fresh isolated Neon rehearsal branch, a separate empty database
      and a verified post-migration backup branch.
- [x] Import a transactionally consistent Render PostgreSQL logical snapshot
      into that isolated empty database with PostgreSQL 18.6 tooling.
- [x] Avoid overwriting destination data: the restore target was empty and the
      pre-migration strict audit found no missing or extra rows.
- [x] Run the strict PostgreSQL-to-Neon auditor. The strict policy must compare
      `municipio_ticket.estado`; Render remains authoritative until freeze.
- [x] Apply exactly these revisions from the live Render baseline, in order:
  1. `20260825_demo_survey_participation_v1`
  2. `20260825_legacy_municipio_ticket_scope_repair_v1`
  3. `20260825_chat_idempotency_v1`
- [ ] Repeat preflight, parity, migration, rollback and application canaries on
      the isolated branch.

Exit evidence: restorable branch ID, source snapshot ID, WAL positions,
strict-parity evidence digest and exact-migration evidence digest.

## Gate 2 - Render writer freeze

This is the first disruptive step and must begin only inside the approved
window.

- [ ] Replace the live Render predeploy command with
      `python -m scripts.run_predeploy_migrations`.
- [ ] Deploy the approved hardened revision with
      `CUTOVER_WRITER_FENCE_ENABLED=true`.
- [ ] Keep `CHATBOC_RENDER_STANDBY_MODE=false` while Render still points to its
      original PostgreSQL.
- [ ] Restart the web generation and prove every unsafe request and every known
      mutating GET returns the no-store `503` fence contract.
- [ ] Confirm no old Render generation remains.
- [ ] Stop or fence every external writer: Preview, scheduled jobs, manual
      shells, provider polling, seeds and direct SQL.
- [ ] Divert inbound WhatsApp to the approved durable cutover buffer before
      the source fence. Do not return `503` for a multi-minute freeze unless a
      separately tested fallback captures the same signed request.
- [ ] Drain in-flight requests/provider effects and prove source WAL/counts
      remain stable across the observation interval.

Exit evidence: `render-fence-*.json` binding revision, process generation,
route matrix, timestamps and stable source WAL.

## Gate 3 - final source snapshot and strict parity

- [ ] Create the managed Render PostgreSQL logical export at the fenced
      boundary and retain the PITR reference.
- [ ] Record export ID, completion time, source schema revision, source WAL and
      artifact checksum without exposing its download URL.
- [ ] Create a fresh Neon final-cutover branch and an explicitly empty target
      database. Record both IDs before restore; never restore this dump over
      the existing non-empty Neon main database.
- [ ] Restore the frozen export into that empty target on the already
      rehearsed path. A different reconciliation path requires its own
      row-ownership specification, rehearsal and approval.
- [ ] Run `scripts/audit_render_neon_parity.py` in PostgreSQL source mode,
      strict comparison mode and with the final strict policy.
- [ ] Resolve every missing, changed and destination-only row by explicit
      ownership. No blanket last-write-wins rule is allowed.
- [ ] Repeat until the strict auditor certifies the frozen source.

Exit evidence: signed parity artifact tied to the fence and final export IDs.

## Gate 4 - exact Neon migrations

- [ ] Use a direct, TLS Neon connection; reject pooler URLs.
- [ ] Prove project, branch, endpoint and current revision match the approved
      rehearsal.
- [ ] From the Render source revision, target only
      `20260825_demo_survey_participation_v1`; never run an open-ended
      `upgrade head`.
- [ ] Run the dedicated cutover migration command in dry-run mode for the two
      remaining revisions.
- [ ] Apply the two remaining approved revisions under an advisory lock with
      bounded statement/lock timeouts.
- [ ] Verify the single Alembic revision after each step, the three Junin
      ticket repairs and the chat-idempotency table/index.
- [ ] Run the full read-only Neon preflight and take a post-migration backup.

Exit evidence: exact-migration JSON plus post-migration backup branch ID.

## Gate 5 - Vercel runtime, jobs and providers

- [x] Point the fenced no-alias Vercel candidate to the restored Neon rehearsal
      runtime target and preserve a separate direct migration target. The
      rehearsal runtime uses the pooler; Alembic verification remains direct.
- [ ] Configure R2. Prove put/get/delete with a non-personal canary on a
      separate no-alias deployment whose writes target only a disposable Neon
      database and whose crons/provider effects are disabled; a global writer
      fence necessarily blocks this write canary. Use an existing scoped token.
      Permit demo-auth mutation only on that disposable rehearsal database.
- [ ] Configure the official Junin sender ending `3718`; do not use the
      sandbox ending `8886`. Keep the other online sender isolated by tenant.
- [ ] Reconcile exactly one production `provider_sender` for tenant `junin`
      against its existing credentialed connection. Classify the legacy mapping
      ending `5678`; do not silently delete or promote it.
- [ ] Keep WhatsApp inbound in synchronous legacy mode for the initial cut
      unless the durable queue, secrets and tenant allowlist have passed their
      own staging gate.
- [x] Keep every Vercel writer cron disabled on the no-alias candidate until
      ownership is transferred.
- [ ] Verify login, tenant isolation, tickets, assignment, expanded
      conversation, text reply, image/R2, location, form, survey, vote,
      analytics and heatmap against the certified database.
- [ ] Verify Twilio signatures and callbacks read-only before any live send.

Exit evidence: redacted variable-name inventory, authenticated application
canary report and provider configuration report.

## Gate 6 - traffic switch and live canary

- [ ] Move `api.chatboc.ar` to the approved Vercel deployment while both
      environments remain fenced.
- [ ] Confirm DNS/TLS, `/health`, `/health/ready`, version and CORS.
- [ ] Transfer cron/outbox ownership, with no overlap.
- [ ] Remove the writer fence only on Vercel and prove Render remains fenced.
- [ ] Run one controlled Junin WhatsApp canary: inbound text, location, audio,
      image and one form/vote interaction; then one reply from the ticket.
- [ ] Prove one persisted inbound row per provider SID, one CRM/ticket effect,
      one outbound attempt and signed callback transitions through delivery.
- [ ] Drain and replay every cutover-buffer receipt exactly once; match both
      `MessageSid` and `I-Twilio-Idempotency-Token` evidence and leave the
      buffer empty.
- [ ] Confirm queues, `send_uncertain`, dead-letter and failed counters are
      zero before normal operation resumes.

Exit evidence: live canary ID, timestamps, redacted database receipts and
provider delivery status.

## Gate 7 - rollback and soak

Rollback after Vercel accepts writes must return compute to Render **while
Render uses Neon**. Do not resume writes against the old Render PostgreSQL.

- [ ] Configure Render standby with the certified Neon target,
      `CHATBOC_RENDER_STANDBY_MODE=true`, the hardened predeploy command and
      writer fence still enabled.
- [ ] Test the rollback route without enabling two writers.
- [ ] Observe application errors, latency, database connections, R2 delivery,
      Twilio callbacks, queue age, OOM/restarts and business canaries.
- [ ] Complete at least 24 continuous hours of soak spanning one municipal
      business period. Invoke every scheduled route once in a controlled,
      authenticated and idempotent validation window as well; the weekly cron
      cannot be covered by a 24-hour natural soak. During soak: readiness must be
      100%, application 5xx below 0.5%, no lost/duplicate provider SID, no
      `send_uncertain`, dead-letter or `63019`, queue age below five minutes,
      and the critical Junin CRM/WhatsApp canary must pass at the start and end.
- [ ] Keep Render PostgreSQL, export, PITR and the old compute recoverable
      throughout the 24-hour soak. Stopping old compute and deleting the old
      database are separate decisions; database deletion requires a later
      explicit sign-off after the retained export and Neon backup are tested.
- [ ] Retire Render only after a rollback drill and explicit final sign-off.

## Immediate NO-GO conditions

Stop and restore the previous safe state if any of these occur:

- source WAL changes after the fence evidence;
- Preview or another client can still write Neon during final parity;
- exact parity reports missing, changed or unclassified destination-only rows;
- Neon is not at the expected single migration revision;
- Vercel readiness is not green;
- two workers/crons can own the same queue;
- the official Junin sender is not online/owned by tenant Junin;
- Twilio attachment delivery repeats error `63019`;
- a live canary duplicates, loses or ambiguously sends a message;
- rollback cannot be executed without returning to stale Render PostgreSQL.

## Current decision

Preparation may continue safely: code review, tests, isolated Neon rehearsal,
variable-name inventories and canaries that do not send provider messages.
The Render writer freeze, managed final export, Neon main mutation, DNS move,
live WhatsApp canary and Render retirement remain maintenance-window actions.
