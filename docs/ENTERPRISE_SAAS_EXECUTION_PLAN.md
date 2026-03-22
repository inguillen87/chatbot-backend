# Enterprise SaaS Execution Plan — Chatboc

## Objetivo

Convertir Chatboc en un SaaS enterprise global con una ejecución incremental,
cerrando primero las bases operativas que destraban todo lo demás:

1. **conversation stream unificado**
2. **realtime backbone enterprise**
3. **SLA engine real**
4. **notification orchestration**
5. **operator inbox de clase mundial**

---

## Fase 1 — Foundation (2-4 semanas)

### Backend
- Introducir un contrato común para eventos realtime y conversación.
- Emitir envelopes consistentes para:
  - `conversation.message.created`
  - `ticket.status.changed`
  - `ticket.assignment.changed`
- Mantener compatibilidad con eventos legacy.
- Definir `schema_version`, `occurred_at`, `conversation.id`, `ticket.id`.

### Frontend
- Consumir envelopes normalizados sin romper los listeners actuales.
- Guardar `X-Chat-Session-Id`, `X-Anon-Id`, `entityToken`, `pin`.
- Reconciliar HTTP inicial + deltas socket.

### Entregables
- helper backend `conversation_stream`
- tests de contrato
- documento frontend de consumo

---

## Fase 2 — Unified Inbox (3-5 semanas)

### Producto
- inbox único con lista + detalle + timeline + mapa + SLA
- colaboración:
  - notes internas
  - read state
  - presence
  - handoff entre agentes

### Backend
- timeline canónica con `origin`:
  - `public_tracking`
  - `admin_panel`
  - `whatsapp`
  - `email`
  - `system`
- nuevos eventos:
  - `conversation.message.read`
  - `ticket.presence.changed`
  - `ticket.sla.changed`

### Frontend
- split view persistente
- filtros guardables
- badges SLA / unread / assignment
- optimistic updates con rollback

---

## Fase 3 — SaaS Enterprise Controls (4-6 semanas)

### Capacidades
- feature flags por plan
- quotas / usage metering
- tenant health scoring
- onboarding self-serve
- audit trail exportable
- retention / masking de PII

### Revenue impact
- activation rate por tenant
- time-to-value
- adopción por feature premium
- churn risk por inactividad / baja configuración

---

## Fase 4 — AI Copilot (3-4 semanas)

### Operador
- resumen automático del caso
- borrador sugerido editable
- next best action
- detección de tickets trabados / alto riesgo

### Tenant admin
- insights accionables:
  - canal subutilizado
  - backlog crítico
  - áreas con peor SLA
  - pérdida de conversión por datos faltantes

---

## KPIs North Star

### Operación
- first response time
- SLA compliance
- reopen rate
- backlog aging

### Producto
- completion rate de guided intake
- self-resolution rate
- multimodal conversion rate

### Revenue
- activation rate
- expansion by plan
- tenant health score

