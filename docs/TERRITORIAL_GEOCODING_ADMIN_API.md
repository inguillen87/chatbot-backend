# Cola administrativa de geocodificación territorial

Contrato: `operations.territorial_geocoding_admin.v1`

Esta frontera reemplaza el enlace visual `focus=open_geocoding_queue` por una
API tenant-scoped consumible por el CRM. Requiere autenticación y rol `admin` o
`super_admin`; un operador `empleado` no puede abrirla aunque tenga acceso a
reclamos. `X-Tenant-Slug` es obligatorio y todo cruce de tenant falla cerrado.

## Endpoints

- `GET /api/v2/analytics/operations/geocoding-queue`
  - resumen y lista paginada;
  - filtros: `status`, `review_state`, `source_model`, `reason_code`,
    `ticket_id`, `category`/`categoria`, `zone`/`zona`, `quality_state`,
    `page`, `per_page`;
  - no devuelve domicilio, digest del domicilio ni coordenadas exactas.
- `GET /api/v2/analytics/operations/geocoding-queue/{job_id}`
  - detalle seleccionado, propuesta, calidad, intentos y revisiones;
  - nunca devuelve el domicilio crudo.
- `GET /api/v2/analytics/operations/geocoding-queue/{job_id}/attempts`
  - recibos auditables y reason codes de cada intento;
  - serializa únicamente campos permitidos y descarta `formatted_address`,
    `direccion` u otros campos libres del proveedor.
- `POST /api/v2/analytics/operations/geocoding-queue/{job_id}/review`
  - exige `Idempotency-Key` de 8 a 128 caracteres seguros;
  - cuerpo: `decision`, `reason_code` y opcionalmente
    `apply_coordinates: false`;
  - `201` al crear, `200` al repetir exactamente la misma operación y `409`
    si la clave se reutiliza con otro cuerpo;
  - agrega un recibo humano inmutable; no llama al proveedor ni escribe
    coordenadas.

## Decisiones de revisión

`approved` admite:

- `verified_against_source`
- `verified_on_map`
- `verified_with_field_team`

`rejected` admite:

- `ambiguous_candidate`
- `duplicate_job`
- `incorrect_location`
- `insufficient_precision`
- `outside_jurisdiction`
- `stale_source`

Una aprobación requiere una propuesta con latitud y longitud. Si la propuesta
cambia después de la revisión, el estado efectivo pasa a `stale`; el CRM debe
solicitar una nueva decisión humana.

## Fronteras deliberadas

- Todos los `GET` son read-only y responden `Cache-Control: no-store`.
- El listado agregado expone ID estable, tipo/fuente, ticket, categoría, zona,
  calidad, reason codes y acciones, pero no evidencia domiciliaria sensible.
- La tabla de revisiones sólo persiste hashes, decisión controlada, actor y
  momento. Tiene una restricción física que prohíbe
  `coordinate_write_performed = true`.
- `apply_coordinates: true` se rechaza. La aplicación futura debe pasar por un
  endpoint separado que verifique la autoridad global de escritura y el writer
  tenant-scoped existente.
- No hay cron, worker, llamada de proveedor ni despliegue en este incremento.
