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
