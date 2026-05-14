# Frontend Handoff - Demo Runtime, Landing y Operaciones Reales - 2026-05-14

## Objetivo

La landing y la demo deben sentirse como una experiencia premium tipo WhatsApp operativo, pero sin inventar datos desde frontend.

Backend ya publica contratos logicos, sesiones, lead capture, chat bootstrap, errores JSON, heatmaps reales/empty y slugs reservados. Frontend debe usar esos contratos para construir UX, bloquear interacciones cuando falten capacidades y evitar cualquier mock visual u operativo.

## Frontera de responsabilidad

### Frontend owns

- Layout del hero, mobile mockup, tabs, burbujas, animaciones y responsive.
- Jerarquia visual de headline, subheadline, CTA y demo conversacional.
- Dark/light mode.
- Estados de carga discretos.
- Validar que imagenes remotas carguen antes de mostrarlas.
- Ocultar resultados, imagenes, metricas, mapas, portal o acciones si backend no los publica.
- Pedir datos requeridos antes de enviar lead capture.
- Mostrar errores publicos con copy apto para usuario, sin terminos tecnicos.

### Frontend no debe

- Inventar tickets, leads, pedidos, encuestas, metricas, empleados, categorias, mapas, productos o fotos.
- Generar respuestas locales de chat si falla el runtime.
- Usar PDF como accion principal de demo.
- Tratar slugs reservados como tenants.
- Mostrar portal de usuario final dentro del admin tenant.
- Mostrar fallback visual si backend no publico datos operativos.

## Contratos backend disponibles

### Landing

GET `/api/public/landing-experience`

Campos relevantes:

```json
{
  "contract_version": "public.landing_experience.v1",
  "hero": {
    "conversation_demo": {
      "contract_version": "landing.hero_conversation_demo.v1",
      "flows": []
    }
  },
  "conversion": {
    "lead_capture_endpoint": "/api/public/lead-capture",
    "lead_capture": {
      "contract_version": "public.lead_capture.form.v1",
      "enabled": true,
      "endpoint": "/api/public/lead-capture",
      "fields": [],
      "required_fields": ["name"],
      "required_any_of": [["phone", "email"]],
      "submit_contract": "public.lead_capture.v1"
    },
    "demo_session_endpoint": "/api/v2/demo/session",
    "admin_preview_endpoint": "/api/v2/demo/admin-preview"
  }
}
```

Frontend debe:

- Renderizar el mockup conversacional solo si hay al menos un `flow` con `action` o `result.traceable=true`.
- Mostrar adjuntos solo si vienen con datos reales.
- Si `preview_url`, `thumbnail_url` o `image_url` falla al cargar, ocultar la imagen y conservar solo el dato textual.
- No mostrar `metrics` si el flow no las trae.
- No mostrar resultado si no existe `action` o `result`.
- Usar `conversion.lead_capture.fields` para armar el formulario. No hardcodear campos.

### Demo session

POST `/api/v2/demo/session`

Respuesta relevante:

```json
{
  "contract_version": "demo.session.v2",
  "contract_aliases": ["demo.session.v1"],
  "demo_session_id": "long-token",
  "session_id": "short-id",
  "chat_session_id": "short-id",
  "workspace": {
    "chat_bootstrap": {
      "contract_version": "demo.chat_bootstrap.v1",
      "endpoint": "/ask/municipio",
      "same_origin_endpoint": "/api/ask/municipio",
      "fallback_endpoint": null,
      "method": "POST",
      "session": {
        "chat_session_id": "short-id",
        "demo_session_id": "long-token"
      },
      "headers": {
        "X-Chat-Session-Id": "short-id",
        "X-Demo-Session-Id": "long-token",
        "X-Tenant-Slug": "municipio"
      },
      "empty_states": {
        "runtime_unavailable": {
          "title": "Demo conversacional no disponible",
          "description": "..."
        }
      }
    },
    "lead_capture": {
      "fields": [],
      "required_fields": ["name"],
      "required_any_of": [["phone", "email"]]
    },
    "empty_states": {
      "runtime_unavailable": {
        "title": "Demo conversacional no disponible",
        "description": "..."
      }
    }
  }
}
```

Frontend debe:

- Usar `chat_bootstrap.endpoint` o `same_origin_endpoint` para enviar mensajes.
- Enviar siempre headers `X-Chat-Session-Id`, `X-Demo-Session-Id`, `X-Tenant-Slug`.
- Enviar `chat_session_id` corto en payload cuando corresponda.
- Nunca usar `demo_session_id` como `chat_session_id`.
- Bloquear composer si `chat_bootstrap.endpoint` no existe.
- Si el runtime falla, mostrar `workspace.empty_states.runtime_unavailable` si existe.
- No generar respuesta local del asistente.
- No usar `fallback_endpoint` si viene `null`.

### Chat runtime

POST `/ask/{tenant_slug}` o same-origin `/api/ask/{tenant_slug}`

Respuesta esperada:

```json
{
  "contract_version": "chat.response.v1",
  "contract_aliases": ["chat.runtime.v1"],
  "request_id": "req_...",
  "session": {
    "chat_session_id": "short-id",
    "demo_session_id": "long-token"
  },
  "message": "Respuesta final",
  "actions": [],
  "lead": {
    "created": true,
    "lead_id": 123,
    "ticket_id": 456,
    "detail_endpoint": "/api/v2/inbox/omnichannel/456"
  }
}
```

Frontend debe:

- Renderizar `message` como respuesta IA.
- Si hay `lead.ticket_id`, mostrar accion trazable y link operativo.
- Si hay error JSON `shared.error.v1`, mostrar estado recuperable y `request_id` en soporte/debug discreto.
- No mostrar stack traces, HTML ni texto tecnico al usuario.

### Lead capture

POST `/api/public/lead-capture`

Payload minimo:

```json
{
  "tenant_slug": "municipio",
  "sector": "gobierno",
  "source": "landing_demo",
  "name": "Marcelo",
  "phone": "261...",
  "email": "marcelo@example.com",
  "message": "Quiero probar reclamos con ubicacion.",
  "demo_session_id": "...",
  "chat_session_id": "...",
  "anon_id": "..."
}
```

Reglas frontend:

- No enviar POST si backend no publico `lead_capture.fields`.
- Pedir `name` y al menos `phone` o `email`.
- Usar `field_errors` para marcar inputs.
- Si falta contacto, backend responde `required_fields: ["name", "phone_or_email"]`.
- Si OK, usar `lead.id`, `ticket_id`, `next_actions`.

### Analytics y heatmap

Endpoints:

- GET `/api/v2/analytics/operations/dashboard`
- GET `/api/v2/analytics/operations/heatmap`
- GET `/api/v2/analytics/operations/freshness`
- GET `/api/v2/analytics/operations/action-center`

Reglas frontend:

- Renderizar heatmap solo si `summary.can_render_heatmap=true` o `render_contract.can_render_heatmap=true`.
- Si `can_render_heatmap=false`, mostrar estado vacio limpio, no mapa demo.
- No graficar puntos si `points=[]`.
- No mostrar metricas si no vienen en contrato.

### Encuestas publicas

Endpoints:

- GET `/api/public/encuestas/v1`
- GET `/api/public/encuestas/v1/{slug}`
- POST `/api/public/encuestas/v1/{slug}/responder`
- GET `/api/public/encuestas/v1/{slug}/live-results`
- GET/POST `/api/public/encuestas/v1/{slug}/comentarios`

Reglas frontend:

- Si slug no existe, backend devuelve `public.survey_resolution.v1`.
- Mostrar no encontrado limpio con accion a listado.
- No mostrar heatmap si `heatmap.enabled=false` o no hay puntos.
- No inventar resultados, votos ni comentarios.

### Tickets, inbox y asignacion

Endpoints:

- GET `/api/v2/tickets`
- POST `/api/v2/tickets`
- PATCH `/api/v2/tickets/{ticket_id}`
- GET `/api/v2/tickets/{ticket_id}/events`
- GET `/api/v2/inbox/omnichannel`
- GET `/api/v2/inbox/omnichannel/{ticket_id}`
- POST `/api/v2/inbox/omnichannel/{ticket_id}/actions`
- GET `/api/v2/employee-routing`
- POST `/api/v2/employee-routing/auto-assign`

Reglas frontend:

- Para asignar usar empleados del mismo tenant.
- Si backend responde `assignee_not_found`, refrescar empleados/routing.
- No mostrar empleado inventado.
- No mezclar tickets entre tenants.
- Mostrar eventos desde contrato `tickets.v2.events`.

### Slugs reservados

Si llega `tenant_slug=media`, `demo`, `demo-catalogs`, `public`, `assets`, `static`, `precios`, `sectores`, `casos`, `colegios`, `municipios`, `gobiernos`, `empresas` o `pymes`, backend responde:

```json
{
  "contract_version": "public.reserved_slug.v1",
  "ok": false,
  "reason_code": "reserved_public_slug",
  "slug": "media"
}
```

Frontend debe:

- No bootstrapping de widget/commerce para esos slugs.
- No reintentar contra `/public/...` como si fuera tenant.
- Mostrar/ocultar modulo segun contexto, sin error visible ruidoso.

## Checklist frontend prioritario

1. Hero landing premium
- Consumir `hero.conversation_demo.flows`.
- Render tipo mobile/WhatsApp.
- No mostrar mockup si no hay action/result traceable.
- No mostrar imagen rota.
- CTA principal: `/demo`.
- CTA ventas: formulario basado en `conversion.lead_capture`.

2. Demo conversacional real
- Crear sesion con `POST /api/v2/demo/session`.
- Guardar `demo_session_id` y `chat_session_id`.
- Usar `workspace.chat_bootstrap`.
- Bloquear composer si falta endpoint.
- Mostrar empty state si falla runtime.
- No generar respuesta local.

3. Lead capture
- Construir formulario desde `lead_capture.fields`.
- Validar `name` + (`phone` o `email`) antes del POST.
- Mostrar `field_errors` devueltos por backend.
- Al OK, mostrar lead/ticket trazable.

4. Operaciones reales
- Heatmap solo con `can_render_heatmap=true`.
- Analytics solo con datos reales.
- Encuestas sin votos/comentarios inventados.
- Tickets/inbox/asignacion filtrados por tenant.

5. QA visual y funcional
- Mobile sin overflow horizontal.
- Dark mode con contraste.
- Landing no debe mostrar PDFs como accion principal.
- Demo gobierno: mensaje -> chat runtime -> lead/ticket o error JSON limpio.
- Lead capture: payload valido crea lead/ticket.
- Heatmap vacio: no renderiza mapa falso.
- Encuesta inexistente: muestra contrato `public.survey_resolution.v1`.

## Anti-reglas

- No escribir logica de negocio en frontend.
- No completar datos faltantes con defaults comerciales.
- No mezclar portal usuario final con admin tenant.
- No mostrar datos de `media` como tenant.
- No publicar metricas, mapas, tickets, productos ni empleados si backend no los manda.

## Estado backend para frontend

Listo para consumir:

- `GET /api/public/landing-experience`
- `POST /api/v2/demo/session`
- `POST /ask/{tenant_slug}` con `chat_session_id` corto
- `POST /api/public/lead-capture`
- `GET /api/v2/demo/admin-preview`
- analytics operations
- public surveys v1
- reserved slug contracts
- tickets v2 e inbox omnicanal

Pendiente de producto, no bloqueante para hero/demo:

- UX completa de empleados/roles/asignacion en frontend.
- Vista refinada de analytics/action-center/heatmap vacio.
- Admin preview enriquecido con mas actividad real de sesion cuando haya mas eventos generados.
