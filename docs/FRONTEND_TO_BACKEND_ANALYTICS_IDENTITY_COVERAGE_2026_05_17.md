# Frontend to Backend - Analytics Identity Coverage

Fecha: 2026-05-17

## Objetivo

Evitar que las pantallas de admin, encuestas, perfil y reportes queden cargando por fallas en la metrica auxiliar de cobertura de identidad.

## Ruido de consola que no corresponde a Chatboc

Estos mensajes vienen de extensiones del navegador y no deben tratarse como bugs de la app:

- `SES Removing unpermitted intrinsics`
- `No matching tab found`
- `Cannot assign to read only property 'ethereum'`
- `Disconnected from polkadot{.js}`
- warnings de `feature_collector`

El bug real observado fue:

```txt
GET /api/analytics/identity/coverage?emit_alert_events=1&tenant_slug=junin-1&tenant=junin-1 -> 500
```

## Contrato backend actual

Endpoint de lectura:

```txt
GET /api/analytics/identity/coverage?tenant_slug={tenant_slug}&tenant={tenant_slug}
```

Respuesta normal:

```json
{
  "contract_version": "analytics.identity_coverage.v1",
  "request_id": "req_...",
  "tenant_id": "10",
  "event_tenant_id": 77,
  "tenant_resolution": {
    "tenant_slug": "junin-1",
    "tenant_profile_id": 77,
    "owner_tenant_id": 10
  },
  "total_events": 120,
  "events_with_identity": 96,
  "coverage_pct": 80,
  "channels": {},
  "data_status": "ok",
  "warnings": []
}
```

Respuesta degradada soportada:

```json
{
  "contract_version": "analytics.identity_coverage.v1",
  "request_id": "req_...",
  "total_events": 0,
  "events_with_identity": 0,
  "coverage_pct": 0,
  "channels": {},
  "data_status": "degraded",
  "warnings": [
    {
      "code": "identity_coverage_query_failed",
      "message": "No se pudo leer la cobertura de identidad; se devuelve una muestra vacia para no bloquear el panel."
    }
  ]
}
```

Regla frontend: si `data_status === "degraded"`, mostrar estado no bloqueante y mantener el resto del panel usable.

## Uso correcto de `emit_alert_events`

No usar `emit_alert_events=1` en cargas normales de pagina, polling, tabs de perfil, encuestas o dashboard.

`emit_alert_events=1` dispara una operacion de escritura de alerta y requiere permiso `analytics.admin`. Debe usarse solo en:

- accion explicita de admin,
- job backend,
- tarea de monitoreo interno.

Lectura normal:

```txt
/api/analytics/identity/coverage?tenant_slug=junin-1&tenant=junin-1
```

Accion admin explicita:

```txt
/api/analytics/identity/coverage?tenant_slug=junin-1&tenant=junin-1&emit_alert_events=1
```

## Reglas UX

- No hacer retry infinito si coverage devuelve `403`, `404`, `429`, `500` o `503`.
- Aplicar backoff o desactivar polling hasta cambio de tenant/tab.
- No bloquear `admin/encuestas`, `perfil`, tickets ni modulos principales por esta metrica.
- Mostrar skeleton solo mientras la primera request esta pendiente.
- Si falla, mostrar una nota compacta: `Cobertura no disponible por ahora`.
- Mantener `request_id` visible en logs de frontend para soporte.

## QA compartida

1. Abrir `/admin/encuestas` con tenant `junin-1`.
2. Confirmar que frontend no llama `emit_alert_events=1` en carga normal.
3. Confirmar que una respuesta `data_status=degraded` no deja spinner infinito.
4. Confirmar que el panel sigue mostrando encuestas/perfil/tickets aunque coverage falle.
5. Confirmar que `emit_alert_events=1` solo se dispara desde accion admin o job backend.
