# AUDIT DEMO INJECTION (Backend)

## Hallazgos
- El flujo de “inyectar 100 demo” chocaba conceptualmente con endpoint público de respuestas (dedupe/idempotencia/antifraude).

## Cambios aplicados
1. Endpoint admin dedicado:
   - `POST /admin/encuestas/<encuesta_id>/seed-demo/bulk`
   - acepta `count` como alias de `cantidad`
2. Reutilización de lógica vía `_run_seed_demo` para evitar duplicación.
3. Public responder duplicado (`409` -> `200`) devuelve guía explícita:
   - `suggested_admin_endpoint_template: /admin/encuestas/{encuesta_id}/seed-demo/bulk`
4. Seed demo ahora etiqueta respuestas con metadata de lote para trazabilidad y futura exclusión analítica.

## Impacto esperado
- Menos conflictos 409 en flujo de demos masivos.
- Separación correcta entre público real y seeding operativo.
