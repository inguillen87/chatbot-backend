# Frontend Handoff — Enterprise SaaS Iteration (Backend Ready)

> 📌 **Documento principal actualizado**: usar `docs/FRONTEND_MASTERPEACE_UNIFICADO.md` como fuente única (estado actual + hecho + pendiente).

Este documento resume **lo ya implementado en backend** y el plan de trabajo recomendado para frontend, para que puedan avanzar en paralelo sin bloquearse.

---

## 1) Estado backend (listo para integrar)

### 1.1 Multi-tenant + RBAC (base)
- Tenant scoping activo en endpoints enterprise de analytics e IA.
- Validación de acceso por rol/tenant vía `require_access`.
- Respuesta esperada cuando hay cruce de tenant: `403`.

### 1.2 Demo / onboarding
- `POST /auth/demo` disponible para ingreso demo por rubro/tenant (flujo ya integrado en backend).
- Token demo incluye `demo_mode` para condicionar UX y acciones sensibles.
- Seguridad: login demo usa usuario demo aislado por tenant (no owner/admin real).

### 1.3 Analytics enterprise
- `POST /analytics/event` (ingest tenant-scoped).
- `GET /admin/analytics/overview (alias: /api/admin/analytics/overview)`
- `GET /admin/analytics/heatmap (alias: /api/admin/analytics/heatmap)`
- `GET /admin/analytics/export.csv (alias: /api/admin/analytics/export.csv)`
- `GET /admin/analytics/export.pdf (alias: /api/admin/analytics/export.pdf)`

### 1.4 IA enterprise (admin)
- `POST /admin/ai/executive-summary`
- `POST /admin/tickets/<ticket_id>/ai-summary`
- `POST /admin/ai/product-recommendations`
- `POST /admin/ai/order-draft-from-document` (OCR/PDF/image -> draft)
- `GET /admin/bot/settings?tenant_id=<id>` (alias: `/api/admin/bot/settings`)
- `PUT /admin/bot/settings` (alias: `/api/admin/bot/settings`)

### 1.5 Checkout / MercadoPago / guardrails
- Integración MercadoPago por tenant (credenciales + test + webhook tenant-aware).
- Checkout monetario exige token MercadoPago configurado en el tenant (sin fallback global).
- Checkout demo-mode para evitar cobros reales en demos.
- Guardrails de puntos/tenant para evitar fallback inseguro.

---

## 1.6 Bot IA personalizable por tenant

### `GET /admin/bot/settings?tenant_id=<id>`
- Respuesta: `{ "tenant_id": <id>, "settings": { "name": "...", "tone": "...", "system_prompt": "...", "fallback_behavior": "...", "branding": { "logo_url": "...", "primary_color": "...", "secondary_color": "..." } } }`

### `PUT /admin/bot/settings`
- Body permitido:
```json
{
  "tenant_id": 123,
  "name": "Asistente de Ventas",
  "tone": "profesional",
  "system_prompt": "Ayudá a cerrar pedidos y responder estado de órdenes",
  "fallback_behavior": "derivar_humano",
  "branding": {
    "logo_url": "https://cdn.example.com/logo.png",
    "primary_color": "#123456",
    "secondary_color": "#654321"
  }
}
```
- Validaciones:
  - `tenant_id` obligatorio e integer (`400` si falta/inválido).
  - `fallback_behavior` ∈ `derivar_humano|auto_reply|silent` (`400` si inválido).
  - campos desconocidos en top-level o `branding` -> `400`.
- Seguridad:
  - acceso cross-tenant devuelve `403`.
- Persistencia:
  - `name/tone/system_prompt/fallback_behavior/branding` se guardan en `TenantProfile.configuracion.bot_settings`.
  - `branding.logo_url` sincroniza además `TenantProfile.logo_url` para compatibilidad con clientes legacy.


## 2) Contratos de endpoints para frontend

> Nota: todos los endpoints admin deben enviar auth token de usuario backoffice con permisos (`operador` o `admin`) y contexto tenant correcto.

### 2.1 Demo Login

## `GET /auth/demo/catalog`
- Devuelve credenciales demo de super admin + catálogo de tenants demo + idiomas soportados (`es`, `en`, `pt`).
- Query opcional: `?ensure_users=true` para bootstrap idempotente del usuario demo super admin.

## `POST /auth/demo`
Body sugerido:
```json
{
  "rubro": "municipio"
}
```
Respuesta esperada (shape orientativo):
```json
{
  "token": "...jwt...",
  "demo_mode": true,
  "tenant": {
    "id": 123,
    "slug": "municipio-demo",
    "nombre": "Municipio Demo"
  },
  "user": {
    "id": 999,
    "rol": "admin"
  }
}
```

### 2.2 Analytics Dashboard


## `GET /api/admin/tenants/<slug>/franchise-profile`
- Perfil franquicia/white-label por tenant (para expansión internacional):
  - `white_label_enabled`, `reseller_enabled`
  - `default_language`, `supported_languages`
  - `country`, `currency`, `timezone`
  - `target_markets`, `partner_program`

## `PUT /api/admin/tenants/<slug>/franchise-profile`
- Actualiza perfil franquicia del tenant.
- Validaciones actuales: idiomas soportados `es|en|pt`.

## `GET /api/admin/tenants/<slug>/franchise-readiness`
- Score de preparación comercial internacional (0-100) para venta/franquicia.
- Devuelve `readiness.status` (`basic|in_progress|ready`) + checklist detallado `checks` y `missing`.

## `GET /api/admin/tenants/<slug>/franchise-playbook`
- Plan de acción priorizado para llevar el tenant a estado comercial vendible/franquiciable.
- Devuelve `next_actions[]` + `estimated_phases` para roadmap operativo/comercial.

## `GET /admin/analytics/overview (alias: /api/admin/analytics/overview)?tenant_id=<id>&scope=municipio&from=YYYY-MM-DD&to=YYYY-MM-DD`
- Úsese para KPIs/cards/totales.

## `GET /admin/analytics/heatmap (alias: /api/admin/analytics/heatmap)?tenant_id=<id>&scope=municipio&from=...&to=...&tz=America/Argentina/Cordoba`
- Devuelve bloque geográfico + bloque temporal (día/hora) para heatmap.

## Export
- CSV: `GET /admin/analytics/export.csv (alias: /api/admin/analytics/export.csv)?tenant_id=<id>&scope=municipio&from=...&to=...`
- PDF: `GET /admin/analytics/export.pdf (alias: /api/admin/analytics/export.pdf)?tenant_id=<id>&scope=municipio&from=...&to=...`

### 2.3 Event tracking frontend

## `POST /analytics/event`
Body:
```json
{
  "tenant_id": 123,
  "event_name": "dashboard_view",
  "payload": {
    "path": "/panel/analytics",
    "source": "web"
  },
  "channel": "web_widget",
  "session_id": "sess_abc"
}
```

### 2.4 Admin AI

### 2.6 Portal usuario: historial unificado + tracking + canjes

`GET /api/v1/portal/<tenant_slug>/orders`
- Cada pedido incluye:
  - `status`, `status_label`, `total`, `items_count`
  - `tracking.stage` (`preparing|shipped|delivered|cancelled`)
  - `tracking.eta` (estimado o timestamp final)
  - `tracking.latest_event` (si existe)

`GET /api/v1/portal/<tenant_slug>/orders/<order_id>`
- Devuelve detalle con:
  - `items[]`
  - `tracking.has_timeline`
  - `tracking.timeline[]` (eventos `created`, `status_changed`, etc.)

`GET /api/v1/portal/<tenant_slug>/history`
- Historial unificado para portal autenticado (`?include_network=true` para incluir tenants seguidos):
  - `claims[]` (reclamos del usuario)
  - `orders[]` (pedidos)
  - `points[]` (movimientos de puntos)
  - `surveys[]` (respuestas a encuestas/votaciones)
  - `suggestions[]` (sugerencias enviadas por el usuario)
  - `summary.counts` + `summary.points_breakdown` (puntos por fuente: compras/encuestas/votaciones/sugerencias/reclamos/canjes/etc.)
  - `timeline[]` (feed combinado descendente por fecha, incluye `suggestion`)



`GET /api/v1/portal/<tenant_slug>/surveys/history`
- Historial de encuestas del usuario.
- Soporta `?include_network=true` para incluir respuestas en tenants seguidos.

`GET /api/v1/portal/<tenant_slug>/dashboard`
- Snapshot resumido para Home del portal:
  - `summary` (`claims`, `orders`, `surveys`)
  - `points.current` + `points.breakdown`
  - `tenants_followed`
- Soporta `?include_network=true` para visión multi-tenant.

`GET /api/v1/portal/<tenant_slug>/i18n`
- Configuración de idiomas para portal autenticado:
  - `current_language`
  - `available_languages` (`es`, `en`, `pt`)

`GET /api/v1/portal/<tenant_slug>/network/feed`
- Feed transversal con noticias/eventos del tenant actual + tenants seguidos por el usuario:
  - `items[]` con `type: news|event`, `tenant{...}` y `link`
  - `tenants[]` fuentes incluidas en el feed

`GET /api/v1/portal/<tenant_slug>/benefits`
- Beneficios disponibles de canje en portal:
  - `current_points`
  - `benefits[]` con `eligible` y `points_missing`

`POST /api/v1/portal/<tenant_slug>/redeem`
- Canjea un beneficio por `benefit_id`.
- Registra movimiento de puntos (`tipo: portal_redeem`) con metadata para trazabilidad.

`GET /api/v1/portal/<tenant_slug>/redeems`
- Historial de canjes del usuario (solo débitos de puntos), con `benefit_id` y `benefit_title`.


### 2.5 Catálogo personalizable (nuevo)

En la carga/listado de catálogo ahora pueden venir estos campos en cada producto:
- `personalization_enabled: boolean`
- `personalization_options: [{ id, label, type, required, values, max_length, max_select, help_text }]`

Para guardar configuración de personalización por producto:
- `PATCH /api/admin/tenants/<slug>/catalog/items/<item_id>`
- Body adicional soportado:
```json
{
  "personalization_options": [
    {
      "id": "grabado",
      "label": "Texto grabado",
      "type": "text",
      "required": true,
      "max_length": 30
    },
    {
      "id": "packaging",
      "label": "Packaging",
      "type": "select",
      "values": [
        {"value": "Estándar", "price_delta": 0},
        {"value": "Premium", "price_delta": 500}
      ]
    }
  ]
}
```


## Executive summary
`POST /admin/ai/executive-summary`
```json
{
  "tenant_id": 123,
  "scope": "municipio",
  "from": "2026-01-01",
  "to": "2026-01-31",
  "strict_no_data_message": true
}
```

## Ticket summary
`POST /admin/tickets/<ticket_id>/ai-summary`
```json
{
  "scope": "municipio"
}
```

## Product recommendations
`POST /admin/ai/product-recommendations`
```json
{
  "tenant_id": 123,
  "limit": 8
}
```

## OCR / order draft
`POST /admin/ai/order-draft-from-document` (`multipart/form-data`)
- fields: `tenant_id`, `file`
- extensiones permitidas: `.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp`
- tamaño máximo: `5MB`

Respuesta incluye:
- `draft_items[]` con `match_status`,
- `matched_count`, `unmatched_count`.

---

## 3) Tareas frontend prioritarias (modo “ninja”)

## Sprint FE-1 (impacto inmediato)
1. **Login/Landing demo**
   - Botón “Probar Demo”.
   - Selector de rubro.
   - Invocar `POST /auth/demo`.
   - Persistir token/session + tenant actual.
2. **Banner demo global**
   - Mostrar “Estás en demo”.
   - Botón “Reset demo” (si aplica al flujo FE).
3. **Dashboard analytics**
   - Cards desde `/admin/analytics/overview`.
   - Heatmap temporal/geográfico desde `/admin/analytics/heatmap`.
   - Botones export CSV/PDF.
4. **Tracking básico**
   - Emitir `dashboard_view`, `tab_click`, `export_click` a `/analytics/event`.

## Sprint FE-2 (IA visible para negocio)
1. Botón “Resumen ejecutivo IA” en analíticas.
2. Botón “Resumen IA” en detalle de ticket.
3. Bloque “Productos recomendados” en panel catálogo/comercial.
4. Carga de PDF/imagen para “Borrador de pedido” con tabla editable de matches.

## Sprint FE-3 (robustez productiva)
1. Error boundaries por módulo.
2. Loading + empty + retry states.
3. Toasts de éxito/error homogéneos.
4. Smoke tests FE:
   - login demo,
   - dashboard render,
   - navegación base,
   - export actions.

---

## 4) Recomendaciones de implementación FE

- Crear un `tenantContext` en frontend con `{tenantId, tenantSlug, demoMode}`.
- Incluir `tenant_id` explícito en todas las llamadas admin enterprise.
- Estándar de errores:
  - `400`: input inválido => mensaje orientado a corrección,
  - `403`: sin acceso tenant => redirigir a selector tenant/sesión,
  - `404`: recurso no encontrado => empty state.
- Mantener capa API tipada (DTOs) para evitar drift de contratos.

---

## 5) Checklist de integración FE/BE

- [ ] Demo login consume `/auth/demo` y setea contexto.
- [ ] Analytics dashboard funcional con filtros fecha + tz.
- [ ] Export CSV/PDF descargando archivos correctos.
- [ ] Tracking de eventos visible en backend.
- [ ] Resumen IA ejecutivo y ticket integrados.
- [ ] Recomendaciones de productos renderizadas.
- [ ] OCR draft sube archivo y muestra matched/unmatched.
- [ ] Manejo consistente de 400/403/404/500.

---

## 6) Nota de coordinación

Si el frontend vive en otro repositorio, tomar este documento como contrato de trabajo y abrir PR paralelo con:
- wiring de endpoints,
- UI demo/analytics/IA,
- smoke tests.

Con esto, backend y frontend convergen a cierre enterprise sin fricción.


## 2.7 Portal UX de referencia (implementación frontend)

### Cards sugeridas para Home del portal
- **Mi actividad**: resumen de `summary.counts` (`orders`, `claims`, `surveys`, `suggestions`).
- **Mis puntos**: saldo actual + `summary.points_breakdown`.
- **Seguimiento de pedidos**: últimos `orders[]` usando `tracking.stage` + `tracking.eta`.
- **Novedades de mi red**: `network/feed` (tenant actual + tenants seguidos).

### Contrato sugerido para Timeline unificado
Normalizar visualmente `timeline[]` por `type`:
- `order`: badge por estado (`pending/preparing/shipped/delivered/cancelled`)
- `claim`: badge por estado del reclamo
- `points`: badge `earned/redeemed` + color por signo de `delta`
- `survey`: badge `submitted`
- `suggestion`: badge por `estado` (`nueva/revisada/implementada`)

### Ejemplo de payload (`GET /history`)
```json
{
  "summary": {
    "counts": {"orders": 3, "claims": 2, "surveys": 1, "suggestions": 1, "points_movements": 7},
    "points_breakdown": {
      "compras": 120,
      "encuestas": 50,
      "votaciones": 0,
      "sugerencias": 30,
      "reclamos": 20,
      "canjes": -70,
      "participacion": 0,
      "otros": 0
    }
  },
  "timeline": [
    {"type": "order", "status": "shipped", "at": "2026-02-14T12:00:00+00:00"},
    {"type": "points", "status": "earned", "at": "2026-02-14T11:30:00+00:00"},
    {"type": "suggestion", "status": "nueva", "at": "2026-02-13T18:00:00+00:00"}
  ]
}
```

### Ejemplo de payload (`GET /network/feed`)
```json
{
  "items": [
    {
      "id": 101,
      "type": "news",
      "title": "Nueva obra de pavimentación",
      "date": "2026-02-14T10:00:00+00:00",
      "tenant": {"slug": "mi-ciudad", "name": "Municipio X", "tipo": "municipio"},
      "link": "/mi-ciudad/noticias/101"
    }
  ],
  "tenants": [
    {"slug": "mi-ciudad", "name": "Municipio X", "tipo": "municipio"},
    {"slug": "pyme-favorita", "name": "Pyme Favorita", "tipo": "pyme"}
  ]
}
```
