# Meta WhatsApp Data Exchange Runtime

## Alcance

`services/meta_flow_runtime.py` implementa la resolucion server-side y los handlers
de negocio de solo lectura para los Flow JSON compilados:

- `claim_tracking_helpdesk`
- `order_checkout`

El runtime se conecta a `routes.meta_flow_data_exchange` como
`META_FLOW_DATA_EXCHANGE_CONFIG_RESOLVER`. No descifra payloads ni implementa
criptografia. Esa responsabilidad sigue en `services.meta_flow_data_exchange`.

El application factory registra el Blueprint de Data Exchange e instala este
resolver por defecto cuando la aplicacion no recibe uno explicito.

## Cadena de confianza

La resolucion ocurre antes de descifrar el request:

1. `endpoint_id` se busca contra `ProviderSender.waba_id` o un alias explicito.
2. Todos los matches deben pertenecer a un unico par `tenant_id + waba_id`.
3. El `TenantProfile` debe existir, coincidir con el sender y estar activo.
4. Los secretos se cargan por referencias a variables de entorno.
5. El `MetaFlowEndpointConfig` queda ligado al tenant y WABA resueltos.
6. El `flow_token` se valida con
   `services.whatsapp_flow_security.verify_whatsapp_flow_endpoint_token`.
7. El runtime vuelve a comprobar que `tenant_id`, `provider_sender_id`,
   `flow_id`, `interaction_id` y `recipient_hash` del resultado pertenecen al
   scope del endpoint.

El payload descifrado nunca elige tenant, WABA, sender, clave, reclamo ni
pedido. Los campos `tenant_id`, `waba_id`, `provider_sender_id`, `order_id`,
`recipient`, `amount` o productos enviados por el cliente no son autoridad.

## Resolver

Integracion activa en el application factory:

```python
from services.meta_flow_runtime import create_meta_flow_runtime_resolver

app.config["META_FLOW_DATA_EXCHANGE_CONFIG_RESOLVER"] = (
    create_meta_flow_runtime_resolver()
)
```

El objeto retornado es callable y tambien expone `resolve(endpoint_id)`.
Devuelve `None` para un endpoint inexistente. Una colision de alias entre WABA
distintos devuelve `endpoint_scope_ambiguous` y falla cerrado.

## Alias permitidos

Los alias pueden vivir en `ProviderSender.metadata_json` o
`TenantProfile.configuracion`. Se admiten los bloques:

```json
{
  "meta_flow_data_exchange": {
    "endpoint_id": "junin-production",
    "endpoint_aliases": ["junin-flow"]
  },
  "meta_platform": {
    "data_exchange": {
      "endpoint_id": "junin-production",
      "endpoint_aliases": ["junin-flow"]
    }
  }
}
```

Tambien se reconoce el campo existente
`meta_flow_data_exchange_endpoint_id`. Un alias configurado a nivel tenant con
mas de un WABA produce ambiguedad; debe moverse al sender correspondiente.

## Referencias de secretos

No se aceptan PEM ni app secrets directos desde metadata o configuracion. Solo
se leen referencias seguras con formato `env:NOMBRE_VARIABLE`, `NOMBRE_VARIABLE`
o `{ "env": "NOMBRE_VARIABLE" }`.

Campos:

```json
{
  "private_key_ref": "env:META_FLOW_PRIVATE_KEY",
  "private_key_passphrase_ref": "env:META_FLOW_PRIVATE_KEY_PASSPHRASE",
  "app_secret_ref": "env:META_FLOW_APP_SECRET",
  "previous_app_secret_ref": "env:META_FLOW_PREVIOUS_APP_SECRET",
  "flow_token_key_ref": "env:META_FLOW_TOKEN_KEY_COMPAT"
}
```

`WHATSAPP_FLOW_TOKEN_KEY_V1` es la fuente preferida para validar el token. La
misma clave global debe usarse al emitir la Flow, resolver Data Exchange y
procesar el webhook final. Esto evita que un token valido en una etapa resulte
invalido en otra.

`flow_token_key_ref` y la variable derivada por WABA se conservan solo como
fallback compatible. No deben usarse en produccion salvo que emisor, endpoint
y webhook hayan sido configurados de forma explicita con esa misma clave.

Si no hay una referencia para los demas secretos, el fallback determinista por
WABA es:

```text
META_FLOW_WABA_<WABA_NORMALIZADO>_PRIVATE_KEY_PEM
META_FLOW_WABA_<WABA_NORMALIZADO>_PRIVATE_KEY_PASSPHRASE
META_FLOW_WABA_<WABA_NORMALIZADO>_APP_SECRET
META_FLOW_WABA_<WABA_NORMALIZADO>_PREVIOUS_APP_SECRET
META_FLOW_WABA_<WABA_NORMALIZADO>_FLOW_TOKEN_KEY_V1
```

El PEM admite saltos de linea reales o `\\n` escapados. Los nombres de variable
solo aceptan mayusculas, numeros y guion bajo. Una referencia invalida no hace
fallback silencioso. El `repr` de `MetaFlowEndpointConfig` no expone secretos.

## Verificacion endpoint-safe

El runtime usa directamente:

```python
verify_whatsapp_flow_endpoint_token(
    token,
    secret=flow_token_secret,
    tenant_id=tenant_id,
    allowed_flow_ids={"claim_tracking_helpdesk", "order_checkout"},
)
```

Esta API valida firma, TTL, tenant, flow, interaccion persistida, sender y el
`recipient_hash` firmado sin necesitar telefono, tenant o recipient enviados en
el endpoint. El runtime comprueba ademas que el sender retornado pertenece al
WABA resuelto. El token se verifica pero no se consume.

## Reclamos

`data_exchange` desde `CLAIM_LOOKUP` requiere:

```json
{
  "ticket_number": "M-100001",
  "access_pin": "900144"
}
```

La busqueda siempre queda acotada al tenant:

- `MunicipioTicket`: `tenant_id`; para datos legacy se permite el propietario
  municipal del mismo tenant cuando `tenant_id` esta vacio.
- `PymeTicket`: `tenant_id` exacto.
- `TenantTicket`: `tenant_id` exacto y credenciales en `datos_extra`.

Los prefijos `M-`, `P-` y `T-` seleccionan el modelo correspondiente. Sin
prefijo se mantiene la busqueda compatible en los tres modelos y se falla
cerrado si mas de uno coincide.

La respuesta `CLAIM_RESULT` contiene solo `status`, `last_update` y un resumen
operativo generico. No devuelve categoria libre, nombre, DNI, telefono, email,
direccion, descripcion, adjuntos ni comentarios.

Al completar la Flow, el payload del formulario entrega `ticket_number` desde
`CLAIM_LOOKUP` y `follow_up_note` desde `CLAIM_RESULT`. Ambos son datos
ingresados por la persona; el backend vuelve a aplicar scope, autorizacion e
idempotencia antes de cualquier escritura posterior.

## Pedidos

`order_checkout` nunca usa `order_id`, productos o monto enviados por el
cliente. La referencia autorizada debe estar persistida en
`WhatsAppFlowInteraction.metadata_json`:

```json
{
  "order_context": {
    "kind": "order",
    "id": "order-authoritative-001"
  }
}
```

Kinds admitidos:

- `order`: `Order`
- `market`: `MarketOrder`
- `pyme`: `PymePedido`
- `conversational`: `PedidoConversacional`

Tambien se reconocen las claves server-side `order_id`, `market_order_id`,
`pyme_pedido_id`, `pedido_conversacional_id` y `order_ref`. Cada consulta exige
`tenant_id` exacto. Sin contexto persistido se devuelve `order_context_missing`.

El envio admin exige un `order_context`, lo valida contra un pedido real del
mismo tenant y persiste unicamente la referencia canonica autorizada en la
interaccion. Un pedido inexistente, de otro tenant o de un tipo no admitido
bloquea el envio antes de emitir el token.

La respuesta `ORDER_CONFIRM` calcula cantidad y total desde la orden guardada.
Los datos de entrega ingresados en el Flow se validan por forma y longitud, pero
este endpoint inicial no los persiste.

Al completar la Flow, el formulario entrega `full_name`, `phone`,
`delivery_address`, `delivery_notes` y `confirm_order` mediante referencias a
los formularios `ORDER_DETAILS` y `ORDER_CONFIRM`. No entrega IDs, totales,
precios, tokens ni estado de servidor controlados por el cliente.

## Acciones e idempotencia

- `INIT`: verifica token y abre la pantalla inicial.
- `BACK`: verifica token y vuelve a la pantalla inicial.
- `data_exchange`: ejecuta lookup tenant-bound solo desde `CLAIM_LOOKUP` u
  `ORDER_DETAILS`; las pantallas de resultado no se aceptan como origen.
- `error`: valida Data API 3.0, token y screen, y confirma recepcion sin reflejar
  el error.

No hay `db.session.commit`, creacion de pedidos, creacion de reclamos, consumo de
token ni cambio de estado. La confirmacion final y cualquier writeback deben
vivir en un endpoint posterior con idempotency key y auditoria explicita.

## Activacion pendiente

El resolver, el Blueprint y la persistencia de `order_context` autorizada ya
estan integrados. Antes de produccion todavia se debe, por cada WABA:

1. Cargar referencias y variables secretas reales en el deployment.
2. Subir y verificar en Meta la public key correspondiente.
3. Configurar la URL HTTPS del endpoint en Meta.
4. Ejecutar el health check y PING, INIT, BACK y `data_exchange` cifrados.
5. Publicar la revision exacta del Flow JSON y guardar su hash verificado.
6. Ejecutar una prueba viva completa con el sender, tenant y orden/reclamo
   correctos.
