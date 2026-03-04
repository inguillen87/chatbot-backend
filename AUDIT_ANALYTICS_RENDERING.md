# AUDIT ANALYTICS RENDERING (Backend contracts)

## Causa raíz por endpoint/servicio

### `GET /analytics/geo/heatmap` y `GET /analytics/geo/points` (`routes/analytics.py`, `services/analytics/service.py`)
- Frontend no tenía señales robustas de readiness/fallback para provider de mapas.
- Había respuestas con datos pero sin contrato explícito de render para degradar bien.

### `GET /admin/encuestas/:id/analytics/dashboard` y `.../heatmap` (`routes/encuestas_analytics.py`, `services/encuestas_analytics_service.py`)
- Faltaba una jerarquía declarativa de engines/chart-map para evitar decisiones ambiguas en FE.
- Observabilidad insuficiente para rastrear módulos parcialmente rotos (ej. latest responses/heatmap).

## Mejoras aplicadas
1. `meta.map` enriquecido con `available_providers`, `provider_aliases`, `render_ready`, `render_reason`.
2. `render_contract` en geo endpoints (`heatmap`/`points`) con `state`, `module`, `source_keys`.
3. `frontend_render_contract` en dashboard de encuestas con jerarquía explícita:
   - charts: `echarts -> recharts -> plotly`
   - mapas: `preferred -> maplibre -> google`
4. `render_contract` también en `get_heatmap` de encuestas (dataset key/fallback).
5. `X-Request-Id` + `Server-Timing` + logs estructurados en heatmap/dashboard de encuestas.

## Resultado esperado
- Menos pantallas en blanco por decisiones de render inconsistentes.
- Diagnóstico más rápido en producción por request-id y timing por endpoint.
