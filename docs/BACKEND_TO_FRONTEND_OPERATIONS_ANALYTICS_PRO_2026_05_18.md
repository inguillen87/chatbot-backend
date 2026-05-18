# Backend to Frontend - Operations Analytics Pro

Fecha: 2026-05-18

## Objetivo

Dejar el dashboard operativo listo para mapas de calor, filtros por segmento, resumen IA y export PDF sin que el frontend invente datos.

## Endpoints principales

```txt
GET /api/v2/analytics/operations/dashboard
GET /api/v2/analytics/operations/heatmap
GET|POST /api/v2/analytics/operations/executive-summary
GET /api/v2/analytics/operations/export.pdf
GET /api/v2/analytics/operations/freshness
```

Todos requieren auth admin/empleado/super_admin y `X-Tenant-Slug`.

## Heatmap

`GET /api/v2/analytics/operations/heatmap` acepta:

- `categoria` / `category`
- `genero` / `gender` / `sexo`
- `edad` / `age`
- `rango_edad` / `age_range`
- `source` / `fuente`
- `channel` / `canal`
- `range=all` para historico completo

El backend devuelve:

- `points`: puntos con coordenadas reales.
- `cells`: celdas agregadas para heatmap.
- `hotspots`: top celdas ordenadas por peso.
- `category_layers`: capas por categoria para prender/apagar en mapa.
- `segments`: categorias, genero, rango_edad, canal y source.
- `location_quality`: cobertura de coordenadas.
- `geocoding.candidates`: tickets con direccion pero sin lat/lng.

Reglas frontend:

- No dibujar un punto si no hay `lat` y `lng`.
- Si `geocoding.candidates` trae items, mostrar cola "pendiente geocodificar".
- No simular coordenadas desde direcciones en el cliente.
- Para completar una direccion, usar el update real del ticket y volver a consultar el heatmap.

## Resumen IA

`GET|POST /api/v2/analytics/operations/executive-summary` devuelve:

```json
{
  "contract_version": "operations.executive_summary.v1",
  "reason_code": "ai_summary_generated",
  "summary": {},
  "ai": {
    "summary": "...",
    "opportunities": [],
    "threats": [],
    "tone": "..."
  },
  "model_policy": {
    "provider": "openai",
    "model_env": "OPENAI_ANALYTICS_MODEL",
    "fallback_behavior": "deterministic_json_when_unavailable"
  }
}
```

Reglas frontend:

- Ejecutar manualmente o despues de refrescar dashboard, no cada 30 segundos.
- Si `reason_code=no_operational_data_in_period`, mostrar estado vacio.
- Si OpenAI no esta configurado, el backend devuelve JSON estable; no mostrar error rojo.

## PDF operativo

`GET /api/v2/analytics/operations/export.pdf` descarga un PDF con:

- resumen operativo,
- tickets,
- cobertura del mapa,
- alertas,
- proximas acciones.

Reglas frontend:

- Usar el endpoint como descarga directa.
- Mostrar `X-Request-Id` en errores o soporte.
- No generar PDF client-side como fuente de verdad.

## QA

1. Abrir dashboard con `X-Tenant-Slug`.
2. Heatmap devuelve `points`, `cells`, `segments`, `category_layers` y `location_quality`.
3. Filtro `?categoria=...&genero=...&edad=...` reduce puntos sin romper segmentos.
4. Direcciones sin coordenadas aparecen en `geocoding.candidates`.
5. Resumen IA devuelve `operations.executive_summary.v1`.
6. Export PDF responde `application/pdf` y `X-Request-Id`.
7. Frontend no inventa coordenadas, resumen IA ni PDF local cuando backend no valida datos.
