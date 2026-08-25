# Render deploy quick-check (backend)

Checklist mínimo para probar despliegue sin sorpresas.

## Variables obligatorias

- `ENV=prod`
- `SECRET_KEY=<valor-largo-aleatorio>`
- `DATABASE_URL=postgresql+psycopg://...`
- `RATELIMIT_STORAGE_URI=rediss://...` (Redis administrado y compartido; no `memory://`)

PostgreSQL y Redis son dependencias obligatorias del web service en Render. No
se considera listo un deploy que arranca con SQLite, un rate limiter en memoria
o una URL de Redis configurada pero inaccesible. En runtimes production-like,
la sonda PostgreSQL también exige la tabla crítica
`municipio_chat_idempotency_receipt`: la ruta municipal con idempotencia está
registrada siempre y no puede servir tráfico correctamente sin esa migración.
No se inspeccionan filas ni se exige el catálogo completo de tablas opcionales.

## Variables recomendadas (hardening)

- `ENABLE_RUNTIME_SCHEMA_SYNC=false`
- `ENABLE_RUNTIME_TENANT_INIT=false`
- `FLASK_ENABLE_RUNTIME_SCHEMA_SYNC=false`
- `FLASK_ENABLE_RUNTIME_TENANT_INIT=false`
- `ENABLE_DEMO_MODE=false`
- `FLASK_ENABLE_DEMO_MODE=false`
- `DATABASE_CONNECT_TIMEOUT_SECONDS=2` (apertura DB; rango efectivo 1-10 s)
- `DATABASE_POOL_TIMEOUT_SECONDS=2` (checkout del pool; rango efectivo 0.1-10 s)
- `READINESS_DATABASE_TIMEOUT_SECONDS=1.5`
- `READINESS_REDIS_TIMEOUT_SECONDS=1.0`
- `READINESS_CACHE_TTL_SECONDS=1.0` (caché por proceso con single-flight)

## Variables recomendadas (widget auth hardening)

- `WIDGET_JWT_ALG=RS256` (o `ES256`)
- `WIDGET_JWT_PRIVATE_KEY=<pem>`
- `WIDGET_JWT_PUBLIC_KEY=<pem>`
- `WIDGET_JWT_KID=widget-rs256-v1`

## Pasos de release

1. Configurar variables en Render.
2. Ejecutar migraciones (`flask db upgrade`) en release phase o job manual.
3. Deploy del web service.
4. Smoke test:
   - `GET /health` devuelve `200` (liveness del proceso).
   - `GET /health/ready` devuelve `200`, `status=ready`, DB `ok` y Redis `ok`.
     Un `503` bloquea la promoción; usar `request_id` para correlacionar logs.
     `components.database.reason_code=required_schema_missing` identifica una
     base conectada cuya migración crítica todavía no está disponible.
   - login
   - `/auth/widget/bootstrap`
   - `/analytics/event`

El contrato `runtime.readiness.v1` no expone hosts, URLs, credenciales ni textos
de excepciones. Validar también que la respuesta incluya
`Cache-Control: no-store` y el mismo `request_id` en JSON y `X-Request-Id`.
Cada proceso coalesce probes concurrentes y reutiliza el resultado durante un
TTL corto; el `request_id` se agrega después y nunca queda dentro del caché.

## Corte del health check automático

`render.yaml` conserva temporalmente `healthCheckPath: /health`: el servicio
inventariado el 2026-08-24 todavía no tenía Redis y cambiarlo ahora haría fallar
el próximo deploy antes de poder validar staging. Después de provisionar
PostgreSQL y Redis administrados, ejecutar el smoke de `/health/ready` en un
entorno aislado y observarlo estable. Recién entonces, en un cambio separado y
reversible, apuntar `healthCheckPath` a `/health/ready`, desplegar y repetir los
smokes autenticados. No promover si readiness devuelve `503`.

## Señales de configuración incorrecta

- Logs con `Startup runtime schema sync executed (db.create_all)` en producción.
- Logs con `Startup runtime tenant init executed` en producción.
- Backend corriendo con SQLite en Render por falta de `DATABASE_URL`.
- `/health` responde `200` pero `/health/ready` responde `503`.
- Redis figura `not_configured`, `invalid_configuration` o `error` en readiness.
- Respuestas con `is_demo_placeholder=true` en `/tenant-profile` cuando no se esperaba modo demo.
