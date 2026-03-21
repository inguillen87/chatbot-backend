# Frontend Task Board – Demo UX + CRM Superadmin (Parallel Work)

## Sprint objetivo
Subir conversión de demo y velocidad comercial con UX consistente entre widget público y CRM superadmin.

---

## EPIC A — Widget Demo UX (Público)

### FE-101 · Bootstrap robusto
**Descripción:** enviar `__INIT__` y mantener `X-Chat-Session-Id` estable en todos los mensajes.
**Backend contrato:** `/api/ask/{tipo}` + respuesta `fuente=demo_selector`.
**Aceptación:** al abrir widget en chatboc.ar, siempre aparece selector rubro.

### FE-102 · Selector de rubros visual
**Descripción:** render `options_list/botones` con cards y CTA claros.
**Aceptación:** click envía `action_id=demo_select_rubro:<key>`.

### FE-103 · Captura de lead 3 pasos
**Descripción:** flujo UI para `open_demo_form` + `pedir_info=nombre/telefono/email`.
**Aceptación:** se ve progreso 1/3, 2/3, 3/3 y confirma `#nro_ticket`.

### FE-104 · Estados de error amigables
**Descripción:** validar teléfono/email antes de enviar y mostrar errores inline.
**Aceptación:** usuario corrige sin romper conversación.

### FE-105 · Continuidad tenant real vs demo
**Descripción:** consumir `ux_context` en la respuesta del chat para decidir si el widget debe renderizar shell demo o shell tenant real.
**Backend contrato:** `/api/ask/{tipo}` devuelve `ux_context.trusted_owner`, `ux_context.owner_tipo_chat`, `ux_context.should_render_demo_shell`.
**Aceptación:** si `trusted_owner=true` y `owner_tipo_chat=municipio`, nunca reaparece el showroom genérico al segundo turno.

### FE-106 · Propagación fuerte de contexto
**Descripción:** reenviar en todos los mensajes `entityToken`/`X-Entity-Token`, `X-Chat-Session-Id`, `X-Anon-Id` y `pin` cuando el usuario venga desde seguimiento de reclamo.
**Aceptación:** no hay saltos de contexto entre saludo inicial, captura de nombre y mensajes siguientes; el seguimiento público no vuelve a `403`.

---

## EPIC B — CRM Superadmin Multitenant

### FE-201 · Dashboard KPI
**Descripción:** cards usando `/api/admin/leads/pipeline`.
**Métricas:** `total`, `conversion_rate`, `avg_first_response_seconds`, `ganado/perdido`.

### FE-202 · Kanban pipeline
**Descripción:** columnas por `stage` y cards por lead.
**Datos:** `items[]` del endpoint pipeline.

### FE-203 · Cambio de etapa (nuevo)
**Descripción:** drag/drop o selector que llama:
`PATCH /api/admin/leads/{ticket_id}/stage` con `{ "stage": "...", "note": "..." }`.
**Stages válidos:**
`nuevo`, `contactado`, `calificado`, `demo_agendada`, `propuesta_enviada`, `ganado`, `perdido`.

### FE-204 · Filtros globales
**Descripción:** filtros por `tenant_slug` y `since_days` en pipeline e interactions.

### FE-205 · Contacto rápido
**Descripción:** acciones `wa.me`, `mailto`, copiar datos.
**Aceptación:** 1 click para contactar prospecto.

---

## EPIC C — Tracking & Analytics UX

### FE-301 · Telemetría onboarding
Eventos: `widget_opened`, `demo_selector_rendered`, `demo_option_clicked`, `lead_cta_clicked`, `lead_completed`.

### FE-302 · Funnel visual
Vista embudo usando `by_stage` + filtros.

---

## Dependencias backend (ya listas)
- `GET /api/admin/leads/pipeline`
- `PATCH /api/admin/leads/{ticket_id}/stage`
- `GET /api/admin/leads/interactions`
- Flujo demo lead capture: `action_id=open_demo_form`
- Respuesta chat enriquecida con `ux_context`
