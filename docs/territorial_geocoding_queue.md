# Cola territorial de geocodificación

## Objetivo

Convertir direcciones ya persistidas en tickets en propuestas de coordenadas
auditables para el mapa operativo. La categoría clasifica el punto; la
dirección determina la propuesta territorial. Ninguna coordenada se inventa y
ninguna respuesta de proveedor se aplica sin validación.

Contrato: `operations.territorial_geocoding.v1`.

## Flujo seguro

1. `discover_territorial_geocoding_candidates` recibe registros tenant-scoped.
2. Conserva únicamente casos con dirección persistida y sin coordenadas.
3. Genera una identidad estable con `tenant_id`, modelo, id del caso, hash de
   dirección y hash de jurisdicción. La cola no duplica la dirección.
4. `process_territorial_geocoding_candidate` queda en `pending` y no llama a
   ningún proveedor de forma predeterminada.
5. Cuando una ejecución habilita explícitamente el proveedor, valida:
   `bounds`, localidad, provincia, país, `partial_match` y `location_type`.
6. Un resultado parcial, aproximado, fuera de límites o territorialmente
   inconsistente pasa a `needs_review`.
7. Un resultado válido permanece en `pending` durante `dry_run`.
8. Para escribir se requieren simultáneamente:
   `dry_run=False`, `writes_enabled=True`, autoridad global de escritura
   confirmada y un callback tenant-scoped explícito.

Estados durables: `pending`, `needs_review`, `applied`, `failed`.

## Auditoría e idempotencia

- `territorial_geocoding_job` representa el candidato vigente.
- `territorial_geocoding_attempt` guarda intentos inmutables y su resultado.
- La misma operación se identifica por `request_digest`; su repetición devuelve
  el resultado guardado sin repetir proveedor ni escritura.
- Los recibos guardan hashes, coordenadas propuestas, precisión, controles y
  reason codes. No guardan la dirección, nombre ni datos de contacto.
- Una tarea `applied` no puede degradarse por un dry-run posterior.

## Límites de este incremento

- No se agregó cron, worker ni endpoint de ejecución.
- No se habilitaron llamadas de red.
- No se ejecutó la migración fuera de pruebas SQLite.
- No se modificaron Vercel, Producción, Render ni datos de Junín.
- El caller futuro debe obtener la autoridad global desde el writer fence; no
  debe convertir la confirmación en una constante o variable pública.
