# Frontend Contract — Analytics Hub Unificado

Objetivo: reducir fragmentación de UX/UI en `/analytics` y evitar pantallas colgadas por múltiples llamadas paralelas.

## Endpoint recomendado (nuevo)
- `GET /admin/analytics/hub`
- Alias: `GET /admin/analytics/dashboard`
- Alias API: `GET /api/admin/analytics/hub`, `GET /api/admin/analytics/dashboard`

## Query params
- `tenant_id` (opcional si backend puede inferirlo por sesión/contexto)
- `tenant_slug|tenant` (opcional)
- `scope=municipio|pyme|operaciones`
- `from`, `to`
- filtros estándar (`categoria`, `canal`, `estado`, `bbox`, etc.)

## Respuesta
```json
{
  "tenant_id": "12",
  "scope": "municipio",
  "period": {"from": null, "to": null},
  "sections": {
    "general": {...},
    "municipio": {...},
    "ventas": {...},
    "mapas": {"geo": {...}}
  },
  "navigation": {
    "primary": [
      {"key": "analytics", "path": "/analytics", "active": true},
      {"key": "estadisticas", "path": "/estadisticas", "active": false},
      {"key": "encuestas", "path": "/admin/encuestas", "active": false}
    ],
    "encuestas": {
      "admin_list_endpoint": "/api/admin/encuestas",
      "templates_endpoint": "/api/admin/encuestas/templates",
      "seed_demo_endpoint_template": "/api/admin/encuestas/{encuesta_id}/seed-demo",
      "public_results_endpoint_template": "/api/public/encuestas/{slug}/live-results"
    }
  }
}
```

## Performance
- Respuesta incluye `ETag` + `Cache-Control: private, max-age=20`.
- Frontend debe enviar `If-None-Match` para polling/refetch y aprovechar `304`.

## UX recomendada
1. Cargar solo `hub` al entrar a `/analytics`.
2. Render tabs desde `sections` sin hacer 4 requests iniciales.
3. Mostrar CTA de seed demo de encuestas usando `seed_demo_endpoint_template`.
4. Fallback de tenant: si no hay `tenant_id`, intentar con `tenant_slug`; backend ahora infiere por contexto en la mayoría de flujos autenticados.


## Observability y SLA UI
- Respuesta incluye `meta.contract_version`, `meta.generated_at`, `meta.request_id`, `meta.cache` para trazabilidad de UX.
- Headers: `X-Analytics-Request-Id` y `X-Analytics-Contract-Version` para correlación entre frontend, backend y logs.
- Frontend debería propagar `X-Request-Id` en cada request de analytics para debugging enterprise.
- Con `If-None-Match` + `304`, el frontend puede refrescar cada 15-30s sin recargar toda la UI.
