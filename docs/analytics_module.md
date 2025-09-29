# Módulo de Analítica y CRM

Este módulo incorpora tableros avanzados para municipios, PyMEs y equipos de operaciones sin modificar los endpoints existentes. Se entrega como un paquete autocontenido con API REST (`/analytics/*`), vistas web y jobs de pre-cómputo.

## Características

- **Dashboards dedicados**: Municipio, PyME y Operaciones comparten filtros globales (fecha, canal, categoría, estado, agente, zona, bbox) y exportaciones CSV/PNG.
- **Mapas interactivos**: heatmap + clusters con selección mediante rectángulo que aplica filtros cruzados a todos los widgets.
- **KPIs listos para chart**: series temporales, breakdowns, tablas top-N, métricas SLA (P50/P90/P95) y cohortes de recurrencia.
- **Multitenancy y RBAC**: acceso protegido por `tenant_id` y roles (`admin`, `operador`, `visor`). El módulo reutiliza el viewer de `g.viewer` y expone encabezados `X-Debug-*` en modo testing.
- **Cache TTL configurable**: respuestas cacheadas en memoria 10 minutos por combinación de filtros (se puede ajustar vía `ANALYTICS_CACHE_TTL`).
- **Pre-cómputo nocturno**: el job `services.analytics.jobs.rebuild_analytics_snapshot` calcula tablas agregadas diarias, métricas geográficas, top-N, cohortes y rendimiento de plantillas WhatsApp.
- **Observabilidad**: endpoint `/analytics/health` con métricas de cache y estado de jobs. Cada consulta se loguea con filtros aplicados.
- **Feature flag**: habilitar/deshabilitar con `ANALYTICS_ENABLED`.

## Endpoints

| Ruta | Descripción |
|------|-------------|
| `GET /analytics/summary` | KPIs principales (tiles + SLA) según scope. |
| `GET /analytics/timeseries` | Series temporales por día; acepta `metric` y `group`. |
| `GET /analytics/breakdown` | Desglose por categoría/canal/estado. |
| `GET /analytics/geo/heatmap` | Celdas H3 con conteos y centroides. |
| `GET /analytics/geo/points` | Muestra de puntos para zoom alto. |
| `GET /analytics/top` | Top-N (barrios, calles, productos, plantillas). |
| `GET /analytics/operations` | Métricas de colas, aging, agentes, SLA. |
| `GET /analytics/cohorts` | Cohortes de recurrencia (PyME). |
| `GET /analytics/whatsapp/templates` | KPI de plantillas (envíos, entregas, CTR, bloqueos). |
| `GET /analytics/health` | Estado del módulo (cache, jobs, último snapshot). |
| `GET /analytics/ui` | Dashboard web responsive con modo oscuro. |

Todos los endpoints requieren `tenant_id` y respetan los filtros opcionales: `from`, `to`, `canal`, `categoria`, `estado`, `agente`, `zona`, `etiqueta`, `bbox`, `pyme`, `resolution`.

## Jobs nocturnos

```python
from services.analytics.jobs import rebuild_analytics_snapshot

# recomputar últimos 7 días para tenant 42 (municipio)
rebuild_analytics_snapshot('42', 'municipio')
```

Los resultados se almacenan en las tablas:

- `analytics_daily_metric`
- `analytics_geo_cell`
- `analytics_top_metric`
- `analytics_cohort_metric`
- `analytics_whatsapp_template`
- `analytics_module_status`

## Datos de prueba

El script `scripts/analytics_seed.py` genera 100k tickets y pedidos distribuidos por municipios/PyMEs, incluyendo adjuntos, comentarios, encuestas y eventos de plantillas. Útil para pruebas de performance y UX.

```bash
python scripts/analytics_seed.py --tenant 42 --days 60 --tickets 100000
```

## Extender el módulo

1. **Nuevas métricas**: crear una función en `services/analytics/service.py` y exponerla desde la ruta correspondiente. Guardar agregados en `services/analytics/jobs.py`.
2. **Nuevos widgets UI**: añadir tarjetas en `templates/analytics/dashboard.html` y lógica en `static/analytics/dashboard.js` (los datasets se renderizan mediante Chart.js y Leaflet).
3. **Cache**: ajustar TTL o tamaño con `ANALYTICS_CACHE_TTL` y `ANALYTICS_CACHE_MAX_ITEMS`.
4. **RBAC**: ampliar controles en `services/analytics/rbac.py`.

## Recomendaciones de despliegue

- Activar la flag `ANALYTICS_ENABLED=true` y configurar cron para `rebuild_analytics_snapshot`.
- Asegurar índices por `tenant_id`, `fecha`, `estado`, `categoria`, `cell_id` (la migración incluida los crea).
- Configurar logs en nivel INFO para monitorear latencia y filtros de acceso.

## Troubleshooting

- **401/403**: revisar que el usuario tenga rol permitido y `tenant_id` coincida.
- **Mapa vacío**: confirmar que existan lat/lon en los tickets o pedidos; el fallback de puntos se limita a 1000 registros.
- **Tiempo de respuesta >3s**: verificar que el job nocturno esté poblado; de lo contrario se usa modo on-the-fly que puede ser más pesado.

