# Survey response effect worker runbook

Updated: 2026-07-30

## Scope

`SurveyResponseEffect` is a dedicated database outbox for analytics, rewards
and live-result events created by a committed survey response. It is not the
generic `DomainEffectOutbox` used by tickets, comments and orders. The two
tables, executors and workers must remain separate.

The authoritative production process is:

```text
python -m services.survey_response_effect_worker
```

The Render Blueprint service is `chatboc-survey-effects`. A Celery task can
trigger the same bounded sweep, but Celery is only an optional wakeup; recovery
does not depend on a broker message surviving.

## Runtime contract

Each cycle discovers tenants that have effects eligible under the canonical
executor's due filter. It rotates the starting tenant and divides a global
batch among the selected tenants. A continuously busy tenant therefore cannot
consume every cycle.

Automatic claims are limited to:

- `pending` and due `retry_wait` rows below `max_attempts`;
- expired `processing` leases, recovered with the same attempt number.

`succeeded`, `skipped` and `dead` are terminal and are never selected. The
current survey-effect schema has no `unknown` state. Do not create an automatic
replay path if one is introduced later: ambiguous external outcomes require an
operator reconciliation decision. The existing realtime effect is explicitly
at-least-once and carries a stable `event_id` so clients can deduplicate an
expired-lease replay.

`realtime.v2` has an additional process boundary. The web service subscribes
to a shared Socket.IO Redis channel, while `chatboc-survey-effects` initializes
the same channel as a write-only publisher. Immediately before an effect emits,
the worker verifies the Redis/Redis-TLS URL, channel, manager mode and a bounded
Redis `PING`. Its worker-specific manager also raises if `python-socketio`
exhausts the actual Redis publish instead of allowing the library's default
log-and-return behavior. Without that evidence the row enters `retry_wait` (or
eventually `dead`); it is never recorded as successfully emitted.

A successful row means Redis accepted publication on the verified shared
transport. It does not prove that a browser was connected, rendered the event,
or acknowledged it. Clients must deduplicate the stable `event_id`.

## Configuration

| Variable | Default | Allowed | Meaning |
| --- | ---: | ---: | --- |
| `SURVEY_RESPONSE_EFFECT_LEASE_SECONDS` | 120 | 30-3600 | CAS lease duration |
| `SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE` | 50 | 1-500 | Maximum effects claimed per cycle |
| `SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE` | 50 | 1-500 | Maximum fair tenant window |
| `SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS` | 0.5 | 0.05-60 | Idle/failure polling interval |
| `SOCKETIO_MESSAGE_QUEUE_URL` | empty | `redis://` or `rediss://` | Shared Socket.IO pub/sub; mandatory on the survey worker |
| `SOCKETIO_MESSAGE_QUEUE_CHANNEL` | `chatboc-realtime-v1` | safe 1-128 char token | Exact channel shared by web and worker |
| `SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS` | 2 | 0.1-10 | Connect/read bound for per-effect Redis verification |

Production startup validation fails when these settings are malformed or out
of range. It also refuses to start the survey worker without an explicit shared
queue URL. Worker startup queries the outbox so a missing migration fails loudly
rather than leaving a healthy-looking idle process. The URL is a secret: keep it
only in Render/environment configuration and never paste it into logs or tickets.

## Operator commands

Run one bounded cycle locally or in a one-off shell:

```powershell
python -m services.survey_response_effect_worker --once
```

Read payload-free aggregate health:

```powershell
python -m services.survey_response_effect_worker --health
```

The health document reports totals by status, the number of tenants with due
work and the dead-letter count. It never renders answer payloads or respondent
identity.

## Deployment sequence

1. Apply migrations and verify `survey_response_effect_outbox` exists.
2. Configure `SOCKETIO_MESSAGE_QUEUE_URL` once on `chatboc-backend`. The
   Blueprint passes that exact secret plus the same channel to
   `chatboc-survey-effects`; do not maintain two manually copied URLs.
3. Deploy the web service and `chatboc-survey-effects` from the same revision.
4. Run `--health`; confirm the process can read the production database.
5. Submit one controlled survey response while a browser has joined its
   tenant-scoped survey room. Verify Redis transport evidence, the effect's
   `publish_accepted` result and exactly one rendered update for its `event_id`.
6. Stop/restart the worker while a test effect is in due `retry_wait`; confirm
   exactly one terminal transition and no duplicate analytics/reward record.
7. Alert on sustained `pending`/`retry_wait`, expired `processing`, any `dead`,
   transport failures, or repeated `cycle_failures` in worker logs.

Local tests prove code and SQLite restart behavior only. They do not certify
the Render worker, PostgreSQL leases, cross-process realtime delivery or any
external provider until the controlled production smoke above succeeds.

## Incident handling

- Database unavailable: leave the worker running; every cycle rolls back and
  retries after the bounded poll interval. The DB rows remain authoritative.
- `retry_wait`: inspect the sanitized `last_error`; let normal backoff recover
  transient failures.
- `survey_realtime_shared_transport_*`: compare the web and worker service
  revisions and channel, then verify the Redis secret/network. Never replace it
  with an in-memory queue or mark the row succeeded manually.
- `dead`: do not change it to `pending` blindly. Fix or reconcile the root cause
  and use an audited operator procedure.
- Repeated expired leases: compare handler latency with the configured lease
  and inspect worker shutdown/health before raising the lease bound.
- Rollback: stop `chatboc-survey-effects`. This stops dispatch without deleting
  queued rows; restore the process after the incident is corrected.
