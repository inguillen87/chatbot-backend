# AUDIT DEMO INJECTION (Backend)

## Causa raíz por endpoint/servicio

### `POST /api/public/encuestas/:id/respuestas` (`routes/encuestas_public.py`)
- El endpoint público tiene semántica operativa real (dedupe/idempotencia/antifraude).
- Usarlo para carga sintética masiva provoca `409 Conflict` y ruido de negocio.

### `POST /admin/encuestas/:id/seed-demo/bulk` (`routes/encuestas_admin.py`, `services/encuestas_service.py`)
- Antes faltaba un camino claramente separado para bulk demo con trazabilidad de lote.

## Mejoras aplicadas
1. Endpoint admin dedicado para inyección bulk demo (`seed-demo/bulk`) con alias `count/cantidad`.
2. Reuso de `_run_seed_demo` para mantener una sola lógica de seeding.
3. Metadata por respuesta demo:
   - `is_demo_seed`
   - `demo_batch_id`
   - `demo_scenario`
4. Analytics con filtros `include_demo` / `exclude_demo` para separar real vs demo.
5. Respuesta pública duplicada degradada a éxito idempotente con guía a endpoint admin.

## Resultado esperado
- Bulk `100 demo` sin colisión con endpoint público.
- Trazabilidad por lote y exclusión limpia de demo en tableros reales.
