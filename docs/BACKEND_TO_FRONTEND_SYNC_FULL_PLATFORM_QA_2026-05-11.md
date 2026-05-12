# Backend to Frontend Sync - Full Platform QA 2026-05-11

Fecha: 2026-05-11

Objetivo: cerrar los blockers runtime detectados desde frontend/prod sin crear una app paralela. Backend mantiene las rutas actuales y agrega compatibilidad para builds cacheados o proxies que todavia llaman paths legacy.

## Aplicado backend

### Chat demo /ask

Rutas canonicas:

- `POST /ask/municipio`
- `POST /ask/pyme`

Aliases compatibles:

- `POST /api/ask/municipio`
- `POST /api/ask/pyme`
- `POST /api/ask`

Si el runtime explota en un build cacheado, el alias degrada a `200` con:

```json
{
  "contract_version": "chat.runtime_fallback.v1",
  "respuesta_usuario": "...",
  "message_body": "...",
  "botones": [],
  "actions": [],
  "ticket": null,
  "request_id": "req_123"
}
```

### Demo session

`POST /api/v2/demo/session` acepta aliases del selector frontend:

- `sector`, `pilar`, `pillar`, `segment`, `vertical`
- `rubro`, `rubro_slug`, `rubro_key`, `rubro_clave`
- `categoria`, `categoria_slug`, `category`, `category_slug`, `category_key`
- `tenant_slug`, `tenant`, `tenantSlug`, `tenant_key`, `slug`

Los tres pilares quedan estables: `educacion`, `gobierno`, `empresas`.

### Realtime voice

`GET /api/public/realtime/voice-capabilities` responde `200` con contrato aunque el tenant no tenga voice habilitado.

Cuando voice esta deshabilitado:

```json
{
  "contract_version": "realtime.voice_capabilities.v1",
  "enabled": false,
  "reason_code": "voice_not_enabled",
  "features": {
    "tool_calling": false
  },
  "request_id": "req_123"
}
```

### Socket / realtime UI

`GET /api/public/widget-config` ahora expone:

```json
{
  "realtime": {
    "socket_enabled": false,
    "socket_url": null,
    "fallback_mode": "polling_disabled"
  },
  "visibility_rules": {
    "allow_websocket": false,
    "allow_realtime_live_chat": false,
    "fallback_mode": "polling_disabled"
  }
}
```

Frontend debe usar `visibility_rules.allow_websocket` para decidir si intenta Socket.IO.

### Upload multimedia

Ruta canonica:

- `POST /archivos/upload/chat_attachment`

Alias compatible:

- `POST /api/archivos/upload/chat_attachment`

La respuesta incluye `attachmentInfo` y `request_id`.

### Catalog import legacy

`POST /api/admin/catalogo/importar` conserva errores JSON `{ codigo, mensaje }`.

Tambien se cubren metodos no soportados como `GET`, `PUT`, `PATCH` y `DELETE` con:

```json
{
  "codigo": "method_not_allowed",
  "mensaje": "Method not allowed. Use POST para importar un catalogo."
}
```

## Pedido frontend

- Seguir llamando `chat_bootstrap.endpoint` tal como lo manda backend (`/ask/municipio` o `/ask/pyme`).
- Usar `/api/ask/*` solo como compat para builds viejos, no como ruta nueva primaria.
- Gatear Socket.IO con `visibility_rules.allow_websocket`.
- Para upload, preferir `/archivos/upload/chat_attachment`; aceptar `/api/archivos/upload/chat_attachment` como fallback.
- En demo, mandar `tenant_slug` cuando exista, pero backend ya tolera `pilar/categoria/rubro`.

