# Backend to Frontend Sync Runtime + Marketplace 2026-05-11

Objetivo: cerrar errores de runtime vistos en landing/demo/widget y habilitar marketplace con imagenes editables e importables, sin crear una app paralela y sin romper compatibilidad legacy.

## 1. Demo/chat runtime

Backend ya acepta sesiones demo como contexto confiable para chat publico.

Frontend debe enviar en cada mensaje demo:

```json
{
  "headers": {
    "X-Demo-Session-Id": "demo_session_id",
    "X-Chat-Session-Id": "demo_session_id",
    "X-Tenant-Slug": "municipio|bodega|colegio-demo"
  }
}
```

Tambien puede mandar `demo_session_id` o `session` en query/body. Backend decodifica tokens `kind=demo_session` y resuelve owner/tenant/rubro sin pedir login.

Contrato confirmado:

- `POST /api/v2/demo/session` responde `demo.session.v2`.
- `workspace.chat_bootstrap.endpoint` indica `/api/ask/municipio` o `/api/ask/pyme`.
- `workspace.chat_bootstrap.payload` trae `tenant_slug`, `rubro`, `vertical`, `demo_mode`.
- Si algo interno falla en demo/widget, backend devuelve `chat.runtime_fallback.v1` con HTTP 200, `request_id` y acciones de recuperacion.

## 2. Demo catalog

`GET /api/v2/demo/catalog` estabiliza tres pilares:

```json
{
  "sectors": ["gobierno", "empresas", "educacion"],
  "sector_groups": [
    { "key": "gobierno", "label": "Gobiernos", "tenant_slug": "municipio" },
    { "key": "empresas", "label": "Empresas", "tenant_slug": "bodega" },
    { "key": "educacion", "label": "Colegios", "tenant_slug": "colegio-demo" }
  ]
}
```

Frontend puede usar `sector_groups[].tenant_slug` como demo default cuando el usuario todavia no eligio categoria fina.

## 3. Realtime, llamadas y Socket.IO

`GET /api/public/realtime/voice-capabilities` y aliases publicos responden JSON accionable con `request_id`.

Si backend no puede resolver tenant, responde 200 con:

```json
{
  "contract_version": "realtime.voice_capabilities.v1",
  "reason_code": "tenant_resolution_failed",
  "action_hint": "send tenant_slug or X-Tenant-Slug",
  "request_id": "req_..."
}
```

Widget config ahora publica flags para no intentar Socket.IO si no esta listo:

```json
{
  "support_channels": {
    "live_chat": {
      "realtime": false,
      "available": false,
      "socket_enabled": false,
      "socket_url": null,
      "fallback_mode": "polling_disabled"
    }
  },
  "realtime": {
    "socket_enabled": false,
    "socket_url": null,
    "fallback_mode": "polling_disabled"
  }
}
```

Regla frontend: no conectar `/socket.io` si `realtime.socket_enabled !== true`. Si se habilita en backend, usar `realtime.socket_url`.

## 4. CORS/headers publicos

Backend permite:

- `X-Demo-Session`
- `X-Demo-Session-Id`
- `X-Chat-Session-Id`
- `X-Tenant-Slug`
- `X-Widget-Token`
- `Idempotency-Key`

Y expone:

- `X-Request-Id`
- `X-Correlation-Id`

Frontend debe mostrar o loguear `request_id`/`X-Request-Id` en errores publicos.

## 5. Marketplace: imagenes de productos

Los items de catalogo serializados pueden incluir:

```json
{
  "id": 1,
  "nombre": "Vino Malbec Reserva",
  "imagen_url": "https://...",
  "image_url": "https://...",
  "gallery_urls": ["https://..."],
  "image_status": "ready|missing",
  "image_alt": "Botella de vino Malbec Reserva"
}
```

Crear/editar producto acepta:

- `imagen_url`
- `image_url`
- `thumbnail`
- `foto`
- `photo`
- `gallery_urls`
- `imagenes`
- `images`
- `image_urls`
- `image_alt`
- `alt`

Nuevo endpoint admin:

`POST /api/admin/market/catalog/{product_id}/images`

Soporta multipart:

- `image`
- `images`
- `file`

Soporta JSON/form:

- `image_url`
- `gallery_urls`
- `replace=true`
- `make_primary=true`
- `primary_image_url`
- `image_alt`

Respuesta:

```json
{
  "ok": true,
  "contract_version": "market.product_images.v1",
  "product": {},
  "uploaded_urls": []
}
```

Tareas frontend recomendadas:

- Editor de imagen principal por producto.
- Galeria drag/drop con reordenar, reemplazar y marcar principal.
- Estado visual `image_status=missing` para productos importados sin imagen.
- Preview de bulk import con columna de imagen detectada.
- Mapeo manual de columnas: imagen, foto, thumbnail, galeria, imagenes.
- Validacion de MIME/tamano antes de subir.

## 6. Bulk import marketplace

Importacion legacy y v2 normalizan imagenes desde CSV/Excel/TXT/PDF cuando aparezcan columnas tipo:

- `imagen_url`
- `image_url`
- `foto`
- `photo`
- `thumbnail`
- `gallery_urls`
- `imagenes`
- `images`
- `image_urls`
- `fotos`
- `photos`

Respuestas incluyen:

```json
{
  "image_summary": {
    "with_images": 0,
    "missing_images": 0
  },
  "imagenes_detectadas": 0
}
```

Al confirmar preview, backend persiste `imagen_url`, `gallery_urls`, `image_status` y manda `imagen_url` al payload de Qdrant.

## 7. IA y audio

Backend dejo de generar TTS automaticamente para cada respuesta web/demo. Solo genera audio si:

- el input viene de audio,
- el canal es de voz/llamada,
- el usuario tiene preferencia de audio,
- o `TTS_AUTO_GENERATE_FOR_TEXT=true`.

Cohere fallback usa `COHERE_CHAT_MODEL=command-a-03-2025` por defecto y `COHERE_CHAT_API_VERSION=v2`.

## 8. Pendientes frontend

- Consumir `realtime.socket_enabled` antes de conectar live chat.
- En demo/chat usar siempre `chat_bootstrap` como fuente de endpoint/payload/headers.
- Agregar UI de marketplace para imagen principal, galeria y bulk preview.
- Mostrar request id en errores publicos de demo/widget/marketplace.
- No mostrar errores de extensiones de navegador como errores de Chatboc.

## 9. Pendientes backend/deploy

- Deployar estos cambios y confirmar en produccion:
  - `/api/public/realtime/voice-capabilities`
  - `/api/v2/demo/session`
  - `/api/v2/demo/catalog`
  - `/api/public/tenants/{slug}/widget-config`
  - `/api/ask/municipio?tenant_slug=municipio`
- Configurar claves reales de OpenAI en Render; la key local actual devuelve 401.
- Si se quiere Socket.IO same-origin en `/socket.io`, configurar proxy o habilitar backend para publicar `realtime.socket_enabled=true`.
