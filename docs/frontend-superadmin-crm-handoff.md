# Frontend Handoff – Superadmin CRM Multitenant (Leads)

## Nuevos endpoints backend

### 1) Pipeline de leads
`GET /api/admin/leads/pipeline`

Query params:
- `tenant_slug` (opcional)
- `since_days` (opcional, default 30)

Respuesta:
- `total`
- `by_stage` (nuevo/contactado/calificado/demo_agendada/propuesta_enviada/ganado/perdido)
- `by_tenant`
- `conversion_rate`
- `avg_first_response_seconds`
- `items[]` con detalle por lead

### 2) Interacciones (ya existente)
`GET /api/admin/leads/interactions`

Usar para:
- feed de preguntas frecuentes
- ranking de leads por `relevance_score`

## Tareas frontend recomendadas (prioridad alta)

1. **Dashboard KPI superior**
   - Leads totales
   - Conversion rate
   - Avg first response
   - Ganados / Perdidos

2. **Kanban pipeline**
   - Columnas por `stage`.
   - Mostrar cards por lead (`nombre`, `email`, `telefono`, `tenant_slug`, `nro_ticket`).

3. **Filtros globales**
   - `tenant_slug`
   - rango temporal (`since_days`)

4. **Tabla detallada**
   - Orden por fecha/relevancia
   - Export CSV (front-side inicial)

5. **UX de contacto rápido**
   - botón WhatsApp (`wa.me`)
   - botón mailto
   - botón copiar teléfono/email

## Criterios de aceptación
- Superadmin ve leads multitenant agregados.
- Puede filtrar por tenant sin recargar toda la app.
- Visualiza funnel y tasa de conversión en segundos.
- Puede accionar contacto rápido desde cada lead.
