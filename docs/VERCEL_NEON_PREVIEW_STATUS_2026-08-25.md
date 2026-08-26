# Chatboc Vercel + Neon — estado verificable de migración

Fecha de corte: 2026-08-25
Alcance: Preview validado, auditoría de candidato Production y gates pendientes
para retirar Render. El DNS público no fue movido.

## Resultado ejecutivo

El frontend y el backend están desplegados en Vercel Preview, integrados entre sí y validados contra una copia QA en Neon. La demo de gobierno incluye tablero ejecutivo, encuesta, reclamos/casos, mapa territorial, analítica, QR/WhatsApp y tiempo real. Los datos sintéticos están identificados como demostración y no se presentan como verdad municipal.

### Corte Preview vigente — 2026-08-25 21:52 ART

- Frontend: SHA `bc4d63397a486bfd242d893d6a4de3df0f72bc60`, deployment
  `dpl_3xQicnoxfFpxVG25c7p31RzxT9XM`, alias
  `https://chatboc-r2-preview.vercel.app`.
- Backend: SHA `279638d16bc6fb6698aabcae86559b296b9c0f5e`, deployment
  `dpl_Hf7FjxohZZhc5ZFigS2Z3sG6L2Kd`, región `gru1`, alias
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
- El tablero con actividad vinculada usa `mixed_partitioned`: publica tres KPIs
  observados de sesión —reclamos, reclamos geolocalizados y evidencias— con
  denominadores y procedencia `demo.metric_provenance.v1`; la encuesta se
  mantiene como partición sintética independiente y no se suma ni promedia con
  esos valores.
- Validación local del incremento: backend 65/65 pruebas; frontend 37/37 pruebas
  focales, typecheck y build. Validación remota: aliases `Ready`, versión y
  health 200, contrato sandbox y detalle/resultados de los tres escenarios 200,
  navegación CRM/encuestas/analítica, página pública, login sin credenciales y
  mapa real. En 390 px no hubo desborde horizontal.
- Una carga nueva con el cache caliente quedó interactiva en 408 ms y sin
  warnings ni errores de consola. El primer acceso frío todavía debe incluirse
  en el precalentamiento de reunión documentado más abajo.
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
| Backend Vercel Preview | Completa | Deployment `dpl_Hf7FjxohZZhc5ZFigS2Z3sG6L2Kd`, región `gru1`, estado `Ready`, SHA runtime exacto `279638d16bc6fb6698aabcae86559b296b9c0f5e`; alias, versión, health y conexión DB fueron verificados. |
| Frontend Vercel Preview | Completa | Deployment `dpl_3xQicnoxfFpxVG25c7p31RzxT9XM`, estado `Ready`, SHA exacto `bc4d63397a486bfd242d893d6a4de3df0f72bc60`; el artefacto conserva ocho rewrites hacia Preview y cero rutas al backend de Producción. |
| Demo ejecutiva y territorial | Completa en Preview | Recorrido de 30 segundos, tres escenarios Junín, CRM de reclamos, KPIs con fuente/base y mapa MapLibre híbrido calor/puntos con escala, capas, ranking y procedencia. Todos los datos sintéticos están declarados como no oficiales. |
| Aceptación remota | Completa en Preview | Contratos de versión/health/sandbox y las tres encuestas pasaron por frontend y backend. Desktop y móvil quedaron sin overflow; CRM, encuestas, analítica, mapa y login cargaron. La página pública de votación se probó sin enviar respuesta y no se envió un mensaje WhatsApp. |
| Arranque en frío | Mejorado, todavía pendiente estructural | Una primera resolución pública superó el timeout seguro de 5 s; después del warm-up, una pestaña nueva quedó interactiva en 408 ms y sin warnings/errores. Para la reunión sigue siendo obligatorio el precalentamiento. |
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
