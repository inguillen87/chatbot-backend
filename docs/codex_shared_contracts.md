# Shared contracts — backend/frontend

## 1. Conversation domain

### conversation
- `id: uuid`
- `tenant_id: uuid|string`
- `contact_id: uuid|null`
- `anon_id: string|null`
- `status: open|pending_human|in_progress|waiting_user|resolved|archived`
- `priority: low|normal|high|urgent`
- `assigned_team_id: uuid|null`
- `assigned_agent_id: uuid|null`
- `tags: jsonb|string[]`
- `created_at`
- `last_activity_at`
- `closed_at`

### channel_session
- `id: uuid`
- `conversation_id: uuid`
- `tenant_id`
- `channel: web_widget|whatsapp|voice|email|live_chat|survey`
- `external_key: string`
- `legacy_chat_session_id: string|null`
- `started_at`
- `ended_at`
- `metadata: jsonb`

### message
- `id: uuid`
- `conversation_id: uuid`
- `channel_session_id: uuid`
- `direction: inbound|outbound|internal`
- `message_type: text|audio|image|file|event|system`
- `body: text|null`
- `payload: jsonb`
- `attachments: jsonb`
- `external_message_id: string|null`
- `created_at`
- `created_by: user_id|null`

## 2. Ticket relation
- ticket puede existir sin perder timeline
- ticket referencia `conversation_id`
- una conversación puede tener 0..n tickets

## 3. Notification domain

### template
- `id`
- `tenant_id`
- `channel`
- `name`
- `category`
- `locale`
- `status: active|paused|disabled|draft`
- `version`
- `content`
- `variables`

### notification
- `id`
- `tenant_id`
- `conversation_id|null`
- `recipient_ref`
- `channel`
- `template_id|null`
- `status: queued|sent|failed|cancelled`
- `idempotency_key`
- `scheduled_at|null`
- `sent_at|null`
- `metadata`

## 4. RBAC domain
- `org_unit`
- `team`
- `role`
- `permission`
- `membership`
- `audit_log`

## 5. API contracts

### GET /api/conversations/:id
Respuesta:
- `conversation`
- `participants`
- `assignment`
- `stats`

### GET /api/conversations/:id/timeline
Respuesta:
- `items[]`
  - `id`
  - `kind: message|event|note|notification|handoff`
  - `channel`
  - `direction`
  - `body`
  - `attachments`
  - `actor`
  - `created_at`
  - `metadata`

### POST /api/conversations/link/whatsapp
Request:
- `conversation_id`
- `phone`
Respuesta:
- `link_code`
- `expires_at`
- `deep_link`

### POST /api/conversations/link/confirm
Request:
- `phone`
- `link_code`
Respuesta:
- `conversation_id`
- `channel_session_id`
- `linked: true`

### POST /api/notifications
Request:
- `channel`
- `recipient`
- `conversation_id|null`
- `template_id|null`
- `body|null`
- `variables|null`
- `schedule_at|null`
- `idempotency_key`

### GET /api/analytics/kpis
Respuesta:
- `frt`
- `art`
- `resolution_time`
- `backlog`
- `sla_breach_rate`
- `handoff_rate`
- `deflection_rate`
- `csat`
- `nps`

## 6. Events
Envelope:
- `id`
- `type`
- `source`
- `subject`
- `tenant_id`
- `conversation_id`
- `channel_session_id|null`
- `occurred_at`
- `trace_id`
- `actor`
- `payload`

Tipos mínimos:
- `conversation.created`
- `conversation.updated`
- `conversation.linked`
- `message.received`
- `message.sent`
- `ticket.created`
- `ticket.updated`
- `handoff.requested`
- `handoff.assigned`
- `notification.queued`
- `notification.sent`
- `notification.failed`
- `survey.published`
- `vote.submitted`

## 7. KPIs y definiciones
- `FRT`: tiempo desde primer inbound hasta primera respuesta humana o automática, según filtro.
- `ART`: promedio entre inbound y respuesta.
- `Resolution time`: tiempo hasta estado resuelto.
- `Deflection rate`: conversaciones resueltas sin intervención humana.
- `Handoff rate`: porcentaje derivado a humano.
- `CSAT`: satisfacción posterior a resolución.
- `NPS`: recomendación 0-10 convertida a score.

## 8. Compatibilidad temporal
- `chat_session_id` no desaparece al inicio.
- se mantiene como alias legado mientras migran UI y canales.
- el ID canónico nuevo es `conversation_id`.
