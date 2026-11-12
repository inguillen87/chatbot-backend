# Cloudflare AI Gateway for OpenAI Responses

## Scope

This is an opt-in transport for the existing OpenAI Responses adapter. Direct
OpenAI remains the default and retains its existing configuration. The gateway
path uses Cloudflare's current OpenAI-compatible REST endpoint:

```text
https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/responses
```

The older `gateway.ai.cloudflare.com/.../compat/chat/completions` URL is not
used. This integration does not migrate Realtime, speech, transcription, image
generation, or other OpenAI clients in the repository.

## Configuration

Keep the feature disabled until the Cloudflare account has Unified Billing
credits, a spend limit, and a dedicated API token with the Cloudflare account
permission `Workers AI - Read`. The `/accounts/{id}/ai/*` inference endpoints
use that permission; a token with only `AI Gateway` permissions returns 401.

```dotenv
CLOUDFLARE_AI_GATEWAY_ENABLED=false
CLOUDFLARE_AI_GATEWAY_ACCOUNT_ID=
CLOUDFLARE_AI_GATEWAY_API_TOKEN=
CLOUDFLARE_AI_GATEWAY_ID=default
```

When enabled, all three identity/credential values are mandatory. Invalid or
missing values stop the request before provider I/O; the adapter never silently
falls back to the direct OpenAI credential. The Cloudflare token is supplied to
the OpenAI SDK as its API key so the SDK emits the normal `Authorization:
Bearer ...` header to Cloudflare. It must remain server-only.

The opt-in transport is pinned to `openai/gpt-5.6-sol`, as required for this
specific billing route and promotion. Direct OpenAI keeps the existing
channel-specific model selection; enabling Cloudflare deliberately overrides
that selection only for this Responses transport. GPT-5.6 reasoning options
remain attached after prefixing.

The adapter sends these gateway headers on every request:

```text
cf-aig-gateway-id: {configured gateway id}
cf-aig-skip-cache: true
cf-aig-collect-log-payload: false
```

Caching is bypassed because Chatboc handles user- and tenant-specific content.
Cloudflare may retain operational metadata such as model, token counts, cost,
status, and duration, but the per-request payload header prevents AI Gateway
from storing raw prompts and responses. The Responses request also keeps the
existing `store=false` provider setting.

## Controlled rollout

1. Create a dedicated non-production gateway and API token.
2. Load only a bounded credit amount and configure Cloudflare spend/rate limits.
3. Add the four variables to Preview only, keeping the flag `false` first.
4. Set the flag to `true`, redeploy Preview, and run a single non-sensitive
   Responses smoke. Confirm model, token accounting, latency, failure behavior,
   and that gateway logs contain metadata but no request/response payload.
5. Compare quality and total effective cost with direct OpenAI before expanding
   traffic. A successful deployment alone is not provider verification.

Do not copy a dashboard URL or account identifier into source control. Never
print the API token in deployment output, health payloads, or logs.

## Rollback

Set `CLOUDFLARE_AI_GATEWAY_ENABLED=false` (or remove the flag) and redeploy.
The next lazy client initialization uses the unchanged direct OpenAI path and
`OPENAI_API_KEY`. Remove the three Cloudflare values after the rollback smoke,
then revoke the dedicated Cloudflare token if the experiment is closed.

Rollback is pre-request and deterministic: there is no automatic replay or
cross-provider fallback after a request may have reached either provider.
