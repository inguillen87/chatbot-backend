# WhatsApp durable turns: rollout and operations

This runbook covers the DB-backed inbound queue and ordered outbound outbox.
The feature remains opt-in. Do not enable `queue` on the public web service
until the migration and a continuously running worker are both verified.

> Production safety note (2026-08-02): `legacy` mode records its bounded
> `MessageSid` dedupe claim before all synchronous handlers finish. A later
> `503 RETRY` can therefore be acknowledged as a duplicate on the next Twilio
> delivery, while blindly releasing the claim could duplicate an already
> committed ticket/comment or an ambiguous provider send. Do not certify
> legacy mode as lossless or retry-safe. Production cutover requires `queue`,
> the dedicated worker, canonical channel identity in `enforce`, migrations,
> secrets and a signed provider staging smoke test.

> Current Render boundary (2026-08-02): the checked-in Blueprint explicitly
> pins the public web service to `legacy`, leaves the queue tenant allowlist
> empty, keeps the Celery wakeup disabled and leaves canonical channel identity
> in `shadow`. It declares a permanent worker only in zero-I/O standby and a
> retention cron with `SCRUB_ENABLED=false`, legal hold enabled and an empty
> historical tenant scope. This prepares infrastructure; it is not evidence of
> an active queue, a migrated Render database or a provider-certified rollout.

## Guarantees and current boundary

- In `queue` mode for an allowlisted tenant, a signed, tenant-scoped Twilio
  webhook is committed before returning `200`; excluded tenants remain legacy.
- In global `legacy` mode an authoritative historical sender/owner mapping may
  still run synchronously without a materialized `TenantProfile`; this preserves
  pre-rollout compatibility. In `queue` mode that same missing canonical tenant
  fails closed and can never select or bypass the canary allowlist.
- `(tenant, provider, MessageSid)` is unique and conflicting replays fail closed.
- Every new queued turn snapshots a canonical, tenant-scoped session binding
  (`binding_id`, version and identity HMAC). The worker verifies that snapshot
  before replay; it never reconstructs a phone-derived global session ID.
- Turns are processed FIFO per HMAC-derived conversation stream.
- Worker claims use leases and fencing; expired attempts have a bounded budget.
- Provider sends are staged in an ordered outbox. A timeout becomes
  `send_uncertain` and is never retried automatically.
- Failures proven to occur before provider I/O use bounded retry/backoff;
  permanent policy failures become `dead`. Later unsent messages from that
  same turn are cancelled, while a later inbound turn may continue.
- A signed Twilio status callback containing `outbound_attempt_id` can reconcile
  an uncertain send without resending it. Callback state is monotonic and a
  transient reconciliation exception returns `503`. The callback URL opts into
  bounded Twilio retries for `5xx`, connect and read-timeout failures using
  connection overrides; Twilio's default retry policy alone does not cover
  `5xx` responses.
- Payloads are allowlisted and queue health never exposes message bodies,
  addresses, credentials, or provider SIDs.
- A completed inbound turn atomically replaces its raw payload with a versioned
  tombstone containing only the original SHA-256 digest, categorical kind,
  size/count metadata and scrub timestamp. Dead turns retain payloads for a
  finite diagnostic window (72 hours by default) and are then scrubbed in a
  bounded batch. The outbox has a separate lifecycle because it needs its
  payload until provider delivery reaches a terminal state.

This is not yet a universal exactly-once guarantee for every business effect.
For explicit canary tenants the generic `domain_effect_outbox` now stages
ticket-created effects, direction-aware ticket-comment notifications and new
PyME order notification intents atomically with their aggregate. Canary orders
created through `PedidoService` (including the multimodal AI confirmation
path) materialize both CRM projections in the same transaction. Ticket-status
notifications, direct writes outside the covered services and final
provider-delivery reconciliation remain outside that contract. A socket-origin comment deliberately emits its single
realtime room event after the service transaction. Keep both queue systems
gated and follow `docs/domain-effect-outbox-runbook.md` for exact scope.

## Welcome template and media evidence

Do not infer which outbound failed from adjacent log lines. Correlate provider
request/message IDs and signed status callbacks. In the supplied 2026-07-28
trace, Twilio error `63019` belongs to the template submission; the sticker was
a different message and reached `sent`, `delivered` and `read`. The current
public sticker was independently fetched on 2026-07-29 and matched the local
39,418-byte WebP (512 x 512, transparent and static). That verifies the asset at
that instant, not a new production send.

Welcome template resolution now fails closed unless it comes from a recently
provider-synchronized approved registry row or fresh approval manifest. The raw
`WELCOME_TEMPLATE_SID` path is emergency-only behind
`WELCOME_TEMPLATE_OVERRIDE_ENABLED=false`; production overrides must be `HX`
references. Direct WhatsApp callback URLs request bounded retries for `5xx`,
connect and read-timeout failures, and callback ledger persistence failure
returns `503` instead of discarding the event.

Synchronous Twilio API acceptance is recorded as `provider_accepted`, never as
final delivery. Remaining work is a semantic receipt that binds each welcome
purpose (`template`, `sticker`, `audio`, `menu`) to its message reference and
callback lifecycle, plus a tenant/sender/template circuit breaker and a single
audited text fallback for asynchronous template failure. Do not retry a failed
template blindly.

## Safe rollout

1. Deploy the code with the web service still set to:

   ```text
   WHATSAPP_INBOUND_DURABILITY_MODE=legacy
   WHATSAPP_INBOUND_QUEUE_TENANT_IDS=
   WHATSAPP_INBOUND_CELERY_WAKEUP_ENABLED=false
   CHANNEL_SESSION_IDENTITY_MODE=shadow
   ```

2. Apply migrations through the current Alembic head and verify there is exactly
   one head. For this local change set the expected head is
   `20260802_notification_wa_v1`; its ancestor chain must include
   `20260729_pyme_order_payload_hash`,
   `20260729_whatsapp_turns`, `20260729_ticket_effects` and
   `20260729_realtime_tool_receipts`. Verify the deployed database separately
   with `flask db current`; a local head only proves repository topology.

3. Generate dedicated random secrets of at least 32 bytes and store them only in
   the platform secret manager as `WHATSAPP_INBOUND_HASH_SECRET`. Do not reuse a
   Twilio, Flask, JWT, Flow, or OpenAI secret. Rotating it changes FIFO stream
   identities, so rotation requires a controlled queue drain. Configure a
   separate `CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1`; follow
   `docs/channel-session-identity-runbook.md` for migration and rotation.

4. Review the separately declared background-worker service and verify that its
   database, AI, media/storage and tenant/provider secret scope matches the web
   service. Its Blueprint command and process role are:

   ```text
   CHATBOC_PROCESS_ROLE=whatsapp-durable-worker
   WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED=true
   python -m services.whatsapp_inbound_worker --standby-when-legacy
   ```

   In `legacy`, the CLI checks the explicit standby flag before importing the
   Flask application, then blocks on one signal-aware wait: it does not poll,
   open the database or construct a provider client. Before queue activation,
   independently verify every manually named per-tenant Twilio token reference
   is present on the worker; the generic subaccount token is not proof that all
   tenants share one credential. `OPENAI_API_KEY`, model selection, Gemini,
   Maps, Hugging Face, R2/audio cache, media bounds and Socket.IO are inherited
   from the web service in the Blueprint, but configured values must still be
   verified in Render without printing them. Google ADC/file credentials, if a
   tenant needs them, require a separately mounted worker secret/file and are
   not supplied by `fromService`.

   Move channel identity from `shadow` to `enforce` only after its separate
   shadow audit, configure the same dedicated identity/hash secrets, and set a
   non-empty `WHATSAPP_INBOUND_QUEUE_TENANT_IDS` while the web mode remains
   `legacy`. Runtime validation rejects `queue` with an empty or invalid tenant
   list. The worker mode is deliberately an independent service value: change
   the worker to `queue` first, prove its health, and only then change the web
   mode to `queue`. Perform this sequence first in isolated staging; Render
   service deployments are not an atomic production cutover.

5. Verify the worker contract and empty backlog:

   ```text
   python -m services.whatsapp_inbound_worker --health
   python -m services.whatsapp_inbound_worker --once
   ```

   Set the independent worker `WHATSAPP_INBOUND_DURABILITY_MODE=queue`, then run
   both commands in the worker environment. `--health` proves the schema
   and reports a payload-free backlog; it does not prove a continuously running
   poller or provider delivery. `--once` must complete in `queue` mode, and the
   permanent process must then remain healthy before web cutover.

   On PostgreSQL, also race an expired-lease recovery against a signed delivery
   callback and verify the callback cannot be overwritten. SQLite ignores the
   `FOR UPDATE`/`SKIP LOCKED` semantics used by this fence and is not sufficient
   evidence for that concurrency invariant.

6. Enable `queue` with exactly one controlled tenant ID in
   `WHATSAPP_INBOUND_QUEUE_TENANT_IDS`. Tenants outside the allowlist remain on
   the synchronous legacy path. Run signed text, emoji, location, audio, image
   and Flow/vote smoke cases. Confirm webhook acknowledgement happens after
   ingress commit but before media, STT, vision, LLM or Twilio outbound work.

7. Monitor queue health and delivery callbacks, then expand gradually. Do not
   describe local/SQLite evidence as Render, PostgreSQL or Twilio certification.

## Required environment

```text
WHATSAPP_INBOUND_DURABILITY_MODE=queue  # set independently on web and worker
WHATSAPP_INBOUND_QUEUE_TENANT_IDS=<positive canary tenant IDs>
WHATSAPP_INBOUND_HASH_SECRET=<dedicated secret, at least 32 bytes>
CHATBOC_PROCESS_ROLE=whatsapp-durable-worker  # worker only
WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED=true # permits only legacy zero-I/O standby
CHANNEL_SESSION_IDENTITY_MODE=enforce
CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1=<different dedicated secret, at least 32 bytes>
CHANNEL_SESSION_IDENTITY_VERSION_V1=v1
WHATSAPP_INBOUND_MAX_PAYLOAD_BYTES=65536
WHATSAPP_INBOUND_LEASE_SECONDS=180
WHATSAPP_INBOUND_MAX_ATTEMPTS=8
WHATSAPP_INBOUND_WORKER_BATCH_SIZE=8
WHATSAPP_INBOUND_WORKER_POLL_SECONDS=0.5
WHATSAPP_INBOUND_CELERY_WAKEUP_ENABLED=false
WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS=<active and former durable tenant IDs>
WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=false
WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS=72
WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE=200
WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=true
```

Celery is only an optional wakeup optimization. The database poller is
authoritative. Do not set `WHATSAPP_INBOUND_CELERY_WAKEUP_ENABLED=true` unless
Celery is initialized and a real broker plus worker are deployed.

## Payload retention job

The Blueprint schedules this command daily at 03:43 UTC (or invoke the
equivalent Celery task `whatsapp.scrub_expired_inbound_payloads`):

```text
python -m services.whatsapp_inbound_worker --scrub-expired-payloads
```

The scheduled default exits before importing the Flask application and performs
no database access. Actual retention requires both
`WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=true` and
`WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=false`, plus a non-empty, explicit
`WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS`. This list is intentionally separate
from the active queue canaries: keep former canaries in it after rollback until
their durable rows have reached the scrubbed tombstone lifecycle.

It emits only a versioned audit summary with aggregate counts; it never prints
turn IDs, SIDs or payload content. Re-run bounded batches until no rows are
selected. `WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS` accepts 0 to 720
hours; it is not an unlimited-retention switch.

For an exceptional preservation order, set
`WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=true`. The job then reports `legal_hold`
and performs no deletion. An invalid value also enables the hold fail-closed.
Record the external authorization and removal date in the compliance system;
do not use the application log or payload column as the legal-hold registry.
No schema migration is needed for this policy because the existing JSON column
stores the compact tombstone and the immutable digest remains authoritative.

## Alerts

Alert immediately on:

- any inbound `dead` row;
- any outbound `send_uncertain`, `failed` or `dead` row;
- a stale `processing` or `sending` lease;
- oldest due turn age above the service objective;
- a sustained increase in due backlog;
- digest conflicts, tenant/sender scope mismatches, or invalid signed callbacks.

`send_uncertain` is an operator decision point. First reconcile provider status;
never reset it to pending or resend it blindly. It intentionally blocks that
conversation stream until a signed callback or explicit audited operator action
resolves the ambiguity.

## Rollback

1. Pause inbound routing for the affected tenant. Do not remove it from the
   queue allowlist first: the worker uses that same scope and would stop
   draining its committed rows.
2. Set the public web service back to `legacy` while leaving the worker's
   independent mode at `queue`. This stops new durable ingress without shutting
   down the drain process. Keep routing paused to avoid interleaving new legacy
   turns with the older queued stream.
3. Keep the tenant allowlist in place until its inbound and outbound backlog is
   drained. Investigate every `send_uncertain` row before proceeding.
4. Remove the tenant from `WHATSAPP_INBOUND_QUEUE_TENANT_IDS`, put the worker in
   standby only after all canaries are drained, then resume routing on legacy.
5. Keep the tenant in `WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS` until all
   retained terminal payloads are scrubbed; rollback is not a retention opt-out.
6. Do not downgrade/drop the migration while any queue or outbox row remains.
