# Callback de Junín por número: inspección, cambio y recuperación

Estado: adaptador de inspección implementado localmente para revisión; escritura
de callback pendiente, sin ejecución contra Meta. Actualizado: 2026-11-13.
Base inspeccionada: `e05a2cc62d8856d34c003502018f08776fce9b93`, árbol
`5e5a4075a29aae85300f43a4b30627076c337cf7` (PR backend #2831).

Este documento completa la fase de callback de
[la guía de activación](meta-canary-activation.md). No certifica una publicación,
un token, una suscripción, un cambio de callback ni una entrega real. El estado
de partida previsto es código publicado con Meta apagado y administración de
credenciales habilitada; verificarlo en el deployment antes de operar.

## 1. Alcance fijo y controles

| Recurso | Identidad esperada | Papel |
| --- | --- | --- |
| Aplicación | `1719510329487224` | Aplicación compartida; no modificar su callback global |
| Junín | tenant `22`, slug `junin`, Production, `+17432643718`, phone ID `660753250460901`, WABA `10068207913300410` | Único candidato a cambio de callback |
| Inmovar | `+18564858583`, WABA compartida `10068207913300410` | Control; el Phone Number ID aún no está validado |
| TDF de prueba | tenant `46`, slug `tierra-del-fuego`, sandbox, `+15556565679`, phone ID `1137550632773388`, WABA `2192778428137676` | Control con credencial propia; no cambiar callback |

El número de Inmovar procede de una lectura comunicada por el operador, no de
una respuesta Graph conservada en este documento. Debe quedar asociado de forma
única a un ID mediante inventario Graph antes de habilitar un plan de escritura.
No deducirlo a partir del nombre comercial ni copiar el ID de otro número.

Destino propuesto exclusivamente para Junín:

`https://api.chatboc.ar/webhook/meta/whatsapp/1719510329487224`

Destino institucional del piloto TDF, a comparar con su configuración observada:

`https://api.chatboc.ar/webhook/meta/tdf-sandbox`

El único contacto canario es el ya aprobado en la configuración privada. No
incluir su número en el runbook, la salida de inspección ni un comentario de PR.
No ampliar la allowlist para resolver un rechazo de identidad.

## 2. Evidencia primaria y límite del contrato

Fuente oficial: [Meta OpenAPI v23.0, commit fijo e96a1c9](https://github.com/meta/openapi/blob/e96a1c9dbf240c2937b6a95920685230458e346b/business-messaging-api_v23.0.yaml).
El repositorio [se identifica como especificación oficial de Meta](https://github.com/meta/openapi).
La [descarga pública del mismo archivo](https://raw.githubusercontent.com/meta/openapi/e96a1c9dbf240c2937b6a95920685230458e346b/business-messaging-api_v23.0.yaml)
permitió inspeccionarlo sin credenciales.

| Hecho documentado | Referencia primaria |
| --- | --- |
| POST sobre `/{Version}/{Phone-Number-ID}` admite `webhook_configuration` | `updatePhoneNumberStatus`, líneas 15849–16008 |
| El objeto contiene `override_callback_uri` y `verify_token`; el primer campo es obligatorio y admite cadena vacía | `PhoneNumberStatusUpdateRequest` / `WebhookConfiguration`, líneas 3417–3481 |
| Se requiere aplicación vinculada a WABA y suscripción previa; se verifica el callback externo | Descripción de `updatePhoneNumberStatus` |
| La autenticación describe un token de usuario o usuario del sistema; `bearerAuth` menciona `whatsapp_business_management` | Líneas 262–267 y 8469–8478 |

La especificación inspeccionada no define el objeto de respuesta GET
`webhook_configuration`, su precedencia efectiva ni afirma expresamente que
la cadena vacía elimine el override. Tampoco basta para certificar v25.0, la
versión configurada por el runtime. Son condiciones pendientes de la escritura,
no permisos para inventar un parser o declarar recuperable la operación.

No se halló `WhatsAppBusinessPhoneNumber` en los árboles actuales de los SDK
oficiales Python, Node y PHP; no citar esa clase como prueba. La página oficial
de overrides respondió 429 en la investigación previa y no se volvió a
solicitar. La colección oficial de Meta en Postman se identifica en
[este workspace](https://www.postman.com/meta/whatsapp-business-platform/overview),
pero su lectura no aportó el contrato faltante. Los ejemplos de terceros no se
usaron como especificación de producción.

Para cerrar el contrato faltan dos evidencias:

1. Lectura real, sin mutación, de `webhook_configuration` en la versión efectiva,
   con identidad del número comprobada y proyección privada de sus valores.
   La respuesta demuestra lo observado; no demuestra por sí sola toda la
   precedencia de entrega ni la semántica de una futura eliminación.
2. Fuente primaria que defina retirada/restauración del override, o una prueba
   separada y autorizada sobre un número descartable. No usar Junín, Inmovar ni
   TDF como experimento de mutación para descubrir esa semántica.

## 3. Qué puede ejecutar el código de esta base

| Pieza existente | Capacidad comprobada | Límite |
| --- | --- | --- |
| [Administración HTTP](../routes/meta_credential_admin.py) y [runtime](../services/meta_credential_admin_runtime.py) | Sesión Clerk verificada, superadmin allowlisted, Origin exacto, rate limit compartido, transacciones y revisión de credenciales | `status`, `plan` y `apply` de instalación/rotación; no cambia callbacks de Meta |
| [Inspección por número](../services/meta_phone_webhook_inspection.py) | Binding y vault exactos, autoridad Graph, inventario, descubrimiento y revalidación final | Endpoint fijo de sólo lectura; no verifica aún semántica de readback/reversión ni habilita escritura |
| [Frontera privada](../utils/meta_credential_http.py) | JSON acotado, Production real, no-store y supresión de logs en el prefijo administrativo | Endpoints registrados enumerados; una URL nueva no queda autorizada por compartir prefijo |
| [Vault Meta](../services/meta_whatsapp_credentials.py) | Abre un envelope vigente ligado a tenant, conexión, proveedor, entorno, app y WABA | No exporta el token ni acredita acceso externo por sí solo |
| [Runtime Graph](../services/meta_whatsapp_runtime.py) | TLS a `graph.facebook.com`, límites, sin redirects/reintentos; autoridad app/scopes/membresía/suscripción | Es una biblioteca interna, no una consola ni un endpoint administrativo genérico |
| [Callback Junín](../routes/meta_whatsapp_callback.py) | Challenge GET y admisión POST firmada con scope canario | Devuelve 404 mientras `META_WHATSAPP_CALLBACK_ENABLED` sea false o falte configuración |

`/api/internal/meta-credentials/status` no llama Graph ni descifra el token.
Después de instalar Junín y renovar TDF, `operation_available: false` es normal
para el contrato de carga de una sola vez. Confirmar revisión, vigencia y recibo
de auditoría; no exigir que vuelva a quedar habilitado para cargar otra vez.

El baseline #2831 no incluye esta inspección: su disponibilidad requiere una
publicación revisada de este cambio posterior. Una vez publicada y con ambas
credenciales instaladas, el endpoint fijo puede ejecutar los GET enumerados
desde el runtime. Sigue sin haber un canal para POST del override. No extraer
variables de Vercel, abrir el vault en una estación de trabajo ni usar cookies
del navegador para hacer llamadas a Graph.

## 4. Adaptador de inspección de sólo lectura

Implementado para revisión y publicación separada:

`POST /api/internal/meta-credentials/inspect-junin-webhook`

El POST es hacia nuestro servidor autenticado; todos los requests del adaptador
a Graph son GET. Body fijo: `{"target":"junin-production"}`. Rechaza
propiedades adicionales, tokens, URLs, IDs de actor, scopes, DSNs y comandos.

Reutiliza el contrato de actor, linaje y revocación del admin actual y registra
explícitamente la ruta en la frontera privada. Conserva Production, Origin,
JSON, no-store, errores finitos y el límite compartido de operaciones de
12 por minuto / 60 por hora, con Redis requerido en Production. La excepción
`cutover_read_only_view` está declarada sólo sobre este endpoint. La inspección
usa dos transacciones de sólo lectura y no solicita leases de escritura ni
instala credenciales. No requiere activar callback, worker ni outbound Meta.

Secuencia interna:

1. Autenticar al actor y leer las dos conexiones/senders con identidad exacta.
   Comprobar las revisiones instaladas observadas y expiración futura. Abrir las
   credenciales dentro del runtime; nunca devolver plaintext, ciphertext, nonce
   ni configuración completa. Cerrar la transacción de lectura antes de I/O.
2. Verificar app, scopes, expiraciones, WABA, phone y suscripción con la autoridad
   existente. Junín usa sus registros de Production; TDF usa su configuración
   sandbox. No llamar `MetaOutboundRuntime.load_binding` para TDF: ese método
   fija `environment="production"`. Reutilizar sólo las piezas compatibles de
   validación y `open_token` con el binding sandbox exacto.
3. Ejecutar exclusivamente las lecturas enumeradas en la sección siguiente.
   Usar el Bearer de cada WABA desde el vault. Reservar el app-token interno
   para `debug_token`; no emplearlo como credencial de phone/WABA.
4. Revalidar actor/linaje, binding local, revisión y vigencia en otra transacción
   de lectura antes de publicar el resultado. Si cambió cualquiera, retirar la
   observación y devolver un código finito. Tras cerrar esa transacción, volver
   a comprobar reloj, expiración de sesión y pruebas de autoridad. El resultado
   no es un plan de escritura.
5. Emitir metadata acotada, digests y hechos observados. No persistir ni cachear
   tokens, no enviar mensajes, no tocar filas de negocio/credenciales y no
   modificar Meta, flags ni cron.

Máximo nueve requests Graph por inspección completa: tres verificaciones de
autoridad por credencial y tres lecturas de número. No sigue paginación ni
reintenta. Presupuesto de ejecución de 35 segundos, tope de I/O de 4 segundos
y reserva de cierre de 3 segundos; cada respuesta Graph tiene un máximo de
64 KiB. La vigencia de evidencia se acota a la menor vigencia de ambas pruebas
de autoridad y de la sesión del actor. No intentar llamar el endpoint en un
deployment hasta confirmar que contiene este cambio. El acceso legítimo de
Claude puede aportar inventario adicional; no sustituye las comprobaciones.

## 5. Matriz exacta de lecturas Graph

`V` significa la versión validada de configuración, actualmente prevista v25.0.
El host es siempre `https://graph.facebook.com`. Son especificaciones de requests
internos; no son comandos con secretos para copiar a una terminal.

| Paso | Request GET | Credencial / condición |
| --- | --- | --- |
| A | `/V/debug_token` con `input_token` privado | App-token de la aplicación correcta, sólo dentro del transporte sin logs; validar cada token de WABA |
| B | `/V/10068207913300410/phone_numbers?fields=id,display_phone_number&limit=100` | Token vigente de Junín; encontrar exactamente Junín e Inmovar por número/ID |
| C | `/V/2192778428137676/phone_numbers?fields=id,display_phone_number&limit=100` | Token propio TDF; comprobar su phone ID |
| D | `/V/10068207913300410/subscribed_apps` | Token Junín; comprobar aplicación y registrar configuración observada |
| E | `/V/2192778428137676/subscribed_apps` | Token TDF; misma comprobación, sin modificar suscripción |
| F | `/V/660753250460901?fields=id,display_phone_number,webhook_configuration` | Token Junín; contrato del campo en lectura pendiente de evidencia |
| G | `/V/INMOVAR_ID_VALIDADO?fields=id,display_phone_number,webhook_configuration` | Sólo después de resolver B de forma única; token de la WABA compartida |
| H | `/V/1137550632773388?fields=id,display_phone_number,webhook_configuration` | Token TDF; misma condición de evidencia que F |

`fields=webhook_configuration` es una consulta de descubrimiento de sólo lectura.
Un rechazo, un campo omitido o una forma desconocida significa evidencia
incompleta. No convertir `null`, ausencia o error en "sin override". No construir
un parser de precedencia usando sólo una captura de terceros.

Aplicar el comportamiento cerrado del runtime actual frente a paginación
incompleta: no declarar ausencia con una primera página ni seguir ciegamente
`paging.next` (puede llevar parámetros privados). Si hace falta paginar, revisar
por separado un recorrido limitado a los mismos paths con cursores opacos,
sin redirects ni URLs proporcionadas por el cliente.

No añadir lecturas genéricas a `/{APP_ID}/subscriptions` sin revisar su permiso
y adaptador. Registrar la configuración de la aplicación desde una observación
administrativa legítima existente o un contrato primario específico. Una
suscripción WABA no demuestra por sí sola que el campo `messages` esté activo
en la aplicación.

### Salida de inspección

Contrato nuevo, no confundible con un recibo de escritura:
`chatboc.meta_phone_webhook_inspection.v1`.

- Versión Graph, hora y vigencia de la observación; identidad de base sólo como
  digest. Registrar el commit/deployment por separado mediante `/api/version`
  y Vercel; el reporte no afirma haber verificado la publicación.
- WABA/phone/tenant/entorno, revisión local, fecha de vencimiento y resultado
  de autoridad; ID de Inmovar sólo después de su asociación comprobada.
- Inventario completo o indicador de incompletitud; digests canónicos de las
  suscripciones y configuraciones de Junín, Inmovar y TDF.
- Proyección de campos candidatos `application`, `whatsapp_business_account` y
  `phone_number`, tipos observados, digests y comparación exacta con el destino
  fijo. Sus nombres no afirman un esquema verificado. Las claves desconocidas
  sólo se cuentan. Las URLs desconocidas se omiten completamente, incluido
  origen y userinfo; sólo los dos callbacks públicos fijos pueden devolverse.
  No volcar JSON Graph bruto en la respuesta, logs, archivos o comentarios.
- `provider_mutations:false`, `messages_sent:false`, `writes_performed:false`.
- Lista de bloqueos concretos; `inspection_complete:false` y `write_ready:false`
  siempre en esta versión. Incluso una forma candidata conserva
  `schema_verified:false`: siguen faltando contrato primario de readback,
  reversión y adaptador de mutación. Un fallo de autoridad/paginación rechaza
  el reporte; un fallo de descubrimiento devuelve ese recurso como incompleto.

Para revisar una reversión futura, una URL anterior con segmentos privados debe
quedar únicamente en estado administrativo protegido dentro de Production. Su
digest público no alcanza para reconstruirla y no justifica exportarla.

## 6. Orden correcto entre flags, receptor y override

Con el baseline Meta apagado, un 404 del callback es esperado y no prueba que
la URL esté mal. No intentar escribir el override esperando habilitar el
callback después: Meta verifica el destino externo durante esa operación.

Antes de una futura escritura, preparar y publicar un receptor revisado:

- `META_WHATSAPP_CALLBACK_ENABLED` activo y registros privados de app-secret y
  verify-token correctos (`META_WHATSAPP_APP_SECRETS_JSON` y
  `META_WHATSAPP_CALLBACK_VERIFY_TOKENS_JSON`). No imprimir valores.
- `META_WHATSAPP_CALLBACK_CANARY_RECIPIENTS_JSON` con app/WABA/phone/tenant y el
  único contacto autorizado, sin IDs de Inmovar/TDF.
- Aislamiento Meta activo, `META_WHATSAPP_QUEUE_TENANT_IDS` limitado a `22` y sus
  claves/version propias. No modificar modos, secretos ni derivación Twilio.
- Vault y sender Junín válidos, writer authority vigente y admisión transaccional
  lista. Un challenge 200 sólo prueba el handshake, no la admisión POST.
- Workers y outbound Meta pueden permanecer apagados mientras se verifica el
  receptor y el readback. No anunciar todavía la prueba "hola".

Comprobar el challenge desde el runtime autorizado sin sacar el verify-token a
una URL de chat, consola o reporte. Observar sólo resultado y digest de nonce.
El POST sin firma debe rechazarse. No fabricar un mensaje firmado en Production
para aparentar una entrega real.

Esta secuencia requiere una publicación revisada posterior al baseline; este
documento no habilita flags ni certifica que ya se hayan habilitado.

## 7. Diseño de la escritura futura: aún bloqueada

Sólo después de cerrar los requisitos anteriores, implementar/revisar una
operación separada, fija a `660753250460901`. No extender ahora el admin existente
con un POST de mutación basado en semántica incompleta.

El plan debe ligar actor/linaje, runtime/base, versión Graph, revisión del vault,
epoch, identidad exacta de Junín, callback deseado fijo y digests anteriores de
Junín y de ambos controles. Comprobar nuevamente esos hechos antes del envío;
un cambio de credencial, control, autoridad o callback exige un plan nuevo.

La única escritura candidata a Meta tiene esta forma documentada:

```json
{
  "webhook_configuration": {
    "override_callback_uri": "https://api.chatboc.ar/webhook/meta/whatsapp/1719510329487224",
    "verify_token": "<obtenido internamente de la configuración privada>"
  }
}
```

No agregar campos de estado, registro, nombre, PIN ni `webhook_url` por imitar
un ejemplo de actualización integral. No tocar `/APP_ID/subscriptions`, WABA
`subscribed_apps`, registro del número, historial ni remitentes Twilio.

Reutilizar lease y sesión de actor con duración acotada. Registrar la intención
durable antes del único POST y un resultado observado después. Meta y PostgreSQL
no forman una transacción atómica: un commit local o un HTTP 200 aislado no
demuestra que el callback observado coincida. Si el proceso pierde el ACK,
marcar resultado externo incierto y consultar estado antes de otra escritura.
Una clave de operación local evita repetir un intento incierto; no simular una
garantía de idempotencia de Graph que no está documentada.

La recuperación readonly de un intento incierto debe seguir disponible si el
receptor o los workers se apagan. No hacer depender la inspección de esos flags.

## 8. Readback, controles y recuperación

Después del único POST futuro, repetir F/G/H y D/E con las mismas identidades y
credenciales vigentes, más la observación del callback global de aplicación
si forma parte del contrato aprobado. Confirmar únicamente cuando:

1. La lectura validada de Junín prueba el callback por número esperado.
2. Los digests de Inmovar, TDF y suscripciones no cambiaron.
3. La sesión, revisión, epoch y runtime del intento siguen siendo los previstos.
4. No quedan errores, paginación o campos ausentes convertidos en éxito.

| Estado anterior de Junín | Recuperación que debe estar resuelta antes de aplicar |
| --- | --- |
| Sin override de número, demostrado | Retirar sólo el override de Junín y volver a verificar la configuración heredada anterior; falta cerrar la semántica oficial de retirada |
| Override anterior propio y controlado | Restaurar URL exacta con su verify-token disponible dentro del runtime; verificar de nuevo todos los controles |
| Override externo o verify-token anterior desconocido | Bloqueo: conocer la URL no garantiza que Meta permita restaurarla; no aplicar mientras no exista una reversión ejecutable |
| Estado anterior ambiguo/incompleto | Bloqueo: completar lecturas/contrato; no usar un valor vacío como ensayo |

La forma candidata `{"webhook_configuration":{"override_callback_uri":""}}`
contiene un valor permitido por OpenAPI, pero la investigación primaria aún no
acredita su efecto exacto de retirada. No se proporciona un comando ejecutable
de reversión hasta resolverlo.

Antes de compensar, confirmar que la configuración actual de Junín es todavía
la escrita por esta operación. Si un tercero la cambió, no sobreescribirla con
un snapshot viejo. Un error sostenido permite detener sólo consumidores Meta
canarios, conservar evidencia/filas y usar el deployment baseline con aislamiento
de remitentes Twilio/Meta. Apagar el receptor no devuelve el tráfico al callback
anterior: la compensación externa sigue siendo un paso independiente.

## 9. Aceptación funcional posterior

Con override y controles ya confirmados, publicar la activación revisada de
worker/outbound y el consumidor/cron elegido. Comprobar readiness, commit y el
401 del cron sin credenciales. Avisar entonces al único contacto canario para
que envíe un mensaje nuevo "hola".

Correlacionar mensaje entrante, tenant/phone correctos, turno confirmado, dos
respuestas de texto del productor existente, IDs Meta, estado firmado y respuesta
observada. No reproducir mensajes anteriores ni generar envíos a terceros.
`accepted`, challenge 200 y dos tildes de una observación anterior no sustituyen
esta evidencia. El parser nativo mantiene el bloqueo de identidades BSUID no
adaptadas y media no admitida; no normalizar contactos por conjetura.

Observar diez minutos desde la prueba/promoción y registrar el intervalo real,
estados y crecimiento de colas. La observación no puede empezar antes de la
prueba ni certificarse mientras el usuario está ausente.

## 10. Entregas y comprobaciones siguientes

1. Revisar este diseño y completar en paralelo la evidencia primaria pendiente.
2. Revisar y publicar por separado el adaptador readonly de sección 4. Su suite
   `tests/test_meta_phone_webhook_inspection.py` cubre auth/frontera privada,
   binding/revisión, paginación incompleta, mapping único Inmovar, transporte
   GET-only, cambios durante Graph, caducidad posterior a la segunda transacción,
   salida/logs sin secretos y cero escrituras de negocio/proveedor. Se incluye
   en el CI de canales junto con las regresiones del administrador existente.
3. Cuando las credenciales se instalen por la pantalla privada, confirmar los
   recibos de Junín/TDF e inspeccionar desde ese canal seguro; no pedir al usuario
   que copie tokens o IDs técnicos a otro lugar.
4. Fijar en el PR la evidencia de Inmovar, versión y configuración observada,
   usando sólo metadata/digests. Resolver todos los bloqueos de secciones 2 y 8.
5. Implementar por separado la operación fija de plan/apply/status/compensación,
   con pruebas de intención durable, ACK incierto, autorización/epoch revocados,
   estado concurrente y controles ajenos intactos. Después publicar el receptor,
   operar Junín y verificar las lecturas antes de activar la respuesta canaria.

El cambio implementa únicamente la inspección autenticada. La validación local
ejecutó 152 pruebas: 56 de inspección, 48 del admin HTTP y 48 del instalador,
con SQLite y Graph sintéticos, red externa bloqueada. Comprueba comportamiento
de aplicación y persistencia local; no certifica un esquema Graph real,
publicación ni locks de PostgreSQL. No implementa ni ejecuta la escritura o
compensación externas descritas en las secciones 7 y 8.
