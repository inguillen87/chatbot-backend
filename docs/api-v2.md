# API v2 Foundation (Chatboc Backend)

Este documento describe la primera base de `/api/v2` incorporada en el monolito Flask actual, manteniendo compatibilidad con rutas legacy.

## Objetivo

- Introducir namespace estable para nuevas features backend: `/api/v2`.
- Aplicar resolución estricta de tenant en v2 (sin fallback implícito).
- Separar demo v2 de auth productiva v2.
- Congelar aliases legacy (`/api/*`) para compatibilidad, sin ampliarlos para features nuevas.

## Endpoints v2 disponibles

### Health
- `GET /api/v2/health`

Respuesta:

```json
{
  "ok": true,
  "version": "v2"
}
```

### Demo
- `GET /api/v2/demo/catalog`
  - Devuelve catálogo limpio por sectores (`gobierno`, `empresas`) con rubros demo públicos.
  - No incluye contraseñas ni tokens administrativos.

- `POST /api/v2/demo/session`
  - Body:
    ```json
    {
      "sector": "gobierno|empresas",
      "rubro": "string",
      "tenant_slug": "optional"
    }
    ```
  - Respuesta:
    ```json
    {
      "demo_session_id": "...",
      "tenant": {"id": 1, "slug": "...", "nombre": "...", "tipo": "..."},
      "chat_seed": {"entry_prompt": "...", "autostart_chat": true, "open_widget": true},
      "chat_bootstrap": {
        "contract_version": "demo.chat_bootstrap.v1",
        "endpoint": "/ask/municipio|/ask/pyme",
        "fallback_endpoint": "/ask",
        "headers": {"X-Chat-Session-Id": "...", "X-Tenant-Slug": "..."},
        "query": {"tenant_slug": "..."},
        "payload": {"pregunta": "", "tipo_chat": "...", "tenant_slug": "...", "rubro": "...", "demo_mode": true},
        "supports": {"text": true, "image": true, "audio": true, "location": true, "file": true}
      },
      "quick_replies": ["..."]
    }
    ```

### Auth v2 mínima
- `POST /api/v2/auth/login`
- `POST /api/v2/auth/refresh`
- `POST /api/v2/auth/logout`
- `GET /api/v2/auth/me`

Notas:
- Reusa `User` y `check_password` del backend actual.
- No mezcla login demo con auth productiva.
- Devuelve payload de usuario mínimo (sin campos sensibles).

### Tenant strict resolver (v2)
- `GET /api/v2/tenants/current`

Este endpoint usa la resolución estricta v2 y sirve como referencia para nuevas rutas tenant-aware.

### Tickets v2 operativos
- `GET /api/v2/tickets`
- `POST /api/v2/tickets`
- `PATCH /api/v2/tickets/<ticket_id>`
- `POST /api/v2/tickets/<ticket_id>/comments`
- `GET /api/v2/tickets/<ticket_id>/events`

Características:
- Scope estricto por tenant.
- Eventos de auditoría por cambios clave (`ticket.created`, `ticket.status_changed`, `ticket.assigned`, `ticket.priority_changed`, `ticket.comment_added`).
- Comentarios internos/privados soportados en payload (`visibility`), filtrados para usuario final.

### SLA v2
- `GET /api/v2/sla/policies`
- `POST /api/v2/sla/policies`
- `GET /api/v2/sla/breaches`

SLA calcula y persiste en `datos_extra.sla` por ticket:
- `first_response_due_at`
- `resolution_due_at`
- `next_update_due_at`
- pausa automática cuando estado está en `waiting_customer` (o equivalente).

Breaches emiten evento de auditoría `sla.breach_detected` y quedan listados vía endpoint.

## Resolución de tenant en v2

Para rutas v2 tenant-aware, se permite resolver tenant solo por:
1. Path param explícito (si aplica).
2. Header `X-Tenant-Slug`.
3. Token autenticado con `tenant_slug`.
4. `X-Widget-Token` o sesión demo válida (`X-Demo-Session` / `demo_session_id`).

### Reglas
- No hay fallback al primer tenant disponible.
- Si falta el dato obligatorio: `400`.
- Si el tenant no existe: `404`.

## Legacy congelado

`routes/api_aliases.py` sigue activo para compatibilidad retroactiva. Queda congelado para features nuevas: toda nueva capacidad debe ir en `/api/v2`.

## Hardening de entorno para producción

En producción (`ENV=prod` o `ENV=production`):
- `SECRET_KEY` debe ser segura (no default/insegura, ni demasiado corta).
- `DEBUG=True` está bloqueado.
- Si no cumple, el arranque falla con `RuntimeError`.

## Variables de entorno obligatorias (producción)

- `ENV=prod` (o `production`)
- `SECRET_KEY=<valor largo y aleatorio>`
- `DEBUG=false`
- `DATABASE_URL=...`

Recomendadas:
- `DEMO_SESSION_SECRET=<valor distinto de SECRET_KEY>`
- `ENABLE_DEMO_MODE=false` (salvo ambientes controlados)


### Surveys v2 (opinar.ar compatible)
- `GET /api/v2/surveys`
- `POST /api/v2/surveys`
- `GET /api/v2/surveys/<survey_id>`
- `PATCH /api/v2/surveys/<survey_id>`
- `POST /api/v2/surveys/<survey_id>/publish`
- `POST /api/v2/surveys/<survey_id>/close`
- `GET /api/v2/surveys/<survey_id>/analytics`

Notas de contrato:
- Se acepta payload v2 en inglés (`title`, `description`, `questions`, `opens_at`, `closes_at`) y se mapea al modelo actual `EncEncuesta`.
- `questions[].type` soporta: `single|multi|rating|text|nps|ranking|location` (con normalización al tipo interno vigente).
- Publicación devuelve `public_token` para consumo frontend.

### Public Surveys v2
- `GET /api/v2/public/surveys/<public_token>`
- `POST /api/v2/public/surveys/<public_token>/respond`

Reglas:
- El token público no expone metadata sensible de tenant.
- `respond` soporta `anon_id` y fuente (`source/channel/canal`), persistiendo con validaciones existentes.
- Una encuesta cerrada o no publicada rechaza respuestas.

### Analytics v2 (dashboard)
- `GET /api/v2/analytics/overview`
- `GET /api/v2/analytics/tickets`
- `GET /api/v2/analytics/surveys`
- `GET /api/v2/analytics/funnel`
- `GET /api/v2/analytics/operations`
- `GET /api/v2/analytics/operations/dashboard`
- `GET /api/v2/analytics/operations/heatmap`
- `GET /api/v2/analytics/operations/action-center`
- `GET /api/v2/analytics/operations/freshness`

Notas operativas:
- `operations.dashboard.v1` agrega tickets, encuestas, chats, WhatsApp, empleados, mapa y acciones recomendadas.
- `operations.heatmap.v1` devuelve puntos/celdas/hotspots con capas para MapLibre.
- `operations.action_center.v1` prioriza acciones de operador.
- `operations.freshness.v1` informa si cada fuente esta `fresh`, `stale` o `empty` para que frontend muestre estados UX accionables.

También se mantienen aliases v2 de compatibilidad para endpoints legacy analytics (`/summary`, `/heatmap`, `/surveys/summary`, `/surveys/sentiment`, etc.) para no romper integraciones existentes.
