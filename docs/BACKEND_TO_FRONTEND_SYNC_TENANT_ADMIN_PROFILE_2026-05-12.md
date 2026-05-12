# Backend to Frontend Sync - Tenant Admin Profile 2026-05-12

Objetivo: convertir el perfil de cada PyME, colegio, municipio o rama de gobierno en un panel operativo SaaS coherente, sin crear una app paralela. El frontend debe renderizar estos contratos backend-first y usar los endpoints legacy ya existentes solo como drilldowns.

## 1. Nuevo bundle tenant admin

Endpoint canonico:

`GET /api/v2/tenant/admin-experience`

Alias por superadmin o rutas tenant-aware:

`GET /api/v2/tenants/{tenant_slug}/admin-experience`

Headers:

- `Authorization: Bearer ...`
- `X-Tenant-Slug: {tenant_slug}` cuando no venga por path.

Shape principal:

```json
{
  "contract_version": "tenant.admin_experience.v1",
  "tenant": {
    "slug": "colegio-demo",
    "tipo": "pyme",
    "vertical": "educacion",
    "subvertical": "colegio",
    "plan": "growth",
    "is_active": true
  },
  "profile": {
    "display_name": "Colegio Demo",
    "status": "active",
    "vertical": "educacion",
    "readiness": {
      "contract_version": "tenant.readiness.v1",
      "score": 87.5,
      "checks": {
        "profile": true,
        "branding": true,
        "widget": true,
        "whatsapp": true,
        "team": true,
        "catalog": true,
        "surveys": true,
        "sla": true
      },
      "missing": []
    }
  },
  "modules": [],
  "health": {},
  "operations": {
    "dashboard": {},
    "freshness": {}
  },
  "lead_capture": {},
  "surveys_votings": {},
  "marketplace": {},
  "education": {},
  "frontend_contract": {
    "render_as": "tenant_admin_operating_system",
    "primary_refresh_seconds": 30
  }
}
```

## 2. Modulos que frontend debe renderizar

`modules[]` trae id, label, route, endpoint, secondary_endpoints y widgets. No hardcodear navegacion por tenant; usar esto como menu base del perfil.

Ids actuales:

- `profile`: readiness, branding, capabilities e integraciones.
- `inbox`: tickets, timeline, presence y handoff.
- `analytics`: KPIs, mapas de calor, trends y action center.
- `surveys_votings`: encuestas, votaciones en vivo, respuestas y comentarios.
- `employees`: cobertura, workload y asignacion.
- `marketplace`: bulk import, cobertura de imagenes, pedidos y PDF catalog.
- `widget_whatsapp`: quick menu, multimedia, voz realtime y notifications.
- `education`: aparece solo si backend detecta vertical educativa.

### QA 2026-05-12: campos estabilizados

Backend mantiene estos campos en el shape para que el frontend no tenga que adivinar:

- Cada item de `modules[]` trae siempre `secondary_endpoints` y `widgets`, aunque esten vacios.
- `lead_capture.items[]` trae `id`, `ticket_id`, `status`, `channel`, `created_at`, `contact`, `intent` y `next_action`.
- `marketplace.summary` trae aliases directos `with_images`, `missing_images`, `products_without_image` y `bulk_import_status`.
- `operations.freshness.summary.can_render_heatmap` es booleano.
- `education.admin_menu.panel_sections[]` trae siempre `id`, `label`, `route`, `endpoint`, `secondary_endpoints` y `widgets`.
- `superadmin.command_center.tenants.items[]` y `top_risky[]` traen aliases top-level `slug`, `tenant_slug`, `display_name`, `tenant_name`, `health_score`, `status` y `risk_reason`, ademas del objeto `tenant`.

## 3. UX/UI pedida para frontend

Pantalla tenant profile:

- Header compacto con logo, nombre, vertical, plan, health badge y readiness score.
- Tabs o sidebar: Resumen, Inbox, Mapa, Encuestas/Votaciones, Marketplace, Equipo, Canales.
- Cards densas, no landing-style, con estados empty/degraded/ready desde `operations.freshness`.
- Un drawer 360 para cada ticket/lead usando `lead_capture.items[]` y `/api/v2/inbox/omnichannel`.
- Mapa operativo con layers de tickets, surveys y analytics events desde `/api/v2/analytics/operations/heatmap`.
- Marketplace debe mostrar cobertura de imagenes: productos sin imagen, bulk import y accion de editar/subir imagen.
- En colegios, renderizar `education.admin_menu.panel_sections[]` y `education.profile.media_inputs`.

### Inbox 360 premium

Endpoints:

`GET /api/v2/inbox/omnichannel`

`GET /api/v2/inbox/omnichannel/{ticket_id}`

El listado sigue respondiendo `inbox.omnichannel.v1`, pero cada item ahora trae contrato de drawer 360:

```json
{
  "id": 123,
  "ticket_id": 123,
  "detail_endpoint": "/api/v2/inbox/omnichannel/123",
  "conversation_id": "conv_123",
  "title": "Consulta por beca",
  "description": "Detalle del ticket",
  "status": "nuevo",
  "priority": "high",
  "channel": "whatsapp",
  "category": "educacion",
  "intent": "consulta_beca",
  "assignee": { "id": 10, "name": "Mesa de entrada", "email": "mesa@test.com" },
  "contact": {},
  "location": { "lat": -34.6, "lng": -58.4, "address": "..." },
  "map": {
    "can_render": true,
    "fallback_when_no_coordinates": "timeline_only"
  },
  "attachments": [],
  "sla": {
    "status": "ok",
    "overdue": false,
    "priority": "high",
    "first_response_due_at": null,
    "resolution_due_at": null,
    "next_update_due_at": null
  },
  "timeline": [],
  "allowed_actions": [
    { "id": "reply", "label": "Responder", "endpoint": "/api/v2/inbox/omnichannel/123/actions" },
    { "id": "assign", "label": "Asignar", "endpoint": "/api/v2/inbox/omnichannel/123/actions" }
  ],
  "next_steps": [],
  "source_metadata": {
    "origin": "whatsapp",
    "channel": "whatsapp",
    "demo_session_id": "demo_...",
    "widget_id": "landing-widget",
    "contact_key": "whatsapp:+549..."
  },
  "frontend_contract": {
    "render_as": "inbox_360_drawer"
  }
}
```

El detalle responde:

```json
{
  "contract_version": "inbox.omnichannel.detail.v1",
  "item": {}
}
```

Frontend recomendado:

- Usar `GET /api/v2/inbox/omnichannel` para lista/drawer inicial.
- Abrir drawer 360 con `detail_endpoint` cuando el usuario entra a un lead/ticket.
- Renderizar timeline, adjuntos, SLA, mapa, origen demo/widget/WhatsApp, proximo paso y acciones permitidas desde backend.
- Si `map.can_render=false`, mostrar timeline sin mapa.

## 4. Nuevo command center superadmin

Endpoint:

`GET /api/v2/superadmin/command-center`

Shape:

```json
{
  "contract_version": "superadmin.command_center.v1",
  "summary": {
    "tenants": 12,
    "active_tenants": 10,
    "avg_health_score": 84.1,
    "open_tickets": 33,
    "overdue_tickets": 2,
    "open_leads": 18,
    "risky_tenants": 3
  },
  "tenants": {
    "items": [],
    "top_risky": []
  },
  "tenant_creation": {
    "endpoint": "/api/admin/tenants",
    "method": "POST",
    "required_fields": ["nombre", "tipo"],
    "supported_verticals": ["empresas", "gobierno", "educacion"]
  },
  "frontend_contract": {
    "render_as": "superadmin_command_center",
    "drilldown_endpoint_template": "/api/v2/tenants/{tenant_slug}/admin-experience"
  }
}
```

UX superadmin:

- Dashboard con KPIs globales, ranking de tenants riesgosos y tabla de tenants.
- Crear tenant desde `tenant_creation`; despues abrir el drilldown `/api/v2/tenants/{tenant_slug}/admin-experience`.
- Ver leads/tickets originados en demo/widget/landing en `lead_capture`.
- Acciones rapidas: impersonate, profile 360, health, crear admin, configurar WhatsApp.

## 5. Marketplace y catalogos

`marketplace.media_capabilities` ya declara:

- `bulk_import`: csv, xlsx, txt, pdf.
- `image_extraction_from_import`: true.
- `manual_image_upload`: true.
- `product_gallery`: true.
- `pdf_catalog_generation`: true.

Nuevo endpoint backend-first para la cabina de calidad del catalogo:

`GET /api/v2/catalog/quality`

Alias tenant-aware:

`GET /api/v2/tenants/{tenant_slug}/catalog/quality`

Devuelve `catalog.quality.v1`:

```json
{
  "contract_version": "catalog.quality.v1",
  "summary": {
    "products": 120,
    "ready_to_sell": 98,
    "missing_images": 14,
    "missing_price": 3,
    "missing_stock": 11,
    "missing_description": 5,
    "unavailable": 2,
    "image_coverage_rate": 88.33,
    "price_coverage_rate": 97.5,
    "stock_coverage_rate": 90.83,
    "ready_rate": 81.67
  },
  "queues": {
    "missing_images": [],
    "missing_price": [],
    "missing_stock": [],
    "unavailable": [],
    "missing_description": []
  },
  "imports": {
    "latest": [],
    "accepted_file_types": ["csv", "xlsx", "xls", "txt", "pdf", "png", "jpg", "jpeg", "webp"],
    "image_columns": ["imagen_url", "image_url", "foto", "foto_url", "gallery_urls", "imagenes", "images"]
  },
  "frontend_contract": {
    "render_as": "catalog_quality_command_center",
    "queue_tabs": ["missing_images", "missing_price", "missing_stock", "unavailable", "missing_description"],
    "allow_inline_patch": true
  }
}
```

Frontend debe:

- Permitir editar imagen por producto.
- Permitir editar galeria por producto (`gallery_urls`) cuando exista.
- Mostrar productos sin imagen como cola de calidad.
- Mostrar colas de productos sin precio, sin stock, no disponibles y sin descripcion corta.
- En bulk import, mostrar preview de columnas, imagen detectada y errores JSON.
- Usar `PATCH /api/admin/tenants/{slug}/catalog/items/{item_id}` para edicion inline.
- Mantener endpoint legacy `/api/admin/catalogo/importar` para subida.
- Usar `/api/admin/catalog/import` para la experiencia nueva con preview/commit cuando este disponible.

PATCH de item ahora acepta, sin crear CRUD paralelo:

```json
{
  "imagen_url": "https://cdn...",
  "gallery_urls": ["https://cdn..."],
  "precio": "12500",
  "cantidad": "24",
  "descripcion_corta": "Pack escolar",
  "promocion_info": "10% off",
  "external_url": "https://tienda...",
  "checkout_type": "chatboc"
}
```

UX recomendado para frontend:

- Cabecera con score de catalogo listo para vender.
- Tabla editable con columnas: imagen, nombre, precio, stock, estado, categoria, acciones.
- Drawer por producto con galeria, descripcion, precio, stock, promos, personalizacion y links externos.
- Import wizard con pasos: subir archivo, mapear columnas, revisar imagenes, corregir faltantes, confirmar.
- Accion masiva: "Completar imagenes", "Publicar listos", "Sincronizar vectores".

## 6. Verificacion backend

## 6. Equipo, zonas y asignacion inteligente

Endpoints nuevos:

`GET /api/v2/employee-routing`

`GET /api/v2/tenants/{tenant_slug}/employee-routing`

Devuelve `employee.routing.v1`:

```json
{
  "contract_version": "employee.routing.v1",
  "routing_policy": {
    "source": "user.accesibilidad.employee_scope",
    "dimensions": ["categorias", "zonas", "channels", "permisos"],
    "assignment_targets": ["TenantTicket", "MunicipioTicket", "PymeTicket"]
  },
  "dimensions": {
    "categorias": ["educacion", "alumbrado"],
    "zonas": ["centro", "norte"],
    "channels": ["whatsapp", "widget"]
  },
  "employees": [
    {
      "id": 10,
      "name": "Mesa de entrada",
      "scope": {
        "categorias": ["educacion"],
        "zonas": ["centro"],
        "channels": ["whatsapp"],
        "permisos": ["tickets_assign"]
      },
      "workload_open": 2
    }
  ],
  "queues": {
    "unassigned": [],
    "unassigned_count": 0
  },
  "recommendations": [
    {
      "ticket": {"source_model": "TenantTicket", "id": 123},
      "suggested_assignee": {"id": 10, "name": "Mesa de entrada"},
      "score": 96,
      "reasons": ["category_match", "zone_match", "channel_match"]
    }
  ],
  "frontend_contract": {
    "render_as": "employee_routing_matrix"
  }
}
```

Actualizar scope de empleado:

`PATCH /api/v2/employees/{employee_id}/routing-scope`

Body:

```json
{
  "categorias": ["educacion", "pagos"],
  "zonas": ["centro", "norte"],
  "channels": ["whatsapp", "widget"],
  "permisos": ["tickets_assign", "orders_assign"]
}
```

Auto-asignacion:

`POST /api/v2/employee-routing/auto-assign`

Por defecto se puede usar `dry_run: true` para preview. Con `dry_run: false` aplica asignaciones sobre `TenantTicket`, `MunicipioTicket` y `PymeTicket`.

Frontend debe renderizar:

- Matriz empleados x categorias/zonas/canales.
- Cola de tickets/reclamos sin asignar.
- Recomendacion de mejor empleado con razones visibles.
- Boton "Previsualizar asignacion" y despues "Aplicar".
- Editor de scope por empleado con chips de categorias, zonas, canales y permisos.
- Workload por empleado para evitar sobrecargar siempre a la misma persona.

## 7. Production smoke / soporte

Endpoints protegidos:

`GET /api/v2/platform/production-smoke`

`GET /api/v2/production-smoke`

`GET /api/v2/tenants/{tenant_slug}/production-smoke`

Devuelve `platform.production_smoke.v1`:

```json
{
  "contract_version": "platform.production_smoke.v1",
  "status": "pass|warning|fail",
  "summary": {
    "total": 8,
    "passed": 8,
    "failed": 0,
    "critical_failed": 0
  },
  "checks": [
    {
      "id": "widget_platform_onboarding",
      "ok": true,
      "status": "pass",
      "endpoint": "/api/public/widget-config"
    },
    {
      "id": "inbox_360",
      "ok": true,
      "endpoint": "/api/v2/inbox/omnichannel"
    }
  ],
  "frontend_contract": {
    "render_as": "production_smoke_report",
    "fail_http_query_param": "fail_http=1"
  }
}
```

Checks actuales:

- Rutas criticas registradas: widget-config, demo/session, ask, realtime, inbox, tenant admin, WhatsApp, catalog quality, tracking.
- Widget landing selector plataforma.
- Socket deshabilitado si no hay contrato valido.
- Tenant Admin OS.
- Catalog quality.
- WhatsApp Operations Hub.
- Inbox 360.

Frontend/superadmin recomendado:

- Mostrarlo como herramienta interna de soporte, no como pantalla publica.
- Si `status=fail`, mostrar `checks[]` con endpoint y detalles.
- Para monitores externos se puede llamar con `?fail_http=1` para recibir HTTP 500 cuando haya falla critica.

## 8. Verificacion backend

Test agregado:

`tests/test_v2_saas_contracts.py`

Cobertura nueva:

- `tenant.admin_experience.v1`
- `tenant.readiness.v1`
- `tenant.marketplace_ops.v1`
- `tenant.surveys_ops.v1`
- `tenant.lead_capture.v1`
- `superadmin.command_center.v1`
- `employee.routing.v1`
- `employee.routing_scope.v1`
- `employee.routing.auto_assign.v1`
- `inbox.omnichannel.detail.v1`
- `platform.production_smoke.v1`
