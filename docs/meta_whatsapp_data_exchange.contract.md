# Meta WhatsApp Flows Data Exchange - Data API 3.0

## Estado

Contrato e implementacion activa del endpoint de Data Exchange para Data API
`3.0`. El application factory registra el Blueprint y, cuando no se inyecta un
resolver explicito, instala el resolver runtime multi-tenant por defecto.

La implementacion local no configura por si sola la URL publica en Meta, no
sube claves publicas y no publica ninguna Flow. Esas operaciones requieren la
configuracion real de cada WABA y validacion externa.

Archivos de implementacion:

- `services/meta_flow_data_exchange.py`
- `services/meta_flow_runtime.py`
- `routes/meta_flow_data_exchange.py`
- `app.py`
- `tests/test_meta_flow_data_exchange.py`

## Referencias oficiales

- [Meta: Implementing your Flow endpoint](https://developers.facebook.com/documentation/business-messaging/whatsapp/flows/guides/implementingyourflowendpoint/)
- [Meta: WhatsApp Flows error codes](https://developers.facebook.com/docs/whatsapp/flows/reference/error-codes#endpoint_error_codes)
- [WhatsApp official Flows Tools: endpoint encryption](https://github.com/WhatsApp/WhatsApp-Flows-Tools/blob/main/examples/endpoint/nodejs/basic/src/encryption.js)
- [WhatsApp official Flows Tools: signature verification and endpoint](https://github.com/WhatsApp/WhatsApp-Flows-Tools/blob/main/examples/endpoint/nodejs/basic/src/server.js)
- [WhatsApp official Flows Tools: PING, INIT, data_exchange and error handling](https://github.com/WhatsApp/WhatsApp-Flows-Tools/blob/main/examples/endpoint/nodejs/basic/src/flow.js)

La implementacion sigue el contrato Data API `3.0` documentado por Meta:

1. Base64 decode de `encrypted_aes_key`.
2. RSA-OAEP con SHA-256 y MGF1-SHA-256 para obtener una clave AES de 128 bits.
3. Base64 decode de `encrypted_flow_data` e `initial_vector`.
4. AES-128-GCM; los ultimos 16 bytes de `encrypted_flow_data` son el authentication tag.
5. JSON UTF-8 como payload autenticado.
6. Para la respuesta, XOR de cada byte del IV original con `0xFF`.
7. AES-128-GCM de la respuesta JSON, concatenando ciphertext + tag de 16 bytes y devolviendo Base64 como `text/plain`.

## Endpoint HTTP

```text
POST /api/whatsapp/flows/data-exchange/{endpoint_id}
Content-Type: application/json
X-Hub-Signature-256: sha256=<hex-hmac>
```

`endpoint_id` es un identificador no secreto y estable. Puede ser el WABA ID o un alias opaco. Nunca debe ser una clave, token o secreto. La URL publica configurada en Meta debe ser HTTPS. TLS puede terminar en un proxy confiable; este endpoint no interpreta directamente `X-Forwarded-Proto` ni confia en headers de proxy sin configuracion global.

Envelope requerido:

```json
{
  "encrypted_flow_data": "<base64>",
  "encrypted_aes_key": "<base64>",
  "initial_vector": "<base64>"
}
```

La firma HMAC-SHA256 se calcula sobre los bytes HTTP crudos, antes de parsear JSON. Cuando hay `app_secret`, una firma ausente, mal formada o incorrecta devuelve `432`. Se admite `previous_app_secret` solamente para una ventana controlada de rotacion. Si no hay app secret configurado, la validacion se omite y se registra un warning sin payload; produccion deberia configurar el secreto.

## Resolucion WABA/tenant

La ruta resuelve primero un registro explicito en
`META_FLOW_DATA_EXCHANGE_ENDPOINTS` y, si no existe, usa el callable
`META_FLOW_DATA_EXCHANGE_CONFIG_RESOLVER`. El application factory instala por
defecto el resolver de `services.meta_flow_runtime`, que vincula `endpoint_id`,
WABA, sender y tenant antes de descifrar el payload.

Los mecanismos server-side admitidos son:

- `META_FLOW_DATA_EXCHANGE_CONFIG_RESOLVER`: callable que recibe `endpoint_id` y devuelve `MetaFlowEndpointConfig`, un mapping equivalente o `None`.
- `META_FLOW_DATA_EXCHANGE_ENDPOINTS`: mapping indexado por `endpoint_id`.

Campos del mapping:

```python
{
    "endpoint_id": "<opaque endpoint alias>",
    "waba_id": "<resolved WABA ID>",
    "tenant_id": "<internal tenant ID>",
    "private_key_pem": "<secret PEM from secret manager>",
    "private_key_passphrase": "<optional secret>",
    "app_secret": "<Meta app secret>",
    "previous_app_secret": "<optional rotation secret>",
    "handlers": {
        "init": init_handler,
        "back": back_handler,
        "data_exchange": exchange_handler,
        "error": error_handler,
    },
}
```

El resolver debe obtener secretos desde el secret manager o configuracion segura del deployment. No debe devolverlos por APIs, serializarlos ni incluirlos en logs. `MetaFlowEndpointConfig.__repr__` esta redactado y los handlers reciben solo `MetaFlowRequestContext` con IDs no secretos.

No se usa `tenant_id`, `waba_id`, `flow_token` ni datos del formulario sin cifrar para seleccionar la clave. La clave se resuelve antes de descifrar y queda ligada al `endpoint_id`, evitando un confused-deputy entre tenants.

## Handlers Data API 3.0

Firma sincronica:

```python
def handler(payload: Mapping[str, Any], context: MetaFlowRequestContext) -> Mapping[str, Any]:
    ...
```

Despacho:

| Entrada | Comportamiento |
| --- | --- |
| `action=ping` o `PING` | Handler `ping` opcional; default oficial `{"data":{"status":"active"}}`. |
| `data.error` presente | Handler `error` opcional; default oficial `{"data":{"acknowledged":true}}`. |
| `action=INIT` | Requiere handler `init`. |
| `action=BACK` | Requiere handler `back`. |
| `action=data_exchange` | Requiere handler `data_exchange`. |

No hay default de exito para INIT, BACK o data_exchange. La ausencia de handler devuelve un error cifrado `handler_not_configured`; no crea pedidos, reclamos, publicaciones ni estados ficticios.

El handler debe validar `screen`, `flow_token`, campos, autorizacion e idempotencia antes de ejecutar efectos. El endpoint valida transporte y criptografia, no reemplaza reglas de negocio.

Para mantener una unica cadena de confianza entre emision, Data Exchange y
webhook de finalizacion, `WHATSAPP_FLOW_TOKEN_KEY_V1` es la clave preferida. Las
referencias o variables derivadas por WABA se conservan solo como fallback de
compatibilidad; no deben activarse de forma aislada si el emisor y el webhook
no usan exactamente la misma clave.

## Limites y errores

Defaults:

- Request HTTP cifrado: 64 KiB.
- Payload JSON descifrado: 64 KiB.
- JSON de respuesta: 64 KiB.
- Limite absoluto configurable: 1 MiB.
- IV Data API 3.0: 16 bytes.
- AES key: 16 bytes.
- GCM tag: 16 bytes.
- RSA: 2048 a 8192 bits.
- JSON: sin claves duplicadas, `NaN` o infinitos; profundidad maxima 24 y 4096 nodos.

Codigos HTTP:

| HTTP | Codigo estable | Uso |
| --- | --- | --- |
| 400 | `invalid_envelope`, `invalid_decrypted_payload`, `unsupported_action` | Request invalido. |
| 404 | `endpoint_not_found` | No existe configuracion para el endpoint. |
| 413 | `payload_too_large` | Supera el limite antes de procesar el body. |
| 415 | `unsupported_media_type` | No es `application/json`. |
| 421 | `decryption_failed` | Clave ausente/invalida, OAEP fallido, AES key/IV invalidos o tag GCM invalido. Meta puede refrescar la public key y reintentar. |
| 432 | `invalid_signature` | HMAC `X-Hub-Signature-256` invalido. |
| 500 | `handler_failed`, `handler_not_configured`, `response_encryption_failed` | Error posterior al descifrado. |
| 503 | `endpoint_configuration_invalid`, `cryptography_unavailable` | Configuracion o dependencia no disponible; fail closed. |

Los errores previos al descifrado son JSON estructurado con `code`, mensaje estable y `request_id`. Los errores posteriores al descifrado se intentan devolver cifrados con el mismo contrato. Nunca se incluyen cuerpos, firmas, PEM, app secrets, AES keys, IV, flow tokens, screen data ni mensajes internos de excepcion.

## Integracion operativa y pendientes reales

Ya estan integrados el Blueprint, el resolver runtime multi-tenant y los
handlers de lectura para `claim_tracking_helpdesk` y `order_checkout`.

Antes de habilitar una Flow en produccion falta, por cada WABA real:

1. Cargar la clave privada, su passphrase opcional y el app secret mediante
   referencias seguras; nunca en metadata, repositorio o logs.
2. Subir y verificar en Meta la public key correspondiente a esa misma WABA.
3. Configurar en Meta la URL HTTPS publica del endpoint.
4. Ejecutar el health check PING y las pruebas cifradas de INIT, BACK y
   `data_exchange`.
5. Publicar la revision exacta del Flow JSON validado y registrar el hash
   aprobado.
6. Ejecutar una prueba viva de punta a punta con el sender y tenant correctos
   antes de habilitar el envio general.
