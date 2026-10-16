# Tenant provider credentials

The internal credential store uses the existing `ProviderConnection.config` JSONB
and the reserved `_chatboc_provider_credentials_v1` namespace. There is no new
database, public credential-upload endpoint, provisioning caller or provider call.

`TENANT_PROVIDER_CREDENTIAL_KEYRING` is a server-only JSON keyring containing
standard-base64 AES-256 keys. `TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID` selects
the active key. Configuration is absent by default. Never use an application
session key or a provider token as the encryption key, or expose this keyring in
client variables, logs, API responses or Git.

Each encrypted token is bound to the tenant and connection IDs, account SID,
provider, channel, environment, revision and key ID. Retain old keys while their
envelopes remain in use. Missing keys, changed bindings or damaged envelopes stop
resolution; a vault reference never falls back to a platform or environment token.
Managed cutover connections require a separate reviewed migration and are excluded.

`store_tenant_twilio_token` locks and refreshes the row without flushing a stale
cached identity, verifies the expected revision, updates the JSON with CAS and
adds a secret-free `AuditEvent` in the caller's transaction. The caller authorizes
the actor, obtains writer authority and commits or rolls back both changes. Token
installation does not activate a sender, change account access or send a message.
Synchronizations preserve the envelope and reference; public status redacts them
and normalizes sensitive key aliases recursively.

Local keyring readiness proves configuration only. Creation of subaccounts and
Messaging Services remains blocked until durable provisioning and reconciliation
are implemented. Legacy onboarding consumers stop before provider I/O for a vault
tenant until they join that transaction. Existing environment-based connections
retain their current operation. The active workflow contains no Render secret-sync
step; historical data and rollback resources are preserved.

The CI suite distinguishes SQLite persistence checks, disposable PostgreSQL
concurrency checks, isolated UI regressions and actual provider acceptance.
WhatsApp acceptance still requires the customer's real authorization, approved
sender/template, a persisted message SID, signed delivery evidence and inbound
deduplication. Test fixtures and a successful dry run do not satisfy that gate.
