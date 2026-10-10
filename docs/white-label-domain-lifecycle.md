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

## Activation remains a trusted provisioner step

activate_verified_binding is an internal service, not registered as an HTTP route.
It requires a fresh PlatformObservation (at most 60 seconds old), exact normalized
host and expected existing frontend project ID, provider ownership, HTTPS readiness
and verified same-host app routes. DNS evidence and provider evidence expire in at
most seven days. No current route invents or obtains that observation.

Before a customer host can genuinely become active, the existing Vercel project
needs its domain binding, DNS ownership/routing and HTTPS certificate confirmed.
The frontend must bootstrap the authoritative full-host contract before any stored
tenant and preserve the exact host for root, navigation, login and CRM. Same-origin
API proxying and OAuth authorized origins/callbacks must be validated for that host;
host-only cookies and current credentials must stay isolated. A trusted operational
adapter must read those facts and call activation with their exact evidence, and
renew or revoke before expiry. No wildcard CORS/OAuth grant is introduced here.

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
& 'C:/Temp/chatboc-scoped-testenv-20260930/Scripts/python.exe' -X utf8 -c "from tests.profile_acceptance_runtime import prepare_process; prepare_process(); import pytest; raise SystemExit(pytest.main(['-q','tests/test_organization_domain_binding.py','tests/test_plan_access.py','tests/test_public_tenant_config_security.py','tests/test_organization_branding.py']))"
```

The disposable runtime disables dotenv/customer databases, blocks external sockets,
uses actual Flask routes, real password login, current permission middleware and SQL
transactions. DNS and platform observations are bounded offline fixtures; these
tests cannot certify real DNS, TLS, login on a custom host, or a live customer.
