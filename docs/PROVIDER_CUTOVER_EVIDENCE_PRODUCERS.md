# Provider cutover evidence producers

This package closes the gap between an operator-authored JSON document and
evidence actually observed by Twilio and the exact Vercel runtime. It does not
change Twilio, Vercel, DNS, Render, Neon, or application records.

## Trust split

Three independent HMAC keys are required:

1. `CUTOVER_PROVIDER_SNAPSHOT_HMAC_SECRET` signs the GET-only Twilio snapshot.
2. `CUTOVER_RUNTIME_ATTESTATION_HMAC_SECRET` signs the exact Vercel runtime
   attestation.
3. `CUTOVER_CREDENTIAL_BINDING_HMAC_SECRET` produces the same opaque binding
   from the Twilio account SID and credential in both environments.

The two signing keys, the binding key, the endpoint bearer, and the provider
credential must have different environment-variable names and different
values. The promotion verifier rejects a reused signing value even when it is
hidden behind two different environment-variable names. Raw key and provider
credential values never appear in an evidence document or failure response.

## Twilio collector

`scripts/collect_twilio_provider_snapshot.py` exposes only four allowlisted GET
operations:

- list and fetch `/v2/Channels/Senders`;
- list `/v1/Services`;
- list each service's `/ChannelSenders` association.

It requires exactly one `+17432643718` sender, canonical `ONLINE` state, one
Messaging Service association, and the exact inbound and status callback URLs.
Redirects and incomplete pagination fail closed. The module has no provider
POST/PATCH/PUT/DELETE or message-send capability.

The collector must be invoked with a fresh evidence ID, nonce, cutover-window
ID, target project/deployment/revision, target database identity fingerprint,
and the names of the credential and signing environment variables. Output is a
single signed canonical JSON envelope. Store the output as the promotion
snapshot input; do not copy it into source control.

## Vercel runtime attestor

The authenticated endpoint is:

`POST /api/internal/cutover/runtime-credential-attestation`

It reuses the constant-time bearer pattern used by internal cron routes through
`CUTOVER_RUNTIME_ATTESTATION_BEARER_SECRET`. Before signing, it requires:

- `VERCEL=1` and `VERCEL_ENV=production`;
- exact platform-provided `VERCEL_PROJECT_ID`, `VERCEL_DEPLOYMENT_ID`, and
  `VERCEL_URL`;
- exact `CHATBOC_DEPLOYMENT_REVISION` and, when present, matching
  `VERCEL_GIT_COMMIT_SHA`;
- exact TLS PostgreSQL database fingerprint calculated in runtime;
- exact tenant account SID and a tenant-scoped credential environment variable;
- fresh nonce, cutover-window ID, and independent evidence ID.

The endpoint performs no Twilio or database request. It returns a signed
document containing account/scope metadata and the opaque credential binding,
never the provider credential. A local process, Preview deployment, different
Production deployment, different revision, or different database fails closed.

## Promotion verifier

`scripts/promote_tenant_provider_connection.py` now rejects the former weak
document shape. It cross-checks both signed envelopes against the operator's
explicit project, deployment, URL, revision, database identity, nonce, window,
callbacks, account, credential reference, and shared opaque credential
binding. Dry-run remains the default. An apply still needs the exact approved
plan digest and the existing PostgreSQL transaction/advisory-lock gates.

This implementation alone is not authorization to deploy, call the collector,
promote the connection, switch provider callbacks, or stop Render.

