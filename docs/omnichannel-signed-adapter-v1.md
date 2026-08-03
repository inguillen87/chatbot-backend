# Adaptador omnicanal firmado v1

Estado actual: implementado y validado localmente; deshabilitado por defecto.
No constituye una certificación con Telegram, Messenger, Instagram, email u
otro proveedor hasta completar una prueba firmada en staging con una
`ProviderConnection` real.

## Límite de confianza

El endpoint legado `POST /omnichannel/inbound` permanece cerrado porque
aceptaba selectores de tenant y contacto provistos por el cliente. Los
adaptadores verificados deben usar:

```text
POST /omnichannel/adapters/{provider_connection_id}/inbound
Content-Type: application/json
X-Chatboc-Timestamp: 1785585600
X-Chatboc-Event-Id: evt-provider-123
X-Chatboc-Event-Type: message.created
X-Chatboc-Signature-V1: sha256=<64 caracteres hexadecimales>
```

El backend deriva tenant, proveedor y canal exclusivamente desde la
`ProviderConnection` activa indicada en la URL. Cualquier `tenant_id`,
`municipio_id` o `pyme_id` incluido en el JSON se ignora.

Cada conexión usa una clave HMAC propia, derivada de
`OMNICHANNEL_INBOUND_HMAC_SECRET_V1`. La clave raíz no se entrega a los
adaptadores. Hasta que exista una API administrativa de rotación con
reautenticación, la provisión de la clave derivada debe hacerse por un canal
operativo seguro.

La firma cubre exactamente:

```text
v1\n
timestamp_unix\n
provider_connection_id\n
event_id\n
event_type\n
sha256(raw_body)
```

La ventana anti-replay predeterminada es de 300 segundos. Los eventos se
reclaman en `webhook_delivery` antes de persistir y usan un recibo idempotente
tenant-scoped para que una caída entre la creación del ticket y la respuesta
HTTP no duplique el reclamo ni su comentario inicial.

## Contrato JSON

```json
{
  "contract_version": "omnichannel.inbound.v1",
  "direction": "inbound",
  "channel": "telegram",
  "event_type": "message.created",
  "contact": {
    "external_id": "opaque-provider-contact-id",
    "name": "María",
    "email": "maria@example.com",
    "phone": "+5491112345678"
  },
  "message": {
    "kind": "text",
    "text": "Hay un árbol caído en la plaza"
  }
}
```

`contact.external_id` nunca se persiste como identidad global: se reemplaza
por un HMAC ligado a tenant, conexión y canal. Email y teléfono quedan como
metadatos del ticket, pero no autorizan la vinculación automática con una
cuenta global de Chatboc.

Tipos admitidos:

- `text`, `emoji`, `reaction`, `interactive`
- `audio`, `image`, `video`, `document`
- `location`
- `call_event`

### Audio o imagen

```json
{
  "kind": "audio",
  "transcript": "Hay una pérdida de agua frente al 1200",
  "media": [
    {
      "provider_media_id": "media-123",
      "mime_type": "audio/ogg",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "size_bytes": 3200
    }
  ]
}
```

Una referencia remota debe ser HTTPS y nunca puede contener credenciales en
la URL. El adaptador admite hasta ocho archivos y 25 MB por archivo. Si llega
audio, imagen, video o documento sin transcripción/caption, la respuesta marca
`media_processing: pending`. Eso significa “persistido para procesar”, no
“comprendido por la IA”. Un worker de descarga autenticada y análisis por
proveedor sigue siendo requisito para declarar multimodalidad E2E en ese canal.

### Ubicación

```json
{
  "kind": "location",
  "location": {
    "lat": -34.585,
    "lng": -60.949,
    "address": "Plaza 25 de Mayo, Junín"
  }
}
```

Latitud y longitud se validan en sus rangos geográficos antes de llegar a la
base de datos.

### Evento de llamada

```json
{
  "kind": "call_event",
  "transcript": "Necesito reportar una luminaria caída",
  "call": {
    "state": "completed",
    "duration_seconds": 82,
    "provider_call_id": "provider-call-456"
  }
}
```

El ID de llamada se persiste únicamente como SHA-256. Este contrato registra
eventos y transcripciones provenientes de un adaptador autenticado; no inicia
llamadas ni sustituye el consentimiento de voz o entrevistas.

## Activación

Variables del servicio web:

```text
OMNICHANNEL_SIGNED_INBOUND_MODE=enforce
OMNICHANNEL_INBOUND_HMAC_SECRET_V1=<secreto aleatorio dedicado de 32+ bytes>
OMNICHANNEL_INBOUND_MAX_PAYLOAD_BYTES=65536
OMNICHANNEL_INBOUND_MAX_CLOCK_SKEW_SECONDS=300
```

El manifiesto de Render mantiene `OMNICHANNEL_SIGNED_INBOUND_MODE=disabled`.
No debe habilitarse hasta completar, para cada proveedor canario:

1. conexión tenant-scoped activa;
2. provisión y rotación de clave por conexión;
3. verificación de firma y replay en staging;
4. worker autenticado de medios cuando el canal acepte audio o imagen;
5. adaptador de respuesta saliente con recibos de entrega y estado incierto;
6. prueba E2E sin reutilizar IDs de evento ni reintentar envíos ambiguos.
