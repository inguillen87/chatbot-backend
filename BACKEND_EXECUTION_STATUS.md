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

### Etapa 3 — Integración por dominios (market/tickets/encuestas/analytics) 🔄 EN CURSO

Avances implementados en este corte:
1. Analytics ingest ahora enriquece `payload` con `contact_key`, `conversation_id`, `phone_e164` e `identity_source` cuando están disponibles.
2. Analytics ingest usa fallback de identidad para `anon_id` y `session_id` (conversation/contact key).
3. Market/cart prioriza `conversation_id` y `contact_key` resueltos globalmente para continuidad de sesión.
4. Encuestas públicas enriquecen metadata de respuestas con identidad omnicanal (`contact_key`, `conversation_id`, `phone_e164`).

Siguientes tareas backend:
1. Extender la misma estrategia de identidad en tickets públicos y timeline de reclamos.
2. Estandarizar contratos de respuesta con `contact_identity` opcional para depuración/observabilidad.
3. Trazabilidad de correlación WhatsApp -> portal/market con IDs de interacción en analytics funnel.

## Riesgos vigentes
- Existen rutas legacy con lógica de headers ad-hoc (`X-Anon-Id`) que deben migrarse gradualmente al resolver central.
- Requiere coordinación con frontend para adopción total de `X-Contact-Key`/`X-Conversation-Id`.

## Señales de done para etapa 3
- >90% de endpoints críticos usando resolver central.
- Eventos analytics clave con `contact_key` poblado.
- Reducción de discrepancias entre sesión web y continuidad WhatsApp.
