# Backend to Frontend Sync - Chat Bootstrap + Freshness 2026-05-02

Fecha: 2026-05-02

Objetivo: cerrar dos huecos detectados al leer los MD recientes sin crear una app paralela:

1. Elegir rubro debe iniciar el chat correcto sin inferencias locales.
2. Analytics/mapas deben poder mostrar estado fresco, stale o vacio de forma profesional.
3. La captura de lead debe cerrar la demo con un objeto operativo trazable.

## Backend listo

### Demo chat bootstrap

Endpoint:

`POST /api/v2/demo/session`

Campos nuevos aditivos:

- `chat_bootstrap` top-level.
- `workspace.chat_bootstrap`.
- `chat_seed.chat_bootstrap`.

Contrato:

```json
{
  "contract_version": "demo.chat_bootstrap.v1",
  "endpoint": "/ask/pyme",
  "fallback_endpoint": "/ask",
  "method": "POST",
  "headers": {
    "X-Chat-Session-Id": "demo_session_id",
    "X-Demo-Session-Id": "demo_session_id",
    "X-Tenant-Slug": "tenant-slug"
  },
  "query": {
    "tenant_slug": "tenant-slug"
  },
  "payload": {
    "pregunta": "",
    "tipo_chat": "pyme",
    "tenant_slug": "tenant-slug",
    "rubro": "bodega",
    "rubro_clave": "bodega",
    "demo_session_id": "demo_session_id",
    "demo_mode": true
  },
  "supports": {
    "text": true,
    "image": true,
    "audio": true,
    "location": true,
    "file": true
  }
}
```

Regla frontend:

- Flujo canonico: seleccionar sector/rubro -> `POST /api/v2/demo/session` -> abrir el chat usando `chat_bootstrap.endpoint`, `headers`, `query` y `payload`.
- Para `tenant_tipo=municipio`, backend devuelve `/ask/municipio`; para `tenant_tipo=pyme`, devuelve `/ask/pyme`.
- No reconstruir en React `tipo_chat`, `rubro`, `tenant_slug` ni `X-Chat-Session-Id`.
- Imagen/archivo/audio/ubicacion siguen usando `media_capabilities` y las rutas existentes: `/archivos/upload/chat_attachment` y `/ask*`.

### Operational freshness

Endpoint:

`GET /api/v2/analytics/operations/freshness`

Contrato:

```json
{
  "contract_version": "operations.freshness.v1",
  "status": "fresh|degraded|empty",
  "reason_code": "all_sources_fresh|one_or_more_sources_stale|no_operational_data_in_period",
  "summary": {
    "has_operational_data": true,
    "can_render_dashboard": true,
    "can_render_heatmap": true,
    "fresh_sources": 3,
    "stale_sources": 1,
    "empty_sources": 1
  },
  "sources": [
    {
      "key": "tickets",
      "label": "Tickets y reclamos",
      "status": "fresh",
      "reason_code": "source_fresh",
      "period_count": 1,
      "latest_at": "2026-05-02T12:00:00+00:00",
      "age_seconds": 30,
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

Fuentes actuales:

- `tickets`
- `surveys`
- `analytics_events`
- `chats`
- `heatmap`

### Public lead capture

Endpoint:

`POST /api/public/lead-capture`

Contrato:

```json
{
  "ok": true,
  "contract_version": "public.lead_capture.v1",
  "request_id": "req_123",
  "tenant": {
    "slug": "tenant-slug",
    "tipo": "pyme"
  },
  "lead_id": "lead_123",
  "ticket_id": 123,
  "ticket_type": "tenant_ticket",
  "status": "nuevo",
  "deduplicated": false,
  "idempotency_key": "lead-key-1",
  "message_body": "Gracias...",
  "next_actions": [
    { "id": "open_lead", "label": "Abrir lead", "endpoint": "/api/v2/tickets/123" }
  ]
}
```

Backend persiste:

- `User` anon/contacto.
- `ChatSessionContext.context_data.lead_profile`.
- `TenantTicket` con `categoria=lead_capture`, `fingerprint=idempotency_key`, `lead_stage`, `lead_score`, `lead_profile` y `lead_timeline`.
- `AnalyticsEventV2` con `event_name=lead_capture_created`.

## Tareas frontend recomendadas

1. Demo/landing: reemplazar inferencias locales de endpoint/payload por `chat_bootstrap`.
2. Demo/landing: cuando el usuario elige rubro, conservar `X-Chat-Session-Id` de `chat_bootstrap.headers`.
3. Chat widget/demo: usar `chat_bootstrap.supports` junto con `media_capabilities` para habilitar texto, imagen, audio, ubicacion y archivo.
4. Lead capture: enviar `tenant_slug`, `chat_session_id`, `anon_id`, `channel/source`, `trigger/intent` y header `Idempotency-Key`.
5. Lead capture: si `deduplicated=true`, mostrar success igual; no tratarlo como error.
6. Analytics: llamar `/operations/freshness` antes o junto al dashboard para mostrar chip de frescura.
7. Mapas: si `summary.can_render_heatmap` es false, mostrar estado vacio accionable y no un mapa roto.
8. Analytics: si `status=degraded`, mostrar fuentes stale con `sources[].recommended_action`.

## Tests backend verdes

- `tests.test_api_v2_foundation`
- `tests.test_v2_operational_analytics`
- `tests/test_public_lead_capture.py`
