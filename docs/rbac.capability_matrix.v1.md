# rbac.capability_matrix.v1

> Versión inicial de referencia compartida backend/frontend para etapa 4 (operación CRM con enforcement).

## 1) Roles base

- `superadmin`
- `tenant_admin`
- `employee_agent`
- `catalog_manager`
- `analytics_viewer`
- `end_user`

## 2) Capacidades canónicas

- `tickets.read`
- `tickets.write`
- `tickets.assign`
- `tickets.resolve`
- `tickets.admin`
- `market.catalog.read`
- `market.catalog.write`
- `market.orders.read`
- `market.orders.write`
- `surveys.read`
- `surveys.write`
- `analytics.read`
- `analytics.admin`
- `settings.tenant.write`

## 3) Matriz rol -> capability

| Role | tickets.read | tickets.write | tickets.assign | tickets.resolve | tickets.admin | market.catalog.read | market.catalog.write | market.orders.read | market.orders.write | surveys.read | surveys.write | analytics.read | analytics.admin | settings.tenant.write |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| superadmin | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| tenant_admin | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| employee_agent | ✅ | ✅ | ⚠️ (solo su scope) | ✅ (solo asignados/scope) | ❌ | ✅ | ❌ | ✅ | ⚠️ (actualización estado) | ✅ | ❌ | ✅ | ❌ | ❌ |
| catalog_manager | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ |
| analytics_viewer | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ | ❌ | ❌ |
| end_user | ✅ (solo propios) | ✅ (crear/comentar propios) | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ (solo propios) | ✅ (crear propios) | ✅ (públicas) | ✅ (responder) | ❌ | ❌ | ❌ |

### Fuente de capabilities en backend (analytics)

- Prioridad 1: `user.scope.permisos` / `user.scope.permissions`.
- Prioridad 2: `user.accesibilidad.employee_scope.permisos` / `permissions`.
- Si no hay capabilities en ninguna fuente, aplica fallback legacy (allow) durante transición CT-02.

## 4) Capability -> endpoints backend (v1 inicial)

> Mapeo orientativo para iniciar enforcement en rutas críticas.

### Tickets

- `tickets.read`
  - `GET /tickets/<tipo>/<id>`
  - `GET /tickets/<tipo>/<id>/timeline`
- `tickets.write`
  - `POST /tickets/<tipo>/<id>/comments`
  - `POST /tickets/<tipo>/<id>/read-state`
- `tickets.assign`
  - `POST /tickets/<tipo>/<id>/assign`
- `tickets.resolve`
  - `POST /tickets/<tipo>/<id>/status`
- `tickets.admin`
  - `GET /admin/tickets/metrics`

### Market

- `market.catalog.read`
  - `GET /market/products`
- `market.catalog.write`
  - `POST /api/pymes/<pyme_id>/products`
  - `PUT /api/pymes/<pyme_id>/products/<product_id>`
- `market.orders.read`
  - `GET /pyme/pedidos/<ticket_id>`
  - `GET /api/pymes/<pyme_id>/orders`
- `market.orders.write`
  - `POST /market/cart/add`
  - `POST /pyme/finalizar-pedido`

### Surveys

- `surveys.read`
  - `GET /public/encuestas/<slug>`
  - `GET /api/encuestas/<id>/resultados`
- `surveys.write`
  - `POST /public/encuestas/<slug>/respuestas`
  - `POST /api/encuestas`

### Analytics

- `analytics.read`
  - `GET /analytics/identity/coverage`
  - `GET /admin/analytics/whatsapp-funnel`
- `analytics.admin`
  - `POST /analytics/event`
  - `POST /admin/analytics/rebuild`

### Settings

- `settings.tenant.write`
  - `PUT /api/tenants/<tenant_id>/settings`
  - `POST /api/tenants/<tenant_id>/branding`

## 5) Capability -> pantallas frontend (alineación FE)

- `tickets.read`: inbox, detalle ticket, timeline.
- `tickets.assign`: panel de asignación y cola sin owner.
- `market.catalog.write`: ABM de catálogo.
- `market.orders.read`: dashboard de pedidos y tracking operador.
- `analytics.read`: dashboard analytics, funnel WhatsApp, coverage identidad.
- `settings.tenant.write`: configuración tenant, branding, canales.

## 6) Reglas de enforcement (acuerdo inicial)

1. Backend valida capability en cada endpoint crítico (no confiar sólo en guards frontend).
2. Respuesta de denegación estándar:
   ```json
   {
     "error": {
       "code": 403,
       "message": "forbidden",
       "capability": "tickets.assign"
     }
   }
   ```
3. Cada denegación emite evento auditado con `tenant_id`, `actor_id`, `capability`, `endpoint`.
4. Donde aplique ABAC (zona/categoría/estado), validar además de capability.

## 7) Próximo paso (implementación)

- Backend: introducir middleware/helper `require_capability(capability_name)` en rutas críticas P0.
- Frontend: mapear `requiredCapabilities` por pantalla y fallback UX 403.
- QA: matriz de pruebas cruzadas por rol y tenant con casos happy-path + denegación.
