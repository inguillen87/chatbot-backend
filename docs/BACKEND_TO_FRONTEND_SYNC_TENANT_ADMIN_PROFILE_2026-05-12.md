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

## 3. UX/UI pedida para frontend

Pantalla tenant profile:

- Header compacto con logo, nombre, vertical, plan, health badge y readiness score.
- Tabs o sidebar: Resumen, Inbox, Mapa, Encuestas/Votaciones, Marketplace, Equipo, Canales.
- Cards densas, no landing-style, con estados empty/degraded/ready desde `operations.freshness`.
- Un drawer 360 para cada ticket/lead usando `lead_capture.items[]` y `/api/v2/inbox/omnichannel`.
- Mapa operativo con layers de tickets, surveys y analytics events desde `/api/v2/analytics/operations/heatmap`.
- Marketplace debe mostrar cobertura de imagenes: productos sin imagen, bulk import y accion de editar/subir imagen.
- En colegios, renderizar `education.admin_menu.panel_sections[]` y `education.profile.media_inputs`.

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
- `pdf_catalog_generation`: true.

Frontend debe:

- Permitir editar imagen por producto.
- Mostrar productos sin imagen como cola de calidad.
- En bulk import, mostrar preview de columnas, imagen detectada y errores JSON.
- Mantener endpoint legacy `/api/admin/catalogo/importar` para subida.

## 6. Verificacion backend

Test agregado:

`tests/test_v2_saas_contracts.py`

Cobertura nueva:

- `tenant.admin_experience.v1`
- `tenant.readiness.v1`
- `tenant.marketplace_ops.v1`
- `tenant.surveys_ops.v1`
- `tenant.lead_capture.v1`
- `superadmin.command_center.v1`

