# Shared Contracts

## BE-01 Conversation core (backend)

### Conversation timeline endpoint
- **GET** `/api/conversations/<conversation_id>/timeline`
- Auth: `Authorization: Bearer <jwt>`.
- Tenant resolution: `X-Tenant`, `X-Tenant-Id`, o slug en contexto (`require_tenant`).
- Authorization: usuario debe estar autorizado para el tenant (`_is_authorized_for_tenant`).

#### 200 response
```json
{
  "conversation": {
    "id": "<uuid>",
    "tenant_id": 12,
    "legacy_chat_session_id": "optional-legacy-session",
    "status": "active"
  },
  "timeline": [
    {
      "id": 101,
      "conversation_id": "<uuid>",
      "channel_session_id": 55,
      "sender_type": "user|assistant|agent|system",
      "sender_user_id": 88,
      "direction": "in|out",
      "body": "texto",
      "metadata": {},
      "created_at": "2026-03-31T00:00:00+00:00"
    }
  ]
}
```

#### Errors
- `403` sin permisos para el tenant.
- `404` conversación inexistente en el tenant.

## BE-02 Resolver omnicanal (widget -> WhatsApp)

### Crear solicitud de link
- **POST** `/api/conversations/link/whatsapp`
- Auth + tenant + permisos igual que BE-01.
- Body:
  - `conversation_id` **o** `chat_session_id`
  - `whatsapp_number`
  - `ttl_minutes` (opcional, default 10)

Response (`201`): incluye `deep_link_token`, `expires_at`.
- `otp_code` solo se retorna en entorno de testing/desarrollo (en producción se entrega por provider).
- Rate limit anti-abuso por número destino (tenant-scoped): al exceder umbral reciente responde `429`.

### Confirmar link
- **POST** `/api/conversations/link/confirm`
- Auth + tenant + permisos igual que BE-01.
- Body:
  - `deep_link_token`
  - `otp_code` (opcional si se confirma por deep-link puro)
  - `whatsapp_chat_session_id` (opcional)

Comportamiento:
- `200 linked`: primer confirm válido.
- `200 already_confirmed`: confirm repetido (idempotente).
- `410`: token expirado.
- `400`: OTP inválido.
- `404`: solicitud de link inexistente.

Evento socket:
- `conversation.linked` con `conversation_id`, `source_channel_session_id`, `target_channel_session_id`.

### Consultar estado de link
- **GET** `/api/conversations/link/<link_request_id>`
- Auth + tenant + permisos igual que BE-01.
- Response (`200`): `id`, `conversation_id`, `target_channel`, `target_identity`, `status`, `expires_at`, `confirmed_at`.
- `404`: solicitud no encontrada en el tenant.

## BE-05 Notification orchestrator

### Endpoints
- `GET /notifications`: notificaciones del usuario autenticado dentro del tenant.
- `POST /api/admin/notifications/templates`: alta/actualización de template por `key+channel`.
- `GET /api/admin/notifications/templates`: listado de templates (filtro opcional `channel`).
- `POST /api/admin/notifications`: enqueue de notificación con `idempotency_key`.
- `POST /api/admin/notifications/dispatch`: encola tarea Celery para despacho.
- `POST /api/workers/notifications/dispatch`: ejecuta despacho inmediato (worker/internal).
- `GET /api/admin/notifications/<id>`: detalle de notificación individual.
- `GET /api/admin/notifications/<id>/attempts`: historial de intentos.
- `GET /api/admin/notifications/metrics?period_days=7`: agregados de entrega por canal/estado.
- `GET /api/admin/notifications/alerts?period_days=7&threshold_pct=5&min_volume=5`: alertas por tasa de falla.

### Canales soportados
- `email`, `whatsapp`, `push`, `in_app`.

### Reglas
- Idempotencia por `tenant_id + idempotency_key`.
- Retry/backoff exponencial sobre fallos.
- Quiet hours por template (`quiet_hours_start`, `quiet_hours_end`) que difieren envío.

## BE-06 Roles / org units / audit

### Endpoints
- `POST /api/admin/org-units`
- `POST /api/admin/users/<user_id>/roles`
- `POST /api/admin/users/<user_id>/org-units`
- `GET /api/admin/audit/events`

### Reglas
- Todos requieren `token_requerido` + `require_tenant` + control admin/tenant.
- Cada cambio administrativo crea un `audit_event`.

## BE-04 WhatsApp enterprise rules

### Endpoints
- `GET /api/admin/whatsapp/rules`
- `PUT /api/admin/whatsapp/rules`
- `GET /api/admin/templates` (whatsapp)
- `POST /api/admin/templates` (whatsapp)
- `PATCH /api/admin/templates/<id>` (whatsapp)
- `POST /api/notifications/whatsapp/test`

### Reglas
- Políticas por tenant: `enforce_template_outside_24h`, `max_outbound_per_hour`, `blocked_keywords`.
- Aplicación en dispatch de notificaciones WhatsApp dentro del orquestador.
- Soporte de estado por contacto (`whatsapp_contact_state.last_inbound_at`) para evaluación de ventana 24h.

## BE-03 Voice refactor

### Servicio
- `services/voice_session_service.resolve_voice_chat_session_id`
- Unifica generación de `chat_session_id` para `voice_handler` y `voice_stream_service`.
