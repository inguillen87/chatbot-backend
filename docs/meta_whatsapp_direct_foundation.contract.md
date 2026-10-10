# Meta directo: base local sin activación

Esta base pertenece al backend existente de Chatboc y parte de
`28ff2503867ed4556927ef96db07a2cc261ca678`. No crea aplicación, base de datos,
tabla, endpoint, suscripción, sender ni configuración de producción. No cambia
los ejecutores Twilio ni la integración Meta Flows existente.

Los nuevos módulos no están importados por ninguna ruta. Tenerlos implementados
y probarlos localmente no equivale a publicar la aplicación Meta, conectar una
organización, enviar un mensaje ni verificar su entrega.

## Contrato de credenciales

`meta_whatsapp_credentials.py` usa AES-GCM y el keyring existente de
`tenant_provider_credentials.py`. Guarda un envelope de contrato Meta distinto
en la misma ubicación privada por conexión: `_chatboc_provider_credentials_v1`.
La proyección pública existente elimina esa ubicación también cuando está
anidada. El token sólo aparece en RAM y en el header de la llamada autorizada.

AAD liga el ciphertext a tenant, conexión, proveedor Meta, canal, entorno,
app ID, WABA, revisión, expiración y key ID. Una conexión administrada por el
cutover, una referencia Twilio/env, un envelope ausente, un keyring ausente,
un token vencido o una copia en otro tenant/conexión fallan sin fallback.
El token y los eventos privados quedan excluidos de `repr`; los errores usan
códigos fijos. No se implementa aquí un endpoint para cargar credenciales ni
un escritor de vault. El eventual escritor debe autorizar al actor, aplicar
CAS, registrar auditoría sin secretos y confirmar todo en su transacción.

La expiración debe ser finita y explícita. Un token sin expiración declarada no
se transforma en disponibilidad permanente: esa variante exige definir y
revisar una política de revalidación antes de ampliar este contrato.

## Autoridad y transporte

`resolve_sender` lee columnas persistidas de `ProviderSender`,
`ProviderConnection` y `TenantProfile`, sin cargar identidades ORM como fuente
de autoridad y con autoflush desactivado. La búsqueda global por phone-number
ID requiere exactamente una fila: duplicados, conexiones ausentes, tenant
inactivo, proveedor/entorno/app/WABA ajenos y ownership incoherente se rechazan.
Un `expected_tenant_id` sólo limita el resultado; no lo autoriza.

Los estados persistidos de sender y conexión deben pasar el predicado existente
`is_sender_ready_status`. Una pausa, suspensión o desconexión local veta la
operación antes de abrir el token o consultar autoridad, aunque Meta todavía
reportara el sender disponible. Un estado ORM obsoleto o una reparación dirty
no modifica esa consulta ni puede reactivar el canal.

Los estados locales `connected`, `ONLINE` o `verified` no bastan. Hace falta un
`authority_verifier` de servidor explícito que produzca `VerifiedMetaAuthority`
para esa misma identidad y revisión de credencial. El contrato exige versión
Graph explícita, observación no futura y vigencia limitada por la del token.
**No existe un adaptador real ni un marker que reclame verificación remota en
este cambio.** Las autoridades de las pruebas son objetos sintéticos.

El adaptador futuro debe comprobar realmente en Meta el app/token, permisos
efectivos, pertenencia de WABA y phone ID, registro del número y suscripción
del app. La evidencia de App Review o de business verification del app de
Chatboc, por sí sola, no demuestra autoridad sobre un sender de un tenant.
Un caller HTTP nunca puede suministrar el callback de autoridad.

`send_once` exige un loader fresco por intento, política explícita con resultado
`True` estricto y transporte inyectado. La política futura debe comprobar actor,
consentimiento, ventana de atención o plantilla aprobada del tenant. No está
implementado un permiso global para saltar estos contratos.

El único destino de POST es
`https://graph.facebook.com/<version>/<phone_number_id>/messages`, sin token en
query. TLS se exige, redirects se desactivan y los timeouts son 3 segundos para
conexión y 10 para lectura. El adaptador no puede habilitar retries automáticos.
Además del permiso de messaging, la autoridad debe confirmar suscripción vigente
del webhook: un sender sin callback de recepción verificado no puede enviarse
por esta base.
La respuesta se limita a 64 KiB y no se conserva su cuerpo/error libre.
Se admite texto y plantilla con parámetros de texto del body. Media, botones,
Flows, creación/sincronización de templates y Graph batch quedan pendientes.

`accepted` requiere una respuesta WhatsApp con exactamente un `wamid`; sólo
significa aceptación del POST. `delivered`/`read` provienen de otro webhook.
Timeout, excepción, redirect, 5xx, respuesta inválida o éxito ambiguo producen
`uncertain`, con `retry_allowed=False`. Un 4xx produce `rejected` y sólo conserva
un código numérico acotado. Ningún resultado permite retry automático.

Esta función hace como máximo una llamada por invocación; no persiste un intent.
Antes de conectarla al runtime hace falta reserva durable/CAS en la infraestructura
existente, deduplicación y reconciliación del intento incierto. Reinvocar la
función sin ese contrato no es un mecanismo seguro de recuperación.

## Webhook e identidad del usuario

`meta_whatsapp_webhook.py` valida `X-Hub-Signature-256` con HMAC-SHA256 del cuerpo
original y el app secret mediante comparación constante, antes de parsear o
resolver senders. El verify token del GET challenge es otro secreto. Cuerpos
de más de 1 MiB, JSON con claves duplicadas, esquema inválido y lotes de más de
100 eventos fallan. Cada change debe corresponder al app, WABA y phone ID que
resuelva una autoridad vigente con suscripción comprobada.

El lote completo se valida antes de retornar eventos. Un sender desconocido,
ambiguo o incoherente bloquea todo el lote. Se normalizan sólo mensajes de texto
y estados `sent`, `delivered`, `read`, `failed`, conservando códigos numéricos
de error. Los eventos reciben claves semánticas por tenant/conexión/sender/tipo/
wamid/estado. Duplicados con contenido incoherente se rechazan. No se escriben
ledger, contactos, colas ni respuestas, y no se dispara procesamiento pesado.

La identidad de contacto es opaca y nunca se convierte en teléfono. Un valor
opaco de `from`/`recipient_id` se conserva sin modificar; no se vincula a otro
tenant o sender. Los campos nuevos de identidad requieren un `contact_resolver`
confiable explícito. Las pruebas de ese adaptador son sintéticas: no afirman que
ese ejemplo sea el schema actual desplegado de BSUID.

**BSUID outbound permanece bloqueado** por los builders hasta verificar el schema
actual de Cloud API con documentación Meta accesible y payload real autorizado.
No se infiere un teléfono a partir de BSUID ni se fusionan identidades por
nombre/username. Antes de integrar deberá revisarse su alcance por business
portfolio y los eventos de cambio de identidad; no se implementa ese merge aquí.

Un parser válido tampoco prueba persistencia o ACK durable. La ruta futura debe
deduplicar y encolar de forma durable antes de responder, usando los contratos
existentes y respetando fences/runtime. No se agrega esa ruta en esta base.

## Fuentes primarias consultadas el 2026-10-06

- [Colección oficial Meta: Messages](https://www.postman.com/meta/whatsapp-business-platform/folder/13382743-ba8d099d-007e-4b52-b9f2-3cf3c60e4fbc): endpoint de messages, bearer y prerrequisitos de messaging permission/phone ID registrado.
- [Colección oficial Meta: templates](https://www.postman.com/meta/whatsapp-business-platform/request/lwtlz1k/send-message-template-interactive): objeto template, lenguaje/components y respuesta `wamid`; las plantillas requieren aprobación.
- [Colección oficial Meta: status notifications](https://www.postman.com/meta/whatsapp-business-platform/request/rgtfq23/message-status-update-notifications): estructura WABA/metadata/statuses y orden de notificaciones que puede diferir del orden temporal.
- [Documentación oficial del SDK Meta, archivado](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/): distingue verify token del challenge y app secret de firma POST. Se usa como referencia de protocolo, no como SDK/dependencia actual recomendado.
- [Ejemplo oficial Meta de HMAC sobre raw payload](https://github.com/fbsamples/whatsapp-api-examples/blob/main/signature-validation-with-webhooks-payloads/app.py): confirma el cálculo HMAC sobre `request.get_data()`. Esta base aplica comparación constante y no copia logging de cuerpos ni callbacks del ejemplo.

Las páginas actuales de Meta developers
[messages webhook](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/messages) y
[BSUID](https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids)
devolvieron HTTP 429 en esta consulta. Su schema actual y disponibilidad no se
declaran verificados. No se usaron schemas de BSPs ajenos como autoridad Meta.

## Validación local y siguiente revisión

La suite nueva usa claves/tokens generados sintéticamente, SQLite `:memory:` y
transportes falsos. Bloquea requests HTTP y conserva el guard de red del repo.
Cubrir foreign AAD, estados locales engañosos, sender duplicado, dirty/stale ORM,
firma alterada, lote parcial, identidad opaca y POST incierto aporta evidencia de
contrato local; no reemplaza pruebas nativas ni aceptación de clientes.

Resultado final: 75 PASS focales. Los cinco casos nuevos de veto de estado local
y suscripción fallan contra el código exacto del patch previo a la revisión
(overlay sólo en RAM) y pasan contra la versión corregida. Esa diferencia causal
no usa ni modifica el runtime publicado.

La regresión vecina corrió con la versión focal de 69 casos: 167 PASS, 1 SKIP y
3 FAIL antes de detenerse. Los tres fallos 401 de tests existentes de Meta Flows
se reprodujeron solos, sin importar estos módulos; su archivo sigue igual al
baseline. No se declara PASS de toda la suite y no se arregla autenticación ajena
a este alcance. La versión final de 75 casos pasó en ejecución focal separada.

Antes de integrar: revisar este patch, implementar authority/policy/adaptadores
con evidencia real autorizada, vault CAS/audit, sender/portfolio binding y
deduplicación/cola/outbound intent durables. Después podrán revisarse endpoints,
callback, publicación y una prueba real limitada de envío/recepción. Nada de
esa activación se efectuó aquí.
