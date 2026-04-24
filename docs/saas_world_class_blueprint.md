# Blueprint SaaS World-Class — Chatboc

## Visión

Llevar Chatboc a una plataforma SaaS competitiva a nivel global requiere que
cada módulo se comporte como producto enterprise:

- **tenant-aware por defecto**
- **realtime-first**
- **omnichannel nativo**
- **medible end-to-end**
- **operable por equipos grandes sin fricción**

Este blueprint aterriza mejoras concretas para backend, frontend, datos,
seguridad y operaciones sobre la base ya existente del sistema.

---

## 1. North Star de producto

### 1.1 Ciudadano / cliente final
- Puede iniciar, seguir y responder un ticket desde **WhatsApp, widget, portal público o email** sin perder contexto.
- Ve estados claros, mapa, SLA y canal de contacto real.
- Recibe notificaciones consistentes: alta, toma, en proceso, resolución, encuesta.

### 1.2 Operador / agente
- Tiene una **bandeja unificada** con mensajes, tickets, SLA y prioridad.
- Puede responder desde un solo lugar y disparar email/SMS/WhatsApp desde el mismo hilo.
- Ve sugerencias IA útiles, no intrusivas: resumen, siguiente mejor acción, clasificación y borrador.

### 1.3 Admin tenant
- Configura branding, canales, horarios, automatizaciones y reportes sin tocar código.
- Tiene dashboards accionables y comparables por área/canal/agente.
- Puede activar features enterprise por plan.

---

## 2. Arquitectura recomendada por capas

### 2.1 Conversation Core
Crear un dominio común `conversation_stream` para:
- mensajes usuario
- mensajes agente
- comentarios internos
- cambios de estado
- adjuntos
- eventos de automatización

**Resultado:** ticket, tracking público, Atención Ciudadana y panel admin dejan de ser silos.

### 2.2 Real-time backbone
Estándar de eventos:
- `conversation.message.created`
- `conversation.message.updated`
- `ticket.status.changed`
- `ticket.assignment.changed`
- `ticket.presence.changed`
- `ticket.notification.dispatched`

**Resultado:** frontend puede reconciliar HTTP + sockets con un contrato único.

### 2.3 Tenant Context Engine
Unificar resolución tenant para:
- JWT
- entity token
- static widget token
- dominio/subdominio
- `tenant_slug`
- número de WhatsApp

**Resultado:** menos drift entre widget, tracking, portal y panel.

### 2.4 Notification Orchestrator
Centralizar plantillas, reintentos y tracking de entrega para:
- email
- SMS
- WhatsApp
- push/web notifications

**Resultado:** observabilidad real de entregas y fallas por tenant/canal.

---

## 3. Roadmap backend de alto impacto

### P0 — Consistencia operacional
1. **Conversation stream unificado**
   - reemplazar múltiples orígenes dispersos por un timeline canónico.
2. **SLA engine**
   - reloj por ticket/canal/prioridad.
   - alertas de “sin respuesta”, “vencido”, “sin asignación”.
3. **Audit trail fuerte**
   - quién cambió estado, asignación, mensaje o configuración.
4. **Delivery logs**
   - tabla/evento por intento de email/SMS/WhatsApp.

### P1 — Inteligencia aplicada
1. **Auto-triage**
   - categoría, prioridad, área y sugerencia de agente.
2. **Copilot de operador**
   - resumen del caso, respuesta sugerida, checklist siguiente paso.
3. **Detección de riesgo**
   - tickets trabados, vecinos enojados, múltiples recontactos, picos por zona.

### P2 — Plataforma enterprise
1. **Feature flags por plan**
2. **rate limits por tenant/canal**
3. **quotas y billing hooks**
4. **outbox/event bus para integraciones**

---

## 4. Roadmap frontend para verse “empresa de miles de empleados”

### 4.1 Unified Inbox
Un solo frontend para:
- bandeja viva
- detalle del ticket
- conversación en tiempo real
- historial de estados
- adjuntos y mapa

### 4.2 Shell adaptable por contexto
El frontend debe decidir visualmente entre:
- demo pública
- municipio real
- pyme real
- tracking público
- operador/admin

Usar `ux_context` y contexto tenant persistido para no “mezclar mundos”.

### 4.3 UI enterprise-grade
- skeletons consistentes
- optimistic updates con rollback
- badges de SLA/no leídos
- filtros guardables
- atajos de teclado
- panel dividido tipo CRM/helpdesk

### 4.4 Observabilidad UX
Registrar:
- pérdida de contexto tenant
- chat abierto sin owner confiable
- 403/404 por tracking
- socket reconnects
- mensajes duplicados
- time-to-first-response en UI

---

## 5. Omnicanalidad profesional

### WhatsApp
- continuidad total con ticket/timeline
- plantillas por estado
- receipt y delivery status visibles en panel

### Widget
- persistencia fuerte de `entityToken`, `anon_id`, `session_id`
- fallback visual elegante si se pierde contexto
- soporte de adjuntos, ubicación, audio y live chat

### Email
- reply-to-ticket por hilo real
- parseo seguro de respuestas y archivos

### SMS
- usarlo como canal de urgencia/SLA, no solo como espejo

---

## 6. Mapa, territorio y analítica geográfica

### Lo mínimo competitivo
- todos los tickets con geocoding o razón explícita de ausencia
- clusters, heatmap, filtros por estado/categoría/canal
- rutas operativas y priorización territorial

### Nivel enterprise
- capas por cuadrilla/zona/área
- tiempos promedio por barrio
- correlación entre volumen, satisfacción y tiempos

---

## 7. Seguridad y compliance de verdad

### Prioridades
- RBAC formal por tenant
- scopes de agente por categoría/zona
- rotación/regeneración de tokens
- masking de PII en logs y analytics
- export/delete de datos por tenant
- retention policies por canal

### Ideal siguiente fase
- SSO/SAML para enterprise
- auditoría exportable
- políticas por país/tenant

---

## 8. Métricas de “empresa seria”

### Producto
- activation rate por tenant
- porcentaje de tickets con respuesta < X min
- self-resolution rate
- reopen rate
- CSAT/NPS por canal

### Operación
- backlog por agente
- SLA compliance
- volumen por canal
- first contact resolution

### Revenue / SaaS
- expansión por plan
- uso por feature
- tenants activos 7/30 días
- health score por tenant

---

## 9. Próximos 30/60/90 días sugeridos

### Próximos 30 días
- unificar tracking público + ticket comments + live chat
- terminar contrato frontend con `ux_context`
- delivery logs por canal
- websocket rooms por ticket y tenant

### Próximos 60 días
- unified inbox en frontend
- SLA engine y badges operativos
- plantillas omnicanal por estado
- dashboards por agente/canal/zona

### Próximos 90 días
- copiloto IA operativo
- feature flags/billing hooks
- automatizaciones por tenant
- reporting ejecutivo y health score

---

## 10. Recomendación ejecutiva

Si el objetivo es construir un producto “mundial” y no solo funcional, las tres
palancas con mejor retorno ahora mismo son:

1. **unificar conversación/ticket/timeline**
2. **hacer realtime confiable y observable**
3. **dar al frontend un contrato explícito de contexto tenant + estado UX**

Eso hace que WhatsApp, widget, tracking público y panel se sientan como un solo
sistema premium, en vez de módulos correctos pero separados.
