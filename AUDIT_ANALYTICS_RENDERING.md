# AUDIT ANALYTICS RENDERING (Backend contract)

## Hallazgos
- Hay datos para analytics, pero render incompleto en frontend (charts/maps).
- Faltaba control para excluir datos demo del análisis real cuando se inyectan respuestas sintéticas.

## Cambios aplicados
1. Se marcó seed demo con metadata estructurada en cada `EncRespuesta`:
   - `is_demo_seed: true`
   - `demo_batch_id`
   - `demo_scenario`
2. Se agregaron filtros en analytics para controlar inclusión demo:
   - `include_demo`
   - `exclude_demo`
3. Se aceptan esos filtros desde `routes/encuestas_analytics.py`.

## Impacto esperado
- Dashboards ejecutivos pueden excluir demo/test y mostrar operación real.
- Mejor trazabilidad de lotes de demo.
