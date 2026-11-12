# Native-thread runtime release runbook

## Scope

This release removes Eventlet and all global monkey-patching from the web
runtime. Gunicorn runs one `gthread` worker with a configurable thread pool,
Flask-SocketIO is pinned to `threading`, and the realtime voice bridge uses a
daemon listener thread plus a cancellable native timer.

There is no database migration in this change. It must not be used as evidence
that Twilio/OpenAI voice or inter-process Redis delivery works in Production;
those require provider-backed gates below.

## Runtime contract

- `SOCKETIO_ASYNC_MODE` is unset or exactly `threading`. Any green-thread value
  fails startup.
- `GUNICORN_WORKERS` must be unset or exactly `1`; any other value fails
  startup. Scale horizontally with one worker per instance and load-balancer
  sticky sessions. A shared Redis manager coordinates broadcasts but does not
  replace client affinity.
- On Vercel, `GUNICORN_THREADS` defaults to `32`. Explicit integer overrides are
  preserved within the bounded `16..64` range; lower or higher values are
  clamped to the nearest limit. Other runtimes retain the established default
  of `100` and their explicit overrides. This is bounded per-instance
  concurrency, not unlimited scale.
- `simple-websocket==1.1.0` is an explicit runtime dependency.
- Eventlet must not appear in the resolved deployment dependency set.

## Pre-deploy gates

1. Install from `requirements.txt` in a clean Python 3.12 environment and
   confirm dependency resolution succeeds without Eventlet.
2. Run the Socket.IO, room-scope, voice stream, voice consent, and survey
   inter-process suites.
3. The hash-locked `native-thread-runtime.yml` CI gate must boot the Production
   `gunicorn.conf.py` worker contract against its minimal Socket.IO smoke app
   and complete a real WebSocket upgrade on Ubuntu 24.04 and Python 3.12.
   Separately boot the exact `app:app` command from `render.yaml` in staging and
   verify `/health`, Flask-Sock `/twilio/voice/stream`, and the full application
   import. The focal CI smoke is necessary evidence, not a substitute for this
   integration gate.
4. On an isolated Preview/staging service with the same environment contract:
   verify Engine.IO polling, WebSocket upgrade, reconnect, and tenant-room
   isolation. Confirm the shared Redis manager is healthy before testing an
   event emitted by a separate worker process.
5. Run a signed Twilio Media Stream against the intended provider sandbox.
   Confirm OpenAI audio in both directions, barge-in, normal close, maximum-call
   timer, and listener cleanup. Do not use mocked provider results for this gate.
6. Soak for at least 30 minutes while observing worker restarts, active threads,
   memory, WebSocket upgrade failures, request latency, and 5xx rates.

## Controlled Production rollout

1. Record the currently served commit and deployment ID before changing state.
2. Deploy the reviewed commit during an announced window. The attached Render
   disk makes the service restart user-visible; do not describe this as zero
   downtime.
3. Repeat HTTP health, Engine.IO polling/upgrade, authenticated tenant-room, and
   provider voice smokes against the served Production commit.
4. Keep the prior deployment available for immediate rollback until the soak is
   complete.

## Rollback

Rollback to the recorded pre-release deployment if the worker restarts, the
WebSocket upgrade rate regresses, tenant-scoped delivery fails, or voice threads
do not terminate. No schema rollback is required. After rollback, repeat health
and WebSocket checks and retain the failed deployment logs for diagnosis.
