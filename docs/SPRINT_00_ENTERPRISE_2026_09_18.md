# Sprint 00 - Public keys and scoped payment notifications

Date: 2026-09-18. Baseline: `efdc0d8bb443b1d2b13f0fd746aa05d8028fbf43`.
Status: implemented and component-tested; production validation remains separate.

## Implemented
- Public JWKS publishes only allowlisted public RSA/EC fields; no symmetric key, secret fallback, or private PEM publication.
- Rejects weak RSA keys and wrong EC curves. All four existing HTTP aliases remain available, with no-store responses.
- Existing server-side HS256 signing/refresh remains compatible; asymmetric key provisioning is a separate rollout.
- Payment/order websocket events are opaque collection invalidations, routed to the persisted tenant operator room.
- Missing/invalid tenant scope cannot become a broadcast. Notifications contain no customer, payment status, or order identifiers.
- Existing regression expectations updated. Added path-scoped CI with minimal dependency installation and cancellation of superseded runs.

## Evidence and limitations
Command: `python -m unittest tests.test_enterprise_security_sprint0 -v`.
Result: **16 tests passed** on Python 3.11.9, Flask 3.1.1, Flask-SocketIO 5.5.1, PyJWT 2.10.1, cryptography 50.0.0, python-socketio 5.16.4, python-engineio 4.13.2.
Tests cover real RSA/EC verification, production handlers in an isolated Flask app, legacy signing/refresh, invalidation payloads and a real Socket.IO test transport with two tenant operators and two buyers.
HTTP handlers are AST-loaded to avoid unrelated application dependencies. Socket rooms are pre-authorized fixtures: these checks do NOT certify the full application factory, database, Redis, or live authentication.
The older application-level tests were updated but were not executed in this focal environment. GitHub CI and production status must be checked independently.

## Rollout gates
- Verify deployed revision and four JWKS aliases without retaining secret material; evaluate prior cache exposure and rotate affected keys separately when required.
- Confirm operator subscriptions and authorized HTTP refetch in a real tenant. Buyer views must never join operator rooms.
- This change does not prove all existing sockets or tenant query paths are isolated.
- Coordinate with frontend branch `sprint/verified-checkout-20260918` for verified, bounded checkout refresh.

## Next delivery
Sprint 01: durable payment effects/outbox, idempotent concurrent callbacks, reconciliation, and recovery tests against PostgreSQL/Redis before production sign-off. Keep key rotation and provider account verification explicitly tracked; do not label this first cut complete enterprise certification.
