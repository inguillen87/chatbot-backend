# Chatboc Vercel + Neon — estado verificable de migración

Fecha de corte: 2026-08-25
Alcance: Preview validado, auditoría de candidato Production y gates pendientes
para retirar Render. El DNS público no fue movido.

## Resultado ejecutivo

El frontend y el backend están desplegados en Vercel Preview, integrados entre sí y validados contra una copia QA en Neon. La demo de gobierno incluye tablero ejecutivo, encuesta, reclamos/casos, mapa territorial, analítica, QR/WhatsApp y tiempo real. Los datos sintéticos están identificados como demostración y no se presentan como verdad municipal.

### Corte Preview vigente — 2026-08-25 22:34 ART

- Frontend funcional: SHA `be1cf8da8c801744d6410443307516fc7255aca1`,
  deployment `dpl_DmkAoJ3f9w2hF7rdok3CLkCVYvGT`, alias
  `https://chatboc-r2-preview.vercel.app`. El gate E2E posterior quedó
  versionado en `27a7add9d775cb997b95f8addfd2dcb3c220ad97` sin cambiar el bundle.
- Backend: SHA `94bb22cf2f2ca54ab11a20d489f4ba05e855e53b`, deployment
  `dpl_h6MbkRSmyj1hBEgSTdnCB7EAcRjb`, región `gru1`, alias
  `https://api-preview.chatboc.ar`. `GET /api/version` comprobó esos dos
  SHA tanto directo como atravesando el frontend; `GET /api/health` respondió
  `status=ok`, `db=connected`.
- Neon Preview real: rama `br-soft-bird-ac2sghha` del proyecto
  `nameless-rain-94060889`. El principal `br-dark-silence-acmnikpq` no fue
  modificado.
- Se crearon de forma idempotente tres borradores institucionales Junín en la
  rama Preview, IDs 636–638, tenant 22 y propietario 4. Cada uno conserva cero
  respuestas persistidas y un recibo de contenido; no hubo publicación oficial.
- El menú demostrativo publica primero los tres escenarios Junín: prioridades
  barriales (`votacion`), obras y servicios a 90 días (`encuesta`) y trámites y
  atención digital (`encuesta`). Cada espejo demostrativo declara 100 respuestas
  sintéticas, 0 respuestas ciudadanas verificadas, cinco puntos territoriales,
  `official=false` y `municipal_truth=false`.
- La experiencia de 30 segundos conecta atención omnicanal, CRM/reclamos,
  encuestas/votaciones y analítica territorial dentro del mismo workspace.
  El mapa vigente es cartográfico MapLibre/MapTiler/OpenStreetMap con capas
  calor + puntos, densidad y puntos, volumen, coordenadas, fuente y disclaimer.
  Su teardown ya no dispara una rotación de estilo al abortar requests internos.
- El tablero con actividad vinculada usa `mixed_partitioned`: publica tres KPIs
  observados de sesión —reclamos, reclamos geolocalizados y evidencias— con
  denominadores y procedencia `demo.metric_provenance.v1`; la encuesta se
  mantiene como partición sintética independiente y no se suma ni promedia con
  esos valores.
- Validación local acumulada: backend 65/65 pruebas; frontend 26/26 pruebas de
  bootstrap/Preview y 10/10 de ciclo de vida del mapa, typecheck y build.
  Validación remota final: aliases `Ready`, versión/health y 18/18 requests de
  contrato por frontend/backend en 200; E2E Chromium 1/1 con CRM, mapa canvas
  real, nueve encuestas, responsive 390 px, cero errores API/página, cero
  warnings Clerk/MapLibre y cero requests a `/auth/clerk/config` en la URL QA.
- La presentación QA explícita ya no espera el bootstrap opcional de Clerk; al
  salir a login o rutas privadas se restaura la topología autenticada mediante
  recarga. El backend todavía puede medir ~8–10 s en frío; caliente quedó entre
  ~0,2 y 0,9 s, por lo que sigue vigente el precalentamiento documentado abajo.
- No se envió un mensaje WhatsApp ni un voto/reclamo remoto en este corte. No se
  modificaron números, remitentes, proveedores, asociaciones de canal ni
  configuración Twilio/WhatsApp; la separación preexistente entre canal
  operativo y destinos demostrativos se preservó sin reinterpretarla.
- Producción, Neon principal, DNS de `api.chatboc.ar` y Render permanecen sin
  cambios. El canario anterior se ejecutó únicamente contra Preview/Neon QA.

`chatboc.ar` ya se sirve desde Vercel, pero `api.chatboc.ar` continúa apuntando a
Render. Un candidato de backend Vercel Production expuso dos gates P0 antes del
corte: estaba conectado a PostgreSQL de Render por precedencia de variables y
Vercel activó el cron declarado apenas publicó el deployment. El candidato fue
revertido y no se movió DNS. Las variables de futuros deployments fueron
corregidas para Neon principal, la cola Socket.IO quedó sincronizada con Redis y
el cron queda fail-closed hasta una activación explícita.

No se debe apagar Render todavía. Neon principal está en
`20260825_demo_survey_participation_v1` y conserva dos revisiones pendientes:
la reparación acotada de tres tickets históricos y la tabla durable de
idempotencia del chat municipal. Ambas ya fueron ensayadas con éxito sobre una
rama temporal aislada; falta la autorización y ventana de corte para aplicarlas
en principal, cerrar la paridad de workers/crons y ejecutar la verificación
remota completa con escritura controlada.

## Evidencia por fase

| Fase | Estado | Evidencia |
| --- | --- | --- |
| Inventario y aislamiento | Completa | Trabajo realizado en ramas/worktrees aislados; el DNS público de API continúa en Render. |
| Neon QA | Completa | Preview sirve desde `br-soft-bird-ac2sghha`; allí quedaron los borradores Junín 636–638 con cero respuestas persistidas. Neon principal `br-dark-silence-acmnikpq` permaneció intacta. |
| Ensayo Neon de migraciones | Completa | Preflight inicial: 171 tablas, 52.751 filas, dos revisiones pendientes, 0/3 tickets reparados y tabla de idempotencia ausente. Preflight final: 172 tablas, misma suma de filas, revisión `20260825_chat_idempotency_v1`, 3/3 tickets reparados, tabla e índice presentes, `ready=true`. Principal fue reconsultada después y permaneció intacta. |
| Backend Vercel Preview | Completa | Deployment `dpl_h6MbkRSmyj1hBEgSTdnCB7EAcRjb`, región `gru1`, estado `Ready`, SHA runtime exacto `94bb22cf2f2ca54ab11a20d489f4ba05e855e53b`; alias, versión, health y conexión DB fueron verificados. |
| Frontend Vercel Preview | Completa | Deployment `dpl_DmkAoJ3f9w2hF7rdok3CLkCVYvGT`, estado `Ready`, SHA funcional `be1cf8da8c801744d6410443307516fc7255aca1`; el artefacto conserva ocho rewrites hacia Preview y cero rutas al backend de Producción. |
| Demo ejecutiva y territorial | Completa en Preview | Recorrido de 30 segundos, tres escenarios Junín, CRM de reclamos, KPIs con fuente/base y mapa MapLibre híbrido calor/puntos con escala, capas, ranking y procedencia. Todos los datos sintéticos están declarados como no oficiales. |
| Aceptación remota | Completa en Preview | 18/18 requests HTTP 200 y E2E Chromium 1/1: CRM, nueve encuestas, tres escenarios Junín prioritarios, canvas MapLibre, accesibilidad, 390 px sin overflow y cero errores/warnings relevantes. No se ingresaron credenciales, no se votó y no se envió WhatsApp. |
| Arranque en frío | Mejorado, todavía pendiente estructural | La URL QA ya no espera el bootstrap Clerk y no solicita `/auth/clerk/config`; el backend conserva picos fríos de ~8–10 s y respuestas calientes de ~0,2–0,9 s. Para la reunión sigue siendo obligatorio el precalentamiento. |
| Candidato Production | Revertido | `dpl_8AAiQbu1oWFZLLDyfFLcYfe4T5Z5` arrancó y respondió health, pero usó la base Render. Se revirtió al deployment anterior antes de cualquier corte de DNS. |
| Seguridad de cron | Corregida en código/configuración futura | `VERCEL_OUTBOX_CRON_ENABLED=false` por defecto; 13 invocaciones del candidato finalizaron a las 08:11:15 UTC y no reaparecieron tras el rollback. WhatsApp quedó sin efectos; los efectos de encuesta requieren reconciliación antes del corte. |
| Variables Production futuras | Parcial | `DATABASE_URL` y `SQLALCHEMY_DATABASE_URI` fueron sincronizadas explícitamente desde `NEON_DATABASE_URL` pooled; `ALEMBIC_DB_URL` y `MIGRATIONS_DATABASE_URL` desde la URL directa; `SOCKETIO_MESSAGE_QUEUE_URL` desde `REDIS_URL`. Los valores no se imprimieron y solo aplican a deployments nuevos. |
| Paridad operativa Render | Parcial | Outbox, WhatsApp payload retention, survey privacy y reporte semanal tienen ciclos acotados y cuatro cron declarados en Vercel. Todos permanecen fail-closed mediante gates independientes en `false` hasta su validación remota y activación controlada. |
| Reporte semanal | Implementado, gate pendiente | El procesamiento ahora es acotado e idempotente y el cron está declarado. Falta verificar una ejecución remota controlada antes de activarlo. |
| Cloudflare/Turnstile | Fuera del corte actual | Cloudflare no forma parte de esta migración ni es requisito para la demo ejecutiva. No se activó AI Gateway ni se incorporó la promoción recibida por correo. |
| Producción y retiro de Render | Pendiente | No ejecutar hasta completar backup, migración final, smoke con escritura, webhooks y rollback. |

## URLs correctas de Preview

- Demo ejecutiva: `https://chatboc-r2-preview.vercel.app/demo?sector=gobierno&rubro=municipio&tenant_slug=junin&remote_preview_qa=1`
- Votación territorial Junín: `https://chatboc-r2-preview.vercel.app/e/demo-gobierno-junin-participa-prioridades-barriales`
- Encuesta de obras y servicios: `https://chatboc-r2-preview.vercel.app/e/demo-gobierno-junin-90-dias-obras-servicios`
- Encuesta de trámites y atención: `https://chatboc-r2-preview.vercel.app/e/demo-gobierno-junin-digital-tramites-atencion`
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
2. Aplicar exclusivamente las dos revisiones ya ensayadas —reparación acotada de
   tickets e idempotencia municipal— y verificar revisión final, 172 tablas,
   3/3 reparaciones y tabla/índice de idempotencia.
3. Cerrar la paridad de los 3 workers y 4 cron declarados en `vercel.json`; no
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
8. Validar webhooks reales de WhatsApp/Twilio con evidencia externa; Turnstile
   queda como hardening opcional separado y no como dependencia del corte.
9. Mover `api.chatboc.ar`, observar y probar rollback a Render durante la ventana
   de corte.
10. Recién después, detener los workers/web de Render y eliminar el servicio pago
    cuando el período de rollback acordado haya terminado.

Un deployment `Ready` prueba que Vercel construyó y arrancó el artefacto; no reemplaza la evidencia completa UI → API → DB → proveedor.
