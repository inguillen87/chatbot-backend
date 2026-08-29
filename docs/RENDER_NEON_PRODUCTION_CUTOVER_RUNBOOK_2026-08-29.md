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
| Source schema / inventory | `20260820_survey_content_jurisdiction_v1`; 170 public tables; 52,864 aggregate rows in the latest read-only audit |
| Neon rehearsal | Project `nameless-rain-94060889`, branch `br-floral-unit-acgqawl6`, database `render_rehearsal_20260829`; 173 public tables; 52,877 aggregate rows; it is not eligible to become the final database |
| Fenced Vercel candidate | `dpl_C2qNUv9rZqHxn6Rc3YuDgbyqDyYi`, revision `df2702f25bcab24223c7e095a5cc4cfa5c101de2`; readiness passes and every unsafe HTTP/background write remains fenced |
| Junin WhatsApp sender | official sender ending `3718` |
| Junin ownership model | provider inspection sees exactly one official Junin WhatsApp sender, but the restored database lacks the exact ready `provider_connection`/`provider_sender` binding and the tenant profile sender field is empty |
| Contact-phone boundary | Phone numbers stored on a user, employee or tenant contact profile are not WhatsApp sender evidence. No second Junin WhatsApp sender is certified by this runbook. |
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
- [ ] Use the guarded Production wrapper documented in
      `docs/VERCEL_PRODUCTION_PREDEPLOY_GUARD.md` from the exact clean backend
      revision. Archive its redacted `ready` JSON; any blank Production value,
      DSN alias mismatch, uncertified Neon identity or unsafe writer flag stops
      the wrapper before `vercel deploy --prod`. This is a release-process
      control, not a platform lock: a raw Vercel CLI command can bypass it and
      is therefore not an approved release path.
- [ ] Confirm that the archived Neon preflight names the same Git revision,
      records a clean worktree and matches the guarded checkout's deterministic
      `migrations/versions` Git-tree SHA-256. Evidence without these fields is
      stale and must be regenerated read-only.
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
- [x] Approve and rehearse the new inbound FIFO revision
      `20260829_inbound_fifo_v2`, which preserves provider receipt order when
      buffered messages are inserted after newer messages.
- [x] Rehearse the exact incremental authority revision
      `20260829_global_writer_authority_v1` from the already-certified FIFO
      revision. The resulting singleton has no owner, epoch `0`, and both
      runtimes fenced; it cannot authorize writes by itself.
- [x] Re-run the read-only Neon preflight and Render standby verify-only gate
      at the exact authority revision. Both verified project/branch identity,
      FIFO, idempotency and authority constraints without acquiring ownership.
- [ ] Create a post-authority backup. The Neon project currently has all ten
      branch slots occupied; the existing `postmigration` branch is the
      pre-authority rollback point until capacity becomes available.
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
- [ ] Run the dedicated cutover migration command in dry-run mode for the four
      remaining revisions: repair, chat idempotency, inbound FIFO and global
      writer authority.
- [ ] Apply the four remaining approved revisions under an advisory lock with
      bounded statement/lock timeouts.
- [ ] Verify the single Alembic revision after each step, the three Junin
      ticket repairs, the chat-idempotency table/index and inbound FIFO index
      `(tenant_id, stream_key, received_at, id)`, plus the fenced singleton
      authority state.
- [ ] Run the full read-only Neon preflight and take a post-migration backup.

Exit evidence: exact-migration JSON plus post-migration backup branch ID.

## Gate 5 - Vercel runtime, jobs and providers

- [x] Point the fenced no-alias Vercel candidate to the restored Neon rehearsal
      runtime target and preserve a separate direct migration target. The
      rehearsal runtime uses the pooler; Alembic verification remains direct.
- [x] Configure and prove R2 put/get/delete with the approved non-personal
      canary on a
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
- [x] Fail closed in the independent cutover ingress when the runtime database
      is SQLite, direct PostgreSQL or lacks TLS, and verify the exact FIFO
      schema/index contract before an ACK-capable process can start.
- [ ] Provision the independent durable ingress database, load its separated
      encryption, envelope HMAC and Twilio idempotency-token HMAC secrets, run
      the signed remote persistence/replay drill and prove the queue is empty
      before and after the controlled webhook switch. Follow
      `docs/CUTOVER_WHATSAPP_INGRESS_CONTRACT.md`; never archive the raw
      `I-Twilio-Idempotency-Token`.
- [ ] Prove a signed request without the optional idempotency header remains
      compatible, while a request carrying that header fails closed without an
      ACK or database row when its dedicated HMAC secret is unavailable.
- [x] Provision the isolated ingress database and verify exact schema plus a
      rolled-back DML probe through the pooled runtime DSN.
- [x] Deploy the dedicated Vercel ingress with synthetic provider credentials;
      prove health, signed persistence, exact duplicate, conflicting replay,
      invalid signature, encrypted storage and exact cleanup without provider
      or Chatboc replay effects.
- [x] Bind the isolated deployment to an exact committed source fingerprint and
      drill rollback/restore of only its technical Vercel alias by deployment
      ID. No provider or Chatboc domain participates in this drill.
- [x] Package the ingress as an isolated non-root Vercel container with a
      deny-first build context, Neon pooled runtime plus direct-only migrations,
      SQLAlchemy `NullPool`, no crons, no LLM and
      no outbound-provider client. Local contract tests and the remote container
      build passed; real replay and controlled webhook switching remain in the
      unchecked item above.
- [x] Keep every Vercel writer cron disabled on the no-alias candidate until
      ownership is transferred.
- [x] Revalidate the approved hardened revision on fenced Vercel: exact source
      SHA, Neon/Redis readiness, Junin public read, unsafe HTTP fence and all
      four internal cron fences passed. `api.chatboc.ar` remained on Render.
- [x] Verify Junin admin login, ticket list, operational inbox, workflow
      metadata, synthetic ticket creation, self-assignment, status transition
      and internal conversation on the disposable Neon rehearsal target.
- [ ] Verify tenant isolation, expanded
      conversation, text reply, image/R2, location, form, survey, vote,
      analytics and heatmap against the certified database.
- [x] Register exactly the four approved Vercel cron definitions against the
      fenced candidate while all three enable flags are false. The 2026-08-29
      formal audit confirmed exactly four enabled registry definitions and
      four runtime probes returning the background-writer fence contract.
      This certifies registered, fail-closed ownership only; it does not
      authorize transferring or enabling job effects.
- [ ] Verify Twilio signatures and callbacks read-only before any live send.

### Evidence contract for Vercel cron ownership

`scripts/audit_vercel_cron_ownership.py` is an offline, fail-closed gate. It
does not query Vercel or invoke a cron route. Version 2 requires four local
snapshots and binds the runtime probes to the approved deployment ID and exact
runtime revision:

1. repository `vercel.json`;
2. the unmodified JSON from `vercel crons list --json` for the linked
   `chatboc-backend` project;
3. a redacted environment declaration containing only the three activation
   flags and `CRON_SECRET` presence/UTF-8 byte count, never its value;
4. one captured response for each cron path from the exact fenced runtime.

The remote registry must contain exactly four deployed definitions, report
`enabled: true`, and have no missing, unexpected or changed schedule. Each
runtime response must be `503`, `Cache-Control: no-store`, `Retry-After: 60`
and the exact `cutover.background_writer_fence.v1` payload with
`executed: false`. The probe capture is allowed only after `/api/version` and
the independent fence evidence identify the approved deployment. Never probe
these mutating GET routes when the fence state is unknown.

Run the offline audit from a private evidence directory outside the repository:

```powershell
py -3 scripts/audit_vercel_cron_ownership.py `
  --vercel-config vercel.json `
  --registry-json $cutoverEvidenceDir\vercel-crons.json `
  --env-json $cutoverEvidenceDir\vercel-cron-env-redacted.json `
  --runtime-probes-json $cutoverEvidenceDir\vercel-cron-fenced-probes.json `
  --expected-deployment-id $approvedVercelDeploymentId `
  --expected-runtime-revision $approvedRuntimeRevision `
  --audit-only
```

Exit `0` and `ready: true` certify only **registered and fail-closed** Vercel
ownership. They do not authorize enabling effects, changing DNS, fencing
Render, transferring the global writer epoch or sending a provider canary.
Vercel documents that cron jobs are created by a Production deployment, invoke
the Production deployment URL, and are not retargeted by Instant Rollback.
Rollback therefore requires an explicit cron disable or a redeploy of the
approved owner; a domain rollback alone is insufficient.

The GET-only Twilio snapshot collector, exact Vercel runtime credential
attestor and fail-closed promotion verifier are implemented and reviewed. The
isolated Junin connection is intentionally
`pending_provider_verification`. Do not mark this gate complete until both
fresh signed envelopes are captured from the same approved deployment/window,
the PostgreSQL concurrency check passes and the promotion dry-run digest is
explicitly approved. Tooling availability is not provider evidence.

Current sender evidence is a deliberate blocker: the read-only audit against
the restored Neon rehearsal database stopped at `tenant_profile_sender_mismatch`,
and the dry-run reconciler stopped at `provider_connection_exactly_one_required`.
No provider identifier was invented, no row was changed and no message was
sent. Production cannot proceed until a fresh provider read snapshot proves
the exact Junin connection, sender, webhook and callback ownership.

Exit evidence: redacted variable-name inventory, authenticated application
canary report and provider configuration report.

## Gate 6 - traffic switch and live canary

- [ ] Move `api.chatboc.ar` to the approved Vercel deployment while both
      environments remain fenced.
- [ ] Confirm DNS/TLS, `/health`, `/health/ready`, version and CORS.
- [ ] Transfer cron/outbox ownership, with no overlap.
- [ ] Bootstrap the shared PostgreSQL writer authority to the current Render
      owner while both global runtime flags are fenced. Both Render and Vercel
      must use the same explicit control DSN; the gate never falls back to
      either runtime's application database.
- [x] Apply the shared authority gate to HTTP, cron, Celery, permanent workers,
      replay and manual survey-effect CLI/dispatcher entry points before any
      application query, claim or effect.
- [ ] Fence and attest Render, transfer ownership by CAS/epoch while both are
      globally fenced, then activate Vercel. A failed/missing control read must
      leave both HTTP and background writers fail-closed.
- [ ] Remove the writer fence only on Vercel and prove Render remains fenced.
- [ ] Run one controlled Junin WhatsApp canary: inbound text, location, audio,
      image and one form/vote interaction; then one reply from the ticket.
- [ ] Prove one persisted inbound row per provider SID, one CRM/ticket effect,
      one outbound attempt and signed callback transitions through delivery.
- [ ] Drain and replay every cutover-buffer receipt exactly once; reconcile the
      canonical `MessageSid` with versioned `token_hmac` evidence (never the raw
      `I-Twilio-Idempotency-Token`) and leave the buffer empty. The token digest
      is audit evidence only; `MessageSid` remains the deduplication boundary.
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
- [x] Implement and prove verify-only schema validation against the isolated
      Neon rehearsal target. It pins project/branch and exact revision, opens
      `READ ONLY`, verifies structural contracts and always rolls back; it
      never executes an open-ended `upgrade head`. Live Render standby binding
      remains part of the unchecked configuration step above.
- [ ] Require `VERCEL_DURABLE_UPLOADS_REQUIRE_R2=true` on both computes.
- [ ] Pass `scripts/rehearse_compute_rollback.py --validate-only` with an
      approved `chatboc.compute_rollback_rehearsal.v2` redacted manifest before
      the remote drill. A v1 manifest is legacy and explicitly non-certifying.
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

### Rollback manifest v2 evidence contract

The validator is an **offline shape-and-binding gate**. It performs no provider,
database, deployment, DNS, queue or storage action. `ready: true` means that the
submitted v2 document is internally consistent and bound to evidence digests;
it does not prove that an artifact is genuine and does not authorize a cutover,
rollback or Render retirement. The decision owner must archive and independently
verify every referenced artifact outside the application repository.

Version 2 is mandatory for operational readiness and binds all of the following:

| Manifest area | Mandatory binding |
| --- | --- |
| Window/release | Window ID, release ID, UTC start/deadline, approved full 40-character backend Git SHA and pre-window approval/identity evidence |
| Neon | Project ID, branch ID, database, exact migration head, expected fingerprint, independently observed matching fingerprint and evidence for each observation |
| Render standby | Same backend SHA and exact Neon identity/fingerprint, `verify-only` schema action and a separate standby evidence artifact |
| R2 | `required` durable-upload policy and verified `put-get-delete` artifacts from both Vercel and Render against the same opaque target-identity SHA-256 |
| Ingress | HTTPS endpoint without credentials/query parameters, committed source fingerprint, queue target/schema, empty pre/post drill depths and a non-empty replay with matched persisted/replayed counts and zero failures |
| Authority states | Exact sequence `vercel_active`, `both_fenced`, `render_fenced`, `render_active`; authority epoch and owner in every state; one writer and one job owner in active states; zero owners during both freeze states |
| Transitions | From/to state, before/after authority epoch and owner, plus one unique transition evidence artifact ordered between its two state attestations |

Every evidence object must contain a unique bounded `id`, the exact expected
`kind`, artifact `sha256`, UTC `captured_at`, and copies of the same `window_id`,
`release_id` and full `backend_revision`. Operational evidence must fall inside
the declared window. Approval and immutable release identity evidence must be
captured no later than the window start. Evidence IDs, raw fingerprints,
provider endpoints and infrastructure identities are never emitted by the
validator; its output contains only aggregate checks and the digest of the
exact input manifest.

Authority epoch rules are fail-closed:

1. `vercel_active` starts at an already-issued positive epoch.
2. Entering `both_fenced` must advance the epoch and clear writer/job owners.
3. The `render_fenced` standby attestation must preserve that same fenced epoch.
4. Activating Render must advance the epoch again and assign both writer and
   job ownership to Render only.

Build the redacted manifest in the private evidence directory, never by
copying database URLs, tokens, provider payloads, phone numbers or message
bodies into it. Opaque infrastructure target identities must be SHA-256
digests, while artifact content remains outside the manifest.

```powershell
& $py scripts/rehearse_compute_rollback.py `
  --manifest $cutoverEvidenceDir\compute-rollback-v2.json `
  --validate-only
```

Required success shape: exit `0`, contract
`chatboc.compute_rollback_rehearsal.v2`, `ready: true`,
`external_actions_performed: false`, four states with job-owner counts
`1,0,0,1`, and three ordered transitions. Any v1 document exits blocked with
`legacy_contract_non_certifying`. Do not create a synthetic passing manifest to
clear this checklist: populate v2 only after the real, externally archived
evidence exists.

## Immediate NO-GO conditions

Stop and restore the previous safe state if any of these occur:

- source WAL changes after the fence evidence;
- Preview or another client can still write Neon during final parity;
- exact parity reports missing, changed or unclassified destination-only rows;
- Neon is not at the expected single migration revision;
- Vercel readiness is not green;
- two workers/crons can own the same queue;
- the Vercel project cron registry is empty, stale or bound to another
  deployment;
- the durable signed-webhook buffer is not accepting and reconciling inbound
  provider events during the dual-fence interval;
- the official Junin sender is not online/owned by tenant Junin;
- Twilio attachment delivery repeats error `63019`;
- a live canary duplicates, loses or ambiguously sends a message;
- rollback cannot be executed without returning to stale Render PostgreSQL.

## Current decision

Preparation may continue safely: code review, tests, isolated Neon rehearsal,
variable-name inventories and canaries that do not send provider messages.
The Render writer freeze, managed final export, Neon main mutation, DNS move,
live WhatsApp canary and Render retirement remain maintenance-window actions.
