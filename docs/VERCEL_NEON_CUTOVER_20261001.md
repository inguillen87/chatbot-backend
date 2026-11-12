# Corte ChatBoc exclusivamente Vercel + Neon

Estado al 1 de octubre de 2026 07:19 UTC: **preparación en curso; producción no
certificada para el corte**. Este documento prepara operaciones; no ejecuta
deploys, cambios de alias, escrituras, crons, webhooks o cambios en Render.

El destino final y la autoridad de datos son Vercel + PostgreSQL en Neon.
Render se conserva como evidencia de recuperación y no vuelve a recibir
autoridad. El rollback operativo debe usar un deployment Vercel anterior
compatible sobre la misma base Neon que haya aceptado las escrituras nuevas.
Los runbooks antiguos siguen siendo referencias históricas y no acreditan
este nuevo modelo de rollback.

## Lecturas actuales y límites

| Superficie | Resultado observado |
|---|---|
| Proyecto Neon | `nameless-rain-94060889` |
| Rama candidata | `br-wandering-term-acqwmdro` |
| Base candidata | `chatboc_candidate_20261001` |
| Revisión efectiva | `20260906_flask_sessions_v1` |
| Writer authority | propietario NULL, epoch 0, Render fenced, Vercel fenced |
| Corpus TDF | 40 nodos, 2 fuentes, versión 1.2.0, privado; recuperación auditada |
| Sender productivo candidato | cero filas provider_sender para Junín y TDF |
| Conexiones candidato | sólo Twilio sandbox; Junín provisioning_plan_ready, TDF needs_setup |
| Backend Preview observado | `dpl_BnLtp1U4G1rUdcTGiBx6hmUWZuBp`, SHA `e32c9ed1739f4d71d710126c46e7301241fda363`, READY, target NULL |
| Render compute/database | ambos suspended por billing desde 30/09 05:37:28Z |
| Último export Render disponible | 30/09 02:26Z; sólo existe además el export de 25/09 |
| Snapshots Neon administrados | lista vacía; backup de rama de agosto archived |

La restauración del export certificó 170 tablas, 266 usuarios y 32 tenants.
El cotejo de 53.259 filas certifica el archivo exportado contra la base
recovery, **no el estado final del origen**. La candidata tiene la cadena
exacta de 16 revisiones, fingerprints/postchecks y tablas protegidas sin cambios.
No reutilizar un recibo de migration/restore como aceptación nominal o paridad.

Entre export y suspensión hay aproximadamente 3h11 de posibles escrituras sin
cotejar. La suspensión por facturación no demuestra una congelación controlada
ni WAL estable. No inventar un delta, marcar strict parity certified o tratar
el export como final mientras esa ventana siga sin resolver. Recuperar un
export/lectura más reciente si está disponible sin devolver autoridad a Render.
Si el origen no puede consultarse, registrar la incertidumbre y mantener el
gate bloqueado; se necesita una decisión explícita sobre esa pérdida potencial.

Las cuentas nominales y hashes de contraseña existentes están conservados;
Mauricio pertenece a Junín y Analía a Tierra del Fuego. El registro no prueba
login. La evidencia anterior sólo aceptó la sesión real del operador y el
directorio con 32 organizaciones; no aceptó login nominal de ambos clientes.

## Gate A — Preview y acceso nominal

- Fijar el par frontend/backend por SHA completo, deployment ID y host inmutable.
  Ejecutar `scripts.verify_paired_preview` y conservar versión directa y a
  través del frontend antes/después.
- Completar desde login normal las sesiones existentes/credenciales reales,
  `/api/me`, organización, rol, capabilities y permissions. No resetear
  contraseñas ni emitir JWT sintéticos como aceptación.
- Recorrer SuperAdmin/directorio, Junín y consola de conocimiento TDF desde
  navegación normal y URL directa. Intentos cruzados de tenant deben denegarse.
- Conservar logs/navegador reales de la misma revisión. Las pruebas unitarias
  de guardas siguen siendo regresión, no sustituyen estas sesiones.
- Verificar encuestas, votaciones/sondeos, pedidos, reclamos, empleados, mapas
  de calor, analytics y CRM con datos reales y permisos de cada organización.
  Separar lectura observada de escritura/entrega aceptada.

El corpus TDF ya está reincorporado como privado. Publicarlo únicamente por el
endpoint real con sesión autorizada, permiso de gestión, tenant correcto,
revisión optimista y autoridad de escritura válida. No insertar/publicar por
SQL para sortear una guarda frontend o backend.

## Gate B — datos finales, esquema y backup

- Resolver la ventana posterior al export con evidencia del origen final.
  El camino ensayado es restore a una base vacía aislada; un delta requiere un
  plan adicional por tabla, claves, deletes, ownership y ensayo.
- Ejecutar `scripts.audit_render_neon_parity` con comparación exact y
  `config/render_neon_final_cutover_policy.v1.toml`, identidades explícitas,
  fingerprint HMAC y evidencia real de writers congelados. No usar SQLite
  legado como origen cuando producción usa PostgreSQL.
- Aplicar sólo la cadena exacta allowlisted hasta
  `20260906_flask_sessions_v1`, con lock, timeouts, fingerprints, postchecks y
  cotejo de tablas protegidas. No ejecutar upgrade head.
- Crear un backup actual con identidad/snapshot definidos y comprobar su
  restauración. La rama postmigration de agosto no contiene los datos actuales.
- Después de nuevas escrituras Neon, un rollback de compute conserva ESA
  Neon. Volver a la copia Render antigua causaría divergencia/pérdida de datos.

## Gate C — rollback sólo Vercel sobre la misma Neon

Preparar una revisión Vercel anterior realmente compatible con el esquema
final, `/api/me`, sesiones/cookies, permisos y corpus. Debe conservar R2
obligatorio y el mismo destino Neon. Registrar frontend y backend anteriores,
deployment IDs, alias, registry de crons, callbacks y configuración redacted.

Secuencia del ensayo:

1. Deployment nuevo activo de prueba, escritor único y destino Neon verificado.
2. Fences en ambos deployments de prueba; jobs y efectos externos detenidos,
   ingress durable aceptando solicitudes firmadas durante el intervalo.
3. Mover el alias de prueba al deployment anterior por ID. Verificar SHA,
   identidad Neon, readiness, sesiones/permisos y lectura de datos creados por
   el nuevo deployment; confirmar compatibilidad de esquema.
4. Retarget de crons y consumidores al deployment anterior, sin solapamiento.
   Sólo entonces activar el escritor anterior Vercel y ejecutar un canario.
5. Ensayar vuelta al nuevo con la misma secuencia y comprobar receipts únicos,
   datos nuevos conservados, buffer vacío y callbacks correctos.

La autoridad actual identifica runtime `vercel`, no deployment ID. Por eso la
autoridad compartida sola no evita dos generaciones Vercel activas: verificar
fences/env en todas las revisiones alcanzables y dueño único de jobs/provider.
No dejar un deployment anterior escribible por su URL técnica.

`scripts.rehearse_compute_rollback` certifica una secuencia que termina en
render_active; no usarla como certificación de este rollback. Hace falta un
contrato/ensayo nuevo compatible. Revertir sólo un alias Preview
nuevo→anterior→nuevo prueba routing recuperable; no demuestra rollback de
producción, compatibilidad de schema, escritura, crons ni proveedor real.

## Gate D — writer authority, crons, colas y proveedor

Preparar el runtime Production Vercel aún fenced y verificar versión,
DB/Redis/R2, CORS/TLS, canales nominales y cero efectos externos inesperados.
Mantener Preview y revisiones anteriores fenced.

La autoridad observada es NULL/epoch 0. No asignar primero a Render sólo porque
lo dice el runbook histórico. El CLI existente admite bootstrap a `vercel`.
Antes de cualquier transición ejecutar `scripts.manage_global_writer_authority
status`, leer el epoch actual y preservar fences. `bootstrap` y `activate` son
mutaciones separadas con CAS; cada transición aumenta epoch. No ejecutar
transfer desde Render cuando el propietario observado es NULL.

Usar la conexión de control directa, TLS, al mismo destino Neon aprobado;
runtime read-only y operador UPDATE con privilegios mínimos. Ningún runtime
puede eludir el control por una conexión de aplicación fallback.

Los cuatro crons aprobados son outbox-reconciliation, whatsapp-payload-retention,
survey-privacy-retention y weekly-analytics-report. Certificar registry remoto,
host/deployment/SHA exactos, schedules, bearer presente, fences, ausencia de
solapamiento y plan de retarget durante rollback. `vercel.json` y un health 200
no prueban que estén desplegados ni que el trabajo persista.

Certificar colas WhatsApp, domain-effect y survey-effect; drain/replay durable,
idempotencia y propietario único. Todo consumidor/CLI/replay debe respetar la
autoridad antes de claim o efecto externo.

Para WhatsApp se necesitan sender y conexión productivos reales por tenant.
La candidata no los tiene. Reconciliar las credenciales existentes mediante
`scripts.collect_twilio_provider_snapshot` GET-only y la atestación del runtime
exacto; promover con plan/digest verificados. Preservar callback URLs anteriores
para rollback Vercel y comprobar firma, persistence y delivery sin mezclar
Junín/TDF ni usar una sandbox como remitente productivo.

## Gate E — corte y aceptación

El agregador `scripts.audit_cutover_release_manifest` exige diez certificaciones
frescas y coherentes: writer fence, paridad estricta, migraciones exactas,
workers/colas, cuatro crons, webhooks, canario autenticado, canario real del
proveedor, rollback y cold start. Vincularlas al mismo release/window, SHA,
deployment, host y base. La certificación de rollback debe corresponder al
modelo Vercel vigente, no al runbook anterior de Render.

Sólo con gates anteriores aprobados:

1. Con ambos destinos fenced, mover api.chatboc.ar y el frontend normal al par
   aprobado; comprobar DNS/TLS/version/CORS, login y permisos nominales.
2. Adquirir ownership Vercel por CAS/epoch y activar sólo su escritor/jobs.
   Render continúa suspendido/sin autoridad; Preview continúa fenced.
3. Ejecutar canario controlado real: SID único, fila inbound durable, un efecto
   CRM/reclamo y respuesta con callback/entrega. Añadir ubicación/audio/imagen,
   encuesta/voto/formulario y R2. Cotejar receipts de persistencia y proveedor.
4. Reconciliar buffer vacío, cero duplicados/send_uncertain/dead-letter y queues
   dentro del presupuesto. Ejecutar cada cron de forma autenticada/idempotente.
5. Observar al menos 24h incluyendo un período municipal. Conservar export/PITR,
   backup actual y deployments de rollback recuperables. No eliminar Render ni
   su PostgreSQL hasta que paridad y rollback efectivos estén comprobados.

Los canarios que realmente envían mensajes a personas requieren autorización
de destinatario/acción; preparar primero todo el resultado revisable. Ningún
recibo synthetic/mock se presenta como aceptación del proveedor o usuario.

## Estado de checks

En el PR backend #2810, GitGuardian sigue FAILURE por un fixture PostgreSQL en
tests/test_postgres_tls.py, commit 3e735b3d6548790734aa4944e899bf28760776f6.
El host de la URL es reservado `db.example.invalid`; no es una conexión real.
Corregir el fixture y resolver el incidente histórico si permanece; no llamar
verde al check ni rotar contraseñas de usuarios por una URL sintética.
