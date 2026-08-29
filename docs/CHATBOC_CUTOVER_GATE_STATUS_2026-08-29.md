# Chatboc Render to Vercel/Neon gate status

Observed: 2026-08-29, approximately 18:10 ART.

Decision: **NO-GO for the production cutover and NO-GO for retiring Render.**

This is a status ledger, not a cutover authorization. It contains no secrets
and does not replace the signed evidence required by the production runbook.

## Certified or rehearsed

| Gate | Current evidence |
| --- | --- |
| Public source | `api.chatboc.ar` still resolves to Render. Live backend revision is `8ced9216ff134951a0e3cb050c473e364c28be5c`; `/health` is HTTP 200. |
| Fenced Vercel backend | Deployment `dpl_C2qNUv9rZqHxn6Rc3YuDgbyqDyYi` serves backend `df2702f25bcab24223c7e095a5cc4cfa5c101de2`; readiness passed and unsafe HTTP writes return the writer-fence contract. |
| Vercel cron registry | Four approved cron definitions are registered on the fenced candidate. All three execution enable flags remain false and all four runtime probes return the background-writer fence contract. |
| Frontend Preview | Deployment `dpl_2S7B9no7wniJjW6Vn8genrYzCVCs`, revision `8061bd3fcf5381fff79427e5e710c2a658f856c5`, is `READY` at `chatboc-r2-preview.vercel.app`; the compiled QA routes target `api-preview.chatboc.ar`, not the public Render API. The build contains eight Preview rewrites, zero public Render rewrites and the exact revision in its served HTML. |
| Neon rehearsal identity | Project `nameless-rain-94060889`, branch `br-floral-unit-acgqawl6`, database `render_rehearsal_20260829`, migration head `20260829_global_writer_authority_v1`. Schema preflight passes. |
| Ingress retry evidence | Revision `42e693d49434734969517b934f8274ce1d3044c3` records the optional Twilio retry token only as a versioned, domain-separated HMAC after signature validation. The raw header is never persisted or returned; the full focal module passes 49/49 tests. This is code/rehearsal evidence, not a real Junin replay certificate. |
| Rollback contract | Offline manifest validator v2 exists and is test-covered. It validates evidence shape only; a real window-bound manifest is still required. |

## Blocking evidence

### Render writer authority

The live Render service does not currently declare the writer-fence, standby,
global-authority or runtime-identity controls. A local blueprint containing
false defaults is not evidence that the live service is fenced. Render and all
other possible writers must be fenced and attested inside the maintenance
window before the final export.

### Final content parity

The rehearsal database is not a valid final destination. A fresh read-only
comparison found:

- Render: PostgreSQL 18.3, migration `20260820_survey_content_jurisdiction_v1`,
  170 tables and 52,864 aggregate rows.
- Neon rehearsal: PostgreSQL 18.6, 173 tables and 52,877 aggregate rows.
- 169 common tables audited; 162 exact and 7 divergent.
- 12 extra common-table rows, 12 different cells and one additional
  destination-only writer-authority row.
- Render WAL advanced after a short stable sample, so no continuous writer
  freeze can be attested.

The final target must therefore be a new empty Neon database restored from an
export produced after the Render fence. The rehearsal database must not be
relabelled or reused as an exact-parity production database.

### Vercel production runtime prerequisites

The current Production and Preview environment declarations now contain
`TENANT_CLAIM_RECEIPT_SECRET_V1` and `RATELIMIT_STORAGE_URI` as sensitive
variables. A dedicated random HMAC secret was created independently per
environment, and each rate-limit URI was derived from that environment's
existing shared `REDIS_URL` without printing any value. A new fenced deployment
and guarded predeploy must still prove that the runtime receives valid values;
environment-name presence alone is not certification and does not modify an
already-built deployment.

The durable WhatsApp queue also remains uncertified because
`WHATSAPP_INBOUND_HASH_SECRET` is absent. The initial cutover must therefore
stay on the synchronous legacy ingress unless the independent durable-queue
gate is completed separately.

### Junin WhatsApp/Twilio

Read-only provider inspection confirmed one official Junin sender ending
`3718`, online, with the expected webhook and status callback. The gate remains
blocked because:

- the sender has zero Messaging Service associations; the contract requires
  exactly one;
- the Junin tenant profile has no matching sender binding;
- the provider connection is not in the ready state;
- no ready provider-sender record exists;
- the credential reference is not the approved Junin tenant-scoped reference;
- the formal snapshot/runtime-attestation HMAC and bearer variables are not
  configured on the Vercel candidate.

No provider settings, messages, callbacks or tenant rows were changed during
this inspection.

### Durable ingress and replay

The isolated `chatboc-cutover-ingress` deployment is healthy in
`buffer_only` mode with provider effects disabled, but its current Twilio
identity is synthetic. It proves isolated infrastructure only, not real Junin
ingress. A real signed drill still has to prove invalid-signature rejection,
exact duplicate handling, conflicting-payload rejection, encrypted
persistence, exactly-once replay and an empty final queue with zero dead,
retry or uncertain deliveries.

## Required maintenance-window sequence

1. Archive the exact owners, identities, TTL, callback rollback values and
   maximum read-only interval.
2. Divert signed WhatsApp ingress to the independently verified durable buffer.
3. Deploy and activate the Render web/background writer fence; fence Preview,
   jobs, workers, shells and direct SQL writers; attest stable counts and WAL.
4. Produce the final logical export/PITR reference.
5. Restore into a fresh empty Neon final database.
6. Run strict exact parity with real snapshot/fence evidence and persistent
   HMAC; any non-zero exit stops the window.
7. Apply only the approved cutover migrations through the direct TLS endpoint;
   rerun preflight and backup.
8. Bind Vercel and Render standby to the same certified Neon identity while
   both remain fenced.
9. Certify provider snapshot, ingress/replay and cron ownership; then transfer
   global writer/job authority without overlap.
10. Run the controlled Junin canary and begin the 24-hour soak.

## Rollback invariant

Before Vercel accepts any write, aborting the window may keep the old Render
database authoritative after the durable buffer is reconciled. After Vercel
accepts a write, rollback may move compute back to Render only while Render is
bound to the same certified Neon database. The stale Render PostgreSQL must
never be reactivated as writer after that point.

Render compute, Render PostgreSQL, the final export and PITR recovery remain
available throughout the soak. Retirement requires a successful rollback drill
and explicit final sign-off.
