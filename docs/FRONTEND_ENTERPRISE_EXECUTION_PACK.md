# Frontend Enterprise Execution Pack — Chatboc

## TL;DR

El frontend debe evolucionar desde “consumir respuestas del bot” a operar como
**cliente enterprise de conversación en tiempo real**.

---

## 1. Headers obligatorios en todas las requests del widget/tracking

- `X-Chat-Session-Id`
- `X-Anon-Id`
- `entityToken` o `X-Entity-Token` cuando exista tenant real
- `pin` cuando el usuario viene por tracking público

Sin estos headers, el frontend reintroduce:
- pérdida de contexto tenant
- fallback erróneo a demo shell
- 403 en chat/timeline público

---

## 2. Contrato realtime normalizado

Además de escuchar los eventos actuales:
- `conversation.message.created`
- `conversation.message.read`
- `ticket.status.changed`
- `ticket.assignment.changed`
- `ticket.presence.changed`
- `ticket.unread.changed`

el frontend debe empezar a consumir un envelope con:

```ts
type EnterpriseRealtimeEnvelope = {
  event_name: string;
  schema_version: string;
  occurred_at: string;
  room?: string | null;
  conversation: {
    id?: string | null;
    channel?: string | null;
    origin?: string | null;
    visibility?: string | null;
  };
  ticket: {
    id?: string | number | null;
    tenant_type?: string | null;
    tenant_id?: string | number | null;
    status?: string | null;
    priority?: string | null;
  };
  message: {
    id?: string | number | null;
    text?: string | null;
    author_type?: string | null;
  };
  payload: Record<string, unknown>;
}
```

### Regla importante
- usar `payload` para compatibilidad hacia atrás
- usar `conversation/ticket/message` para UI nueva enterprise

---

## 3. Reconciliación recomendada

### Inbox/admin
1. fetch HTTP inicial
2. subscribe socket rooms
3. aplicar deltas realtime
4. deduplicar por `event_name + ticket.id + message.id + occurred_at`

### Tracking público
1. cargar timeline HTTP
2. reenviar siempre `pin` y `X-Anon-Id`
3. refrescar solo el ticket afectado por delta

---

## 4. Vistas que debe construir frontend

### A. Unified Inbox
- lista
- detalle
- timeline
- mapa
- SLA badges
- unread badges
- assignment state

### B. Ticket 360 drawer
- conversación
- estados
- archivos
- ubicación
- customer/contact snapshot
- confirmation cards
- collaboration state:
  - `active_viewers_count`
  - `unread_viewer_count`
  - `latest_comment_id`
  - `latest_read_at`

### C. Public tracking
- timeline canónica
- estado actual
- fallback claro de ubicación
- CTA de responder / adjuntar / abrir maps

---

## 5. UX enterprise obligatoria

- skeletons consistentes
- optimistic updates con rollback
- toast/inline errors con retry
- keyboard shortcuts
- unread markers
- “otro agente viendo este ticket”
- read state

---

## 6. Artefactos esperados del equipo frontend

1. `realtime-envelope-adapter.ts`
2. `useTicketRealtime.ts`
3. `ticket-360-drawer`
4. `inbox-split-layout`
5. `tracking-timeline`
6. métricas UI:
   - `tenant_context_lost`
   - `socket_reconnect`
   - `realtime_duplicate_dropped`
   - `tracking_403_detected`

---

## 7. Endpoints nuevos para colaboración enterprise

- `POST /tickets/<tipo>/<ticket_id>/presence`
- `POST /tickets/<tipo>/<ticket_id>/read-state`

### Payload presence
```json
{
  "presence_status": "active"
}
```

### Payload read-state
```json
{
  "last_read_comment_id": 123
}
```

### Uso recomendado
- al abrir ticket => `presence=active`
- al salir/minimizar => `presence=idle|inactive`
- al llegar al final del timeline/chat => `last_read_comment_id=<último>`

### Datos nuevos ya disponibles en payloads
- `ticket.collaboration_state`
- `response.realtime_state.presence`
- `response.realtime_state.read_state`
- `response.realtime_state.presence.idle_count`
- `response.realtime_state.read_state.viewers[*].effective_presence_status`
- `dashboard-bundle.summary.active_viewers`
- `dashboard-bundle.summary.unread_viewers`
- `dashboard-bundle.leads.items[*].collaboration_state`
- `dashboard-bundle.leads.items[*].priority_score`
- `dashboard-bundle.leads.items[*].priority_breakdown`
- `dashboard-bundle.leads.items[*].priority_reasons`
- `tickets/unread-summary.items[*].collaboration_state`
- realtime delta: `ticket.unread.changed`
- `dashboard-bundle.team.items[*].active_ticket_views`
- `dashboard-bundle.team.items[*].idle_ticket_views`
- `dashboard-bundle.team.items[*].unread_ticket_views`
- `tickets/<tipo>/<id>/timeline.unified_conversation_stream`
- `tickets/<tipo>/<id>/timeline.unified_conversation_stream[*].id`
- `tickets/<tipo>/<id>/timeline.unified_conversation_stream[*].actor_type`
- `tickets/<tipo>/<id>/timeline.unified_conversation_stream[*].preview_text`
- `tickets/<tipo>/<id>/timeline.unified_conversation_stream[*].status`
- `tickets/<tipo>/<id>/timeline.unified_conversation_stream[*].badge`
- `tickets/<tipo>/<id>/timeline.unified_conversation_stream[*].is_unread`
- `market.cart.contact_key`
- `market.cart.channel`
- `market.cart.contacto.email`
- `market.cart.promotions`
- `market.order.contact_key`
- `market.order.channel`
- `admin.catalog[*].price_numeric`
- `admin.catalog[*].channel_availability`

### Regla de UI recomendada
- si `collaboration_state.unread_viewer_count > 0` => mostrar badge de unread
- si `collaboration_state.active_viewers_count > 1` => mostrar “otro agente viendo este ticket”
- cuando llegue `ticket.unread.changed`, reconciliar counters de listas sin refetch completo
- si `effective_presence_status == "idle"` => mostrar presencia atenuada, no como online fuerte
- ordenar listas de inbox/admin por `priority_score` cuando exista
- usar `unified_conversation_stream` como fuente principal en ticket detail/tracking nuevo
- mostrar tooltip/modal de explicabilidad usando `priority_breakdown` y `priority_reasons`
- usar `collaboration_state.operational_status` + `collaboration_state.collaboration_hint` para pintar cabecera del ticket
- mantener continuidad comercial por `contact_key` entre widget / whatsapp / teléfono
- renderizar ahorro/beneficio comercial cuando `market.cart.promotions.total_ahorrado > 0`
- usar `channel_availability` para deshabilitar CTAs no soportados por el producto o canal
