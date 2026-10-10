# Full-host white-label binding

This increment uses the existing TenantProfile.configuracion record, under the
private organization_domain_binding key. It creates no database, table, migration,
provider domain, DNS record, certificate, subscription, or plan charge. The branch
has Vercel Git deployment disabled; local tests do not prove a live customer host.

## HTTP contracts

GET /api/public/host-resolution?host=conversa.gob.ar has exactly one host query.
It ignores session, slug, first-label inference and stored tenant choices. The
public.tenant_host.v1 response contains exact tenant identity, safe logo/palette,
HTTPS origin and same-host paths /, /login, /perfil only for a unique, active,
verified, unexpired binding on an active Pro or Full-family tenant. Pending,
unknown, revoked, malformed or colliding bindings return indistinguishable 404
host_not_available; invalid host/query returns 400 host_invalid. Database failure
returns 503 without identity. All responses are Cache-Control: no-store.

GET/PUT /api/admin/tenants/:slug/domain require a real authenticated owner/admin
of that exact tenant (or authorized platform superadmin). Reads do not write.
PUT requires expected_revision and operation request with host, verify_dns, or
revoke. Request creates a random expiring TXT proof. verify_dns reads only the
fixed public TXT name, with a three-second DNS lifetime and bounded records, then
enters pending_platform. Owner HTTP commands cannot set active/verified/plan or
provide provider observations. A plan downgrade removes public resolution but
still permits an authorized administrator to revoke an old binding.

Domain preparation has an explicit Pro/Full entitlement separate from existing
Full-only branding, integrations and module entitlements. Prices, WhatsApp,
payments and other plan capabilities are unchanged.

## Bounded Vercel provisioner and pending-host readiness

activate_verified_binding is an internal service, not registered as an HTTP route.
It requires a fresh PlatformObservation (at most 60 seconds old), exact normalized
host and expected existing frontend project ID, provider ownership, HTTPS readiness
and verified same-host app routes. DNS evidence and provider evidence expire in at
most seven days. The operator adapter in services/organization_domain_vercel.py
obtains this observation from fresh provider reads and the actual custom-host
HTTPS response; owner HTTP writes never accept a supplied observation.

The fixed existing frontend project is prj_CEIKYsPgxlSEOBEKjizziSJBsGKf. An immutable
DomainPlan pins the full host, exact tenant ID/slug, current binding revision, DNS
expiry, production deployment ID, frontend/backend commit SHAs and a reviewed
canonical deployment-routes SHA-256. No new Vercel app, deploy, wildcard domain,
domain transfer, redirect, deletion, force move, environment or OAuth grant exists
in this adapter. The operator must obtain credentials through the existing secure
process; credentials are never printed, put in the plan, or persisted in a journal.

owner_scope_guard re-reads the application's owner/admin authorization, current
active Pro/Full tenant, exact pending binding revision, collision state and DNS
expiry before add/verify. VercelTransport performs only bounded verified-TLS calls
against api.vercel.com. Add uses POST /v10/projects/:id/domains with exactly name;
verify uses POST /v9/projects/:id/domains/:host/verify. Each explicit operation
requires a matching durable create-only local IntentJournal, fsynced before its
single POST. A timeout, wrong readback or receipt-write failure cannot cause an
automatic retry. reconcile performs GET reads for that exact plan/intent only.
These operations report assigned/ownership_verified with active=false and
auth_e2e_verified=false. The journal must live in an operator's durable directory,
not in a Vercel function's transient /tmp.

The readiness endpoint breaks the bootstrap cycle while the normal public host
contract still returns 404 for pending domains. GET /api/public/host-readiness
accepts exactly host, tenant_id, tenant_slug, revision and a fresh 64-hex nonce.
It requires the exact pending_platform row, unexpired DNS evidence, no collision,
fresh matching public TXT and a known backend commit. Its no-store
public.tenant_host_readiness.v1 response echoes only the supplied exact identity,
revision/nonce and backend source with scope=pending_domain_infrastructure_only,
active=false, auth_e2e_verified=false and private_assets_included=false. It contains
no profile, logo, menu, credentials or private assets and does not write to SQL.

observe checks the exact verified project-domain, production READY deployment
(including withGitRepoInfo=true), source and reviewed routes, then the exact alias.
It rejects redirect, branch/custom-environment and microfrontend/deleted aliases.
The reviewed routes must contain the same-origin /api/(.*) proxy to
https://api.chatboc.ar/api/$1. The custom HTTPS host resolves only public IPs; one
checked IP is pinned for the connection while hostname TLS/SNI remains verified.
No redirect is followed. The nonce response must match ID, slug, revision, host,
backend source, public-only flags and a 60-second observation budget. The adapter
re-reads the domain/deployment/alias before producing PlatformObservation.
activate_from_vercel then calls the existing atomic SQL activation, which reloads
the owner, entitlement, revision and collision state under the original locks.
Revocation during provider observation prevents activation.

Before a customer host can genuinely become active, the existing Vercel project
needs its domain binding, DNS ownership/routing and HTTPS certificate confirmed.
The frontend must bootstrap the authoritative full-host contract before any stored
tenant and preserve the exact host for root, navigation, login and CRM. Same-origin
API proxying and OAuth authorized origins/callbacks must be validated for that host;
host-only cookies and current credentials must stay isolated. A trusted operational
adapter must read those facts and call activation with their exact evidence, and
renew or revoke before expiry. No wildcard CORS/OAuth grant is introduced here.
The adapter proves infrastructure and the exact pending backend route, not a real
authenticated browser session, CRM operation or human acceptance. Deploying the
matching frontend/backend code, obtaining customer DNS/provider ownership and
validating a normal same-host login/CRM session remain release and acceptance work.

The old TenantProfile.dominio setter/resolver remains legacy code; it alone never
authorizes this new public contract. Other legacy routes must eventually adopt the
verified binding before a complete custom-host deployment is certified. Exact-host
collisions (including legacy fields and their legacy www/apex aliases on another
tenant) fail closed; these aliases never select a public tenant. New domain
mutations serialize claims using the existing tenant_profile table lock on
PostgreSQL; SQLite tests cover transactions but do not certify PostgreSQL concurrency.

Existing generic TenantConfig and branding/profile APIs cannot write the new private
record. Public config sanitization hides TXT challenges and all internal evidence.
There is one binding per tenant in this increment; replacing an active domain first
requires explicit revocation. Add a registry/unique index in a separately reviewed
migration if multi-domain scale or cross-service claim writers require it.

## Offline verification

Use the existing scoped environment and run from the backend worktree:

```powershell
& 'C:/Temp/chatboc-scoped-testenv-20260930/Scripts/python.exe' -X utf8 -c "from tests.profile_acceptance_runtime import prepare_process; prepare_process(); import pytest; raise SystemExit(pytest.main(['-q','tests/test_organization_domain_binding.py','tests/test_organization_domain_vercel.py','tests/test_plan_access.py','tests/test_public_tenant_config_security.py','tests/test_organization_branding.py']))"
```

The disposable runtime disables dotenv/customer databases, blocks external sockets,
uses actual Flask routes, real password login, current permission middleware and SQL
transactions. DNS and platform observations are bounded offline fixtures; these
tests cannot certify real DNS, TLS, login on a custom host, or a live customer.

Primary API contracts checked for this implementation:

- [Project-domain readback](https://vercel.com/docs/rest-api/projects/get-a-project-domain)
- [Add a project domain](https://vercel.com/docs/rest-api/projects/add-a-domain-to-a-project)
- [Verify project ownership](https://vercel.com/docs/rest-api/projects/verify-project-domain)
- [Exact alias readback](https://vercel.com/docs/rest-api/aliases/get-an-alias)
- [Deployment/source readback](https://vercel.com/docs/rest-api/deployments/get-a-deployment-by-id-or-url)

If a provider response omits the pinned source/routes/alias evidence, the adapter
stops without activation. Operator inspection must establish the actual schema and
review a compatible change; a guessed truth value cannot replace missing evidence.
