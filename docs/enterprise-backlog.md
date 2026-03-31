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

## BE-05 — Notification orchestrator

### Estado
Implementado en backend (fase inicial):
- Reemplazo de placeholder `/notifications` por lectura real para usuario autenticado + tenant.
- Nuevos modelos: `notification`, `notification_attempt`, `notification_template`.
- Soporte de canales: `email`, `whatsapp`, `push`, `in_app`.
- Idempotencia por `tenant_id + idempotency_key`.
- Retry/backoff exponencial + quiet hours.
- Endpoints admin y worker para enqueue/dispatch.
- Tarea Celery `tasks.dispatch_notifications`.

### Pendientes sugeridos
- Integrar providers reales de envío (SMTP/Twilio/Push provider).
- Métricas por canal y alertas sobre tasa de error.

## BE-06 — Roles / org units / audit

### Estado
Implementado en backend (fase inicial):
- Nuevas entidades: `org_unit`, `user_org_unit`, `audit_event`.
- Endpoints admin para crear unidades, asignar roles/unidades y consultar auditoría.
- Auditoría estructurada por tenant para cambios administrativos.

### Pendientes sugeridos
- Jerarquía de permisos por org unit (scope efectivo por recurso).
- Políticas avanzadas RBAC (deny/allow granulares).
- Exportación de auditoría y retención por políticas.

## BE-04 — WhatsApp enterprise rules

### Estado
Implementado en backend (fase inicial):
- Entidad `whatsapp_enterprise_rule` por tenant.
- Endpoints admin para lectura/actualización de políticas.
- Enforcements en notification dispatch para canal WhatsApp.

### Pendientes sugeridos
- Conectar a ventana real de conversación por contacto (no solo metadata).
- Reglas de plantillas aprobadas por categoría.

## BE-03 — Voice refactor

### Estado
Implementado en backend (fase inicial):
- Servicio compartido `voice_session_service` para normalizar `chat_session_id`.
- Integración en `voice_handler` y `voice_stream_service` para evitar lógica duplicada.

### Pendientes sugeridos
- Extraer más bloques compartidos (resolución tenant/contacto/context merge).
- Cobertura de tests de integración de flujos de llamada completos.
