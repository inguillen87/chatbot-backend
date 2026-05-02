# Backend to Frontend Sync - Education + WhatsApp UX

Fecha: 2026-05-02

Objetivo: sumar colegios como vertical profesional sobre la plataforma actual, sin crear una app paralela. Colegios usa los mismos pilares actuales: demo catalog/session, widget public config, `/ask/pyme`, tickets, education routes, tenant profile y WhatsApp webhook.

## Implementado backend

### Contrato compartido educacion

Nuevo modulo:

`services/education_contracts.py`

Contratos:

- `education.profile.v1`
- `education.quick_menu.v1`
- `education.admin_menu.v1`
- `education.whatsapp_playbook.v1`
- `education.case_intake.v1`
- `education.case_alias.v1`
- `education.operations_summary.v1`
- `education.operations_heatmap.v1`

Este modulo alimenta demo, widget, panel admin y WhatsApp con el mismo menu y la misma taxonomia.

### Demo catalog

`GET /api/v2/demo/catalog`

Ahora devuelve:

```json
{
  "contract_version": "demo.catalog.v2",
  "sectors": ["gobierno", "empresas", "educacion"],
  "sector_groups": [
    { "key": "gobierno" },
    { "key": "empresas" },
    { "key": "educacion", "label": "Colegios e instituciones educativas" }
  ]
}
```

Frontend debe mostrar `educacion` como tercer sector, sin reemplazar pymes/gobiernos.

### Demo session colegios

`POST /api/v2/demo/session`

Acepta:

```json
{
  "sector": "educacion",
  "tenant_slug": "colegio-demo"
}
```

Respuesta mantiene `contract_version: demo.session.v2` y agrega:

- `experience_blueprint.experience_type: "education"`
- `chat_bootstrap.payload.vertical: "educacion"`
- `workspace.education.profile`
- `workspace.education.quick_menu`
- `workspace.education.whatsapp_playbook`
- `workspace.education.admin_menu`

Importante: el endpoint de chat sigue siendo `/ask/pyme` para colegios privados. No crear flujo paralelo en frontend.

### Widget config colegios

`GET /api/public/widget-config?tenant={slug}`

`GET /api/public/tenants/{slug}/widget-config`

Ahora exponen:

- top-level `quick_menu`
- top-level `education`
- `builder_config.education`
- `widget.education`
- `tenant.vertical`
- `tenant.subvertical`

Menu esperado:

```json
[
  { "id": "menu_asistencia", "label": "Asistencia", "intent": "asistencia_alumno" },
  { "id": "menu_inasistencia", "label": "Justificar inasistencia", "intent": "justificar_inasistencia" },
  { "id": "menu_comunicados", "label": "Comunicados", "intent": "comunicados_familias" },
  { "id": "menu_agenda", "label": "Agenda academica", "intent": "agenda_academica" },
  { "id": "menu_documentacion", "label": "Documentacion", "intent": "documentacion_certificados" },
  { "id": "menu_tramites", "label": "Tramites secretaria", "intent": "tramites_secretaria" },
  { "id": "menu_pagos", "label": "Pagos y cuotas", "intent": "pagos_cuotas" },
  { "id": "menu_admisiones", "label": "Admisiones", "intent": "admisiones_colegio" },
  { "id": "menu_convivencia", "label": "Convivencia", "intent": "convivencia_escolar" },
  { "id": "menu_humano", "label": "Hablar con secretaria", "intent": "derivar_humano" }
]
```

Cada item puede traer `category`, `requires_verification`, `accepted_media`, `sensitivity_level`, `requires_handoff`, `channel_support`.

### Panel admin tenant/colegio

Nuevos endpoints:

`GET /api/v1/education/admin/menu`

Devuelve `education.admin_menu.v1` con:

- `profile`
- `quick_menu`
- `taxonomy`
- `panel_sections`
- `profile_fields`
- `whatsapp_playbook`

`GET /api/v1/education/whatsapp/playbook`

Devuelve `education.whatsapp_playbook.v1` con:

- `welcome`
- `quick_menu`
- `starter_messages`
- `media_intelligence`
- `routing_rules`
- `safety`

`GET /api/v1/education/tenant/capabilities`

Ahora tambien incluye `education_profile`, `admin_menu` y `whatsapp_playbook`.

`GET /api/v1/education/operations/summary`

Devuelve `education.operations_summary.v1` para el dashboard escolar:

```json
{
  "contract_version": "education.operations_summary.v1",
  "status": "ok",
  "tenant_id": 1,
  "tenant_slug": "colegio-demo",
  "education_enabled": true,
  "summary": {
    "schools": 1,
    "total_cases": 12,
    "open_cases": 5,
    "waiting_assignment": 3,
    "sensitive_open_cases": 1,
    "cases_today": 2,
    "cases_with_location": 1,
    "cases_with_attachments": 4
  },
  "breakdown": {
    "by_type": { "inasistencia": 5 },
    "by_channel": { "whatsapp": 8 },
    "by_status": { "nuevo": 3 },
    "by_school": { "Colegio Demo": 12 }
  },
  "next_best_actions": [
    {
      "id": "assign_open_cases",
      "label": "Asignar casos escolares sin responsable",
      "priority": "medium",
      "endpoint": "/api/v1/education/cases"
    }
  ]
}
```

El `panel_sections[education_overview].endpoint` de `education.admin_menu.v1` apunta a este resumen.

`GET /api/v1/education/operations/heatmap`

Devuelve `education.operations_heatmap.v1` con puntos reales de casos escolares que tengan coordenadas en el ticket:

```json
{
  "contract_version": "education.operations_heatmap.v1",
  "render_contract": {
    "state": "ready",
    "map_engine": "maplibre",
    "layers": ["education_cases"]
  },
  "summary": {
    "points": 1,
    "cells": 1,
    "open_points": 1,
    "sensitive_points": 0
  },
  "points": [
    {
      "id": "school_case:10",
      "school_case_id": 10,
      "lat": -34.601,
      "lng": -58.381,
      "weight": 1.5,
      "case_type": "inasistencia",
      "channel": "whatsapp",
      "ticket": { "type": "pyme", "id": 123, "status": "nuevo" }
    }
  ],
  "cells": [],
  "hotspots": []
}
```

Filtros soportados: `school_id`, `case_type`, `channel`, `sensitivity_level`, `limit`.

### Listado de casos escolares con filtros

`GET /api/v1/education/cases` conserva compatibilidad: por defecto devuelve el array legacy.

Nuevos filtros soportados:

- `school_id`
- `campus_id`
- `section_id`
- `student_id`
- `guardian_id`
- `case_type`
- `channel`
- `sensitivity_level`
- `status`
- `assignee_id`
- `unassigned=1`
- `limit`

Si frontend necesita contrato con metadata, puede pedir:

`GET /api/v1/education/cases?unassigned=1&envelope=1`

Respuesta:

```json
{
  "contract_version": "education.cases.list.v1",
  "items": [],
  "count": 0,
  "limit": 100,
  "filters": {
    "unassigned": true
  }
}
```

### WhatsApp colegios

El webhook de WhatsApp ahora:

- detecta tenant educativo por `vertical`, `subvertical`, rubro/nombre o `capabilities_json.education.enabled`.
- guarda `education_context` en `ChatSessionContext`.
- muestra menu escolar en bienvenida de WhatsApp.
- conserva soporte texto/audio/imagen/archivo/ubicacion.
- si el usuario elige un item escolar y luego envia detalle/media/ubicacion, crea un ticket escolar usando el servicio de tickets existente.
- vincula ese ticket con `SchoolCaseAlias` y devuelve `ticket.school_case` (`education.case_alias.v1`) cuando pudo resolver colegio/familia.
- adjunta imagen/archivo al ticket cuando corresponde.
- pasa `education_context` a `/ask/pyme` para que el LLM responda como asistente escolar, no como ecommerce.

No se creo un motor de casos paralelo: se reutilizan tickets + rutas education actuales.

## Tareas frontend recomendadas

1. Demo/rubro selector:
   - Agregar sector `educacion`.
   - Mostrar label `Colegios e instituciones educativas`.
   - Al iniciar chat, usar `chat_bootstrap` sin cambiar endpoint por cuenta propia.

2. Chat widget:
   - Consumir `quick_menu` top-level.
   - Si `education.profile.is_education`, renderizar experiencia escolar: asistencia, comunicados, agenda, secretaria.
   - Usar `media_capabilities` y `education.whatsapp_playbook.media_intelligence` para estados de audio, imagen, ubicacion y archivo.

3. Demo UX:
   - Usar `experience_blueprint.experience_type === "education"` para copy/animaciones escolares.
   - Mostrar ejemplos de inasistencia, certificado medico, comunicados y admisiones.
   - No hardcodear textos de colegio en React; labels vienen del backend.

4. Tenant/admin panel:
   - Consumir `GET /api/v1/education/admin/menu`.
   - Renderizar secciones desde `panel_sections`.
   - Usar `GET /api/v1/education/operations/summary` para KPI del colegio, carga de casos, casos sensibles, adjuntos, ubicaciones y acciones recomendadas.
   - Usar `GET /api/v1/education/operations/heatmap` para mapa/zonas calientes de casos escolares.
   - Para acciones recomendadas, abrir `GET /api/v1/education/cases?...&envelope=1` y respetar filtros del backend.
   - Mostrar/editar `profile_fields` donde aplique.
   - Mostrar playbook de WhatsApp para preview/configuracion.

5. WhatsApp preview:
   - Usar `education.whatsapp_playbook.welcome`, `starter_messages`, `quick_menu` y `media_intelligence`.
   - Para `convivencia_escolar`, mostrar estado sensible/handoff.
   - Para `justificar_inasistencia`, priorizar upload de imagen/PDF/audio.

## Verificacion backend

Ejecutado con `venv\\Scripts\\pythonw.exe`:

- `py_compile` sobre modulos editados.
- `tests.test_demo_experience_contract`
- `tests.test_public_resolver_quick_menu`
- `tests.test_public_resolver_rubro_profile`
- `tests.test_api_v2_foundation`
- `tests.test_education_routes`
- `tests.test_public_resolver`
- `tests.test_public_resolver_widget_config_contract`
- `tests.test_pwa_public_cart_url`
- `tests.test_widget_settings`
- `tests.test_whatsapp_webhook`
- `tests.test_whatsapp_media`
- `tests.test_whatsapp_chunking`
