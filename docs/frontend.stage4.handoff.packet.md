# Frontend Stage 4 Handoff Packet (abril 2026)

> Documento corto para “pasarlo a frontend” con foco en integración inmediata.

## 1) Qué cambió en backend y FE debe consumir ya

1. **Identidad omnicanal en headers**
   - Responses pueden incluir:
     - `X-Contact-Key`
     - `X-Conversation-Id`
   - FE debe persistir por tenant y reinyectar en requests críticas.

2. **Analytics coverage endpoint**
   - `GET /analytics/identity/coverage`
   - Campos clave de UI:
     - `contract_version`
     - `coverage_pct`
     - `slo_status` (`ok` | `below_target`)
     - `alerts` (array)
     - `alert_count` (int)

3. **Analytics ingest ack**
   - `POST /analytics/event`
   - Campos clave de integración:
     - `contract_version` (`analytics.event_ingest.v1`)
     - `contact_key`
     - `conversation_id`
     - `identity_source`

3.1 **Analytics event schema (catálogo canónico)**
   - `GET /analytics/event/schema?tenant_id=<id>`
   - FE puede tomar de ahí:
     - `canonical_events`
     - `required_dimensions`
     - `recommended_dimensions`

4. **WhatsApp funnel admin**
   - `GET /admin/analytics/whatsapp-funnel`
   - Campos mínimos por etapa:
     - `event_name`
     - `label`
     - `sessions`
     - `unique_contacts`
     - `conversion_from_prev_pct`
   - Campo obligatorio payload:
     - `contract_version`

5. **RBAC v1 compartido**
   - Referencia: `docs/rbac.capability_matrix.v1.md`
   - FE-07 debe mapear `requiredCapabilities` por pantalla contra esa matriz.

6. **Widget auth contracts (nuevo)**
   - `GET /auth/widget/bootstrap` incluye `contract_version: auth.widget_bootstrap.v1`.
   - `jwks.alg` y `jwks.kid` en bootstrap permiten al FE detectar estrategia de firma activa.
   - `POST /auth/widget-token` y `POST /auth/widget-refresh` incluyen `contract_version: auth.widget_token.v1`.

7. **Tracking público de reclamos (nuevo)**
   - `GET /tickets/public/status?code=<M-...>&pin=<...>`
   - Contrato: `tickets.public_status.v1`
   - Respuesta acotada para tracking público (estado, categoría, timestamps).

---

## 2) Tipos TS sugeridos (copiar/pegar)

```ts
export type SloStatus = 'ok' | 'below_target';

export interface IdentityCoverageAlert {
  channel: string;
  coverage_pct: number;
  target_pct: number;
  gap_pct: number;
  severity: 'low' | 'medium' | 'high';
}

export interface IdentityCoverageResponseV1 {
  contract_version: 'analytics.identity_coverage.v1';
  tenant_id: number | null;
  coverage_pct: number;
  slo_status: SloStatus;
  alert_count: number;
  alerts: IdentityCoverageAlert[];
}

export interface WhatsappFunnelStageV1 {
  event_name: string;
  label: string;
  sessions: number;
  unique_contacts: number;
  conversion_from_prev_pct: number | null;
}

export interface WhatsappFunnelResponseV1 {
  contract_version: string;
  tenant_id: number | null;
  scope: string;
  window_minutes: number;
  stages: WhatsappFunnelStageV1[];
}

export interface AnalyticsEventIngestAckV1 {
  ok: true;
  contract_version: 'analytics.event_ingest.v1';
  tenant_id: number;
  event_name: string;
  contact_key?: string;
  conversation_id?: string;
  identity_source?: string;
}

export interface WidgetBootstrapV1 {
  contract_version: 'auth.widget_bootstrap.v1';
  tenant: { id: number; slug: string };
  widget: { token_cookie_name: string; access_minutes: number; renew_days: number };
  jwks: { url?: string };
}

export interface WidgetTokenAckV1 {
  contract_version: 'auth.widget_token.v1';
  token: string;
  expires_in: number;
}

export interface AnalyticsEventSchemaV1 {
  contract_version: 'analytics.event_schema.v1';
  tenant_id: number;
  required_dimensions: string[];
  recommended_dimensions: string[];
  canonical_events: string[];
}

export interface PublicTicketStatusV1 {
  contract_version: 'tickets.public_status.v1';
  ticket: {
    nro_ticket: string;
    estado: string;
    categoria?: string;
    subcategoria?: string;
    canal_ingreso?: string;
    fecha_creacion?: string | null;
    ultima_actualizacion?: string | null;
  };
}
```

---

## 3) Criterios de aceptación FE (sprint actual)

- Requests críticas salen con `X-Contact-Key` cuando exista identidad local.
- Si existe `X-Conversation-Id`, se reinyecta en market/tickets/encuestas/analytics.
- Dashboard de coverage muestra banner cuando `alert_count > 0`.
- Ingest de analytics valida `contract_version === 'analytics.event_ingest.v1'`.
- Configurador de eventos valida catálogo desde `/analytics/event/schema`.
- Vista funnel valida `contract_version` antes de renderizar.
- Pantallas con permisos usan `requiredCapabilities` alineado a RBAC v1.

---

## 4) QA manual mínimo (antes de merge FE)

1. **Tenant municipio**
   - Coverage carga y muestra `slo_status` correcto.
   - Funnel muestra `unique_contacts` y no rompe ante `conversion_from_prev_pct = null`.

2. **Tenant pyme**
   - Continuidad de headers en flujo market + pedido.
   - Denegaciones RBAC caen en pantalla 403 usable.

3. **Usuario nuevo sin identidad previa**
   - App degrada elegante sin crash.
   - Al primer response con headers, FE persiste y reinyecta correctamente.

---

## 5) Entrega sugerida al equipo frontend

- Compartir este archivo + `FRONTEND_HANDOFF_CRM_ROADMAP.md`.
- Adjuntar ejemplos de payload:
  - base de referencia: `docs/frontend.stage4.payload_examples.md`
  - adicional: payload real de ambos endpoints desde entorno staging.
- Abrir ticket FE por bloque:
  1. `identity-headers-propagation`
  2. `analytics-coverage-ui`
  3. `analytics-event-ingest-ack-v1`
  4. `analytics-event-schema-v1`
  5. `whatsapp-funnel-contract-v1`
  6. `rbac-required-capabilities-alignment`
