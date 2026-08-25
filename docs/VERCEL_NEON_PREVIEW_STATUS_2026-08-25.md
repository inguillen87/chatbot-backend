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
| Neon QA | Completa | QA y principal permanecen sin promover. Neon principal: 171 tablas, 52.751 filas inventariadas y revisión `20260825_demo_survey_participation_v1`. La rama temporal `br-falling-wind-actm4mcu` fue creada desde principal para el ensayo final. |
| Ensayo Neon de migraciones | Completa | Preflight inicial: 171 tablas, 52.751 filas, dos revisiones pendientes, 0/3 tickets reparados y tabla de idempotencia ausente. Preflight final: 172 tablas, misma suma de filas, revisión `20260825_chat_idempotency_v1`, 3/3 tickets reparados, tabla e índice presentes, `ready=true`. Principal fue reconsultada después y permaneció intacta. |
| Backend Vercel Preview | Completa | Deployment `dpl_FJirKYFqLL1Sp4xLGVqEw8SEEYUW`, región `gru1`, estado `Ready`, construido desde el SHA exacto `a17e90a6be20069655dcf91d0e434a6a2e518291`. El alias `api-preview.chatboc.ar` fue verificado contra ese deployment. |
| Frontend Vercel Preview | Completa | Deployment `dpl_G35fJ8h48qVyzjHdwCGDPoPHNmBp`, estado `Ready`, SHA exacto `054ae14ed33fa471346a1ae45a0d8a10979891b7`; el HTML del alias y de la URL inmutable tuvo el mismo SHA-256. |
| Demo ejecutiva y territorial | Completa en Preview | Contrato `demo.admin_preview.v1`, mapa MapLibre, KPIs reconciliados y responsive. |
| Aceptación remota read-only | Completa | Contratos de versión/readiness/admin Preview, encuesta, resultados, sesión y menú pasaron por frontend y backend. Desktop y móvil quedaron sin errores de página ni respuestas HTTP `>=400`; mapa MapLibre con 5 puntos, encuesta sintética de 100 respuestas y disclosure explícito fueron verificados. WhatsApp quedó comprobado hasta launcher/capacidades, sin enviar un mensaje real. |
| Arranque en frío | Mejorado, todavía pendiente estructural | El primer health del deployment nuevo midió 14,24 s; tres requests activos midieron 0,48 s, 0,21 s y 0,38 s. Mejora respecto de ~16 s, pero todavía requiere precalentamiento para la reunión. |
| Candidato Production | Revertido | `dpl_8AAiQbu1oWFZLLDyfFLcYfe4T5Z5` arrancó y respondió health, pero usó la base Render. Se revirtió al deployment anterior antes de cualquier corte de DNS. |
| Seguridad de cron | Corregida en código/configuración futura | `VERCEL_OUTBOX_CRON_ENABLED=false` por defecto; 13 invocaciones del candidato finalizaron a las 08:11:15 UTC y no reaparecieron tras el rollback. WhatsApp quedó sin efectos; los efectos de encuesta requieren reconciliación antes del corte. |
| Variables Production futuras | Parcial | `DATABASE_URL` y `SQLALCHEMY_DATABASE_URI` fueron sincronizadas explícitamente desde `NEON_DATABASE_URL` pooled; `ALEMBIC_DB_URL` y `MIGRATIONS_DATABASE_URL` desde la URL directa; `SOCKETIO_MESSAGE_QUEUE_URL` desde `REDIS_URL`. Los valores no se imprimieron y solo aplican a deployments nuevos. |
| Paridad operativa Render | Parcial | Outbox, WhatsApp payload retention, survey privacy y reporte semanal tienen ciclos acotados y cuatro cron declarados en Vercel. Todos permanecen fail-closed mediante gates independientes en `false` hasta su validación remota y activación controlada. |
| Reporte semanal | Implementado, gate pendiente | El procesamiento ahora es acotado e idempotente y el cron está declarado. Falta verificar una ejecución remota controlada antes de activarlo. |
| Cloudflare/Turnstile | Fuera del corte actual | Cloudflare no forma parte de esta migración ni es requisito para la demo ejecutiva. No se activó AI Gateway ni se incorporó la promoción recibida por correo. |
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
