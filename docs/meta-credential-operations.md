# Administrative Meta credential operations

Contract: `chatboc.meta_credential_operation.v1`.

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
