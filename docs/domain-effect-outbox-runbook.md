# Domain effect outbox: rollout and operations

Updated: 2026-07-30

This runbook covers effects staged with a domain mutation. The current local
producer scope is:

- new municipal and PyME tickets: tenant-admin/requester email plus the
  fail-closed SIGEM placeholder;
- municipal and PyME ticket comments: direction-aware email/SMS/WhatsApp and
  realtime intents, except a socket-origin comment whose caller deliberately
  owns the single realtime room emission;
- new PyME orders created through `PedidoService`: customer, tenant-owner and
  dispatch email/WhatsApp intents.

For canary tenants, `PedidoService` materializes `PymePedido`, `Order`,
`MarketOrder` and the external-effect intents in one transaction; the real
multimodal AI confirmation path now enters that service. The order rubro and
source channel are persisted on `PymePedido`, so delayed workers and CRM
projections do not depend on transient ORM attributes. Ticket status
notifications and direct model writes outside these services retain their
previous paths and must not be represented as migrated. The order WhatsApp
compatibility helper is log-only today, so queue workers report those effects
as `skipped` until a real transport explicitly opts in.

## Local guarantees

- The ticket/comment/order and one intent per applicable recipient/channel
  commit atomically. Canary order CRM projections commit in that same unit.
- Queue tenants never fall through to the corresponding direct notification
  sender. The admin socket route does not repeat email/SMS/WhatsApp after the
  ticket service has handled them.
- Every row is tenant-scoped, replay-bound by HMAC and contains only opaque
  recipient/owner references. Handlers reload citizen data after a tenant-owner
  check; the outbox does not store names, addresses, phones, email, free text,
  media or tracking PINs.
- Ticket-comment SMS and WhatsApp effects bind an explicit ready
  `ProviderSender` plus its tenant-owned Twilio connection, account, sender and
  callback URL. Missing or changed bindings fail before I/O; queue mode never
  falls back to a deployment-wide sender number.
- Claims use a lease token and compare-and-swap fencing. Provider I/O starts
  only after `io_started_at` is committed under a still-live lease.
- Proven pre-I/O failures retry with a bound. A provider exception, false/empty
  acknowledgement or crash after I/O becomes `unknown` and is not auto-retried.
- `succeeded` means the synchronous adapter returned provider acceptance (or an
  internal realtime emitter accepted the event). It does **not** prove inbox,
  handset or final-provider delivery; that requires a callback/reconciliation
  contract.
- Preflight and delivery adapters cannot commit incidental ORM writes through
  an outbox transition.
- SIGEM and order WhatsApp are currently disabled placeholders and are reported
  as `skipped`, never as successful integrations.

These are local SQLite/contracts, not production certification. The current
Alembic head is `20260730_survey_privacy_v1`. PostgreSQL, Render, SMTP and
provider E2E must pass before enabling any tenant.

## Processes

The database poller is authoritative:

```text
python -m services.domain_effect_worker
```

Useful probes:

```text
python -m services.domain_effect_worker --health
python -m services.domain_effect_worker --once
python -m services.domain_effect_worker --tenant-id <CANARY_TENANT_ID>
```

`render.yaml` defines `chatboc-domain-effects` as a background worker. It reads
the database, outbox policy and SMTP values from `chatboc-backend`. Render
service references update during Blueprint sync, so verify the resolved values
on both services before a canary. Celery is only an optional post-commit wakeup;
it is not the durability mechanism.

## Required environment

Deploy both services in fail-closed legacy mode first:

```text
DOMAIN_EFFECT_OUTBOX_MODE=legacy
DOMAIN_EFFECT_OUTBOX_SECRET=<dedicated random secret, at least 32 bytes>
DOMAIN_EFFECT_OUTBOX_TENANT_IDS=
DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES=4096
DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS=180
DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS=8
DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE=20
DOMAIN_EFFECT_OUTBOX_WORKER_POLL_SECONDS=0.5
DOMAIN_EFFECT_OUTBOX_CELERY_WAKEUP_ENABLED=false
EMAIL_NOTIFICATIONS_ENABLED=false
SIGEM_LIVE_ENABLED=false
```

Do not reuse Flask, JWT, OpenAI, Twilio or Flow secrets. Pending rows depend on
the HMAC secret. Rotation requires draining/reconciling the old-secret backlog
or a versioned dual-secret verification window; changing it in place dead-letters
old pending intents.

## Canary sequence

1. Deploy with `DOMAIN_EFFECT_OUTBOX_MODE=legacy` and apply migrations. Confirm
   exactly one Alembic head: `20260730_survey_privacy_v1`.
2. Start `chatboc-domain-effects` in legacy mode. It must idle cleanly and its
   health command must return only counts.
3. Configure the same dedicated outbox secret on web and worker. Select one
   non-production or controlled tenant whose `TenantProfile.tipo` and owner
   binding are correct.
4. Set the same explicit tenant ID list and `queue` mode on both services.
   Never use `*`, `all`, zero or an unbounded tenant scope.
5. Keep `EMAIL_NOTIFICATIONS_ENABLED=false` for the first persistence smoke.
   Create municipal/PyME tickets, direction-aware comments and PyME orders.
   Verify expected `skipped` rows, zero direct sends, zero PII in the outbox and
   no pending/stale backlog. For socket comments verify exactly one realtime
   room event and no channel bypass.
6. Validate SMTP configuration without sending. Then enable email only for the
   controlled environment and run one admin plus one requester delivery. Verify
   provider acceptance, template rendering, recipient tenant, queue metrics and
   no duplicate on replay. Do not call this final delivery until the matching
   callback/status evidence is stored.
7. Leave `SIGEM_LIVE_ENABLED=false`; the repository has no authenticated SIGEM
   transport. Do not override this flag until that adapter exists and has a
   real staging acknowledgement contract.
8. Exercise worker termination before I/O, after I/O and during provider
   timeout. Pre-I/O must retry; post-I/O must remain `unknown` with no resend.
9. Expand tenant-by-tenant only after alerting and an operator reconciliation
   procedure are active.

## Alerts and reconciliation

Alert on:

- any `unknown` row;
- any `dead` row;
- stale `processing` leases;
- oldest due age above the SLO;
- sustained `pending`/`retry_wait` growth;
- `intent_hmac_mismatch` or `ticket_tenant_binding_invalid`;
- worker heartbeat loss.

An `unknown` row is an operator decision point. Check the provider using its
audited delivery evidence. Never reset it to `pending` or resend blindly. The
current code deliberately has no automatic unknown-resolution endpoint.

## Rollback

1. Set the web service to `legacy` to stop staging new ticket, comment and order
   effects.
2. Keep the worker running to drain known `pending` and `retry_wait` rows.
3. Reconcile every `unknown` row before manual action.
4. Do not drop/downgrade the table while rows remain.
5. Remember that switching to legacy restores direct sends for affected new
   tickets/comments/orders; verify no queued copy exists for the same aggregate
   before any manual resend.
