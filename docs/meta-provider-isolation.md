# Meta identity and durable queue isolation

The opt-in Meta policy permits a tenant canary while the global WhatsApp policy
remains legacy and the existing Twilio identity policy remains shadow. It does
not rotate or reinterpret the global identity, stream, or Flask keys.

## Explicit configuration

| Variable | Contract |
| --- | --- |
| `META_WHATSAPP_PROVIDER_ISOLATION_ENABLED` | Boolean opt-in; defaults to false. |
| `META_WHATSAPP_QUEUE_TENANT_IDS` | Nonempty comma-separated positive tenant IDs; scope ingress and worker claims. |
| `META_WHATSAPP_INBOUND_HASH_SECRET` | Dedicated Meta stream HMAC material, at least 32 UTF-8 bytes. |
| `META_CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1` | Dedicated Meta identity HMAC material, at least 32 UTF-8 bytes. |
| `META_CHANNEL_SESSION_IDENTITY_VERSION` | Explicit namespace matching `meta-[a-z0-9_.:-]{1,27}`, for example `meta-v1`. |

With isolation enabled, these values form an independent configuration view
with queue durability and enforced identity. The global config object remains
unchanged. Missing Meta values fail closed; global keys never fill them in.

With isolation disabled, Meta keeps the previous global queue/enforce contract.
This permits deployment of the code before a deliberate canary configuration.
The callback, inbound worker and outbound flags remain separate controls.

## Identity continuity

HMAC input remains separated by tenant, provider, and identity version. Isolated
Meta creates a new context instead of adopting any legacy context, including one
without a binding. Existing Twilio history and bindings remain untouched. If an
existing isolated Meta binding shares a context with a different provider, its
resolution fails closed; inactive foreign bindings count as history too.

Do not replace `SECRET_KEY`, `WHATSAPP_INBOUND_HASH_SECRET`, or
`CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1` as part of this canary. In shadow mode,
the existing Twilio derivation can depend on the Flask key or the global stream
key; introducing a global key can change that derivation without changing a row.

## Worker scope and rollout

The poller takes the union of eligible provider tenant sets, then intersects each
claim with that provider's own scope. Meta-only tenants cannot wake up pending
Twilio rows or quarantine their expired leases. The outbound claim filter applies
to both candidate/CAS selection and expired-send quarantine. With isolation off,
the previous unsupported-provider quarantine contract is retained.

The Meta inbound worker still requires its enabled flag and a valid DomainEffect
canary for the tenant. Outbound still requires its own flag and all existing
credential, sender, recipient, lease, writer-authority and network checks.
Isolated outbox envelopes also store `canary_scope_digest`, derived from the
callback allowlist for their exact app/WABA/phone/tenant. Staging, dispatch policy
and the final local read before POST revalidate that scope and callback flag.
Changing that sender's allowed contacts invalidates existing staged sends;
changing an unrelated sender does not. An envelope with this proof remains
restricted even if isolation is subsequently disabled. No recipient-prefix
transformation or additional outbound destination is authorized by this proof.
The worker and reconciliation entry point operate in queue mode for eligible
Meta while preserving the global Twilio legacy setting and process identity.

Deploy the verified code first. Provision dedicated Meta material and an explicit
tenant allowlist, then activate the existing Meta callback/worker/outbound gates
according to the release plan. This change does not install credentials, subscribe
an app or sender, modify provider accounts, or enable any production flag itself.

## Verification

`test_meta_provider_policy.py` checks mandatory configuration, absence of fallback,
unchanged Twilio HMAC derivation and production validation. The real SQLite flow in
`test_meta_provider_isolation.py` covers signed admission, processing and ordered
outbox dispatch with synthetic provider adapters, exact replay, preserved Twilio
v1 binding/history, separate contexts, per-provider canaries and untouched pending
or expired Twilio rows. These offline tests do not prove production provider
subscription, real delivery, or PostgreSQL concurrency.
