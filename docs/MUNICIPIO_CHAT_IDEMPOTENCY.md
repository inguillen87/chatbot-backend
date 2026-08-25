# Idempotencia durable de chat municipal

`POST /api/ask/municipio` y su alias compatible `POST /ask/municipio`
aceptan el header opcional `Idempotency-Key`.

## Contrato

- Formato de la clave: entre 8 y 128 caracteres de `A-Z`, `a-z`, `0-9`,
  guion, guion bajo, punto o dos puntos.
- Alcance único: tenant resuelto por el servidor + actor o sesión estable +
  endpoint canónico + clave. Para visitantes anónimos, el alcance combina la
  sesión normalizada con el identificador anónimo; ninguno de los dos se guarda
  en claro.
- El `tenant_id` del receipt se propaga como autoridad explícita al contexto de
  sesión, al responder municipal y al efecto de ticket. Una sesión existente
  con otro tenant, o una sesión legacy sin tenant demostrable, se rechaza con
  `409 municipio_chat_idempotency_scope_conflict` antes de leer su contexto.
- El request se liga a un SHA-256 canónico de método, query y body. El orden de
  propiedades JSON ni de claves query distintas no cambia el hash. El orden de
  valores query o archivos repetidos sí se conserva porque Flask puede observar
  el primer valor.
- Un replay con el mismo payload devuelve el mismo body JSON y status HTTP sin
  volver a ejecutar el LLM, crear reclamos, guardar mensajes ni generar eventos
  de CRM.
- La misma clave y alcance con otro payload devuelve `409` y
  `reason_code=municipio_chat_idempotency_payload_conflict`.
- Una ejecución concurrente espera el bloqueo distribuido. Si no puede obtenerlo
  dentro del límite configurado devuelve `425`, `Retry-After: 1` y no ejecuta
  efectos.
- Una reserva incompleta después de una caída queda cerrada a nuevas ejecuciones
  (`425`). Esto prioriza no duplicar efectos frente a reintentar una operación de
  resultado incierto.
- PostgreSQL usa `pg_try_advisory_xact_lock` dentro de una transacción dedicada.
  El lock se libera por commit/rollback y es compatible con pooling transaccional
  de Neon/PgBouncer; la conexión queda fijada durante esa ejecución acotada.

## Retención sin reejecución

El body de replay se conserva 30 días por defecto (configurable entre 1 y 90).
Un barrido oportunista, acotado por lote y frecuencia, elimina `response_json`,
status/request-id HTTP y marca el receipt como `expired`. Los hashes, tenant,
endpoint y timestamps mínimos permanecen como tombstone: la misma clave nunca se
reclama ni vuelve a ejecutar efectos. Un retry posterior devuelve
`410 municipio_chat_idempotency_response_expired`.

- `MUNICIPIO_CHAT_IDEMPOTENCY_RESPONSE_RETENTION_DAYS` (default `30`)
- `MUNICIPIO_CHAT_IDEMPOTENCY_RETENTION_BATCH_SIZE` (default `100`)
- `MUNICIPIO_CHAT_IDEMPOTENCY_RETENTION_SWEEP_SECONDS` (default `300`)

## Headers de respuesta

- `X-Chat-Idempotency-Contract: chat.municipio.idempotency.v1`
- `X-Idempotency-Status: accepted | replayed | rejected`
- `Idempotency-Replayed: true | false`
- `Retry-After: 1` únicamente cuando corresponde reintentar una ejecución aún
  concurrente.

Estos headers están expuestos por CORS. La API no persiste ni devuelve la clave
cruda. La base guarda hashes del alcance, la clave y el request, además del body
de respuesta estrictamente necesario para el replay exacto.

El tiempo máximo del bloqueo se configura con
`MUNICIPIO_CHAT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS` (20 segundos por defecto,
acotado entre 0,1 y 60 segundos).
