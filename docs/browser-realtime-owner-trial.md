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
  `browser_realtime_voice: {enabled: true, max_sessions_per_hour: 1}` (integer
  cap 1–3); this change does not apply that setting anywhere.
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
- The provider creation is not retried. Normal hangup commits a stop intent
  before its one attempt; unknown closure does not retry or claim success.
  If receipt storage fails after a provider acknowledgment, one compensating
  hangup is attempted using the known ID. During database failure that cleanup
  cannot promise a durable stop intent. A failed receipt write leaves the
  earlier durable admission unresolved, requiring operator reconciliation.
- Only published institutional corpus is sent, with its exact revision checked
  before issuance and after provider I/O. No operational write tools, tracing
  or nominal user data are added to the session. Session defaults cap assistant
  output at 512 tokens per response.

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

## Official API contracts checked on 2026-10-10

- [WebRTC and server SDP exchange](https://developers.openai.com/api/docs/guides/voice-webrtc)
- [Create call schema, output limits, tools and tracing](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/create)
- [Server controls and Location call ID](https://developers.openai.com/api/docs/guides/voice-server-controls)
- [WebRTC hangup](https://developers.openai.com/api/docs/guides/voice-sip?voice-api=realtime)
- [Realtime transcript events](https://developers.openai.com/api/docs/guides/realtime-conversations)

GPT-Live has a separate API and event model. No generated talking video or
outbound SIP service is implied by this Realtime browser transport.
