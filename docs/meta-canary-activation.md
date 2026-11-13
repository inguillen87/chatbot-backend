# Activación acotada de Meta: Junín y piloto TDF

El registro operativo, las horas y los IDs de configuración están en el PR
backend #2829. Esta guía define la secuencia; no certifica que se haya ejecutado.

## Alcance

| Destino | Tenant | Phone Number ID | WABA | Operación |
| --- | --- | --- | --- | --- |
| Junín | 22 | 660753250460901 | 10068207913300410 | Alta de conexión y remitente Meta Production |
| TDF de prueba | 46 | 1137550632773388 | 2192778428137676 | Renovación de connection3/sender1, revisión6 a7 |

App: `1719510329487224`. La única persona autorizada para el canario es el
contacto de Marcelo ya aprobado en la configuración privada. No publicar su
número completo ni ampliar la allowlist. La WABA de Junín también contiene
Inmovar: una suscripción o callback de toda la cuenta puede afectarlo.

## 1. Publicar el código preparado

Publicar backend y la pantalla privada de superadmin
`/integracion/meta/credenciales`. El acceso administrativo puede habilitarse
después de comprobar autenticación, Origin, sesión vigente y rate limit
compartido. La pantalla usa la sesión existente; no solicita IDs de actor,
database URLs ni secretos de Vercel.

En este despliegue permanecen apagados los flags de callback, worker, outbound,
aislamiento Meta y cron. Las claves específicas de Meta pueden estar cargadas
sin modificar las claves ni los modos globales de Twilio.

Verificar el commit de `/api/version`, `/health/ready` y el `401` sin credenciales
de `/api/internal/cron/outbox-reconciliation`. Registrar este nuevo despliegue
con flags apagados: será el punto de reversión del canario posterior, pues ya
contiene el resolver Twilio que excluye los nuevos remitentes Meta.

## 2. Instalar las credenciales en el runtime autorizado

El administrador ingresa las dos credenciales nuevas en campos privados. No
se entregan por chat, comentarios, URLs, archivos o variables de entorno. El
backend ejecuta plan y apply dentro del runtime real de Production, con el
vault existente y la autoridad de escritura vigente.

- Status aporta IDs, revisión y epoch; no abre el token almacenado.
- Plan verifica app, permisos, vigencia, pertenencia del número y suscripción.
- La expiración local es el menor límite entre la política de30 días y las
  expiraciones finitas de Meta. Es una caducidad local cuando Meta no informa
  una expiración finita; no representa una fecha inventada del proveedor.
- Apply conserva exactamente el token, expiry, plan, database y revisiones
  validados. Cada destino tiene su propia transacción y auditoría.
- Un error después de apply puede ocultar un commit exitoso. Consultar status
  y correlacionar el hash de plan antes de cualquier reintento.

La operación de credenciales no modifica Meta, habilita flags ni envía mensajes.
Si el token no permite demostrar la suscripción requerida, la operación queda
bloqueada; no se salta esa validación para completar el alta.

## 3. Verificar y configurar el callback de Junín

Con credenciales vigentes, leer primero las suscripciones de la app/WABA y
`webhook_configuration` de Junín, Inmovar y TDF. Registrar las URLs efectivas y
sus precedencias, sin tokens de verificación. Confirmar la operación admitida
por la versión vigente de Graph antes de escribir.

El destino nativo de Junín es:

`https://api.chatboc.ar/webhook/meta/whatsapp/1719510329487224`

El destino de TDF sigue siendo:

`https://api.chatboc.ar/webhook/meta/tdf-sandbox`

Usar alcance por número para Junín. No trasladar la app completa ni la WABA de
producción al callback canario: el endpoint nativo sólo admite los IDs y el
contacto configurados y no puede recibir en su nombre el tráfico de Inmovar.
Guardar el estado anterior para restaurarlo y comprobar después que los otros
números conservan su configuración.

La identidad entrante y el destinatario de respuesta no se normalizan quitando
prefijos por conjetura. El piloto existente contiene una asociación explícita
entre ambos formatos. El circuito nativo exige el contacto exacto; cualquier
necesidad de asociación debe demostrarse con evidencia de Meta y conservar el
alcance del remitente. Los campos BSUID requieren un adaptador explícito: el
parser nativo actual los retiene sin admitir el evento, incluso si también
incluyen un número. No declarar compatible ese caso sin una prueba específica.

## 4. Activar y comprobar el canario

Después de completar el registro y la validación anterior, habilitar únicamente
la configuración Meta revisada: aislamiento con namespace `meta-v1`, tenant22,
callback, worker y outbound. Habilitar el cron de outbox si es el consumidor
elegido; su ruta programada existente es `outbox-reconciliation`, cada minuto.
No cambiar flags de mantenimiento, claves globales o credenciales Twilio.

Construir desde el commit exacto de main con la nueva configuración Production,
comprobar el deployment inmutable y promoverlo. Confirmar:

1. Commit y readiness correctos; cron sin credenciales devuelve401.
2. Challenge válido del callback y rechazo de firma inválida.
3. Scope del remitente y único contacto; un contacto revocado no puede despachar
   una respuesta ya encolada. Una petición al proveedor ya iniciada no puede
   cancelarse mediante un cambio de configuración posterior.
4. Consumers y cron operativos sin errores sostenidos ni crecimiento anómalo
   de colas.

Recién entonces avisar que se puede enviar «hola». Correlacionar el mensaje
entrante, turno, dos respuestas nativas ordenadas, ID de Meta, estado firmado y
respuesta observada. `accepted` por sí solo no demuestra entrega. Observar
diez minutos desde la promoción/prueba y anotar duración y resultados reales.

## Reversión

Antes de instalar credenciales o crear identidad Meta, revertir el código al
deployment previo y borrar sólo las nuevas entradas de preparación si se desea
deshacerla por completo. Sus IDs y horas están en #2829.

Después del alta, conservar el despliegue de la fase1 como reversión inmediata:
incluye la separación Twilio/Meta y mantiene los consumidores canarios apagados.
No volver a un resolver anterior que interprete un remitente Meta como Twilio.
Restaurar el override de Junín desde la lectura previa y verificar el resultado.

Conservar las claves de identidad, filas, referencias y auditoría si ya hubo
actividad: eliminarlas no equivale a revertir un envío. No restaurar como
recuperación la credencial TDF ya vencida. Las renovaciones se compensan con una
nueva credencial válida y una nueva revisión, manteniendo su rastro de auditoría.
