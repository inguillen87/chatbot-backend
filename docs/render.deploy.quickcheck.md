# Render deploy quick-check (backend)

Checklist mínimo para probar despliegue sin sorpresas.

## Variables obligatorias

- `ENV=prod`
- `SECRET_KEY=<valor-largo-aleatorio>`
- `DATABASE_URL=postgresql+psycopg://...`

## Variables recomendadas (hardening)

- `ENABLE_RUNTIME_SCHEMA_SYNC=false`
- `ENABLE_RUNTIME_TENANT_INIT=false`
- `FLASK_ENABLE_RUNTIME_SCHEMA_SYNC=false`
- `FLASK_ENABLE_RUNTIME_TENANT_INIT=false`

## Pasos de release

1. Configurar variables en Render.
2. Ejecutar migraciones (`flask db upgrade`) en release phase o job manual.
3. Deploy del web service.
4. Smoke test:
   - `/health`
   - login
   - `/auth/widget/bootstrap`
   - `/analytics/event`

## Señales de configuración incorrecta

- Logs con `Startup runtime schema sync executed (db.create_all)` en producción.
- Logs con `Startup runtime tenant init executed` en producción.
- Backend corriendo con SQLite en Render por falta de `DATABASE_URL`.
