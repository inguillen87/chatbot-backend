# Backend execution status — roadmap CRM

## Estado actual (actualizado)

### Etapa 1 — Endurecimiento operativo base ✅ COMPLETADA

Implementaciones cerradas:
- Guardas de bootstrap runtime para esquema/tenants en `app.py`.
- Flags de configuración explícitas en `config/__init__.py`:
  - `ENABLE_RUNTIME_SCHEMA_SYNC`
  - `ENABLE_RUNTIME_TENANT_INIT`
- Recomendación operativa por logs: migraciones explícitas (`flask db upgrade`).

### Etapa 2 — Identidad omnicanal (fundación backend) ✅ COMPLETADA (primer corte)

Implementaciones cerradas:
- Nuevo resolver central: `utils/contact_identity.py`.
- Precedencia canónica implementada para `contact_key`:
  1. `conversation_id`
  2. `phone_e164`
  3. `anon_id`
- Hook global `before_request` para adjuntar identidad en `g.contact_identity`.
- Hook global `after_request` para exponer headers:
  - `X-Contact-Key`
  - `X-Conversation-Id`
- CORS actualizado para permitir/exponer headers de identidad.
- Tests unitarios agregados para resolver de identidad.

## Etapa siguiente (en curso)

### Etapa 3 — Integración por dominios (market/tickets/encuestas/analytics) ✅ COMPLETADA (segundo corte)

Avances implementados en este corte:
1. Analytics ingest ahora enriquece `payload` con `contact_key`, `conversation_id`, `phone_e164` e `identity_source` cuando están disponibles.
2. Analytics ingest usa fallback de identidad para `anon_id` y `session_id` (conversation/contact key).
3. Market/cart prioriza `conversation_id` y `contact_key` resueltos globalmente para continuidad de sesión.
4. Encuestas públicas enriquecen metadata de respuestas con identidad omnicanal (`contact_key`, `conversation_id`, `phone_e164`).
5. Tickets públicos ahora consultan `anon_id` desde resolver global antes de headers legacy.
6. Timeline/presence/read-state de tickets usan fallback de identidad para `anon_id` y `active_session_id`.
7. Endpoint `/analytics/identity/coverage` para medir cobertura de identidad por canal y tenant.
8. Funnel WhatsApp en admin analytics ahora reporta `unique_contacts` por etapa y total.
9. Funnel WhatsApp en admin analytics expone `contract_version` para prevenir drift frontend/backend.
10. `/analytics/identity/coverage` ahora devuelve `alerts` y `alert_count` por canal bajo objetivo.
11. Cobertura permite objetivos por canal (`target_by_channel`) para operación con SLAs diferenciados.
12. Cobertura puede emitir eventos operativos (`identity_coverage_alert`) con `emit_alert_events=1`.
13. Se publica contrato versionado `analytics.identity_coverage.v1` en docs/.

Siguientes tareas backend:
1. Estandarizar contratos de respuesta con `contact_identity` opcional para depuración/observabilidad.
2. Trazabilidad de correlación WhatsApp -> portal/market con IDs de interacción en analytics funnel.
3. Definir alertas/SLO automáticas sobre cobertura mínima de identidad por canal (basadas en `target_pct`).

## Próxima etapa priorizada (abril–junio 2026)

### Etapa 4 — Operación CRM con enforcement de permisos y SLA 🔄 EN CURSO

Objetivos del trimestre:
1. Cerrar enforcement real de RBAC/ABAC en endpoints de mayor riesgo operacional.
2. Bajar el volumen de rutas legacy que todavía consumen headers ad-hoc (`X-Anon-Id`).
3. Activar alertas operativas de cobertura de identidad como señal de calidad de datos por tenant.

Entregables comprometidos:
- Matriz `capability -> endpoint` aplicada en rutas críticas de tickets, market y analytics admin.
- Señales de SLA (`first_response_due_at`, `resolution_due_at`) instrumentadas en timeline/eventos.
- Runbook operativo para incidentes de continuidad (`contact_key` faltante, `conversation_id` huérfano).

Avance actual de etapa 4:
- Publicada la matriz inicial compartida en `docs/rbac.capability_matrix.v1.md` como base de enforcement.
- Pendiente: instrumentar `require_capability(...)` en endpoints P0 y cerrar firma BE/FE de la matriz.

KPIs objetivo:
- >95% endpoints críticos usando resolver central de identidad.
- <3% eventos críticos sin `contact_key` por tenant activo.
- 100% denegaciones de permisos con evento de auditoría estructurado.

## Riesgos vigentes
- Existen rutas legacy con lógica de headers ad-hoc (`X-Anon-Id`) que deben migrarse gradualmente al resolver central.
- Requiere coordinación con frontend para adopción total de `X-Contact-Key`/`X-Conversation-Id`.

## Señales de done para etapa 3
- >90% de endpoints críticos usando resolver central.
- Eventos analytics clave con `contact_key` poblado.
- Reducción de discrepancias entre sesión web y continuidad WhatsApp.

## Referencia ejecutable

Ver `BACKLOG_EJECUTABLE_FULLSTACK_OWNERSHIP.md` para la versión operativa por ownership (CT/BE/FE, prioridades y DoD).
