# Ensayo persistente separado de consultas oficiales

Una creación explícita de SuperAdmin allowlisted con sesión Clerk/AuthSession
vigente habilita una pregunta fija: «¿Pudiste utilizar este
formulario de prueba?», con «Sí» y «No». Cada ejecución dura 24 horas, admite
hasta 20 respuestas y comparte un máximo de 3 ejecuciones vigentes por tenant.
La organización debe ser canónica, estar activa y tener licencia de encuestas.
Definir las rutas no crea ejecuciones ni respuestas.

Siempre se muestra «Prueba técnica, sin valor de consulta oficial». Los resultados
son un agregado de respuestas persistidas, sin respuestas precargadas. No se
crean EncEncuesta, EncRespuesta, SurveyResponseReceipt ni efectos de respuestas
oficiales. No se modifica la aprobación jurisdiccional, el acceso oficial, las
encuestas históricas ni el gate existente de demos en Preview.

## API y UI

Los paths siguientes son relativos al mismo backend Chatboc:

| Método y path | Contrato y autoridad |
| --- | --- |
| GET `/api/v2/tenants/:slug/survey-rehearsals` | `surveys.production_rehearsal.v1`; SA o administrador persistido del mismo tenant. `source_tenant`, `items`, descriptor `create_action` y strings `ui` pertenecen al backend. |
| POST mismo path, cuerpo `{}` | SA allowlisted y Bearer/AuthSession vigente. `Idempotency-Key` obligatorio. 201 creación o 200 repetición exacta; devuelve `submission_id`, `replayed` y metadata. |
| GET mismo path + `/status?submission_id=:key` | SA actual; reconcilia sólo su propia intención del tenant, sin crear nada. 404 significa intención no observada, sin habilitar reenvío automático. |
| GET `/api/v2/public/tenants/:slug/survey-rehearsals/:run` | Metadata mínima pública; verifica prueba firmada, tenant/licencia y vigencia. |
| GET mismo path + `/results` | Mismo contrato y agregado real; `result_version` cambia con el agregado. Polling GET cada **5000 ms**; no acredita sockets. |
| POST mismo path + `/respond` | Bearer real, cuenta persistida activa y pertenencia consistente al tenant, o SA autorizado para el tenant explícito. Cuerpo exacto `{submission_id, option_id}`; opción `yes` o `no`. Header y cuerpo deben coincidir. Límite 2048 bytes. |
| GET mismo path + `/respond/status?submission_id=:key` | ACK de la intención exacta de la cuenta vigente; 404 no observado, 409 cuenta ya admitida con otra intención. |
| GET mismo path + `/respond/status` | `surveys.production_rehearsal.account_status.v1`; `verified_current_account:true` y `participated` leídos de fila y recibo. Permite inhibir un segundo envío tras recargar o borrar cache/cookies. |

El ACK `surveys.production_rehearsal.response.v1` contiene `persisted`, `replayed`,
`response_origin:"interactive_demo"`, `official:false`, `unique_person_certified:false`,
`metrics`, `ui` y `receipt` anidado con `submission_id`, `option_id`, `run_id`,
`tenant_slug`, `instrument_sha256`, `payload_sha256`, `verified_current_account:true`.
No expone IDs de cuentas, HMAC de identidad/intención, contactos ni IP.

Metadata usa `branding.{tenant_slug,display_name}`, sólo nombre institucional;
`links.{metadata_api,respond_api,results_api}` son paths relativos. Los textos
del formulario, acciones, límites, lectura, incertidumbre y error vienen de `ui`.
El descriptor `create_action.can_create` comprueba readonly rol, sesión y
cuota/licencia; el POST vuelve a comprobarlos. `requires_strict_mfa:false` corresponde
sólo al nuevo ensayo fijo y acotado. Las guardas y MFA oficiales existentes no se
modifican. El frontend conserva una intención estable,
consulta GET tras resultado incierto y no repite automáticamente el POST.

## Persistencia y límites

`AuditEvent` registra la autorización de creación con HMAC separado por dominio,
tenant, run UUID, hash del instrumento y fechas. Se valida también su fecha
persistida y el creador SA actual. La clave de creación se almacena sólo como
HMAC ligado a tenant y actor, incluyendo historial: expirarla no permite reutilizar
la misma intención para crear otro run.

`DemoSurveyParticipation` conserva exclusivamente origen `interactive_demo`,
instrumento, opción y hashes. La UNIQUE existente `(survey_slug, submission_id_hash)`
se usa con HMAC del **actor persistido/tenant/run**, independiente de cookie,
teléfono, IP y UUID del cliente. Un `AuditEvent` de admisión liga HMAC de cuenta,
digest de intención y hash del cuerpo dentro de la misma transacción. Una clave y
cuerpo idénticos retornan 200; otra clave para la misma cuenta o cuerpo cambiado
retornan 409 y no agregan filas. Dos cuentas detrás de una misma IP pueden participar.
Esto limita cuentas por ejecución; no certifica humanos únicos ni propiedad de teléfono.

La cuota y creación se serializan por tenant mediante advisory transaction lock
en PostgreSQL y bloqueo real de escritura en SQLite. El tenant/licencia y la
sesión se releen; la autoridad writer se revalida antes del commit. El limiter
existente aporta señal IP y falla cerrado en errores; el modo distribuido estricto
ya existente rechaza memoria/fallback cuando se configura. La IP no se persiste.
Un error de inserción revierte conjuntamente voto y recibo.

## Límites operativos y verificación

El servicio exige Vercel Production, Neon PostgreSQL con `sslmode=verify-full`,
writer habilitado/identidad Vercel y lease permitido en epoch **6**. Las mutaciones
quedan también bajo el middleware writer existente. No hay fallback a Preview,
Render, desarrollo, otra base ni flags relajados para operar. Se reutiliza SECRET_KEY
del servidor con HMAC por dominio, sin introducir credenciales nuevas. Tablas
faltantes producen 503; este cambio no incluye DDL ni migraciones.

La suite `tests/test_production_survey_rehearsal.py` usa Flask, SQLite descartable,
AuthSession real y tokens sintéticos emitidos por el servicio actual de auth.
Sólo sustituye el límite externo de runtime/lease para ejercitar funcionalidad
offline; los negativos ejercitan el guard real sin conectarse. Cuenta filas y
recibos para identidad, repetición, 1000 intenciones, NAT, cuotas, concurrencia,
rollback, expiración, scopes, sesión SA vigente/retirada y tablas faltantes. No es aceptación de producción.

La prueba PostgreSQL es opt-in con `CHATBOC_REHEARSAL_TEST_POSTGRES=1`: usa únicamente
`127.0.0.1:5432`, usuario `postgres` sin contraseña y base CI descartable
`vaultcredregression`. Crea y elimina exclusivamente un schema aleatorio
`rehearsal_contract_<uuid>` validado; comprueba carreras de intención, cuenta y
ambas cuotas con advisory locks reales. Si no se ejecuta, debe informarse como
SKIP y no como PostgreSQL probado. No acepta un DSN de proveedor o producción.
