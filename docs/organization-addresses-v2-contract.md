# Organization addresses v2 (implementation contract)

This is additive to `organization.domain.v1`. There is no DDL or eager data
migration: v2 uses the private `TenantProfile.configuracion` key
`organization_address_bindings_v2`. Existing v1 records are retained, exposed as
`source: legacy_v1`, and keep their existing Pro/Full policy. A non-revoked legacy
record occupies the custom-domain slot until explicitly revoked.
New/replacement custom claims require a recorded Full plan through **both** APIs.
Existing Pro v1 records can still verify, activate, resolve and revoke; a revoked
legacy record does not authorize a new Pro custom claim. Client capabilities do
not grant this entitlement.
New platform-suffix requests must use v2's platform slot; v1 rejects those new
requests with `domain_platform_requires_v2`, while existing v1 platform records
retain their original verification/resolution/revocation behavior.

## Private owner API

`GET /api/admin/tenants/<slug>/addresses` requires the existing real authenticated
tenant administrator or authorized platform superadmin. It never writes.

Example for an active Full tenant with no bindings (values are synthetic):

```json
{
  "contract_version": "organization.addresses.v2",
  "tenant": {"id": 17, "slug": "institution-alpha"},
  "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "version": 0,
  "base": {"url": "https://chatboc.ar/t/institution-alpha", "available": true},
  "canonical_url": "https://chatboc.ar/t/institution-alpha",
  "platform": {
    "kind": "platform", "source": null, "host": null,
    "status": "unconfigured", "active": false, "valid_until": null,
    "dns_proof": null, "allowed_aliases": [],
    "scope_note": "Subdominio de la plataforma. No requiere un TXT del cliente; la publicación y HTTPS deben verificarse antes de activarlo."
  },
  "custom": {
    "kind": "custom", "source": null, "host": null,
    "status": "unconfigured", "active": false, "valid_until": null,
    "dns_proof": null, "allowed_aliases": [],
    "scope_note": "Dominio del cliente. Requiere un plan Full, prueba TXT y verificación de publicación y HTTPS."
  },
  "capabilities": {
    "can_manage": true,
    "writes_blocked": false,
    "platform": {"eligible": true, "can_request": true, "can_verify_dns": false, "can_revoke": false, "reason_code": "ready"},
    "custom": {"eligible": true, "can_request": true, "can_verify_dns": false, "can_revoke": false, "reason_code": "ready"},
    "activation": {"mode": "platform_operator", "available": false, "can_plan": false, "can_activate": false, "reason_code": "operator_runtime_unavailable", "provider_changes_supported": false}
  },
  "save_endpoint": "/api/admin/tenants/institution-alpha/addresses",
  "activation_plan_endpoint": "/api/admin/tenants/institution-alpha/addresses/activation-plan",
  "activation_endpoint": "/api/admin/tenants/institution-alpha/addresses/activate",
  "provider_changes_performed": false,
  "auth_e2e_verified": false
}
```

Slot `source` is `null`, `v2`, or `legacy_v1`. Slot `status` is `unconfigured`,
`pending_dns`, `pending_platform`, `active`, `revoked`, `verification_expired`, or
`suspended`. `active` is true only for an eligible, active tenant and current
provider/DNS evidence. `valid_until` is a Unix integer for active slots, otherwise
null. `dns_proof` is null or the existing `{type:"TXT",name,value,expires_at}`
shape; it is never present for platform bindings. `revision` covers v2 and v1
state, so a change through either API invalidates a prior v2 request.

`PUT` accepts exactly:

```json
{"expected_revision":"<64 hex>","kind":"platform","operation":"request","host":"northside.chatboc.ar"}
```

`kind` is `platform|custom`; `operation` is `request|verify_dns|revoke`. `host`
exists only for `request`. DNS verification applies only to `custom`. A successful
write returns `{contract_version:"organization.addresses_save.v2",saved:true,
addresses:<GET descriptor>,provider_changes_performed:false}`. Requests return
`pending_platform` for platform bindings and `pending_dns` for custom domains.
Request/revoke never alter DNS, Vercel, certificates, OAuth, plans, or credentials.

All JSON errors use `organization.addresses_error.v2`, a finite `reason_code`, and
`writes_performed:false` unless a commit acknowledgement is uncertain. In that
case `reason_code=address_save_unconfirmed` and `requires_status_check=true`;
there is no automatic replay. All routes return `private, no-store`.

## Activation API (platform superadmin only)

The operator supplies reviewed deployment/source/routes pins, never provider
observations or booleans claiming ownership. `POST .../activation-plan` accepts
`kind`, `expected_revision`, `frontend_deployment`, `frontend_source`, and
`routes_sha256`. The server derives the host, tenant, backend source, and expiry
from current authoritative state, then performs real bounded provider/TLS reads.
`POST .../activate` requires the same fields plus `approved_plan_sha256` and
repeats the observation before the atomic revision/authority check and SQL write.

These endpoints are available only with the configured server runtime credential
and opt-in activation flag; no credential is accepted in an HTTP body. Without
them capabilities explicitly report `operator_runtime_unavailable`. Provider
domain creation/verification is not an advertised HTTP capability. Existing
operator tooling and its durable intent journal retain ownership of those writes.

The activation readback pins the fixed existing frontend project, current
Production target, exact host alias, READY deployment, source SHA, reviewed route
digest, same-origin API proxy, verified public HTTPS, fresh nonce and exact backend
revision. A caller-supplied observation cannot activate a record.

## Public compatibility and concurrency

`GET /api/public/host-resolution?host=<exact host>` retains `public.tenant_host.v1`
and now checks the union of legacy and v2 records. New v2 responses may additionally
include `canonical_origin` and `canonical_redirect_required`; a verified active
custom domain takes precedence for a platform binding of the same tenant. No host
label or ambient session selects the tenant. Unknown, colliding, suspended, revoked
or expired records never resolve to another organization.

`GET /api/public/address-readiness` uses host, tenant_id, tenant_slug, revision,
kind and nonce and returns `public.tenant_address_readiness.v2`, infrastructure
only. It never activates, exposes private profile data, or certifies a login.

All v1/v2 binding mutations acquire the same PostgreSQL
`LOCK TABLE tenant_profile IN SHARE ROW EXCLUSIVE MODE` before fresh reads and
collision checks, then lock/revalidate the actor and target row. That lock is held
through audit+binding commit. This serializes claims across both classes and
APIs, rather than relying on two unprotected uniqueness queries. SQLite tests
exercise behavior; separate PostgreSQL tests must prove concurrent serialization.
DNS/TLS/Vercel I/O finishes before acquiring that lock. Writer lock timeout is
three seconds; SQL statement timeout is ten seconds. The operator write
transaction uses READ COMMITTED so a read before waiting cannot hide a winning
claim. After acquiring the lock, it rechecks the durable Clerk provider/session
lineage, actor version and platform role before the binding write, and checks
proof/session expiry and writer authority immediately before COMMIT.

Platform admission allows a single nonreserved `[a-z0-9-]{3,40}` label under
`chatboc.ar`, independent of the tenant slug. Vercel aliases require a closed
server-owned exact-host mapping to the tenant id and slug. A wildcard or a client
suffix grant is never accepted. New custom domains cannot use platform suffixes.
Plan downgrade suspends resolution without deleting stored bindings or data.

## Shared JSON protection

General settings writers replace the same JSON column. A central model guard
therefore preserves `organization_domain_binding` and
`organization_address_bindings_v2` from the **current PostgreSQL row inside the
UPDATE expression**. A stale settings save can neither remove a new claim nor
resurrect/reassign a revoked one. Other settings still save normally. The guard
adds no table lock or provider I/O to those routes.

Only the address service can authorize its own exact section/value for one
flush. V1 can change only v1; v2 can change only its own section, except an explicit
operation on a legacy record, which changes only v1. The permit is consumed and
cleared after flush, rollback, commit and context exit. SQL expression results
are expired/refetched so an in-memory descriptor reflects the actual stored JSON.
ORM/Core bulk replacement of this column is rejected, including legacy bulk ORM
methods, aliases and `ON CONFLICT DO UPDATE`; bulk changes to other columns continue
to work. Private bulk INSERT values and INSERT FROM SELECT of this column cannot
bypass the model. Inserts through the
ordinary model strip unsolicited private fields. This is an application ORM
boundary, not a database trigger: direct external SQL is outside its protection
and is not a supported activation path.

## Runtime prerequisites and real validity

The runtime defaults to disabled. The operator executor requires all of:

- A real, current Clerk session belonging to an allowlisted platform superadmin.
- `ORGANIZATION_ADDRESS_ACTIVATION_ENABLED=true` in the Production web runtime.
- A legitimate server-only `ORGANIZATION_ADDRESS_VERCEL_TOKEN`, plus
  `ORGANIZATION_ADDRESS_VERCEL_TEAM_ID=team_BV1xuY6BnEzGanfok8GAyjZv`.
- A full backend source SHA and an exact current Production frontend deployment,
  source SHA and routes digest. The client cannot override project, team, host,
  backend SHA, credential, tenant, actor, clock or provider observations.

Exact optional aliases use `ORGANIZATION_PLATFORM_ALIAS_BINDINGS_JSON`:
`{"institution-alias.vercel.app":{"tenant_id":17,"tenant_slug":"institution-alpha"}}`.
The default is `{}`. No environment variable or binding is installed by this
change, and publishing this code does not activate any domain. Missing runtime
configuration yields `operator_runtime_unavailable`, not a working self-service
activation promise. Existing provider changes remain an operator responsibility;
this HTTP executor performs only read-only provider verification plus the audited
database activation.

A platform request expires after **24 hours from request creation**, and the
initial activation is bounded by that same deadline. A custom TXT challenge lasts
24 hours; its verified DNS evidence lasts at most seven days and activation is
bounded by the verified evidence. Expired bindings stop resolving. There is
**no automatic renewal or scheduler** in this change. The current supported
re-preparation path is explicit revoke → request → applicable verification →
operator activation. Until a reviewed renewal workflow exists, the UI must show
the actual `valid_until` and must not describe an activation as permanent.

## Focused validation

The offline application suite exercises owner auth, real issued Clerk lineage,
revocation during observation, optimistic revisions, Full admission through both
APIs, v1 Pro compatibility, exact host authority, alias revocation, canonical
behavior, clock/lease checks and rollback of both state and audit.

`tests.run_organization_address_concurrency_postgres` creates its own disposable
PostgreSQL 18 cluster with TLS `verify-full`. It proves two physical concurrent
sessions have one winning claim and one conflict in either v1/v2 order, proves
the operator write transaction sees the winner after waiting, and confirms both
TXT callbacks happen before any tenant writer lock.

`tests.run_organization_address_stale_config_postgres` proves generic stale saves
cannot resurrect a revoke or erase a new request for either v1 or v2, including a
pause immediately before the actual UPDATE. It also checks unsolicited private
injection. Both runners stop and remove their synthetic clusters; they accept no
customer DSN and do not access external providers.

The existing `whatsapp-pack-transactions.yml` PR gate includes the v2 lifecycle,
real HTTP/Clerk executor, guard and observer suites, with their source/test paths.
Its full-app job already provides PostgreSQL 18. The small
`tests.run_organization_address_ci_postgres` runner reuses that fixed loopback CI
service and creates/removes only a fresh fixture database for the seven PostgreSQL
tests. It accepts no DSN and refuses to run outside the declared Linux GitHub
Actions environment. CI transport is explicitly loopback without TLS; that
evidence is distinct from the local `verify-full` runners, and requires the actual
PR job to pass.
