# Accessible browser voice owner trial

This is source implementation and offline validation, not enabled production
voice, physical microphone QA, generated video, or telephone acceptance.

The existing authenticated session issues a server-side SDP exchange against
OpenAI Realtime `POST /v1/realtime/calls`. The API key stays on the backend.
`Location` supplies the private provider call ID; the client receives only an
internal trial ID and SDP answer. A server hangup uses that private call ID.
There is no public client-secret path in this feature and no change to the
older realtime routes, provider environments, databases or WhatsApp senders.

The UI offers explicit microphone consent, start/stop, actual track mute, full
captions, text fallback and an illustrative robot with reduced-motion support.
It announces connection only after both WebRTC and its data channel connect.
It stops late-arriving microphone permissions and closes a late issued call.
It never requests camera permission, sends tool calls or stores transcripts.

## Admission and scope

- Disabled by default. Existing tenant configuration must explicitly contain
  `browser_realtime_voice` with `enabled: true`, integer
  `max_sessions_per_hour: 1`, integer `max_total_sessions: 1` (both caps 1–3),
  and `trial_expires_at`, an explicitly approved fixed UTC Unix timestamp in
  seconds. Missing fields, booleans used as integers, or invalid bounds fail
  closed. Old enabled configurations receive no grace period. This source
  change supplies no deadline and applies no setting to an existing tenant.
- Existing OpenAI configuration and model resolver are reused. No new key,
  provider account, environment variable, app, database or migration is added.
  Key resolution honors an explicit Flask setting, otherwise the existing
  server-only `OPENAI_API_KEY` environment variable. Offline tests deny provider
  network before constructing a connection unless the existing opt-in is set.
- Only the tenant owner or allowlisted platform superadmin with a current
  authenticated session can issue a trial. Widget, demo, employee and foreign
  organization identities are denied. An internal ID cannot close another
  actor's session.
- Tenant admission takes `FOR UPDATE OF tenant_profile` with fresh identity-map
  state. The existing append-only `AuditEvent` table durably records admission
  before provider creation. At most one unresolved call exists per tenant;
  unknown requests block creation even after the hourly admission window.
- Every reservation for this tenant and contract consumes the total trial
  allowance, including closed, failed and unknown attempts. It never resets
  on a new actor, corpus revision, expiry setting or hourly window. Admission
  checks the fixed deadline immediately before the durable intent. An
  acknowledged creation that crosses the deadline is closed once without
  delivering SDP; Stop remains available after trial expiry or exhaustion.
  Capabilities reports the fixed deadline and total reservations/remaining
  allowance as an advisory snapshot, with a fixed expired/exhausted message.
  The authenticated POST rechecks admission; a stale browser capability can
  still request microphone consent before that rejection.
- The provider creation is not retried. Normal hangup commits a stop intent
  before its one attempt; unknown closure does not retry or claim success.
  A validated provider call ID survives an invalid, oversized, compressed or
  interrupted SDP answer. The backend records that private acceptance and a
  durable stop intent, then closes once without returning SDP or call ID.
  If receipt storage fails after a provider acknowledgment, one compensating
  hangup is attempted using the known ID. During database failure that cleanup
  cannot promise a durable stop intent. A failed receipt write leaves the
  earlier durable admission unresolved, requiring operator reconciliation.
- Only published institutional corpus is sent, with its exact revision checked
  before issuance and after provider I/O. No operational write tools, tracing
  or nominal user data are added to the session. Session defaults cap assistant
  output at 512 tokens per response.

## Published instruction budget

The exact configured `gpt-realtime-2.1` model has a documented 128,000-token
context. Its complete UTF-8 instructions (policy, revision and every public
node's ID, title, text and menu options) may occupy at most 65,536 bytes. The
budget uses one UTF-8 byte per text token as a conservative byte-BPE upper
bound, not the usual average bytes/token estimate or an exact tokenizer count.
It reserves another 32,768 tokens for conversation, 2,048 for session framing
and 512 for response output: 100,864 is below the documented context ceiling.
The independent context check also fails closed if that budget no longer fits.

Models without this exact verified contract keep the smaller 32,768-byte
complete-instruction ceiling. Neither path truncates facts or automatically
changes model. Source file paths, private source URLs and other source metadata
remain outside the voice projection. Invalid UTF-8 fails with a fixed error.
These are local admission bounds, not a provider spend limit, access check,
quality evaluation or proof that the provider accepts a real voice session.

## Explicit limits before public rollout

The 120-second client timer is an accessibility/UX limit, **not a hard provider
billing cap**. Serverless issuance does not schedule a hard timed hangup. A
trusted owner can control the native data channel, so the initial model/corpus
instructions and response defaults are not immutable client-proof policy.
Grounding is model guidance; there is no before-playback verification of every
spoken claim and no continuous corpus-revocation watcher during a live call.
These are owner trials, not a publicly certified institutional voice agent.

No live microphone or paid API was exercised in this delivery. Before enabling,
verify the real authenticated owner flow, existing key/model availability, DB
SELECT/INSERT/sequence permissions, PostgreSQL concurrent admission, browser
mic/privacy controls, actual speech/captions and server closure. Confirm no
recording or transcript exposure through infrastructure logs. Public rollout
requires durable timed cancellation/spend controls and speech-grounding review.

Full-app HTTP acceptance uses a fresh disposable SQLite app, real password login,
real session middleware and SQL ledger; only published corpus/provider I/O are
offline fixtures. Run separately from tests that import the application first:

```powershell
& 'C:/Temp/chatboc-scoped-testenv-20260930/Scripts/python.exe' -X utf8 -c "from tests.profile_acceptance_runtime import prepare_process; prepare_process(); import pytest; raise SystemExit(pytest.main(['-q','tests/test_browser_realtime_http_acceptance.py']))"
```

The separate `tests/test_browser_realtime_postgres.py` contract is mandatory in
the workflow step **Verify voice trial admission with actual PostgreSQL
contenders**. It uses the existing disposable loopback `vaultcredregression`
service, refuses a nonempty fixture or any alternate/ambient DSN, creates only
one random schema, and drops it afterwards. Two actual sessions must exhibit a
PostgreSQL `pg_stat_activity.wait_event_type='Lock'` wait on the same tenant.
The tests prove one remaining total slot cannot be admitted twice, subsequent
actors/revisions/hourly windows do not reset it, expiry while waiting for the
tenant lock denies admission, and Stop remains idempotent after expiry/cap.
Only hangup acknowledgments are offline functions; no provider is contacted.
The named JUnit artifact distinguishes these checks from the general PostgreSQL
transaction job. Without the explicit opt-in, these three live-SQL cases skip;
local guard checks passing alone are not concurrent PostgreSQL acceptance.

## Official API contracts checked on 2026-10-10

- [WebRTC and server SDP exchange](https://developers.openai.com/api/docs/guides/voice-webrtc)
- [Create call schema, output limits, tools and tracing](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/create)
- [Exact GPT-Realtime-2.1 context window](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)
- [OpenAI byte-BPE tokenizer properties](https://github.com/openai/tiktoken/blob/main/README.md#what-is-bpe-anyway)
- [Server controls and Location call ID](https://developers.openai.com/api/docs/guides/voice-server-controls)
- [WebRTC hangup](https://developers.openai.com/api/docs/guides/voice-sip?voice-api=realtime)
- [Realtime transcript events](https://developers.openai.com/api/docs/guides/realtime-conversations)

GPT-Live has a separate API and event model. No generated talking video or
outbound SIP service is implied by this Realtime browser transport.
