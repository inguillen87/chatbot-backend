# TDF Meta test-number pilot

Contract: `chatboc.tdf_meta_sandbox.v1`. This increment adds a callable backend
route and runtime adapter; it does not connect or activate a provider. It is
disabled by default. Tests use disposable SQLite and synthetic credentials,
with external networks blocked. No live message delivery has been verified.

## Exact scope

| Binding | Required value |
| --- | --- |
| Tenant | `46`, active, slug `tierra-del-fuego` |
| Provider / channel / environment | `meta` / `whatsapp` / `sandbox` |
| Chatboc Meta app | `1719510329487224` |
| Test WABA | `2192778428137676` |
| Test phone ID | `1137550632773388` |
| Test display number | `+1 555 656 5679` |
| Graph version | `v25.0` observed on 2026-10-10 |

The pilot refuses other resources. In particular, JUNÍ phone ID
`660753250460901`, the shared production WABA, Inmovar and NexID are outside
this route. It does not alias `/webhook/whatsapp` or Twilio workers.

## Backend settings

Set these only after review and authorization of the specific sandbox binding.
The application accepts them from backend environment settings; no values are
installed by this source change. Do not put secrets in the frontend.

| Setting | Type / requirement |
| --- | --- |
| `META_TDF_SANDBOX_ENABLED` | Default absent / false; literal `true` enables |
| `META_TDF_SANDBOX_APP_ID` | Exact app ID above |
| `META_TDF_SANDBOX_WABA_ID` | Exact test WABA above |
| `META_TDF_SANDBOX_PHONE_NUMBER_ID` | Exact test phone ID above |
| `META_TDF_SANDBOX_GRAPH_VERSION` | `v25.0` |
| `META_TDF_SANDBOX_APP_SECRET` | App signing secret, backend only |
| `META_TDF_SANDBOX_VERIFY_TOKEN` | Random callback challenge token, backend only |
| `META_TDF_SANDBOX_RECIPIENTS_JSON` | JSON array of 1–5 approved test recipient IDs, digits only, no `+` |
| `TENANT_PROVIDER_CREDENTIAL_KEYRING` | Existing private keyring JSON, base64 32-byte keys |
| `TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID` | Existing active key ID in that keyring |

Reuse the existing keyring without replacing keys used by other providers.
The access token is **not** a global environment setting: it belongs to the
encrypted tenant connection below. The recipient list is an additional local
restriction; it does not approve recipients with Meta.

## Binding preparation in the existing database (instructions only)

Use the authorized backend application context and existing production database.
Do not create another database or change the tenant owner. First read tenant 46
and require its active status, exact slug, existing `municipio_id` owner and
published public `TenantConfig` knowledge bundle. Review the current rows for
the exact connection tuple and every sender with this phone ID. An ambiguous
or foreign match is a stop condition; do not overwrite it.

Within one authorized transaction, create or reconcile only the following
existing model rows. A token must come through the approved secure mechanism,
never source code, shell command arguments, logs, email or chat.

`ProviderConnection` (`provider_connection` table):

| Field | Value |
| --- | --- |
| `tenant_id` | `46` |
| `provider`, `channel`, `environment` | `meta`, `whatsapp`, `sandbox` |
| `external_app_id` | `1719510329487224` |
| `external_account_id` | `2192778428137676` |
| `credentials_ref` | `vault:meta:whatsapp_access_token:v1` |
| `display_name` | `Conversa TDF · prueba Meta` |
| `status` | Initially `needs_setup`; `connected` only after authority checks |
| `config` | Preserve unrelated reviewed keys; store only the envelope below |

The unique connection tuple is tenant/provider/channel/environment. Flush its
new row to obtain `connection.id`, then call
`services.meta_whatsapp_credentials.seal_token(connection=connection,
tenant_id=46, access_token=secure_token, revision=next_revision,
expires_at=verified_unix_expiry, now=current_unix_time,
app_config=current_app.config)`. Store the returned ciphertext object under
`config['_chatboc_provider_credentials_v1']` by assigning a new config dictionary.
For a first envelope, revision is 1; replacements increment the existing
reviewed revision. Even tokens reporting no provider expiry require a finite,
explicit local expiry. The envelope binds tenant, connection ID, app, WABA,
environment, revision and expiry. It must not be copied to another connection.
`seal_token` itself does not persist, authorize, activate or verify the token.

`ProviderSender` (`provider_sender` table):

| Field | Value |
| --- | --- |
| `tenant_id`, `provider_connection_id` | `46`, exact connection row ID |
| `channel`, `sender_type` | `whatsapp`, `whatsapp_business` |
| `phone_number`, `phone_number_id` | `+15556565679`, `1137550632773388` |
| `waba_id` | `2192778428137676` |
| `display_name` | `Conversa TDF · prueba Meta` (local label) |
| `status` | Initially `draft`; `ONLINE` only after authority checks |
| `webhook_url`, `status_callback_url` | `https://api.chatboc.ar/webhook/meta/tdf-sandbox` |

Local status and labels do not prove provider configuration or display-name
approval. Persist the authorized audit separately without secrets. Commit the
reviewed binding before enabling the route. No schema change is required.

## Callback and actual provider checks

GET/POST `/webhook/meta/tdf-sandbox` is registered in `app.py`. Default disabled
returns 404. All responses are `private, no-store`.

GET accepts exactly `hub.mode`, `hub.verify_token`, `hub.challenge`, one value
each. POST accepts bounded JSON bytes (1 MiB), no query parameters, and verifies
`X-Hub-Signature-256` against the untouched body before parsing or Graph calls.
One batch has at most ten events, all scoped to the exact resources and local
approved recipient list before writes.

The runtime checks the token app/scopes/expiry through `debug_token`, exact
phone membership through `/{waba}/phone_numbers` and subscribed app through
`/{waba}/subscribed_apps`. Authority is fresh for at most 60 seconds and never
outlives the credential. It re-reads committed binding/revocation before send.
Immediately before POST, canonical content also requires a fresh public
knowledge state with the exact revision used to generate the answer. Retirement
or replacement leaves the intent quarantined and cannot be retried by replay.
The Graph transport uses fixed HTTPS host, default TLS validation, bounded
body/timeouts, no redirects, proxy inheritance, automatic retries or URL debug
logging. See the [official Meta Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api).
These readback shapes and permissions remain to be validated against the real
test token; offline fakes do not prove live app subscription.

## Behavior and receipts

Text, numeric menu replies and `knowledge:` list/button selections use the
existing published public institutional knowledge responder. Free text keeps
its existing LLM routing; exact menu navigation needs no LLM. Locations are
range-checked, used only to acknowledge and ask the city, and not stored. Audio
voice notes (Ogg/Opus only in this pilot) use an authenticated media-ID lookup
with the exact test phone ID, then the exact Meta HTTPS attachment host and
path. MIME, declared and streamed size, container prefix, metadata SHA-256 and
the webhook checksum when present are checked before one OpenAI transcription.
The recording cap is 2 MiB. Admission and streamed chunks share a 35-second
budget, and results returned after that budget are discarded. HTTPX timeouts
bound individual I/O operations, so the SDK does not guarantee absolute
wall-clock cancellation at 35 seconds. No recording duration is measured. The private STT
adapter pins the official OpenAI endpoint with the existing configured key and
model, disables environment proxies, redirects, retries and provider fallback,
and bounds successful responses. A transport response hook closes errors,
encoded bodies and excessive declared response lengths before the SDK can read
them. No recording or transcript is stored in files, shared
cache, receipts or context. The transcript only selects published TDF knowledge
with unchanged canonical menu codes; that revision must survive transcription
and the existing final send guard. Credential revision, window, pilot flag and
recipient allowance are checked again before each media stage. Unsupported or
failed audio offers shorter WhatsApp voice notes, text/menu codes and human
help without claiming a successful transcription. See the official
[OpenAI transcription reference](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
and [Meta media lookup](https://www.postman.com/meta/whatsapp-business-platform/request/fpj02x0/retrieve-media-url).
No outbound voice-note upload, WhatsApp Calling, case, appointment or
health-document mutation is implemented in this increment.

Menus with at most ten canonical choices and a body within Meta's 1024-character
list limit use a native list (`Elegir un tema`) plus the complete numbered text
in the same message. Row IDs keep the exact `knowledge:revision:target` action.
Titles are at most 24 characters; long labels also get a bounded description.
Unsupported sizes remain text instead of hiding options or switching to a
pending template. The transport rebuilds the list allowlist and rejects extra
keys, duplicate row IDs, control characters or excess limits before any POST.

`WebhookDelivery` uses the existing unique provider/event key with provider
namespace `meta_tdf_sandbox_v1`. HMAC-pseudonymous contact and semantic digest
avoid storing raw inbound text, coordinates or recipient in these pilot rows.
`MessagingEventLedger` commits an outbound `send_uncertain` intent before the
single POST. A timeout/crash/ambiguous outcome is quarantined and never retried
automatically. A replay cannot send twice. A reused event ID with different
semantic content is rejected. Signed status events require matching sender,
tenant, provider message ID and recipient pseudonym; delivered/read cannot be
downgraded. HTTP 200 is a receipt acknowledgement; `accepted` is provider
acceptance, not delivery. Response `delivery_verified` stays false.

This is a small test-number pilot, with synchronous processing and no contact
FIFO or automatic recovery of interrupted receipts. Outside-window template
sends and the existing draft-only TDF template-pack guards remain unchanged.
The separate Meta-created sandbox templates are not dispatched by this route.

## Verification and rollback

Run the two new focal suites plus institutional menu contracts in a dedicated
process using `tests.profile_acceptance_runtime.prepare_process()`. This clears
developer environment, disables dotenv/schema bootstrap and blocks external
network. The fixtures exercise the actual DB binding, knowledge resolver,
HTTP callback, authority adapter, durable receipts, signature, tenant isolation,
revocation, recipient restriction, time window and single-POST semantics.

Enable only after a reviewed release, secure token binding, actual app callback
and `messages` subscription, Meta-approved recipient and consent for a test
message. A real acceptance run must capture inbound message ID, exact test
phone/tenant, outbound Meta message ID, signed delivered/read status and the
recipient's observed response. Do not certify voice/calling from text success.

Immediate stop: set `META_TDF_SANDBOX_ENABLED=false`. Disabling after an answer
but before POST is checked again. Do not reset receipt rows or resend uncertain
messages to force success. Preserve the encrypted connection for review or
revoke it through the authorized credential procedure. No JUNÍ or Twilio
configuration is affected by this pilot flag.
