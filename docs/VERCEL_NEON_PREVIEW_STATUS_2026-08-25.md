# Chatboc Vercel + Neon — estado verificable de migración

Fecha de corte: 2026-08-25
Alcance: Preview validado, auditoría de candidato Production y gates pendientes
para retirar Render. El DNS público no fue movido.

## Resultado ejecutivo

El frontend y el backend están desplegados en Vercel Preview, integrados entre sí y validados contra una copia QA en Neon. La demo de gobierno incluye tablero ejecutivo, encuesta, reclamos/casos, mapa territorial, analítica, QR/WhatsApp y tiempo real. Los datos sintéticos están identificados como demostración y no se presentan como verdad municipal.

`chatboc.ar` ya se sirve desde Vercel, pero `api.chatboc.ar` continúa apuntando a
Render. Un candidato de backend Vercel Production expuso dos gates P0 antes del
corte: estaba conectado a PostgreSQL de Render por precedencia de variables y
Vercel activó el cron declarado apenas publicó el deployment. El candidato fue
revertido y no se movió DNS. Las variables de futuros deployments fueron
corregidas para Neon principal, la cola Socket.IO quedó sincronizada con Redis y
el cron queda fail-closed hasta una activación explícita.

No se debe apagar Render todavía. Antes faltan aplicar la única migración de
esquema pendiente en Neon principal, cerrar la paridad de workers/crons y ejecutar
la verificación remota completa con escritura controlada.

## Evidencia por fase

| Fase | Estado | Evidencia |
| --- | --- | --- |
| Inventario y aislamiento | Completa | Trabajo realizado en ramas/worktrees aislados; el DNS público de API continúa en Render. |
| Neon QA | Completa | Rama QA: 171 tablas y 53.220 filas. Neon principal: 170 tablas y 52.751 filas, todavía una revisión Alembic detrás. La diferencia de QA incluye tráfico de prueba y no debe fusionarse como datos de Producción. |
| Backend Vercel Preview | Completa | Deployment `dpl_EKUZe4NHX5WvjZRLiv9iqcEhbTrc`, región `gru1`, estado `Ready`, runtime `0a5fd6faa`. |
| Frontend Vercel Preview | Completa | Deployment `dpl_FyqvHD4DNppT3WkboA2LxCH33KWv`, estado `Ready`, commit `80d3ada8`. |
| Demo ejecutiva y territorial | Completa en Preview | Contrato `demo.admin_preview.v1`, mapa MapLibre, KPIs reconciliados y responsive. |
| Aceptación remota read-only | Completa | 3 pruebas pasaron y 1 prueba durable quedó omitida por `WRITE_QA=0`; sin escrituras. |
| Arranque en frío | En optimización | Preview actual: aproximadamente 16 s en frío y 0,2–0,4 s activo. La optimización local reduce el factory de 4,56 s a 2,34 s en el harness focal; una medición independiente de proceso completo dio mediana 2,91 s. Falta medirla en un nuevo Preview. |
| Candidato Production | Revertido | `dpl_8AAiQbu1oWFZLLDyfFLcYfe4T5Z5` arrancó y respondió health, pero usó la base Render. Se revirtió al deployment anterior antes de cualquier corte de DNS. |
| Seguridad de cron | Corregida en código/configuración futura | `VERCEL_OUTBOX_CRON_ENABLED=false` por defecto; 13 invocaciones del candidato finalizaron a las 08:11:15 UTC y no reaparecieron tras el rollback. WhatsApp quedó sin efectos; los efectos de encuesta requieren reconciliación antes del corte. |
| Variables Production futuras | Parcial | `DATABASE_URL` y `SQLALCHEMY_DATABASE_URI` fueron sincronizadas explícitamente desde `NEON_DATABASE_URL` pooled; `ALEMBIC_DB_URL` y `MIGRATIONS_DATABASE_URL` desde la URL directa; `SOCKETIO_MESSAGE_QUEUE_URL` desde `REDIS_URL`. Los valores no se imprimieron y solo aplican a deployments nuevos. |
| Paridad operativa Render | Parcial | Los 3 workers permanentes quedan cubiertos por ciclos acotados del cron de outbox. WhatsApp payload retention y survey privacy ya tienen cron Vercel con horarios equivalentes, bearer y gate destructivo independiente en `false`. Falta validar todo remotamente y reemplazar el reporte semanal. |
| Reporte semanal | Bloqueado correctamente | El comando actual carga todos los tenants sin límite y no posee lease ni unicidad idempotente. No fue expuesto como endpoint ni agendado en Vercel para evitar llamadas duplicadas a proveedores. |
| Turnstile real | Pendiente externo | Requiere completar la autorización de Cloudflare y probar el desafío humano real. |
| Producción y retiro de Render | Pendiente | No ejecutar hasta completar backup, migración final, smoke con escritura, webhooks y rollback. |

## URLs correctas de Preview

- Demo ejecutiva: `https://chatboc-r2-preview.vercel.app/demo?sector=gobierno&rubro=municipio&tenant_slug=junin&remote_preview_qa=1`
- Encuesta territorial Junín: `https://chatboc-r2-preview.vercel.app/e/demo-gobierno-junin-prioridades-barriales?tenant_slug=junin`
- Backend de Preview: `https://api-preview.chatboc.ar`
- Health de Preview: `https://api-preview.chatboc.ar/api/health`

No usar para la presentación la variante genérica con `tenant_slug=municipio`: corresponde a un escenario sintético genérico y no al escenario territorial de Junín.

## Precalentamiento para una reunión

Vercel Preview reduce a cero el contenedor luego de un período corto sin actividad. Para que el primer click de la reunión no pague el cold start, ejecutar desde el repositorio backend:

```powershell
.\scripts\warm_vercel_preview.ps1 -DurationMinutes 120
```

El script:

- solo permite HTTPS hacia `api-preview.chatboc.ar`;
- solo ejecuta `GET /api/health`;
- rechaza el dominio de Producción, otras rutas, queries y fragmentos;
- corre durante un plazo acotado y reporta éxitos, fallos y latencia;
- no escribe datos ni contiene secretos.

Iniciarlo entre dos y cinco minutos antes de la presentación y mantener esa terminal abierta durante la reunión.

## Gates antes de apagar Render

1. Crear una rama/backup verificable de Neon principal y registrar revisión
   Alembic, conteos, constraints y LSN.
2. Aplicar exclusivamente `20260825_demo_survey_participation_v1` a Neon
   principal y verificar 171 tablas, tabla vacía, índices y trigger inmutable.
3. Cerrar la paridad de los 3 workers y 3 cron declarados en `render.yaml`; no
   asumir que un web container reemplaza procesos permanentes.
4. Confirmar por nombres/targets todas las variables de Production sin imprimir
   secretos y comprobar que no quedan referencias a Render.
5. Desplegar un SHA exacto del backend con cron desactivado y restaurar/verificar
   el alias `api-preview.chatboc.ar`, que el proyecto puede reasignar durante un
   deployment Production.
6. Validar health, autenticación, tenant isolation, encuesta, reclamo, analítica,
   subida de archivos, QR/WhatsApp y tiempo real contra Neon principal.
7. Activar cron de forma controlada, ejecutar una escritura canaria idempotente y
   reconciliar UI, API, base, logs y proveedores.
8. Validar webhooks reales de WhatsApp/Twilio y Turnstile con evidencia externa.
9. Mover `api.chatboc.ar`, observar y probar rollback a Render durante la ventana
   de corte.
10. Recién después, detener los workers/web de Render y eliminar el servicio pago
    cuando el período de rollback acordado haya terminado.

Un deployment `Ready` prueba que Vercel construyó y arrancó el artefacto; no reemplaza la evidencia completa UI → API → DB → proveedor.
