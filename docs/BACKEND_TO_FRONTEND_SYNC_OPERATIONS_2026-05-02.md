# Backend to Frontend Sync - Operations 2026-05-02

Fecha: 2026-05-02

Objetivo: dar al frontend una capa operativa unica para tablero ejecutivo, tickets/reclamos, WhatsApp, chats en vivo, empleados, encuestas/votaciones y mapas de calor. Es aditiva: no reemplaza rutas legacy ni endpoints especificos ya existentes.

## Backend listo

### Operational dashboard

Endpoint:

`GET /api/v2/analytics/operations/dashboard`

Alias:

`GET /api/v2/analytics/operations`

Auth:

- `Authorization: Bearer ...`
- `X-Tenant-Slug: {tenant}`
- Roles: `admin`, `empleado`, `super_admin`

Query params:

- `from`: ISO datetime opcional
- `to`: ISO datetime opcional

Contrato:

```json
{
  "contract_version": "operations.dashboard.v1",
  "tenant": {},
  "period": { "from": "...", "to": "..." },
  "summary": {
    "open_tickets": 0,
    "overdue_tickets": 0,
    "survey_responses": 0,
    "live_votes": 0,
    "chat_messages": 0,
    "whatsapp_messages": 0,
    "employees": 0,
    "heatmap_points": 0,
    "alerts": 0
  },
  "previous_summary": {},
  "trends": {
    "contract_version": "operations.trends.v1",
    "items": []
  },
  "tickets": {},
  "surveys": {},
  "chats": {},
  "live_chat": {},
  "employees": {},
  "maps": {},
  "alerts": [],
  "next_best_actions": [],
  "frontend_contract": {}
}
```

Fuentes agregadas:

- `TenantTicket`
- `MunicipioTicket`
- `PymeTicket`
- `AnalyticsEventV2`
- `ChatSessionContext`
- `TicketRealtimeState`
- `EncEncuesta`
- `EncRespuesta`
- `PublicSurvey`
- `PublicSurveyResponse`
- `User` empleados

### Operational heatmap

Endpoint:

`GET /api/v2/analytics/operations/heatmap`

Contrato:

```json
{
  "contract_version": "operations.heatmap.v1",
  "tenant": {},
  "period": {},
  "render_contract": {
    "state": "ready",
    "map_engine": "maplibre",
    "layers": ["tickets", "surveys", "analytics_events"],
    "point_format": { "lat": "number", "lng": "number", "weight": "number" }
  },
  "summary": {
    "points": 0,
    "cells": 0,
    "ticket_points": 0,
    "survey_points": 0,
    "event_points": 0
  },
  "bounds": {},
  "points": [],
  "cells": [],
  "hotspots": []
}
```

### Action center

Endpoint:

`GET /api/v2/analytics/operations/action-center`

Contrato:

```json
{
  "contract_version": "operations.action_center.v1",
  "tenant": {},
  "period": {},
  "summary": {
    "total": 0,
    "high": 0,
    "medium": 0,
    "low": 0,
    "alerts": 0
  },
  "items": [
    {
      "id": "review_overdue_tickets",
      "title": "Revisar tickets vencidos",
      "description": "Priorizar reclamos y tickets con SLA vencido antes de que escalen.",
      "priority": "high",
      "reason_code": "tickets_overdue",
      "endpoint": "/api/v2/tickets?status=overdue",
      "method": "GET",
      "payload_template": {},
      "ui_hint": "open_view"
    }
  ],
  "alerts": [],
  "trends": {},
  "frontend_contract": {
    "render_as": "action_center",
    "primary_refresh_seconds": 30,
    "empty_state_behavior": "show_monitoring_ok"
  }
}
```

Acciones posibles:

- `review_overdue_tickets`
- `assign_unassigned_tickets`
- `improve_employee_coverage`
- `promote_live_vote`
- `monitor_whatsapp_claims`
- `review_high_handoff_rate`
- `inspect_top_hotspot`
- `keep_monitoring`

### Operational freshness

Endpoint:

`GET /api/v2/analytics/operations/freshness`

Objetivo:

Indicarle al frontend si cada fuente operativa tiene datos frescos, stale o vacios. Esto evita mapas rotos, dashboards con ceros ambiguos y estados UX pobres cuando la plataforma no recibio eventos recientes.

Contrato:

```json
{
  "contract_version": "operations.freshness.v1",
  "tenant": {},
  "period": {},
  "status": "fresh|degraded|empty",
  "reason_code": "all_sources_fresh|one_or_more_sources_stale|no_operational_data_in_period",
  "summary": {
    "sources": 5,
    "fresh_sources": 0,
    "stale_sources": 0,
    "empty_sources": 0,
    "latest_at": "...",
    "employee_count": 0,
    "has_operational_data": true,
    "can_render_dashboard": true,
    "can_render_heatmap": true
  },
  "sources": [
    {
      "key": "tickets",
      "label": "Tickets y reclamos",
      "status": "fresh",
      "reason_code": "source_fresh",
      "period_count": 0,
      "latest_at": "...",
      "age_seconds": 0,
      "stale_after_seconds": 21600,
      "recommended_action": {
        "endpoint": "/api/v2/tickets",
        "ui_hint": "open_ticket_board"
      }
    }
  ],
  "frontend_contract": {
    "render_as": "analytics_freshness",
    "primary_refresh_seconds": 60,
    "empty_state_behavior": "show_reason_code",
    "degraded_state_behavior": "show_stale_sources"
  }
}
```

Fuentes:

- `tickets`
- `surveys`
- `analytics_events`
- `chats`
- `heatmap`

## Frontend recomendado

1. Crear una vista `OperationsDashboard` o reforzar analytics existente con `operations.dashboard.v1`.
2. Mostrar KPIs de `summary` arriba: abiertos, vencidos, respuestas, votos live, WhatsApp, empleados, puntos mapa y alertas.
3. Mostrar variaciones con `trends.items`: current, previous, direction, percent_change.
4. Renderizar `next_best_actions` como una barra o panel de acciones prioritarias.
5. Reutilizar cards existentes para `tickets.by_status`, `tickets.by_channel`, `tickets.by_category` y `tickets.by_priority`.
6. En encuestas/votaciones, mostrar `surveys.summary.votaciones_live`, `surveys.summary.responses` y `surveys.live_items`.
7. Para WhatsApp/live chat, usar `chats.summary.whatsapp_messages`, `chats.by_channel`, `live_chat.active_viewers` y `live_chat.items`.
8. Para empleados, mostrar `employees.items`, `employees.summary.coverage_rate`, `employees.coverage.uncovered_categories` y `employees.coverage.uncovered_channels`.
9. Para mapas, usar `maps.heatmap.hotspots` en dashboard y `/operations/heatmap` para el mapa completo.
10. Renderizar capas separables: tickets, surveys y analytics_events.
11. Si `render_contract.state` es `empty`, mostrar estado vacio accionable, no un mapa roto.
12. Mostrar `alerts` arriba del tablero con severidad y `reason_code`.
13. Crear un `ActionCenter` consumiendo `/operations/action-center`; cada item trae endpoint, method, payload_template y ui_hint.
14. Respetar `frontend_contract.primary_refresh_seconds` para auto-refresh.
15. No hardcodear nombres de categorias/canales/estados; usar labels del backend.
16. Usar `/operations/freshness` para mostrar banner/chip de frescura y distinguir `empty` real de `degraded`.
17. Si `summary.can_render_heatmap` es false, mostrar estado vacio accionable antes que mapa sin datos.

## Notas

- El endpoint no crea una app paralela: agrega y normaliza datos existentes.
- El mapa usa puntos reales cuando hay lat/lng en tickets, respuestas de encuestas o eventos.
- `trends` compara contra el periodo anterior del mismo tamano.
- `next_best_actions` y `action-center` son proactivos: no ejecutan cambios solos, pero guian al operador.
- El payload mantiene `request_id` y `X-Request-Id` desde el envelope v2.

## Tests backend verdes

- `tests.test_v2_operational_analytics`
- `tests.test_v2_analytics_overview`
- `tests.test_v2_saas_contracts`
- `tests.test_v2_tickets`
- `tests.test_v2_surveys`
- `tests.test_encuestas_dashboard_bundle_contract`
- `tests.test_encuestas_heatmap_fallback`
- `tests.test_estadisticas_heatmap`
- `tests.test_municipal_tickets_map_data`
- `tests.test_ticket_realtime_state`
