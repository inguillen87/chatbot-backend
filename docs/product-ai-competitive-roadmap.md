# Chatboc AI Product Roadmap

Updated: 2026-08-02

## Product north star

Chatboc is a vertical conversational operating system, not a generic FAQ bot.
The same governed agent must turn text, voice, images, documents, emoji and
location into a municipal claim, school case, order, appointment, survey,
vote, human handoff or status query, with one auditable CRM timeline.

Local tests prove contracts, not production readiness. A capability is called
live only after its provider and deployment smoke tests pass.

## Current evidence

- Backend and frontend tenant-aware CRM surfaces exist for tickets, assignment,
  SLA, timeline, attachments, agent assistance, surveys and live results.
- WhatsApp has signed webhook handling, bounded media downloads, durable inbound
  turn/outbox contracts, Flow completion replay and domain idempotency for
  claims, comments and orders.
- Signed WhatsApp forms now pass through a provider-bound content contract for
  text, emoji/reaction, image, audio (including parameterized Ogg/Opus),
  document, video, sticker, location, vCard, interactive/Flow and control
  events. Status/call callbacks stop before conversation state; failed media or
  STT produces one honest retry/human prompt and cannot degrade into an empty
  LLM turn or speculative ticket. This is local evidence only; real device,
  Meta/Twilio media and OpenAI STT/vision E2E remain pending.
- The exact emoji/reaction text now reaches language understanding alongside a
  revalidated, PII-free modality contract. Ticket follow-up evidence is
  restricted to validated image/audio/video/document content in one exact
  active context; stickers and vCards no longer become evidence or modify
  identity merely because a follow-up window is open.
- OpenAI Responses, vision, completed-audio transcription, TTS and Realtime
  voice contracts exist. Realtime tools return structured outputs and use
  stable effect identities.
- The operations dashboard now publishes an `openai.suite_readiness.v1`
  contract for Chat/Responses, vision, STT, TTS and Realtime. It separates a
  configured platform runtime, dated provider-connectivity evidence and
  workload-specific proof. Provider evidence expires after seven days and no
  modality is marked live without its own evidence. The locally configured
  credential remains HTTP-401/unverified; the dashboard is an honest readiness
  surface, not a successful OpenAI certification.
- Twilio Media Streams now require a short-lived signed envelope and a
  one-time Redis `SET NX EX` claim before tenant resolution or any OpenAI
  connection. Tampered, expired, replayed, cross-tenant or unbounded preflight
  streams fail closed. This is locally tested; the shared Redis path and a real
  Twilio/OpenAI call remain uncertified.
- Voice now also has a durable tenant-bound consent lifecycle. DTMF approval is
  required per call, timeout/empty input is an irreversible decline, recording
  is constrained off, `(provider, CallSid)` is globally unique and only one
  database-backed `stream:claimed` event can open the bridge. The claim locks
  and revalidates the current tenant policy, so changing the same-version
  policy to `disabled` revokes a previous grant before context or OpenAI.
- The supplied Render trace proves that production previously transcribed audio
  and sent Twilio messages. It also proves that rigid state/fuzzy-menu routing
  lost intent, repeated prompts and degraded a specific address.
- The configured local OpenAI credential returned HTTP 401 in the controlled
  2026-07-29 smoke. Live model/audio certification is therefore blocked until
  the deployment credential/project is corrected.
- Browser Realtime no longer treats ephemeral-session provisioning as a live
  call. The UI requires a connected transport plus live local and remote audio
  tracks, closes any partial transport on failure and otherwise returns the
  user to chat with an explicit `realtime_transport_not_connected` state. The
  actual WebRTC SDP/audio adapter and SIP readiness proof remain blocked by the
  invalid OpenAI project credential; no simulated session ID or "listening"
  state is emitted.
- Durable WhatsApp queue mode remains opt-in (`legacy` by default) until its
  migration, worker and provider callback rollout are verified remotely.
- Durable inbound rollout is now tenant-canary scoped locally: `queue` requires
  an explicit `WHATSAPP_INBOUND_QUEUE_TENANT_IDS`, while signed traffic for
  excluded tenants keeps the synchronous legacy path. User/provider form fields
  cannot select the canary; worker sweeps, Celery wakeups and outbound claims
  apply the same allowlist. Render now declares an independently switchable
  worker in zero-I/O standby and a double-gated, historically scoped retention
  cron. Both remain inactive. Because canonical session `enforce` is still a
  process-wide prerequisite, its all-tenant shadow audit and PostgreSQL/provider
  staging proof are required before enabling even one shared-service canary.
- CRM replies now have a local `inbox.action_delivery.v2` contract: the browser
  reuses one cryptographic `client_message_id` across an ambiguous retry, the
  backend stages comment plus effects atomically, and the operator UI separates
  durable queueing, provider acceptance, replay and final callback evidence.
  It never labels queue/provider acceptance as delivered.
- Fifteen vertical WhatsApp template packs now cover municipalities, schools
  and companies across confirmation, follow-up, appointment, payment and human
  handoff. Local materialization validates placeholders, language, category and
  CTAs; provider approval remains a separate dated state. A local draft or
  Content SID never masquerades as Meta approval or permission to send.
- The generic Notification surface now has a locally tested durable Twilio
  template transport. Queueing binds an exact tenant-owned approved registry,
  sender snapshot, canonical payload digest and idempotency key; dispatch uses
  fenced leases and a per-attempt signed callback. A timeout after the provider
  boundary becomes operator-visible `send_uncertain` and is never retried
  blindly, while callback reconciliation is tenant/sender/SID scoped and
  monotonic. The feature remains disabled with an empty tenant allowlist in the
  Render Blueprint. Its migration, permanent worker, PostgreSQL/Twilio staging
  smoke and controlled production canary are still required.
- Local notification templates now use an exact variable contract in both
  preview and durable queue paths: missing or unexpected variables fail before
  a Notification row exists, so literal `${placeholder}` text cannot be queued.
  A tenant-scoped, authorized-staff, no-store preview endpoint renders local and
  provider snapshots without an audit write, provider call or send button; it
  always reports transport readiness as unchecked and production send as
  blocked. The CRM template operations page now consumes that strict contract,
  requires an explicit tenant and shows rendered subject/body, provider
  lifecycle and stable blockers while preserving the zero-side-effect/no-send
  boundary. Campaign preparation now consumes this same strict preview through
  tenant-scoped `crm_campaign_prepare.v1`: it requires explicit marketing
  opt-in, applies bounded frequency caps and persists auditable per-contact
  receipts as `held/not_attempted` behind an idempotency key. Missing consent is
  excluded rather than inferred, scheduling remains a blocked draft, and the
  legacy mutating endpoint returns 410 instead of recording a fake send. No
  Notification, provider call or transport retry is created. A separate,
  reviewable dispatch/callback contract, worker, migration smoke and controlled
  provider canary are still required before campaigns can be called live.
- A generic, tenant-scoped `domain_effect_outbox` and dedicated Render worker
  now exist locally. Ticket creation, ticket comments and PyME order external
  effects stage intents in their domain transaction for explicit canary tenants,
  with owner binding, HMAC, fenced leases, privacy-safe payloads and terminal
  `unknown` for ambiguous provider outcomes. For canary tenants,
  `PedidoService` and the real multimodal AI confirmation path also materialize
  `PymePedido`, `Order`, `MarketOrder` and the external-effect intents in one
  transaction. Other direct order writers and status notifications have not yet
  migrated. All of this has local contract-test evidence only: PostgreSQL,
  Render, SMTP and provider E2E remain unverified.
- Public survey submissions from V2, legacy and PWA aliases now converge on one
  pre-write Turnstile/rate-limit boundary. A dedicated survey-effect worker
  polls the database fairly across tenants; its realtime effect requires a
  verified shared Redis Socket.IO transport and records only publish acceptance,
  never browser delivery. Local tests cover the contracts; Redis/Render/browser
  delivery remains unverified.
- Public survey resolution is now fail-closed when a tenant is explicit. Full
  tokens, aliases and short links cannot fall back to a survey owned by another
  tenant across V2, legacy aliases, PWA, portal votes, live results or Meta
  Flow completion; mismatches return one non-enumerating `survey_not_found`
  contract and create no response or replay receipt. The public frontend now
  consumes canonical `tenant_slug` links, preserves the scope in participation
  and QR URLs, and does not retry a reason-coded tenant/security 404 through a
  compatibility alias. These are local SQLite/mock guarantees; PostgreSQL,
  distributed realtime and provider staging remain pending.
- The survey CRM now consumes `surveys.admin_list.v2`: a tenant-bound,
  source-backed operational envelope with freshness, reconciled response
  counts, survey-versus-voting phase and backend-authoritative capabilities.
  Publish/close/delete/share controls follow that contract, governed releases
  do not expose legacy mutations and closing requires an explicit irreversible
  confirmation while repeated close requests preserve the original terminal
  state. Participation rate and abstention remain null unless a real eligible
  population denominator exists; the UI does not infer them from respondents.
- Surveys can opt into `surveys.privacy.v1` source anonymity: direct identity,
  request metadata, precise coordinates and raw metadata are rejected at the
  database boundary; uniqueness uses a tenant/survey-bound versioned HMAC;
  consent and retention are explicit; a daily purge removes expired response,
  receipt, effects and individual analytics evidence. Historical surveys remain
  explicitly `legacy`. PostgreSQL, Render cron and backup deletion are not yet
  certified.
- Public source-anonymous results now apply `surveys.public_small_cell.v1`
  before any AI-provider call: cohorts, options, time buckets and map cells
  below the configured threshold are suppressed without disclosing exact
  counts or constructing provider prompts from them. This has local regression
  evidence only.
- Surveys can now opt into a tenant-scoped governed release: one immutable v1
  snapshot binds the instrument, privacy contract, minimized eligibility
  policy, consent policy and declarative human-review rules. Responses must
  acknowledge and pin that release across every public alias; closure fails on
  unpinned responses and emits a hashed manifest. Database triggers prevent
  mutation of published/closed releases and the contract explicitly does not
  certify an election or result. Multi-release supersession/rollback, staging
  PostgreSQL proof and the operational/legal review process remain pending.
- Governed releases now store the exact approved public consent as bounded
  plain text inside the immutable snapshot. Python and the browser share a
  deterministic LF/NFC normalization and SHA-256 contract; publishing or
  participating fails closed on missing text, controls, invalid Unicode or a
  hash mismatch. The citizen sees and locally verifies the exact text before
  two unchecked acknowledgements become available. This is integrity evidence,
  not a legal opinion or electoral certification.
- Restricted governed releases now have an opt-in Opaque Eligibility v1 gate.
  A tenant-scoped human review can issue a one-time credential without storing
  the raw subject or credential; expiry, revocation, generation, policy scope
  and redemption are database-enforced, while exact response replay remains
  durable. An authorized admin panel can issue and revoke credentials, but the
  raw value is exposed once and stays only in component memory for explicit
  show/copy/clear; tenant, survey, release and policy are reconciled again from
  the durable ACK. The participant browser uses a one-time-code field and sends
  the credential only through `X-Survey-Eligibility-Credential`, without React
  Query, URL or browser-storage persistence. A success requires a committed
  terminal redemption receipt bound to the same response, release and policy;
  closure recomputes that authoritative terminal contract instead of trusting
  response mirror columns. Meta Flow fails closed because it cannot preserve
  that header.
  Eligible-population, participation and abstention denominators stay null, and
  the contract explicitly does not certify ballot secrecy, a regulated election
  or its result. The feature is disabled by default. SQLite tests and PostgreSQL
  offline SQL compilation are green; no migration, staging or production proof
  has been applied.
- Public survey retention and consent timestamps now use the server receipt
  clock. A client-supplied timestamp is retained only as explicitly
  non-authoritative diagnostic metadata and cannot extend retention or forge a
  governance acknowledgement. Source-anonymous mode drops that metadata.
- Frontend survey/template/governance caches and asynchronous mutations now
  include tenant identity. Ticket drafts, delivery attempts and late responses
  are bound to the active tenant and ticket, while synthetic survey analytics
  are restricted to development/test and visibly labelled with provenance.
- Free-form municipal problem descriptions no longer enter broad multiword
  fuzzy-menu matching. Explicit buttons/actions still win, single-word typo
  recovery remains bounded, and ambiguous category evidence is delegated to
  the structured claim planner instead of taking the first weak keyword match.
- Guided municipal claims now treat the optional evidence step as multimodal:
  an authenticated photo, voice note or document advances the same flow, while
  a substantive transcript can enrich the draft and a media flag without a
  validated attachment ID is never acknowledged as stored evidence. Natural
  control answers such as `Nada mas` still skip the attachment step. Generic
  interactive claim prompts no longer inherit menu TTS merely because they
  have a cache namespace; fixed accessible menus and explicit audio replies
  keep their audio contract.
- A substantive transcript received while the main menu is visible now enters
  the structured claim bootstrap before any category-only fallback. This keeps
  the reported problem and an explicit callback request in the same draft
  instead of reducing the turn to `Luminaria` or another menu label. The real
  supplied `poste caido ... llamarme por telefono` journey has a local
  regression; Render, transcription-provider and WhatsApp E2E remain pending.
  Municipal LLM/tool logs now record lengths, counts, opaque identity presence
  and error types rather than prompts, responses, parameters, results or raw
  contact-recovery exceptions.
- Stale municipal contact state is now reconciled from structured completeness
  before routing the next turn. Inside explicit edit mode, a spoken address
  correction updates the claim address, clears stale coordinates/map evidence
  and cannot overwrite `direccion_contacto`; email tokens are masked before
  address detection. The two exact supplied correction transcripts have local
  regressions. Live OpenAI STT remains blocked by the invalid credential, so
  this is not a WhatsApp/provider E2E claim.
- Workflow Studio now exposes both a tenant-authorized, no-store
  prepublication contract and a canary-gated durable control plane. Admins can
  validate and deterministically simulate a bounded acyclic graph with zero
  effects, then persist append-only draft revisions, independent reviews,
  immutable versions and activation history. A stable per-workflow lock and
  post-lock revalidation prevent stale-draft and competing-publication races;
  only the latest decision for an exact subject can authorize publication, and
  rollback creates a new version instead of rewriting history. The admin panel
  reconciles the tenant contract, keeps idempotency keys in memory and requires
  an explicit `CONTROL_PLANE_ONLY` acknowledgement. Runtime consumption,
  provider calls, messages, tickets and handoffs remain deliberately
  disconnected. SQLite tests, PostgreSQL offline SQL and one-head Alembic proof
  are green; a real PostgreSQL migration/concurrency smoke and staging canary
  remain pending.
- Authentication and contact continuity no longer deserialize arbitrary admin
  JSON before feature, plan and tenant gates. Legacy token-body compatibility
  is size-bounded, while global contact-body enrichment is restricted to an
  explicit public/conversational path allowlist; protected control-plane routes
  resolve identity from headers/cookies only. This boundary has local regression
  evidence, not deployment proof.
- CRM operations now publishes the source-backed
  `operations.queue_truth.v1` contract. It separates the point-in-time backlog
  from tickets created in the selected period, fixes one `as_of` across SLA and
  age calculations, reports missing SLA evidence as unknown instead of healthy,
  and exposes explicit numerators, denominators, ownership, age buckets and
  source-model coverage. Open backlog collection is tenant-scoped and filtered
  in the database; the dashboard does not manufacture backlog trends without a
  historical snapshot ledger. Deep links declare whether they are exact filters
  or navigation-only, and the browser rejects partial, inconsistent or unsafe
  queue contracts instead of rendering reassuring zeroes. A separate
  `inbox.operational_queue.v1` surface now unifies `TenantTicket`,
  `MunicipioTicket` and `PymeTicket` with collision-free source identities,
  bounded-memory keyset pagination, a signed tenant/actor/scope/filter-bound
  cursor, canonical SLA/age/ownership/source/category filters, employee
  category scope and fail-closed request parsing. Every emitted detail endpoint
  re-enforces that category scope and returns the same not-found result for an
  unauthorized or absent record. Legacy titles and assignees are canonicalized
  before filtering/serialization so one malformed row cannot invalidate a
  browser page. Creation membership is now stated exactly as
  `created_at IS NULL OR created_at <= as_of`: null timestamps remain visible at
  the end with unknown age, while future-dated rows are quarantined, counted by
  source and excluded identically from the dashboard and inbox. Mutable status,
  assignment and SLA remain live on every page and the UI states that
  explicitly. Exact age and native-assignee predicates are pushed into SQL;
  adapter-only filters are protected by a shared scan budget that aborts without
  returning a partial page, and tenant+actor rate limiting uses the shared
  Flask-Limiter store with explicit `429`/`503` behavior. The dashboard no
  longer labels navigation-only links as exact drilldowns while both contracts
  remain unbound. The browser now opens a queue row through its exact
  `ticket_id + source_model` identity, so colliding numeric IDs across tenant,
  municipal and PyME tables cannot resolve to the wrong case. Manual refresh
  discards the previous cursor chain, background polling is limited to a
  single-page view and multi-page views state that they require an explicit
  refresh. SLA reads are side-effect free: only the materialization task writes
  breach state or events. A shared employee-category policy now protects the
  v2 and legacy ticket paths covered by the operational inbox, assignment,
  attachments, ticket-derived maps/statistics, Backoffice ticket data and
  ticket/executive AI before data or provider side effects. This is local
  contract/UI evidence only: a durable queue projection, transactionally
  consistent snapshots, logical-case deduplication across the three source
  tables, CRM-to-provider callback linkage and staging validation remain
  pending. Category RBAC is not yet globally closed: historical admin-tenant,
  metrics and aggregate analytics/cache surfaces still need to consume the
  same central policy before this control can be certified platform-wide.
- A tenant-scoped assessment/interview core now models immutable published
  program versions, cases pinned to one version, explicit versioned consent,
  referenced evidence with provenance/hash and a terminal
  `awaiting_human_review` handoff. Consent stores the exact normalized public
  text and accepts a WhatsApp participant-event proof only when it resolves to
  a completed durable turn for the same tenant/provider/message digest. The
  interactive action is a short-lived CSPRNG challenge whose ledger binds the
  session, immutable program version, consent-text hash and expected canonical
  WhatsApp identity HMAC; only its SHA-256 is persisted, consumption is atomic,
  and free-form text cannot mint or replay the receipt. This proves consent from
  the intended channel identity, not the participant's civil identity. The core
  is fail-closed behind a deployment flag; widget/voice consent adapters and
  staging certification remain pending.
- Strict interview definitions now expose deterministic resumability: a
  tenant-scoped no-store session read returns progress, next required step and
  an evidence timeline bound by stable `step_ref` provenance. Unknown steps,
  incompatible evidence types/channels, changed published-definition hashes
  and premature completion fail closed; historical definitions remain in an
  explicit advisory compatibility mode. The automatic WhatsApp adapter, UI
  conductor and real media/provider staging path remain pending.
- The education staff frontend now consumes that resume contract on the
  canonical tenant/session route. Its parser rejects incompatible envelopes,
  the React Query key includes tenant and session, reads are `no-store`, and a
  read-only checkpoint card exposes prompts, verified progress, consent status
  and evidence type/channel without rendering storage references or hashes.
  Admissions remains disabled by default and still lacks mutation controls and
  a live WhatsApp/widget/voice conductor.
- Education staff now also receive a tenant-scoped, no-store interview inbox
  built from persisted sessions, evidence counts, verified progress and
  minimized audit facts. Its parser reconciles counts and capabilities, accepts
  legitimate unassigned/empty sessions and rejects incompatible envelopes.
  Subject references, storage references, provenance, audit details and hashes
  are not exposed. Managed Assignment v1 now adds an explicit capability,
  tenant-eligible candidates, optimistic concurrency, stable idempotency and an
  append-only assignment ledger whose pointer, receipt and redacted audit event
  commit together. Completed or void sessions remain immutable and ambiguous
  client failures require a deliberate retry with the same operation identity.
  The feature is still fail-closed and locally verified only; review and
  follow-up remain visibly disabled because their durable domains do not yet
  exist, and automated decisions are forbidden. A truncated page declares that
  continuation is not yet available rather than presenting a nonfunctional
  pagination promise.
- Legacy municipal tickets with a null tenant are visible only when their owner
  maps to exactly one tenant; ambiguous/orphan rows remain quarantined. New
  writes normalize and validate the tenant/owner pair, and deterministic legacy
  rows have an explicit backfill/index migration.
- Canonical WhatsApp session identity is now tenant/provider scoped and stores
  only a versioned HMAC plus a random context identifier. Unique legacy context
  can be adopted; ambiguous or cross-scope state is isolated, and durable replay
  revalidates the exact binding. The Render blueprint remains in migration mode;
  enforce-mode rollout and real provider/worker evidence are still pending.

## P0 acceptance backlog

### 1. One conversational runtime

- The LLM plans against a strict typed schema; backend actions validate and
  execute. State machines constrain unsafe transitions but do not replace
  language understanding.
- Free-form input never goes through fuzzy menu matching merely because a menu
  was previously displayed.
- Golden evaluations cover the supplied journeys: audio-first claim, image
  first, location first, correction, "nada más", emoji-only, PIN question while
  confirming, request for a call, duplicate/reordered provider callbacks and
  follow-up evidence on an existing ticket.

### 2. Production WhatsApp certification

- Stage and production probes cover text, image, audio, document, location,
  buttons, templates, Flows, callbacks, retries, duplicates and out-of-order
  delivery.
- Queue mode acknowledges only after durable persistence. Per-conversation FIFO,
  leases, bounded retries, dead-letter/replay and metrics are visible in the
  operator panel.
- Provider sends with ambiguous outcomes are never retried blindly.

### 3. Omnichannel CRM and voice operations

- One contact identity and timeline spans WhatsApp, web, email, Instagram and
  voice, with queues, SLA, assignment, presence, takeover and supervision.
- Calling includes consent, PSTN/WhatsApp call lifecycle, recordings policy,
  transcription, DTMF, transfer, callback scheduling and audit evidence.
- Realtime tool calls reserve an atomic tenant/session/call receipt before any
  non-idempotent effect and replay only a completed matching result.

### 4. Governed automation studio

- The local prepublication slice validates and simulates a tenant-scoped
  workflow without effects. The durable canary now persists append-only drafts,
  independent reviews, immutable versions, control-plane activation and
  rollback-as-a-new-version under a reviewed tenant/plan gate. Next, add the
  visual canvas, import/export and a separately gated runtime adapter with
  staging replay evidence; no activation record alone may execute a flow.
- A tenant can ultimately draft, validate, simulate, publish, version and roll
  back flows, campaigns, templates and tools without code changes.
- Publication records approvals, consent rules, audience, rate limits,
  idempotency, quiet hours and delivery analytics.

### 5. Enterprise operations and privacy

- Logs contain operational metadata, hashes and opaque IDs, never raw citizen
  conversations, contact data, access PINs or provider credentials.
- Domain mutations and their external effects must be staged atomically. The
  ticket creation, ticket comments and PyME order external-effect slices now use
  a tenant-scoped `domain_effect_outbox` with one row per channel/recipient,
  fenced leases and explicit `pending`, `processing`, `retry_wait`, `succeeded`,
  `skipped`, `unknown` and `dead` states. A crash after provider I/O becomes
  `unknown` and is never blindly resent. The canary `PedidoService` path now
  commits both CRM projections atomically; extend the same contract to the
  remaining direct writers and status notifications.
- Keep effect payloads data-minimal: handlers reload tenant-scoped domain data;
  the outbox stores only template/role contracts plus an HMAC of the complete
  intent. Ticket, comment and order creation must roll back if effect staging
  fails once queue mode is enabled.
- Completed WhatsApp inbound payloads are compacted to non-PII tombstones.
  Dead payloads have a finite, configurable diagnostic retention window and an
  audited legal-hold override; provider outbox payloads follow a separate
  lifecycle until delivery is terminal.
- Publish SLOs and instrument alerts for queue age, dead letters, provider
  failures, handoff latency, response quality and duplicate suppression.
- Document retention, deletion, backup/restore, DR, tenant isolation, SSO/MFA,
  audit export and a trust/compliance roadmap.

### 6. Surveys and public participation

- Preserve Chatboc's vertical lead: conditional survey builder, WhatsApp Flow,
  live results, QR, territorial heatmaps and comments.
- Source-anonymous intake, unified public anti-abuse and a durable effect worker
  now have local contracts. Small-cell suppression also has local provider-boundary
  coverage. The governed-release v1 also pins minimized eligibility and consent
  policies plus declarative quorum/tie/challenge procedures to an immutable
  snapshot and human review. Its local operator/public UI now manages lifecycle,
  verifies the exact consent text and keeps mutation idempotency stable until a
  durable ACK. Staging proof, granular PII/export permissions and backup/replica
  deletion evidence remain pending.
- Add reviewed roster/eligibility adapters without storing PII in the release,
  multi-release supersession/rollback, approvals, recount/challenge operations
  and an explicit product/legal separation between informal polling and
  regulated votes. The current release manifest is integrity evidence, not an
  electoral certification.
- Productize interviews as their own governed domain: session, rubric,
  assignment, evidence, reviewer decision, appeal and school/company template
  packs. The operational inbox and audited Managed Assignment v1 now exist
  locally behind disabled-by-default gates. Human-review decisions, appeals,
  follow-up, skill queues, workload/SLA routing and template packs still require
  their own reviewed persistence and mutation contracts. A menu or generic
  survey is not an interview workflow.

## Competitive baseline

Auditoría de fuentes oficiales actualizada al 2026-08-02. `Explícito` significa
que el proveedor documenta actualmente la capacidad; sigue siendo una
afirmación del proveedor, no una certificación independiente de calidad ni de
producción. `No verificado` significa que las fuentes oficiales revisadas no
demuestran la capacidad; no prueba que sea inexistente.

| Proveedor | Multimodalidad y agentes IA | CRM, workflows y plantillas | Llamadas y voz | Encuestas, analítica y gobierno | No verificado o límites explícitos |
| --- | --- | --- | --- | --- | --- |
| Jelou | **Explícito:** transporte WhatsApp de texto, imagen, audio, video, archivos, stickers y ubicación; herramientas, memoria y salida multimedia del AI Agent. [Mensajes](https://docs.jelou.ai/en/api/envia-mensajes/introduccion), [AI Agent](https://docs.jelou.ai/en/guides/nodos/ai-agent), [actualizaciones](https://docs.jelou.ai/en/changelog) | **Explícito:** Brain Studio, tester/debugger, versiones inmutables, plantillas de workflows, Inbox CRM, monitoreo y API de HSM. [Versiones](https://docs.jelou.ai/guides/getting-started/publicar-versiones), [plantillas](https://docs.jelou.ai/en/api/campanas/crear-plantilla) | **Declarado en su sitio oficial:** llamadas y campañas entrantes/salientes con IA o humanos; la documentación técnica demuestra explícitamente el botón de llamada de WhatsApp. [Voice Agent](https://voice.jelou.ai/en), [botón de llamada](https://docs.jelou.ai/en/api/envia-mensajes/boton-llamada) | **Explícito:** monitoreo, métricas de operadores, roles, SSO/2FA y analítica según plan. [Precios](https://jelou.ai/en/pricing) | **No verificado:** comprensión arbitraria de audio/imágenes entrantes por el AI Agent y un motor gobernado de encuestas/votaciones. Transportar medios no demuestra comprensión semántica. |
| respond.io | **Explícito:** transcripción de audio entrante con acciones IA y análisis de archivos/imágenes recientes; sus agentes actualizan el CRM, llaman APIs HTTP y disparan workflows. [Acciones](https://respond.io/help/ai-agent-actions/using-ai-agent-actions), [pruebas](https://respond.io/help/ai-agents/how-to-test-ai-agents) | **Explícito:** inbox omnicanal, historial unificado, Lifecycle, integraciones CRM, workflows visuales, routing, campañas y plantillas CSAT. [Resumen](https://respond.io/help/quick-start/what-is-respondio), [workflows](https://respond.io/help/workflows/workflows-overview) | **Explícito:** Voice AI atiende llamadas entrantes de WhatsApp, Messenger y Telnyx y puede transferirlas a una persona. [Handle Calls](https://respond.io/help/ai-agent-actions/ai-agent-action-handle-calls) | **Explícito:** analítica de entrega/funnel, CSAT por workflow y declaraciones publicadas de ISO 27001/GDPR. | **Límites explícitos:** contexto de 20 mensajes; Voice AI sólo entrante, máximo tres minutos, sin takeover humano en vivo y sin consumir resultados HTTP durante la llamada. [Límites de IA](https://respond.io/help/ai-agents/ai-agents-known-limitations-and-workarounds) |
| ChatBot.com / Text | **Explícito:** AI Assist/AI Knowledge, AI Agent guiado por instrucciones y respuestas enriquecidas en web/Messenger. [AI Assist](https://www.chatbot.com/help/artificial-intelligence/what-is-chatbot-ai-assist/), [widget](https://www.chatbot.com/integrations/chat-widget/) | **Explícito:** builder visual, plantillas, Archives/Visitors, webhooks y handoff o tickets mediante LiveChat y HelpDesk. [Integraciones](https://www.chatbot.com/help/integrations/what-is-integration/) | **No verificado:** ninguna fuente técnica revisada demuestra Voice AI o llamadas comparables. | **Explícito:** reportes/exportación básicos, tres roles, TLS/2FA declarado y posicionamiento enterprise de audit logs. [Reportes](https://www.chatbot.com/help/reports/reports-overview/), [roles](https://www.chatbot.com/help/teams/teams-overview/), [precios](https://www.chatbot.com/pricing/) | **Ambiguo:** su marketing menciona WhatsApp, pero el catálogo nativo actual no lo lista; la ruta documentada combina ChatBot.com, LiveChat y WhatsApp Business. No se verificó comprensión de audio/imágenes entrantes ni producto de encuestas/votaciones. [Puente LiveChat](https://www.chatbot.com/integrations/livechat/) |
| Blip | **Explícito:** transporte WhatsApp, stickers y llamadas en Desk. Sus fuentes revisadas no demuestran comprensión semántica general de audio, sticker o ubicación; su propia base de conocimiento declara que no interpreta el contenido de imágenes. [Knowledge Base](https://help.blip.ai/hc/pt-br/articles/35037154666903-Studio-Base-de-Conhecimento), [Blip Calls](https://help.blip.ai/hc/en-us/articles/26971911490199-Blip-Calls-Making-Calls-in-Desk) | **Explícito:** agentes con tareas, herramientas/APIs y RAG; Studio expone logs de árbol, payload, prompt, tools, latencia y tokens. [Tareas](https://help.blip.ai/hc/pt-br/articles/27079634363159-Implementa%C3%A7%C3%A3o-de-tarefas-para-o-Agente-de-IA), [logs](https://help.blip.ai/hc/pt-br/articles/38849360798615-Logs-e-Eventos-no-Studio) | **Explícito:** llamadas desde Desk; no se verificó continuidad completa voz-chat-CRM gobernada por el agente. | **Explícito:** campañas, NPS/CSAT y dashboards. Las reglas SLA siguen documentadas como beta cerrada. [Campañas](https://help.blip.ai/hc/pt-br/articles/33771933812631-Campanhas), [satisfacción](https://help.blip.ai/hc/pt-br/articles/21974005289623-An%C3%A1lise-de-satisfa%C3%A7%C3%A3o) | **No verificado:** votación con elegibilidad, anonimato y auditoría institucional; transportar medios no prueba que el agente los comprenda. |
| Infobip | **Explícito:** AgentOS con LLM, herramientas, MCP, orquestación multiagente, Knowledge Agents, componentes reutilizables y Quality Center; mensajería enriquecida en su stack de canales. [AI Agents](https://www.infobip.com/docs/ai-agents), [componentes](https://www.infobip.com/docs/ai-agents/advanced-topics/component-design) | **Explícito:** Conversations CCaaS, perfiles, colas/routing, handoff humano, Answers/Automation Studio, plantillas, campañas y WhatsApp Flows. [Conversations](https://www.infobip.com/docs/conversations) | **Explícito, Early Access:** Voice AI entrante/saliente, integración IVR y WhatsApp Business Calling. [Voice AI Agents](https://www.infobip.com/docs/voice-ai-agents) | **Explícito:** CSAT, Voice of Customer, Forms, analítica de agentes/herramientas/sesiones, evaluación con ground truth, RBAC y audit logs. [Analítica](https://www.infobip.com/docs/insights-and-analytics/analytics/ai-agents), [roles](https://www.infobip.com/docs/essentials/manage-my-account/manage-roles) | **No verificado:** comprensión arbitraria de imágenes por todos los agentes en todos los canales. Voice AI está explícitamente en Early Access, no en disponibilidad general. |
| Intercom / Fin | **Explícito:** Fin en WhatsApp, transcripción automática de audio WhatsApp y comprensión de imágenes en chat/email. [Notas de voz](https://www.intercom.com/help/en/articles/15082244-reply-with-voice-notes-on-whatsapp), [FAQ de Fin](https://www.intercom.com/help/en/articles/7837535-fin-ai-agent-faqs) | **Explícito:** inbox de soporte maduro, ownership/handoff del bot y workflows omnicanal con routing y automatización reutilizable. [Workflows](https://www.intercom.com/help/en/articles/7836459-workflows-explained) | **Explícito:** notas de voz WhatsApp. **Límite explícito:** no admite llamadas WhatsApp e Intercom Phone requiere otro número. | **Explícito:** CSAT WhatsApp, CX/analítica de Fin, audiencias de contenido y controles de hosting regional. | **Límites WhatsApp explícitos:** sin comentario CSAT, creación directa de ticket/formulario, llamadas grupales ni inicio de templates por REST. La visión se documenta para chat/email, no específicamente para imágenes WhatsApp. [FAQ WhatsApp](https://www.intercom.com/help/en/articles/9067468-whatsapp-faqs) |

## Cinco brechas competitivas a cerrar

Son brechas a demostrar y productizar, no afirmaciones generales de que todos
sus componentes subyacentes estén ausentes del repositorio.

1. **P0 — Contrato multimodal verificado.** Normalizar texto, emoji, audio,
   imagen, documento, ubicación, respuesta citada y eventos de llamada;
   preservar contexto; exponer confianza y aclaraciones; certificar todo con
   recorridos E2E construidos desde las conversaciones y medios reales provistos.
2. **P0 — Plano de acciones confiable.** Completar el outbox transaccional
   genérico para cada efecto externo y proyección interna, con idempotencia,
   callbacks del proveedor, reconciliación, payloads mínimos y manejo de
   `unknown` visible al operador. Nunca afirmar éxito sin evidencia durable.
3. **P0/P1 — Voz Realtime unificada.** Soportar llamadas WhatsApp/PSTN
   entrantes y salientes con consentimiento, política de grabación,
   transcripción, DTMF, herramientas, callbacks y transferencia humana sin
   perder el contexto de chat o CRM. El slice local `voice.consent.v1` ya exige
   consentimiento DTMF por llamada, recarga el grant tenant+CallSid antes de
   OpenAI, deja grabación forzada en `false`, reclama un solo stream y vuelve a
   validar el kill switch del tenant dentro de la transacción. Mantiene un
   lifecycle auditable sin convertir ACKs de Twilio en conexión. Faltan E2E
   PSTN/WhatsApp, operación de callbacks reales, observabilidad y validación de
   transferencia en staging.
   Twilio documenta WhatsApp Business Calling como disponible y enrutable a una
   aplicación TwiML, pero prohíbe conectar un extremo WhatsApp con PSTN. El
   contrato local ahora prueba llamadas entrantes `whatsapp:` bajo el mismo
   consentimiento explícito y bloquea ese puente inválido; un handoff humano
   para WhatsApp debe usar un transporte admitido (por ejemplo, SIP, WebRTC o
   contact center) y todavía necesita certificación real del sender.
   [WhatsApp Business Calling](https://www.twilio.com/docs/voice/whatsapp-business-calling)
4. **P1 — Agent y Workflow Studio gobernado.** El slice local ya ofrece
   validación estricta, simulación multimodal sin efectos y un ledger durable
   con drafts, revisiones independientes, versiones, activación de control y
   rollback append-only. Falta entregar composición visual, packs verticales
   para municipios/colegios/empresas, plantillas/Flows de Meta, replay y
   evaluaciones, más publicación gradual y un runtime separado certificado en
   PostgreSQL/staging.
5. **P1 — Operación enterprise y participación.** El slice local
   `operations.queue_truth.v1` ya diferencia backlog actual de flujo por período
   y publica cobertura SLA/ownership sin convertir faltantes en estados sanos;
   `inbox.operational_queue.v1` agrega unión de tres fuentes, identidad compuesta,
   filtros canónicos, RBAC de categoría también en cada detalle, cuarentena de
   fechas futuras, canonicalización legacy, cursor firmado, pushdown SQL y
   presupuestos compartidos de tasa/escaneo sin páginas parciales. Falta materializar una
   proyección durable que permita vincular KPI y bandeja bajo el mismo RBAC y una
   revisión transaccional, unir cada acción CRM con callbacks finales del
   proveedor, persistir snapshots históricos y completar analítica de
   resolución, encuestas y votaciones con elegibilidad, anonimato configurable,
   deduplicación, consentimiento, retención, RBAC y exportaciones de auditoría.

## Release proof ladder

1. Unit/contract tests and schema checks.
2. Integrated local journeys with deterministic provider doubles.
3. Migration upgrade/downgrade and one-head verification.
4. Staging E2E with real OpenAI, Twilio/Meta and deployment workers.
5. Controlled production canary with metrics and rollback.
6. Only then describe the capability as production-ready.
