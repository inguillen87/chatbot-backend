# Repo backend — backlog ejecutable para Codex

## Objetivo
Transformar el backend actual en núcleo omnicanal enterprise, sin romper compatibilidad con los canales ya existentes.

## Reglas de implementación
- No reescribir todo.
- Mantener adaptadores legacy durante la migración.
- Toda acción sensible debe generar `audit_log`.
- Toda salida cross-channel debe pasar por `conversation_id`.
- Todo endpoint nuevo debe chequear tenant + permisos.

---

## Epic BE-01 — Conversation core

### Entregables
- migraciones para `conversation`, `channel_session`, `message`
- modelos ORM
- índices por `tenant_id`, `conversation_id`, `external_key`, `last_activity_at`
- compatibilidad temporal con `chat_session_id`

### Requisitos
- `conversation` debe representar la unidad canónica de interacción
- `channel_session` debe modelar vínculo con canal externo
- `message` debe almacenar payload normalizado + raw payload

### Criterios de aceptación
- inbound widget crea o reutiliza `conversation`
- inbound whatsapp crea o reutiliza `conversation`
- inbound voice crea o reutiliza `conversation`
- timeline devuelve mensajes de todos los canales en orden temporal

### Tests
- unit tests de resolver
- tests de migración
- tests de deduplicación por external message id

---

## Epic BE-02 — Resolver omnicanal

### Entregables
- servicio `ConversationResolver`
- reglas determinísticas por canal
- linking OTP para widget -> WhatsApp
- linking para voz -> conversación existente

### API
- `POST /api/conversations/link/whatsapp`
- `POST /api/conversations/link/confirm`
- `GET /api/conversations/<id>/timeline`

### Reglas
- no usar heurísticas implícitas si existe identificador confiable
- soportar fallback por `source_chat_session_id`
- persistir trazabilidad del linking

### Criterios de aceptación
- un usuario puede iniciar en widget y continuar en WhatsApp sin crear hilo paralelo
- una llamada ligada a una conversación existente se ve en el mismo timeline
- el admin ve handoff y cambios de canal en una sola vista

---

## Epic BE-03 — Voice refactor enterprise

### Entregables
- separar `TwilioStreamAdapter`
- separar `RealtimeModelAdapter`
- separar `VoiceToolExecutor`
- separar `VoicePersistenceService`
- eliminar duplicación de manejo de eventos del stream

### Reglas
- `callSid` no debe ser el modelo canónico de sesión
- `callSid` debe ser `external_key` del `channel_session`
- persistir `conversation_id` desde el inicio del stream

### Criterios de aceptación
- una llamada con reconnect o eventos repetidos no duplica mensajes
- transferencia a humano queda auditada
- post-call puede disparar comprobante vía notificador

### Tests
- tests de idempotencia de eventos `start/media/stop`
- tests de transferencia humana
- tests de vinculación con `conversation`

---

## Epic BE-04 — WhatsApp enterprise rules

### Entregables
- tabla `whatsapp_contact_state`
- persistencia `last_inbound_at`
- cálculo `is_in_service_window`
- enforcement de 24h window
- fallback a template fuera de ventana
- catálogo de templates por tenant
- health status del template

### API
- `GET /api/admin/templates`
- `POST /api/admin/templates`
- `PATCH /api/admin/templates/<id>`
- `POST /api/notifications/whatsapp/test`

### Criterios de aceptación
- no se envía free-form fuera de ventana
- si no hay template válido, el envío falla con error de negocio claro
- reply buttons/list messages salen desde una capa unificada

### Tests
- tests de ventana 24h
- tests de selección automática template vs free-form
- tests de health state del template

---

## Epic BE-05 — Notification Orchestrator

### Entregables
- tablas `notification`, `notification_attempt`, `template`
- workers Celery para envío por canal
- políticas de retry/backoff
- preferencias del destinatario
- quiet hours
- idempotency key
- métricas de entrega

### API
- `POST /api/notifications`
- `GET /api/notifications`
- `GET /api/notifications/<id>`
- `POST /api/admin/templates`
- `GET /api/admin/templates`

### Reglas
- un disparo = un registro auditable
- `notification.sent` y `notification.failed` deben emitirse como eventos
- enforcement de reglas WhatsApp desde el orquestador, no desde controladores aislados

### Criterios de aceptación
- email/whatsapp/push quedan unificados
- no hay duplicación con reintentos
- el placeholder actual desaparece

---

## Epic BE-06 — Roles, org units y empleados enterprise

### Entregables
- tablas `org_unit`, `team`, `role`, `permission`, `membership`, `audit_log`
- migración de `empleados` actuales
- middleware/decorators centralizados de autorización
- soporte por scope: categorías, zonas, canales, org_unit

### API
- `GET /api/admin/roles`
- `POST /api/admin/roles`
- `GET /api/admin/org-units`
- `POST /api/admin/org-units`
- `POST /api/admin/users`
- `PATCH /api/admin/users/<id>`

### Criterios de aceptación
- un supervisor puede ver más que un agente
- un auditor puede leer sin operar
- exportaciones quedan auditadas
- permisos se validan server-side, no por UI

### Tests
- matrix tests de permisos
- tests de herencia por org unit
- tests de audit log

---

## Epic BE-07 — Event ingestion y KPIs

### Entregables
- `EventEnvelope` interno
- publisher común desde widget/whatsapp/voice/tickets/encuestas
- jobs de agregación de KPIs
- diccionario de métricas versionado en el repo

### KPIs mínimos
- FRT
- ART
- resolution time
- backlog
- reopened rate
- SLA breach
- handoff rate
- deflection rate
- CSAT
- NPS

### API
- `GET /api/analytics/kpis`
- `GET /api/analytics/series`
- `GET /api/analytics/export`

### Criterios de aceptación
- un mensaje entrante genera eventos consistentes
- los KPIs se recalculan sin acoplarse al canal
- export respeta permisos y deja rastro en audit log

---

## Epic BE-08 — Heatmaps enterprise

### Entregables
- capas por categoría/severidad/estado/canal
- resolución adaptativa por zoom
- threshold privacy guard
- filtros consistentes por tenant y permisos

### API
- `GET /api/analytics/heatmap`
- `GET /api/analytics/clusters`
- `GET /api/analytics/routes`

### Criterios de aceptación
- no se exponen celdas con pocos casos bajo threshold
- la resolución cambia con zoom sin romper performance
- filtros son coherentes con roles y áreas

---

## Epic BE-09 — Encuestas/votaciones hardening

### Entregables
- antifraude mejorado
- deduplicación fuerte
- anomalías básicas
- logs de cambios y publicación
- realtime estable con fallback a polling

### Criterios de aceptación
- toda publicación/cierre queda auditada
- exportación respeta permisos
- el panel no expone PII sensible por defecto

---

## Epic BE-10 — Seguridad y tenant isolation

### Entregables
- revisión sistemática de tenant enforcement
- tests automáticos de aislamiento
- soporte para RLS o estrategia híbrida documentada
- headers y contextos normalizados

### Criterios de aceptación
- ningún endpoint devuelve datos cross-tenant
- endpoints sensibles con rate limit y auditoría
- documentación clara de estrategia multi-tenant

---

## Orden sugerido de PRs
1. BE-01 conversation core
2. BE-02 resolver omnicanal
3. BE-05 notification orchestrator
4. BE-06 roles/org units/audit
5. BE-04 WhatsApp rules
6. BE-03 voice refactor
7. BE-07 KPIs/event ingestion
8. BE-08 heatmaps
9. BE-09 encuestas hardening
10. BE-10 tenant isolation

---

## Prompt base para Codex en backend
"""
Trabajá únicamente en el repo backend.
Objetivo: implementar [EPIC_ID] sin reescribir módulos no relacionados.

Antes de cambiar código:
1. identificar archivos relevantes
2. proponer plan corto
3. listar migraciones y tests a crear

Restricciones:
- mantener compatibilidad con flujos legacy donde sea razonable
- tenant enforcement obligatorio
- permisos server-side obligatorios
- audit log obligatorio en acciones sensibles
- agregar tests unitarios/integración
- no tocar frontend

Entregables:
- código
- tests
- notas de migración
- resumen de riesgos
"""
