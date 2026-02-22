# Frontend Next Pack (Widget + CRM)

## 1) Widget: respuesta comercial estructurada

Cuando `fuente` sea `catalogo_qdrant_con_promos_v2`, `catalogo_fallback_faq` o `catalogo_fallback_web`:
- renderizar bloque **Resumen**,
- renderizar hasta **3 productos destacados**,
- mostrar CTA fijos:
  - `pedir_presupuesto_pyme`
  - `hablar_con_agente_pyme_catalogo`
  - `ver_catalogo_pyme_buscar_otra`

### Criterios de aceptación
- Si llega lista de opciones, el CTA de presupuesto aparece en primeras 3 posiciones visibles.
- En WhatsApp-webview/mobile chat, truncar labels largos pero mantener `action_id` intacto.

---

## 2) Widget: captura de lead (1/3, 2/3, 3/3)

Sobre `pedir_info`, mostrar barra de progreso:
- `nombre` -> 1/3
- `telefono` -> 2/3
- `email` -> 3/3

### Criterios de aceptación
- Al completar `email`, mostrar estado visual de éxito y mantener transcript en pantalla.
- Si backend devuelve re-pregunta de validación, no resetear el progreso completo.

---

## 3) Superadmin: cola de calidad de catálogo

Endpoint:
`GET /api/admin/catalog/quality?tenant_slug=<slug>&limit=100`

Render esperado:
- tabla con `confidence_score`, `quality_issues`, `review_required`,
- filtros por tenant,
- badge rojo para `review_required=true`.

### Contrato sugerido para UI
- `confidence_score < 0.65` -> estado "Crítico".
- `0.65 <= confidence_score < 0.85` -> estado "Revisar".
- `>= 0.85` y con issues -> estado "Observación".

---

## 4) Superadmin: SLA y lead score

`GET /api/admin/leads/interactions` devuelve:
- `sla_breached` (true/false)
- `lead_score` (número)

Render recomendado:
- badge SLA cuando `sla_breached=true`,
- ordenar por `lead_score` por defecto,
- acceso rápido a WhatsApp/mail/teléfono desde cada row.

### Criterios de aceptación
- Filtro "Solo SLA vencido" en 1 click.
- Persistir orden/filtros en querystring.

---

## 5) Voz + Widget (consistencia omnicanal)

Cuando backend pida confirmación crítica (reclamo/pedido), usar componente uniforme:
- título: `Confirmemos antes de continuar`
- cuerpo: resumen de categoría/ubicación/contacto
- acciones: `Confirmar` / `Corregir`

### Criterios de aceptación
- Si usuario toca `Corregir`, enfocar campo faltante sin perder contexto previo.
- Mostrar aviso claro de que la acción final se ejecuta tras confirmación explícita.

---

## 6) Telemetría mínima para iteración siguiente

Emitir eventos FE:
- `catalog_structured_rendered`
- `lead_capture_step_viewed`
- `lead_capture_step_completed`
- `catalog_quality_queue_opened`
- `lead_sla_filter_enabled`

Con esto backend + producto podrá optimizar conversión y UX por canal en el próximo sprint.


---

## 7) Nuevos endpoints listos (Tenant Admin + Superadmin)

### Superadmin
- `PATCH /api/admin/leads/bulk-stage`
  - body:
    ```json
    {
      "stage": "contactado",
      "updates": [
        {"ticket_type": "municipio", "ticket_id": 123, "note": "Primer contacto"},
        {"ticket_type": "pyme", "ticket_id": 456}
      ]
    }
    ```
- `PATCH /api/admin/leads/<ticket_type>/<ticket_id>/stage`
- `GET /api/admin/leads/<ticket_type>/<ticket_id>/timeline`
- `POST /api/admin/leads/<ticket_type>/<ticket_id>/timeline` (agrega nota)

### Tenant Admin
- `GET /api/admin/tenants/<slug>/leads?stage=<opcional>&limit=<opcional>`
- `PATCH /api/admin/tenants/<slug>/leads/<ticket_type>/<ticket_id>/stage`

### Criterios FE
- Soportar `ticket_type` en acciones (municipio/pyme).
- En kanban, habilitar selección múltiple + bulk update para superadmin.
- En detalle lead, mostrar timeline con eventos `stage_update`, `bulk_stage_update` y `note`.


---

## 8) Playbooks automáticos (seguimiento SLA)

Nuevo endpoint superadmin:
- `POST /api/admin/leads/playbooks/run`

Body recomendado:
```json
{
  "dry_run": true,
  "only_sla_breached": true,
  "limit": 50
}
```

Respuesta:
- `items[].actions[]` con plantillas sugeridas por canal (`whatsapp`, `email`).
- usar `dry_run=true` para preview en UI antes de ejecutar en productivo.

### UX sugerida FE
- Botón: `Generar seguimientos automáticos`.
- Paso 1: preview (`dry_run=true`).
- Paso 2: confirmación de ejecución (`dry_run=false`).
- Tabla de resultados por lead con chips de canal y estado.

---

## 9) Lead score inteligente (orden comercial)

`GET /api/admin/leads/interactions` ahora prioriza score considerando:
- urgencia semántica del mensaje,
- completitud de contacto (email + teléfono),
- actividad reciente,
- tickets abiertos.

### Criterio FE
- Orden por `lead_score DESC` por defecto.
- Mostrar tooltip de score con breakdown visual (urgencia/completitud/actividad).


---

## 10) Control estratégico CEO + operación tenant (nuevo)

### Superadmin
- `GET /api/admin/leads/strategic-overview?since_days=30`
  - devuelve: `totals`, `by_stage`, `by_tenant`
  - KPIs: `total_leads`, `open_leads`, `won`, `lost`, `sla_breached`, `win_rate`

### Tenant Admin
- `PATCH /api/admin/tenants/<slug>/leads/bulk-stage`
- `GET|POST /api/admin/tenants/<slug>/leads/<ticket_type>/<ticket_id>/timeline`

### UX FE recomendada
- Superadmin dashboard: cards KPI + gráfico por stage + ranking por tenant.
- Tenant board: acciones masivas de stage + panel lateral de timeline por lead.
- Agregar vista de "ejecución playbook" con preview/confirm y resultados por canal.
