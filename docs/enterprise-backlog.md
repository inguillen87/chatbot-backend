# Enterprise Backlog

## BE-01 — Conversation core

### Estado
Implementado en backend (fase inicial):
- Nuevas tablas: `conversation`, `channel_session`, `message`.
- Compatibilidad temporal con `chat_session_id`:
  - `conversation.legacy_chat_session_id`
  - `channel_session.chat_session_id`
  - `chat_session_context` ahora referencia `conversation_id` y `channel_session_id`.
- Servicio `ConversationResolver` para resolve/create + append message.
- Integración inicial en `routes/chat.py` para persistir turnos user/assistant en BE-01.
- Endpoint `GET /api/conversations/<id>/timeline` con validación tenant + permisos.
- Audit log de acceso de timeline (`conversation_timeline_view`).

### Pendientes sugeridos
- Integrar `ConversationResolver` en flujos restantes (webhook WhatsApp, voice, APIs legacy).
- Backfill de conversaciones históricas desde `chat_session_context` y `conversacion` legacy.
- Paginación (`limit`, `before`) en timeline.

## BE-02 — Resolver omnicanal y link widget -> WhatsApp

### Estado
Implementado en backend (fase inicial):
- `POST /api/conversations/link/whatsapp` (genera OTP + deep_link_token con expiración).
- OTP almacenado hasheado (no en texto plano).
- `POST /api/conversations/link/confirm` (confirma OTP/deep-link e idempotencia en repetidos).
- Unificación de `channel_session` web + whatsapp bajo mismo `conversation_id`.
- Emisión de evento `conversation.linked`.
- Audit log para solicitud y confirmación de link.
- Tests: happy path, expirado, inválido, repetido.

### Pendientes sugeridos
- Integración automática con envío real de OTP por WhatsApp provider.
- Validación antifraude/rate limit por número destino.
- Exponer consulta de estado de link para panel interno.
