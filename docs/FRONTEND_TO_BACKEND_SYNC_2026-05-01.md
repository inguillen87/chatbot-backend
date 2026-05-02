# Frontend to Backend Sync 2026-05-01

Fecha: 2026-05-01

Este handoff baja a contrato ejecutable los shapes que frontend ya consume para evitar normalizadores defensivos permanentes y textos locales hardcodeados en React.

## Urgente

### Demo session

Endpoint:

`POST /api/v2/demo/session`

Shape esperado:

```json
{
  "workspace": {
    "title": "...",
    "welcome_message": "...",
    "quick_replies": [],
    "value_cards": [],
    "handoff_labels": {}
  }
}
```

### Widget config

Endpoint:

`GET /api/public/tenants/{slug}/widget-config`

Shape esperado:

```json
{
  "contract_version": "public.widget_config.v1",
  "tenant": { "slug": "...", "tipo": "municipio|pyme" },
  "widget": {},
  "builder_config": {},
  "suppress_global_widget": false,
  "integration_preview": false,
  "quick_menu": [
    { "id": "estado_caso", "label": "Estado de caso", "intent": "ticket_status" }
  ]
}
```

### Tickets v2

Endpoint:

`GET /api/v2/tickets`

Shape esperado:

```json
{
  "items": [
    {
      "id": 1,
      "title": "...",
      "status": "nuevo",
      "priority": "alta",
      "sla_status": "ok|warning|breached",
      "channel": "web|whatsapp|widget",
      "category": "...",
      "assignee": {
        "id": 1,
        "name": "..."
      }
    }
  ],
  "request_id": "..."
}
```

### Analytics v2

Endpoint:

`GET /api/v2/analytics/overview`

Shape esperado:

```json
{
  "summary": {
    "conversations": 0,
    "open_tickets": 0,
    "overdue_tickets": 0,
    "response_time": 0,
    "survey_responses": 0,
    "nps": null,
    "csat": null,
    "handoff_rate": 0
  },
  "request_id": "..."
}
```

### Survey draft/offline

Endpoint:

`POST /api/v2/surveys/draft`

Payload incompleto permitido:

```json
{
  "title": "Borrador offline",
  "description": "",
  "questions": [
    {
      "id": "question-1",
      "title": "",
      "type": "single"
    }
  ]
}
```

Ack recomendado:

```json
{
  "ok": true,
  "contract_version": "surveys.draft.v2",
  "request_id": "...",
  "draft_id": "draft_123",
  "status": "draft"
}
```

### PWA tenant resolution

Endpoint canonico:

`GET /api/pwa/public/tenant-info`

Si no resuelve tenant:

```json
{
  "contract_version": "pwa.public_tenant_resolution.v1",
  "status_code": 404,
  "reason_code": "tenant_resolution_failed",
  "retryable": false,
  "action_hint": "send tenant, tenant_slug, endpoint or X-Tenant-Slug",
  "request_id": "...",
  "hints": {
    "query_params": ["tenant", "tenant_slug", "endpoint", "widget_token"],
    "headers": ["X-Tenant-Slug", "X-Tenant", "X-Entity-Token"]
  }
}
```

### Error envelope publico

```json
{
  "error": {
    "code": 400,
    "message": "Mensaje claro"
  },
  "request_id": "..."
}
```

Para encuestas publicas:

```json
{
  "status_code": 403,
  "reason_code": "survey_not_published",
  "retryable": false,
  "action_hint": "view_other_surveys",
  "request_id": "..."
}
```

## Pendientes / blockers

- Presencia real/live events para inbox omnicanal premium.
- Webhooks v2 para confirmar/cancelar pedidos desde Mercado Pago sin pasar por rutas legacy.
- Catalogo administrable completo de recompensas y reglas por segmento.
- Quick menu educativo completo desde backend.
- Hooks de notifications conectados a proveedores reales.
- Idempotency key persistente para toda sync offline.

## Tests frontend relacionados

- `src/api/client.stage4Contracts.test.ts`
- `src/api/tenant.test.ts`
- `src/api/market.test.ts`
- `src/api/education.test.ts`
- `tests/e2e/chatboc-smoke.spec.ts`
- `src/features/demo/demoApi.ts`
- `src/features/tickets/ticketsApi.ts`
- `src/features/analytics/analyticsApi.ts`
- `src/features/surveys/surveysApi.ts`

## Estado backend 2026-05-01

Implementado en backend:

- `GET /api/v2/demo/catalog` con `contract_version: demo.catalog.v2`, `sectors`, `rubros` y `sector_groups`.
- `POST /api/v2/demo/session` con `contract_version: demo.session.v2`, `demo_session_id`, `session_id`, `tenant_slug`, `workspace` y compat top-level.
- `GET /api/public/tenants/{slug}/widget-config` con `public.widget_config.v1`, `tenant`, `widget`, `builder_config.quick_menu`, `quick_menu`, flags de widget y campos legacy.
- `GET /api/v2/tickets` con `tickets.v2.list`, `items`, `request_id`, `sla_status`, `sla_state`, `assignee` y `assignee_name`.
- `GET /api/v2/analytics/overview` con `analytics.overview.v2`, `summary` canonico y numeros JSON.
- `POST /api/v2/surveys/draft` con ack `surveys.draft.v2`, drafts incompletos e idempotency key opcional.
- `GET /api/pwa/public/tenant-info` con success `public.tenant_profile.v1`; error accionable `pwa.public_tenant_resolution.v1`.
- `POST /api/surveys/sync` con `surveys.sync.v1`.
- `POST /api/tickets/draft/sync` con `tickets.draft_sync.v1`.

Verificacion backend ejecutada:

- `tests.test_api_v2_foundation`
- `tests.test_v2_tickets`
- `tests.test_v2_analytics_overview`
- `tests.test_v2_surveys`
- `tests.test_pwa_public_cart_url`
- `tests.test_offline_sync_contracts`

## Estado backend SaaS P1 2026-05-01

Nueva capa v2 canónica para secciones enterprise:

- `GET /api/v2/employee-coverage`
- `GET /api/v2/tenants/{slug}/employee-coverage`
- `GET /api/v2/tenant-health`
- `GET /api/v2/tenants/{slug}/health`
- `GET /api/v2/superadmin/executive-summary`
- `GET /api/v2/super-admin/executive-summary`
- `GET|POST /api/v2/notifications/hooks`
- `GET /api/v2/notifications/delivery-status`
- `GET /api/v2/inbox/omnichannel`

Contratos:

- `employee.coverage.v1`: empleados, scope, workload, coverage por categorias/zonas/canales y alertas.
- `tenant.health.v1`: score, status, checks, integraciones, colas, errores recientes y acciones recomendadas.
- `superadmin.executive_summary.v1`: KPIs multi-tenant, health por tenant y top risky tenants.
- `notifications.hooks.v1`: preferences, triggers, delivery config, templates y delivery_status.
- `notifications.delivery_status.v1`: totales por estado/canal y success rate.
- `inbox.omnichannel.v1`: lista omnicanal, timeline, presence basica y acciones.

Verificacion backend SaaS P1:

- `tests.test_v2_saas_contracts`

## Estado backend SaaS P2 2026-05-01

Nueva capa v2 para comercio, puntos e inbox accionable:

Nota de arquitectura backend: estos endpoints v2 son fachadas de contrato. La logica reusable queda en `services/commerce_contracts.py` y `services/rewards.py`, apoyada en modelos/servicios existentes, para no duplicar una app paralela ni deprecar el flujo actual.

- `GET /api/v2/payments/checkout-status`
- `GET /api/v2/payments/capabilities`
- `GET /api/v2/tenants/{slug}/payments/checkout-status`
- `POST /api/v2/payments/checkout-preview`
- `POST /api/v2/tenants/{slug}/payments/checkout-preview`
- `POST /api/v2/payments/checkout-session`
- `POST /api/v2/payments/preference`
- `POST /api/v2/tenants/{slug}/payments/checkout-session`
- `GET|POST /api/v2/payments/status`
- `GET|POST /api/v2/tenants/{slug}/payments/status`
- `GET /api/v2/rewards/profile`
- `GET /api/v2/tenants/{slug}/rewards/profile`
- `POST /api/v2/rewards/redeem`
- `POST /api/v2/tenants/{slug}/rewards/redeem`
- `POST /api/v2/inbox/omnichannel/{ticket_id}/actions`
- `POST /api/v2/inbox/omnichannel/actions`

Contratos:

- `payments.checkout_status.v1`: gateway, `payment_ready`, `mercadopago_ready`, capabilities, faltantes y URLs publicas de checkout/catalogo.
- `payments.checkout_preview.v1`: items normalizados, totales monetarios/puntos, `payment_required`, `payment_ready`, `contact_ready`, `checkout_options` y next steps.
- `payments.checkout_session.v1`: crea preference real de Mercado Pago con token por tenant y devuelve `preference_id`, `init_point`, `external_reference`, `checkout_options` y `request_id`.
- `payments.status.v1`: consulta estado post-pago por `pedido_id`, `market_order_id`, `preference_id` o `external_reference`; devuelve pago, orden, timeline y `request_id`.
- `rewards.profile.v1`: wallet real del usuario, reglas por tenant, beneficios disponibles/canjeables e historial de puntos.
- `rewards.redeem.v1`: canje real de puntos con `redemption_id`, balance actualizado e idempotencia por `Idempotency-Key`.
- `inbox.omnichannel.action.v1`: acciones `assign`, `reply`, `handoff`, `close`, `reopen` y `set_priority` con ticket actualizado y timeline.

Verificacion backend SaaS P2:

- `tests.test_v2_commerce_contracts`
- `tests.test_v2_saas_contracts`
