# Chatboc Backend v2 — Guía técnica para Codex

## Objetivo

Elevar el backend real de Chatboc a una base SaaS más segura, ordenada y escalable **sin reescribir el producto ni cambiar de stack**.

El backend actual debe tratarse como un monolito Flask funcional con valor operativo. La tarea de Codex no es migrar a NestJS, Prisma, FastAPI ni otro framework, sino consolidar una capa v2 compatible con producción.

## Stack real que debe respetarse

- Flask
- SQLAlchemy
- Alembic / Flask-Migrate
- Celery
- Redis
- Flask-SocketIO / Socket.IO
- Flask-CORS
- Flask-Session
- Flask-Login
- JWT / cookies según implementación actual
- Integraciones existentes: Twilio/WhatsApp, OpenAI, Pusher, Google, SMTP, analytics, encuestas, widget y rutas públicas

## Reglas no negociables

1. **No migrar de stack.**
   - No Prisma.
   - No NestJS.
   - No FastAPI.
   - No reescritura total.

2. **No borrar rutas legacy.**
   - Las rutas existentes deben seguir funcionando.
   - Los aliases `/api/*` quedan congelados, no ampliados.

3. **No hacer migraciones destructivas.**
   - Toda migración Alembic debe ser reversible.
   - No eliminar columnas/tablas en esta etapa.

4. **No introducir credenciales reales ni tokens hardcodeados.**
   - Cualquier credencial demo debe moverse a variables de entorno.
   - Archivos `.http`, scripts y ejemplos deben usar placeholders.

5. **Todo endpoint nuevo debe vivir en `/api/v2`.**

6. **Toda operación v2 debe estar scopeada por tenant.**
   - Prohibido caer en “primer tenant disponible”.
   - Prohibido inferir tenant ambiguo en rutas v2.

7. **Los cambios deben ser pequeños, testeables y revisables.**

---

## Archivos que Codex debe inspeccionar primero

Antes de modificar código, inspeccionar:

```text
app.py
models.py
config/__init__.py
middleware/tenant_context.py
routes/auth.py
routes/api_aliases.py
routes/chat.py
routes/tickets*.py
routes/encuestas*.py
routes/analytics*.py
routes/public*.py
routes/pwa*.py
celery_utils.py
services/tasks.py
services/
migrations/
tests/
requirements.txt
```

Si los nombres exactos difieren, buscar por:

```text
Blueprint
tenant
Ticket
Encuesta
Survey
auth
login
widget
SocketIO
Celery
```

---

## Diagnóstico de base

Chatboc ya tiene muchas capacidades:

- autenticación panel/admin/widget/demo;
- multi-tenant por slug/header/host/widget token;
- chat por rubro;
- tickets PyME y municipales;
- encuestas;
- widget embebible;
- Twilio/WhatsApp;
- analytics;
- tareas Celery;
- aliases legacy `/api/*`.

El problema principal es **dispersión**:

- auth, demo y widget mezclados;
- rutas canónicas y aliases duplicados;
- tenant resolver demasiado permisivo;
- credenciales demo o tokens en scripts/ejemplos;
- frontend y backend con contratos parcialmente divergentes;
- features existentes sin namespace estable.

La solución es una **capa v2 progresiva**, no una reescritura.

---

# Arquitectura objetivo backend

## Namespace nuevo

Crear o consolidar:

```text
routes/v2/
  __init__.py
  health.py
  auth.py
  demo.py
  tenants.py
  chat.py
  tickets.py
  sla.py
  surveys.py
  analytics.py
  webhooks.py
  notifications.py
```

Registrar bajo:

```text
/api/v2
```

Ejemplo:

```python
api_v2 = Blueprint("api_v2", __name__, url_prefix="/api/v2")
```

O registrar blueprints individuales:

```python
app.register_blueprint(health_v2_bp)
app.register_blueprint(auth_v2_bp)
app.register_blueprint(demo_v2_bp)
```

Usar el patrón existente del repo.

---

# Fase 1 — Fundación backend v2

## Objetivo

Crear una base segura y compatible:

- `/api/v2/health`
- `/api/v2/demo/catalog`
- `/api/v2/demo/session`
- `/api/v2/auth/login`
- `/api/v2/auth/refresh`
- `/api/v2/auth/logout`
- `/api/v2/auth/me`
- tenant resolver estricto para v2
- hardening de secrets/config
- tests mínimos

## Endpoints

### `GET /api/v2/health`

Respuesta:

```json
{
  "ok": true,
  "version": "v2",
  "service": "chatboc-backend"
}
```

### `GET /api/v2/demo/catalog`

Debe devolver catálogo demo limpio, sin credenciales:

```json
{
  "sectors": [
    {
      "key": "gobierno",
      "label": "Gobiernos y municipios",
      "rubros": [
        {
          "key": "municipio",
          "label": "Municipio",
          "description": "Reclamos, trámites, encuestas y atención ciudadana"
        }
      ]
    },
    {
      "key": "empresas",
      "label": "Empresas y pymes",
      "rubros": [
        {
          "key": "comercio",
          "label": "Comercio",
          "description": "Ventas, catálogo, pedidos y soporte"
        }
      ]
    }
  ]
}
```

No debe exponer:

```text
password
token administrativo
secret
JWT
refresh token
widget token real
credenciales de demo reales
```

### `POST /api/v2/demo/session`

Body:

```json
{
  "sector": "gobierno",
  "rubro": "municipio",
  "tenant_slug": "junin-demo"
}
```

Respuesta:

```json
{
  "demo_session_id": "uuid-or-signed-id",
  "tenant": {
    "slug": "junin-demo",
    "name": "Demo Municipio",
    "type": "municipio"
  },
  "chat_seed": {
    "welcome_message": "Soy el asistente virtual del municipio. Puedo ayudarte con reclamos, trámites y encuestas.",
    "context": {
      "sector": "gobierno",
      "rubro": "municipio"
    }
  },
  "quick_replies": [
    "Quiero hacer un reclamo",
    "Consultar un trámite",
    "Ver encuestas disponibles",
    "Hablar con una persona"
  ]
}
```

Reglas:

- Si no existe tenant demo, devolver fallback seguro.
- Nunca crear usuario admin real desde este endpoint.
- Nunca devolver password.
- Nunca setear sesión admin.

---

## Tenant resolver estricto v2

Crear helper específico, por ejemplo:

```text
middleware/v2_tenant_context.py
```

O extender el actual con rama explícita v2.

### Orden permitido

Para rutas `/api/v2`:

1. tenant explícito en path, si aplica;
2. header `X-Tenant-Slug`;
3. token autenticado con tenant asociado;
4. demo session válida;
5. widget session/token válido.

### Prohibido

- fallback a primer tenant;
- fallback silencioso por dominio si el dominio no matchea de forma inequívoca;
- crear tenant implícito;
- mezclar tenants entre requests.

### Errores

```text
400 -> falta tenant obligatorio
404 -> tenant inexistente
403 -> usuario autenticado no pertenece al tenant
```

---

## Auth v2

Crear endpoints:

```text
POST /api/v2/auth/login
POST /api/v2/auth/refresh
POST /api/v2/auth/logout
GET  /api/v2/auth/me
```

### Login

Body:

```json
{
  "email": "usuario@dominio.com",
  "password": "********",
  "tenant_slug": "opcional"
}
```

Respuesta:

```json
{
  "access_token": "...",
  "expires_in": 900,
  "user": {
    "id": 123,
    "email": "usuario@dominio.com",
    "name": "Usuario",
    "role": "admin",
    "tenant_slug": "junin"
  }
}
```

Si el sistema actual usa cookie/JWT ya existente, reutilizarlo, pero encapsular el contrato v2.

### Roles permitidos

Normalizar roles internamente:

```text
super_admin
admin
empleado
usuario
anon
```

Si el repo usa variantes como:

```text
admin_municipio
empleado_pyme
```

crear normalizador:

```python
normalize_role(role: str) -> str
```

---

## Hardening de configuración

Buscar y corregir:

```text
SECRET_KEY default inseguro
DEBUG=True por defecto
credenciales hardcodeadas
tokens demo hardcodeados
passwords en scripts
passwords en archivos .http
runtime db.create_all() en producción
runtime tenant init en producción
```

### Reglas

En producción:

- `SECRET_KEY` debe ser obligatorio.
- `DEBUG` debe ser falso.
- runtime schema sync debe estar deshabilitado.
- runtime tenant init debe estar deshabilitado.
- credenciales demo deben venir por env var.
- si falta env crítica, fallar arranque o emitir error claro.

Variables sugeridas:

```text
ENV=production
SECRET_KEY=...
ENABLE_DEMO_MODE=false
DEMO_TENANT_SLUG=...
DEMO_ADMIN_EMAIL=...
DEMO_ADMIN_PASSWORD=...
ENABLE_RUNTIME_SCHEMA_SYNC=false
ENABLE_RUNTIME_TENANT_INIT=false
```

---

## Tests Fase 1

Agregar tests mínimos con el framework existente.

Casos:

1. `GET /api/v2/health` devuelve `ok: true`.
2. `GET /api/v2/demo/catalog` no contiene `password`, `secret`, `token`.
3. `POST /api/v2/demo/session` no devuelve credenciales.
4. Login inválido devuelve 401.
5. Ruta v2 tenant-aware sin tenant no cae en fallback.
6. Config production con `SECRET_KEY` default falla o advierte de forma testeable.

---

## Criterios de aceptación Fase 1

- Backend levanta.
- Rutas legacy siguen funcionando.
- `/api/v2/health` funciona.
- `/api/v2/demo/catalog` funciona.
- No hay secretos nuevos.
- No hay migraciones destructivas.
- Tests pasan o se documenta exactamente qué falla y por qué.

---

# Fase 2 — Tickets unificados, SLA y eventos

## Objetivo

Crear API v2 para tickets operativos, sin romper tickets legacy.

## Endpoints

```text
GET    /api/v2/tickets
POST   /api/v2/tickets
GET    /api/v2/tickets/<ticket_id>
PATCH  /api/v2/tickets/<ticket_id>
POST   /api/v2/tickets/<ticket_id>/comments
GET    /api/v2/tickets/<ticket_id>/events

GET    /api/v2/sla/policies
POST   /api/v2/sla/policies
GET    /api/v2/sla/breaches
```

## Listado

Query params:

```text
status
priority
assignee_id
category
channel
from
to
q
sla=overdue|due_today|ok
```

Respuesta:

```json
{
  "items": [],
  "pagination": {
    "page": 1,
    "page_size": 25,
    "total": 0
  },
  "summary": {
    "open": 0,
    "in_progress": 0,
    "overdue": 0,
    "closed": 0
  }
}
```

## Crear ticket

Body:

```json
{
  "title": "Luminaria rota",
  "description": "Hay una luz rota en la esquina.",
  "type": "reclamo",
  "category": "alumbrado",
  "priority": "medium",
  "channel": "widget",
  "conversation_id": "optional",
  "contact": {
    "name": "Vecino",
    "email": "vecino@mail.com",
    "phone": "+549..."
  },
  "location": {
    "address": "Calle 123",
    "lat": -33.08,
    "lng": -68.47
  }
}
```

## Estados normalizados

```text
open
in_progress
waiting_customer
waiting_internal
resolved
closed
cancelled
escalated
```

## Prioridades

```text
low
medium
high
urgent
```

## Eventos

Cada cambio importante genera evento:

```text
ticket.created
ticket.status_changed
ticket.assigned
ticket.priority_changed
ticket.comment_added
ticket.location_updated
sla.breach_detected
```

Modelo lógico:

```text
TicketEvent
- id
- tenant_id
- ticket_id
- actor_user_id
- event_type
- payload_json
- created_at
```

Si ya existe tabla similar, reutilizar.

## Comentarios

Body:

```json
{
  "body": "Estamos revisando el caso.",
  "visibility": "public"
}
```

Visibilidad:

```text
public
internal
```

Reglas:

- usuarios finales no ven comentarios internos;
- empleados solo ven tickets de su tenant/categoría asignada;
- admin ve todo el tenant;
- super admin requiere tenant explícito.

---

## SLA

Crear servicio:

```text
services/v2/sla_service.py
```

Funciones sugeridas:

```python
calculate_due_dates(ticket, policy)
pause_sla(ticket)
resume_sla(ticket)
detect_breaches(tenant_id=None)
mark_breach(ticket, breach_type)
```

Campos lógicos:

```text
first_response_due_at
next_update_due_at
resolution_due_at
sla_status
sla_policy_id
```

Reglas:

- al crear ticket, aplicar política por tenant + tipo + prioridad;
- si estado es `waiting_customer`, pausar SLA;
- al responder empleado, registrar first response si todavía no existía;
- si vence SLA, crear evento y notificación;
- worker Celery revisa vencidos periódicamente.

---

## Tests Fase 2

1. Crear ticket con tenant válido.
2. Crear ticket sin tenant en v2 falla.
3. Tenant A no ve tickets de Tenant B.
4. Cambiar estado genera evento.
5. Comentario interno no aparece para usuario final.
6. SLA se calcula al crear ticket.
7. Breach detector marca ticket vencido.

---

# Fase 3 — Encuestas/opinar.ar y analytics v2

## Objetivo

Formalizar encuestas como producto SaaS:

- builder;
- publicación;
- respuesta pública;
- analytics;
- integración con chat/tickets.

## Endpoints

```text
GET    /api/v2/surveys
POST   /api/v2/surveys
GET    /api/v2/surveys/<survey_id>
PATCH  /api/v2/surveys/<survey_id>
POST   /api/v2/surveys/<survey_id>/publish
POST   /api/v2/surveys/<survey_id>/close
GET    /api/v2/surveys/<survey_id>/analytics

GET    /api/v2/public/surveys/<public_token>
POST   /api/v2/public/surveys/<public_token>/respond
```

## Crear encuesta

```json
{
  "title": "Satisfacción con atención ciudadana",
  "description": "Queremos conocer tu opinión.",
  "channel": "public_link",
  "opens_at": null,
  "closes_at": null,
  "questions": [
    {
      "type": "rating",
      "label": "¿Cómo calificás la atención?",
      "required": true,
      "options": [],
      "order_index": 0,
      "conditional_logic": {}
    },
    {
      "type": "text",
      "label": "Dejanos un comentario",
      "required": false,
      "options": [],
      "order_index": 1,
      "conditional_logic": {}
    }
  ]
}
```

## Tipos de pregunta

```text
single
multi
rating
text
nps
ranking
location
```

## Estados

```text
draft
scheduled
live
closed
archived
```

## Respuesta pública

```json
{
  "anon_id": "anon-123",
  "contact": {
    "email": "optional",
    "phone": "optional"
  },
  "source": "widget",
  "answers": [
    {
      "question_id": 1,
      "value": 5
    }
  ]
}
```

Reglas:

- encuesta cerrada no acepta respuestas;
- token público no revela datos internos;
- soportar voto anónimo;
- prevenir abuso básico por `anon_id`, user, contact hash o rate limit;
- registrar fuente: web, widget, whatsapp, qr, public_link.

---

## Analytics v2

Endpoints:

```text
GET /api/v2/analytics/overview
GET /api/v2/analytics/tickets
GET /api/v2/analytics/surveys
GET /api/v2/analytics/funnel
```

Métricas mínimas:

```text
total_conversations
total_tickets
open_tickets
overdue_tickets
avg_first_response_time
avg_resolution_time
survey_response_count
survey_completion_rate
csat_score
nps_score
handoff_rate
```

Respuesta overview:

```json
{
  "range": {
    "from": "2026-05-01",
    "to": "2026-05-31"
  },
  "kpis": {
    "total_conversations": 0,
    "total_tickets": 0,
    "open_tickets": 0,
    "overdue_tickets": 0,
    "avg_first_response_time": null,
    "avg_resolution_time": null,
    "survey_response_count": 0,
    "survey_completion_rate": null,
    "csat_score": null,
    "nps_score": null
  }
}
```

---

# Fase 4 — Chat, IA, NLU y handoff

## Objetivo

Unificar `/ask`, `/ask_pyme`, `/ask_municipio` y variantes en una API v2 de conversación, manteniendo compatibilidad legacy.

## Endpoints

```text
POST /api/v2/chat/messages
GET  /api/v2/chat/conversations/<conversation_id>
POST /api/v2/chat/conversations/<conversation_id>/feedback
POST /api/v2/chat/conversations/<conversation_id>/handoff
```

## Mensaje

```json
{
  "conversation_id": "optional",
  "message": "Quiero hacer un reclamo por luminaria rota",
  "channel": "widget",
  "context": {
    "sector": "gobierno",
    "rubro": "municipio"
  }
}
```

## Respuesta

```json
{
  "conversation_id": "conv-123",
  "message": {
    "role": "assistant",
    "content": "Puedo ayudarte a cargar el reclamo. ¿Me pasás la dirección?"
  },
  "intent": "crear_reclamo",
  "confidence": 0.86,
  "entities": {
    "category": "alumbrado"
  },
  "actions": [
    {
      "type": "collect_location",
      "required": true
    }
  ],
  "quick_replies": [
    "Agregar ubicación",
    "Subir foto",
    "Hablar con una persona"
  ],
  "handoff_required": false
}
```

## Servicio IA

Crear:

```text
services/v2/nlu_service.py
services/v2/chat_orchestrator.py
services/v2/prompt_templates.py
```

### Pipeline

1. normalizar mensaje;
2. detectar intent;
3. extraer entidades;
4. consultar catálogo/FAQ/tenant context;
5. decidir acción:
   - responder;
   - crear ticket;
   - pedir datos faltantes;
   - recomendar encuesta;
   - escalar humano;
6. registrar mensaje;
7. devolver respuesta estructurada.

### Intents gobierno

```text
crear_reclamo
consultar_estado_ticket
consultar_tramite
ver_encuestas
ver_eventos
ver_noticias
solicitar_humano
otro
```

### Intents pyme

```text
consultar_producto
consultar_precio
pedir_presupuesto
crear_pedido
consultar_estado_pedido
medios_pago
horarios
soporte
solicitar_humano
otro
```

### Reglas de handoff

Escalar si:

- confianza menor a 0.60;
- usuario pide humano;
- error de proveedor IA;
- intent sensible;
- faltan datos y el usuario se frustra;
- tenant tiene IA desactivada.

---

# Fase 5 — CRM, contactos, webhooks y notificaciones

## CRM básico

Endpoints:

```text
GET    /api/v2/contacts
POST   /api/v2/contacts
GET    /api/v2/contacts/<contact_id>
PATCH  /api/v2/contacts/<contact_id>
GET    /api/v2/contacts/<contact_id>/timeline
```

Entidad lógica:

```text
Contact
- id
- tenant_id
- name
- email
- phone
- tags_json
- consent_marketing
- last_interaction_at
- created_at
```

Timeline:

```text
conversation.started
message.received
ticket.created
ticket.closed
survey.responded
order.created
```

## Webhooks

Endpoints:

```text
GET    /api/v2/webhooks
POST   /api/v2/webhooks
PATCH  /api/v2/webhooks/<id>
DELETE /api/v2/webhooks/<id>
POST   /api/v2/webhooks/<id>/test
```

Eventos:

```text
ticket.created
ticket.updated
ticket.closed
conversation.handoff_requested
survey.published
survey.response_received
contact.created
catalog.import.completed
sla.breach
```

Reglas:

- firmar payload con `secret`;
- reintentos con backoff;
- registrar deliveries;
- no bloquear request principal por delivery.

## Notificaciones

Canales:

```text
email
whatsapp
sms
push
in_app
```

Crear motor:

```text
services/v2/notification_service.py
```

Debe soportar:

- templates;
- variables;
- cola Celery;
- logs;
- rate limit;
- dry-run/test.

---

# Organización sugerida de servicios backend

```text
services/v2/
  __init__.py
  tenant_service.py
  auth_service.py
  demo_service.py
  chat_orchestrator.py
  nlu_service.py
  ticket_service.py
  ticket_event_service.py
  sla_service.py
  survey_service.py
  survey_analytics_service.py
  analytics_service.py
  contact_service.py
  webhook_service.py
  notification_service.py
```

Schemas:

```text
schemas/v2/
  auth_schema.py
  demo_schema.py
  ticket_schema.py
  survey_schema.py
  chat_schema.py
  analytics_schema.py
```

Si el repo ya usa Marshmallow, Pydantic o validación manual, respetar lo existente.

---

# Auditoría

Crear o reutilizar:

```text
AuditLog
- id
- tenant_id
- actor_user_id
- action
- entity_type
- entity_id
- before_json
- after_json
- ip
- user_agent
- created_at
```

Registrar:

```text
login.success
login.failed
ticket.status_changed
ticket.assigned
survey.published
tenant.settings_updated
webhook.created
user.role_changed
```

---

# Seguridad

## Checklist

- [ ] No secrets hardcodeados.
- [ ] No tokens reales en `.http`.
- [ ] No passwords demo en scripts versionados.
- [ ] `SECRET_KEY` obligatorio en producción.
- [ ] `DEBUG=False` en producción.
- [ ] Runtime schema sync deshabilitado en producción.
- [ ] Runtime tenant init deshabilitado en producción.
- [ ] Cookies `HttpOnly`.
- [ ] Cookies `Secure` en producción.
- [ ] Rate limit en login, encuesta pública, chat público y widget.
- [ ] Tenant isolation testeada.
- [ ] Audit log en acciones críticas.
- [ ] CORS restringido por entorno.
- [ ] No exponer stack traces al cliente en producción.

---

# QA backend

## Unit tests

- auth invalid/valid;
- tenant resolver;
- ticket service;
- SLA;
- survey service;
- analytics service;
- webhook signature.

## Integration tests

- login v2;
- demo session;
- create ticket;
- comment ticket;
- publish survey;
- respond survey;
- analytics overview;
- tenant isolation.

## Smoke tests

```bash
curl /api/v2/health
curl /api/v2/demo/catalog
```

---

# Prompt general para Codex — Backend

Usar este prompt al arrancar la implementación:

```text
Trabajá sobre el repo inguillen87/chatbot-backend.

Objetivo: implementar una capa backend v2 para Chatboc sin cambiar el stack actual.

Stack real obligatorio:
- Flask
- SQLAlchemy
- Alembic/Flask-Migrate
- Celery
- Redis
- Flask-SocketIO

No uses Prisma, NestJS, FastAPI ni reescritura total.

Leé primero:
- app.py
- models.py
- config/__init__.py
- middleware/tenant_context.py
- routes/auth.py
- routes/api_aliases.py
- rutas de tickets, encuestas, analytics, chat y public
- celery_utils.py
- services/tasks.py
- migrations/
- tests/

Implementá por fases, empezando por:
1. /api/v2/health
2. /api/v2/demo/catalog
3. /api/v2/demo/session
4. tenant resolver estricto para /api/v2
5. auth v2 básica
6. hardening de secrets/config
7. tests mínimos

Restricciones:
- No borrar rutas legacy.
- No romper endpoints existentes.
- No hacer migraciones destructivas.
- No agregar secretos reales.
- No ampliar aliases legacy.
- Todo endpoint nuevo va en /api/v2.
- Toda ruta v2 tenant-aware debe fallar si no hay tenant explícito o autenticado.

Criterios de aceptación:
- backend levanta;
- tests existentes no se rompen;
- /api/v2/health funciona;
- /api/v2/demo/catalog no expone credenciales;
- tenant fallback implícito no aplica en v2;
- documentación docs/api-v2.md actualizada.

Al terminar:
- listá archivos modificados;
- comandos ejecutados;
- tests que pasaron/fallaron;
- riesgos pendientes.
```

---

# Documentación que debe quedar en el repo backend

Crear o actualizar:

```text
docs/api-v2.md
docs/security-hardening.md
docs/tenant-isolation.md
docs/backend-roadmap-v2.md
```

`docs/api-v2.md` debe incluir:

- endpoints v2;
- auth;
- tenant headers;
- errores;
- compat legacy;
- ejemplos curl.

---

# Roadmap recomendado para PRs backend

## PR 1

```text
Backend foundation v2
```

Incluye:

- health;
- demo catalog/session;
- tenant resolver estricto;
- auth v2 mínima;
- hardening config;
- tests.

## PR 2

```text
Tickets v2 + SLA
```

Incluye:

- tickets;
- comments;
- events;
- SLA policies;
- breach detector.

## PR 3

```text
Surveys v2 + public responses
```

Incluye:

- builder API;
- publish/close;
- public token;
- responses;
- survey analytics.

## PR 4

```text
Chat v2 + NLU orchestration
```

Incluye:

- conversations;
- messages;
- intent detection;
- quick replies;
- handoff.

## PR 5

```text
CRM + Webhooks + Notifications
```

Incluye:

- contacts;
- timeline;
- webhooks;
- notification templates/delivery.

## PR 6

```text
Backend QA + E2E contracts
```

Incluye:

- OpenAPI;
- integration tests;
- tenant isolation tests;
- smoke tests.

---

# Resultado esperado

Después de estas fases, el backend de Chatboc debe quedar con:

- namespace v2 estable;
- legacy congelado;
- tenant isolation fuerte;
- auth menos ambigua;
- demo seguro y vendible;
- tickets operativos con SLA;
- encuestas públicas y analíticas;
- chat preparado para IA/handoff;
- CRM mínimo;
- webhooks;
- notificaciones;
- base lista para frontend SaaS profesional.
