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
