# Administrative Meta credential operations

Contract: `chatboc.meta_credential_operation.v1`.

## Production administrative screen

The supported execution path for the current Vercel deployment is an
authenticated administrative adapter inside the backend itself. It uses the
existing `db.engine`, private runtime configuration and global writer lease.
It does not export protected Vercel secrets, impersonate a Vercel runtime on a
workstation, use a Sandbox as the Production process, or execute arbitrary code.
The CLI below remains useful only where an operator already has a legitimate
private runtime; having its source does not provide that execution access.

`META_CREDENTIAL_ADMIN_ENABLED` is strictly opt-in and defaults to false. The
registered routes return 404 while disabled. When enabled they require the real
Vercel Production web runtime, an authenticated allowlisted platform superadmin
with an active Clerk-backed Chatboc session, and the existing shared Redis rate
limiter. An in-memory limiter in Production fails closed.

Authentication accepts either an explicit `Authorization: Bearer` Chatboc JWT
or the existing HttpOnly `AUTH_TOKEN_COOKIE_NAME` cookie (default `auth_token`).
An invalid explicit Authorization header never falls back to cookies. Cookie
requests additionally require an exact Origin of `https://chatboc.ar` or
`https://www.chatboc.ar`; absent/null/lookalike origins, trailing dots, extra
ports or whitespace fail closed. All requests use bounded JSON POSTs. Flask
login, widget credentials, query-string tokens and JSON authentication tokens
cannot authorize these routes. The actor is derived from the verified session,
never supplied by the client. Provider-session and lineage revocation, actor
version, account status and the allowlist are rechecked and share-locked in the
apply transaction.

### HTTP contract

| Path | JSON input | Result |
| --- | --- | --- |
| `/api/internal/meta-credentials/status` | `target` only | Fixed target IDs and phone; current/expected/next revision; current writer epoch; runtime prerequisites; current expiry; sanitized last operation receipt |
| `/api/internal/meta-credentials/plan` | `target`, `access_token`, `expected_revision`, `expected_writer_epoch`; optional `expires_at` | Read-only plan, state/token-bound `plan_sha256`, database identity hash, verified local expiry |
| `/api/internal/meta-credentials/apply` | Plan input plus exact `expires_at` from plan, `approved_plan_sha256`, `expected_database_identity_sha256` | Metadata and `committed: true` only after the actual database commit succeeds |

Only `junin-production` and `tdf-sandbox` are valid targets. Extra or duplicate
JSON keys are rejected, including actor, tenant, provider, runtime and DSN
overrides. Request bodies are limited to 16 KiB. Status has a per-actor limit of
30 requests/minute; plan and apply share 12/minute and 60/hour. The screen reads
revision, epoch and resource metadata from status; the operator only supplies
the new credential in a hidden field and reviews the resulting plan.

Status includes `operation_available` and a finite `reason_code` when the exact
expected state, private keyring, app-secret configuration or writer authority
is unavailable. It does not open an old token or call Meta. A completed operation
has `operation_available: false` under this one-time contract; the receipt and
current revision remain readable.

The screen must retain the token only in component memory while the operator
reviews that target. Do not put it in a URL, browser storage, analytics, error
telemetry, snapshots or a chat. Submit it only in the JSON POST body. Clear it
after success, target changes or leaving the screen. Apply resends the same
credential and uses the exact nonsecret expiry returned by plan; the backend
repeats provider GET validation and recomputes the approved digest.

### Expiry policy

An expiry input is not required in the screen. The adapter stores the earliest
of the finite Meta token/data-access expirations and a local limit of 30 days.
If Meta reports no finite expiry, the stored expiry is that conservative local
limit; it is not presented as Meta's actual token expiration. An optional
operator limit may shorten the period but cannot exceed 30 days. Plan returns:

```json
{
  "expiry": {
    "expires_at": 1797115200,
    "provider_expires_at": null,
    "source": "local_policy",
    "local_maximum_seconds": 2592000
  }
}
```

When a finite provider limit determines the result, `source` is `provider`.
The installed vault envelope uses this local not-after value. A later renewal
needs a separately reviewed expected-revision operation; this bounded installer
deliberately does not silently extend or repeatedly rotate existing credentials.

### Privacy, transactions and uncertain results

The early HTTP boundary runs before application auth and tenant discovery.
These exact endpoints skip the ordinary token-extraction and body-hint helpers;
credential JSON is parsed only after successful authentication and is not cached
on the request. No token is stored in `g`, the session, a plan cache or audit.
Every response is private/no-store. Logging records in this narrow request
scope have message, arguments, exception and stack details suppressed, including
SQL/HTTP debug records; other concurrent requests keep normal logging.

Planning uses a separate read-only database transaction. Applying uses a
separate serializable transaction, exact state/revision checks, the existing
global phone uniqueness lock, encrypted envelope installation and metadata-only
audit. It reuses the HTTP writer middleware's request-lifetime lease, whose
shared control-row lock survives the database commit. It never commits the
authentication session's autobegun transaction.

If apply returns `database_outcome: "unverified"`, do not retry automatically.
Read status and compare `current_revision` and
`last_operation.plan_sha256` with the reviewed plan. The commit can succeed
while its acknowledgement is lost. A same-plan receipt at the expected next
revision confirms the completed database operation; ambiguous or unrelated
state requires an operator review. This is not proof of WhatsApp delivery.

The routes never send a message, update Meta, change environment variables,
change the historical Twilio mapping, activate cron or widen a canary. Those
are independent reviewed rollout steps. Once both operations and their
readbacks are complete, disable the administrative flag again.

Offline HTTP tests use real JWT/session verification, synthetic SQLite
persistence and an injected GET-only provider. They cover commit/rollback and
lost-response semantics. Python immutable strings cannot promise cryptographic
memory zeroization.

The additional opt-in runner `tests/run_meta_credential_admin_postgres.py`
exercises the original HTTP adapter, JSONB CAS, session locks and global writer
lease on a new dedicated PostgreSQL 18 cluster with TLS. It accepts only the
existing disposable-fixture contract: loopback, database
`chatboc_auth_lifecycle_ci`, user `postgres`, no other application databases or
roles, and an empty public schema before setup. It never reads `DATABASE_URL`.
Use `--fresh-loopback-fixture --port PORT` with the existing synthetic
`CHATBOC_AUTH_LIFECYCLE_TEST_PASSWORD` input, or the guarded GitHub fixture mode.
The cluster must already exist and support TLS; the runner does not install or
start PostgreSQL. Do not point it at a reused/customer database.

Four cases passed locally on PostgreSQL 18.6 with real separate connections:

1. Concurrent TDF rotations wait on a physically observed PostgreSQL table lock;
   exactly one JSONB/CAS writer commits revision 7 and one audit.
2. A previously revoked Clerk session cannot apply its earlier plan.
3. Revocation from another connection waits until an admitted credential
   transaction commits.
4. The global authority transition waits through that commit and the HTTP
   request lease; it then advances the epoch and fences the writer.

The provider and hosting-identity check are explicit synthetic test adapters;
auth/session verification, the authority engine and SQL locks are originals.
These tests establish local PostgreSQL concurrency behavior, not readiness of
the actual Production keyring, provider token or deployed configuration. The
existing `auth-private-storage.yml` isolated PostgreSQL job uses the same
fixture identity; it needs a fresh TLS-enabled service to add this runner.

## Private-runtime CLI

`scripts/manage_meta_whatsapp_credentials.py` is an internal operator CLI. It
does not add a public credential-upload endpoint, obtain a token, update Meta,
change environment settings, activate workers/cron, or send a message. Default
execution performs bounded provider GETs and a read-only database inspection.
The operator supplies a newly authorized token through hidden terminal input or
an explicitly protected stdin pipe. Tokens are never command arguments or
environment variables. Do not place a token in shell history, source, a PR, a
chat message, an intermediate file, or a diagnostic log.

## Exact authorized operations

| Target | Scope | Permitted database effects |
| --- | --- | --- |
| `junin-production` | tenant 22 / `junin`; app 1719510329487224; WABA 10068207913300410; phone ID 660753250460901; +17432643718 | Create one new Meta/WhatsApp/Production connection and one sender, revision 1, plus audit |
| `tdf-sandbox` | tenant 46 / `tierra-del-fuego`; connection 3; sender 1; app 1719510329487224; WABA 2192778428137676; phone ID 1137550632773388 | Replace only the credential envelope at expected revision 6 with revision 7, plus audit |

These are deliberately fixed operations. An existing Junín Meta production
connection, a different TDF revision, a shared connection, an ambiguous sender,
a foreign owner, or a managed-migration marker stops the command. It does not
guess, upsert another organization, reset a sender, or retry a conflict.

Junín's existing Twilio connection, `WhatsappNumero` mapping, tenant sender
pointer, and conversation history remain unchanged. The new Meta sender stores
the actual E.164 number. The Twilio resolver explicitly excludes Meta
connections; missing or conflicting provider bindings fail closed instead of
falling back across providers. Legacy `WhatsappNumero` routing still works when
no Twilio `ProviderSender` exists.

## Required private runtime configuration

Use the existing authorized backend environment, without loading dotenv files.
The CLI creates no application routes and runs no schema or tenant bootstrap.

- Existing `TENANT_PROVIDER_CREDENTIAL_KEYRING` and
  `TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID`. Never replace shared keys for this
  operation. The new access token is AES-GCM encrypted in the tenant connection,
  not stored in an environment variable.
- Junín: existing parsed `META_WHATSAPP_APP_SECRETS` registry (provided by
  `META_WHATSAPP_APP_SECRETS_JSON`) for the exact app, and Graph version v25.0.
- TDF: its existing `META_TDF_SANDBOX_APP_ID`, `META_TDF_SANDBOX_APP_SECRET` and
  `META_TDF_SANDBOX_GRAPH_VERSION`. No fallback to a different provider secret.
- A PostgreSQL TLS DSN supplied by the existing `MIGRATIONS_DATABASE_URL`, or
  another explicitly named backend variable. The DSN is never returned. Apply
  requires its reviewed identity fingerprint from the plan. Query parameters
  that redirect host/database/service or inject connection options are rejected;
  the explicit port (5432 when absent) and current database are checked. Inherited
  PGHOSTADDR/PGSERVICE/PGSERVICEFILE/PGOPTIONS are blocked. The SQL search path is
  fixed to public for each transaction.
- Apply requires the existing unfenced runtime and enabled global writer
  authority. The authority lease must permit the configured runtime and match
  the explicit expected epoch through the application transaction's commit.
  The command never changes the authority row, ownership, flags, or epoch.
  Its bounded apply uses the existing lease mode with no idle TTL, preserving
  the shared control-row lock through the actual database commit.
- `--actor-user-id` must identify an existing allowlisted platform superadmin
  whose account is not disabled. Operating-system access to the private runtime
  and the operator's approval remain the administrative authentication boundary.

## Provider validation before writing

The existing `MetaOutboundRuntime.authority` validates token app, scopes,
provider and data-access expiry, granular WABA scope when present, exact phone
membership, and exact app subscription. The wrapper permits only GET requests
to `debug_token`, the pinned WABA's `phone_numbers`, and its `subscribed_apps`.
Membership also checks `display_phone_number` against the pinned E.164 number.
Incomplete pagination, unexpected response shapes and redirects fail closed.

This proves the token/resource association and readbacks, not an actual inbound
delivery, phone registration status, callback override URL, or outbound result.
Keep the independently verified Meta UI sender status and callback evidence.
The command does not claim that the app's general callback has been activated.

The operator supplies a finite, verified `--expires-at` Unix UTC value. It must
be future and no later than any finite expiry returned by Meta. Even a provider
expiry of zero needs an explicit local expiry. Renewing an expired TDF token
does not open or decrypt that expired token.

## Plan and apply

Run the same arguments in both phases. Replace the uppercase placeholders with
reviewed nonsecret metadata. Leave the token out of the command:

```bash
python scripts/manage_meta_whatsapp_credentials.py \
  --target junin-production \
  --actor-user-id ADMIN_ID \
  --expected-revision 0 \
  --expires-at VERIFIED_UNIX_EXPIRY \
  --expected-writer-epoch CURRENT_EPOCH
```

For TDF, use `--target tdf-sandbox --expected-revision 6`.

The hidden terminal prompt fails if secure terminal input is unavailable; it
does not fall back to echoed input. A secure automation may use `--token-stdin`
and provide exactly one token through an already protected noninteractive pipe.
The CLI rejects regular-file stdin; only a pipe/socket descriptor is accepted.
The CLI does not create that pipe or export a credential. Extra lines, NULs,
whitespace, oversized values and unknown secret-bearing options are rejected
without echoing the supplied input.

Review the returned target, database identity, actor, old/new revision,
expiry, intended changes and `plan_sha256`. The digest binds the exact credential
and current database snapshot without disclosing either. Apply adds:

```text
--apply
--approved-plan-sha256 REVIEWED_PLAN_SHA256
--expected-database-identity-sha256 REVIEWED_DATABASE_SHA256
```

Apply repeats the provider readbacks and inventory. Approval does not authorize
a different token, expiry, actor, database, revision or changed configuration.
The fresh provider proof expires after at most 60 seconds. There is no automatic
retry after conflict, provider failure, serialization failure or commit error.
An apply error reports `database_outcome: unverified`: a lost connection during
commit can conceal a committed change. Read revision/binding/audit metadata
before any further operation; a failure response is not proof of rollback.

The apply transaction is PostgreSQL SERIALIZABLE, with bounded statement/lock
timeouts. A brief `SHARE ROW EXCLUSIVE` lock on provider connection/sender tables
protects the final global phone uniqueness check because the schema lacks a
global phone-ID uniqueness constraint. Provider requests and token prompts
happen before this lock and before holding global writer authority. Reads
continue; competing provider writes wait or time out. Existing target rows are
refreshed and the old configuration participates in the credential CAS update.

The encrypted envelope and a secret-free `AuditEvent` commit together. TDF's
other configuration keys, statuses, IDs, sender and callback fields remain
unchanged. Junín creation sets only its new connection/sender fields. A local
round-trip validates the newly stored envelope inside the transaction without
printing its plaintext or ciphertext. Python cannot guarantee zeroization of
immutable strings; terminate the administrative process when it completes.

## Post-operation acceptance

1. Read back only scope, revision and expiry. Check that TDF's other fields and
   the existing Twilio mapping/history are unchanged.
2. `/api/v2/tenants/tierra-del-fuego/activation/channels` can report that local
   credential expiry cleared; it still does not certify production delivery.
3. Root/operator separately prepares and reviews the approved Meta callback,
   canary recipient, inbound/outbound worker flags and cron configuration. This
   installer neither enables nor disables them.
4. Use one new message from Marcelo's already approved canary phone. Correlate
   inbound message ID, exact tenant/phone, outbound Meta ID, signed delivery
   status and the observed reply. Do not replay uncertain historic receipts.

## Offline validation scope

`tests/test_meta_credential_operations.py` uses disposable SQLite, synthetic
keys/tokens and fake GET responses with external networks blocked. It tests
read-only planning, actual encrypted persistence, old-revision CAS, stale plans,
rollback of credential and audit, tenant/provider isolation, authority and
expiry checks, and secret-free CLI input/output. PostgreSQL locking and real
Graph/phone delivery require separate authorized verification.

The Twilio scoping tests cover coexistence with Meta, continued legacy routing,
existing scoped Twilio senders, ambiguity, and orphan/foreign connections.
