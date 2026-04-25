# Chatboc.ar - Backend execution brief
## Vertical educacion: colegios publicos y privados de Mendoza y Argentina

## Objetivo del backend

Convertir el backend actual en el motor de un SaaS escolar multi-tenant, omnicanal y auditable.  
Debe soportar operacion real de colegios, no solo respuestas por WhatsApp. La prioridad correcta es:

1. Identidad, RBAC y AI gateway.
2. Directorio escolar y routing operativo.
3. Portal familia/alumno + tienda escolar.
4. Analytics, heatmaps, resumenes IA.
5. Integraciones, multimodal, voz y offline parcial.

## Estado base que se debe preservar

- Backend Flask multi-tenant con chat, tickets, catalogo, embeddings, Qdrant, realtime web y voz.
- Endpoints existentes de chat, widget, tickets, catalogo y sockets.
- EPIC-001 de auth/JWKS ya encaminado.
- Contratos existentes que alimentan el widget y el panel.

No redisenar todo. Extender con capas nuevas y contratos versionados.

## No negociables

1. No romper `/ask`, `/widget/*`, `/tickets/*`, `/catalogo/*` ni sockets existentes sin versionado.
2. No duplicar logica de negocio en frontend.
3. No permitir fugas cross-tenant ni cross-campus.
4. No usar datos sensibles de menores sin RBAC, masking y auditoria.
5. No generar analytics desde mocks ni caches inconsistentes.
6. No abrir integraciones con sistemas externos sin allowlists, idempotencia y trazabilidad.
7. No exponer informacion de convivencia, salud o bullying a roles amplios.

## Modelo de dominio recomendado

| Entidad | Para que sirve | Notas |
| --- | --- | --- |
| `tenant_type` | Diferenciar `school_public`, `school_private`, `school_group`, `district` | Afecta permisos y modulos |
| `school_profile` | Datos base del colegio | Marca, contacto, modo operativo, configuraciones |
| `campus` | Sedes | Necesario para multi-sede |
| `school_level` | Inicial, primaria, secundaria, superior | Filtro de reglas y comunicacion |
| `academic_year` / `term` | Contexto temporal | Evita mezclar ciclos |
| `course` / `division` | Segmentacion academica | Base para comunicados y directorio |
| `student` | Alumno | Minimizar PII en lecturas amplias |
| `guardian` | Madre/padre/tutor | Puede tener multiples alumnos |
| `guardian_link` | Relacion tutor-alumno | Define permisos y vistas |
| `staff_member` | Empleado del tenant | Puede tener varias funciones |
| `staff_role_assignment` | Rol + area + campus + skill | Base de routing |
| `announcement` | Comunicado oficial | Debe soportar acuse |
| `calendar_event` | Reuniones, actos, fechas | Compartido con WhatsApp y portal |
| `support_case` | Reclamo / consulta / incidente | Evolucion de ticket actual |
| `case_assignment_rule` | Reglas de derivacion | Categoria + zona + campus + horario |
| `survey_campaign` / `vote_campaign` | Encuestas y votaciones | Separadas, no mezclar |
| `store_item` / `store_order` | Tienda escolar | Productos, servicios y eventos |
| `payment_intent` / `payment_link` | Pagos | Primero abstraccion; gateway despues |
| `admission_lead` | Lead de inscripcion | Pipeline para privados y mixtos |
| `knowledge_source` / `knowledge_chunk` | RAG escolar | Reglamentos, FAQs, aranceles, etc. |
| `consent_record` | Consentimientos | Imagen, datos, autorizaciones, contacto |
| `audit_event` | Trazabilidad | Acceso, cambios, tool calls, policy gates |
| `analytics_event` | Fuente de verdad operativa | Base para KPIs, heatmaps y resumenes |

## Fase A - Fundaciones

### BE-EDU-001 - AI Gateway Responses
**Objetivo:** encapsular OpenAI Responses como capa central y sacar llamadas dispersas.  
**Componentes:** `services/ai_gateway.py`, provider adapter, tool registry, policy hooks, telemetry.  
**Requisitos:**
- Structured Outputs para payloads que terminan en UI o workflow.
- Trazas por tenant, actor, canal, prompt version y costo.
- Function calling con validacion estricta e idempotencia.
- No mezclar Realtime/voz en este epic.
**Aceptacion:**
- Endpoint o servicio interno estable.
- Tests con fake provider.
- Logs de latencia, uso y errores.

### BE-EDU-002 - Tenant escolar y contexto academico
**Objetivo:** introducir la capa escolar sin ensuciar modelos legacy.  
**Componentes:** `models.py`, migraciones, servicios de directorio.  
**Alcance:**
- `tenant_type`
- `school_profile`
- `campus`, `level`, `academic_year`, `course`, `division`
- seeds demo `Colegio Demo Mendoza`
**Aceptacion:**
- Un tenant puede operar con una o varias sedes.
- Los canales y analytics quedan scopiados por tenant/campus.

### BE-EDU-003 - RBAC + empleados + skills + reglas de derivacion
**Objetivo:** que cada admin tenant pueda crear empleados y distribuir casos por categoria.  
**Componentes:** auth, modelos de staff, reglas de asignacion, ticket service.  
**Alcance:**
- Roles del vertical.
- Skills por area: secretaria, preceptoria, tesoreria, admisiones, mantenimiento, convivencia.
- Reglas: categoria + campus + prioridad + horario + disponibilidad.
**Aceptacion:**
- Un caso nuevo puede autoderivarse.
- Reasignacion manual no rompe SLA ni historial.
- Casos sensibles solo visibles a roles habilitados.

### BE-EDU-004 - Identidad de familia y alumno
**Objetivo:** crear una identidad final separada del panel admin.  
**Componentes:** auth, portal bootstrap, tokens y sesiones.  
**Alcance:**
- `family_user` y `student_user`
- magic links, OTP o acceso por invitacion
- deep links desde WhatsApp y widget hacia el portal del tenant
**Aceptacion:**
- Una familia entra al portal correcto desde el WhatsApp del colegio.
- Los alumnos no ven datos de otros alumnos ni de sus tutores.
- Panel/admin no comparte sesion con family/student app.

## Fase B - Operacion escolar

### BE-EDU-005 - Inbox omnicanal y evolucion del ticket
**Objetivo:** convertir el ticket actual en soporte escolar de verdad.  
**Componentes:** `routes/ticket.py`, `socket_service.py`, notificaciones, categorias.  
**Categorias minimas:**
- Secretaria
- Preceptoria
- Administracion/Tesoreria
- Admisiones
- Mantenimiento
- Tecnologia
- Convivencia/Bullying
- Transporte
- Comedor
**Aceptacion:**
- WhatsApp, widget y portal crean el mismo `support_case`.
- Timeline unificada con mensajes, adjuntos, estado, asignaciones y SLA.
- Estados visibles al usuario final con token seguro de seguimiento.

### BE-EDU-006 - Comunicados, cartelera y acuse
**Objetivo:** soportar comunicacion oficial y trazable.  
**Componentes:** announcements, calendar events, receipt tracking.  
**Alcance:**
- comunicado
- noticia
- evento
- emergencia
- recordatorio
- requerir lectura / acuse / firma simple
**Aceptacion:**
- El colegio puede segmentar por campus, nivel, curso, familia o staff.
- El sistema registra entregado, leido, confirmado.

### BE-EDU-007 - Encuestas y votaciones
**Objetivo:** ordenar un modulo hoy difuso.  
**Componentes:** campaign service, respuestas, agregaciones.  
**Separar claramente:**
- encuesta de satisfaccion
- sondeo
- votacion
- autorizacion (queda fuera; entidad aparte)
**Aceptacion:**
- Publicar, cerrar, exportar y segmentar por tenant/campus/grupo.
- Agregados anonimos o nominales segun configuracion y rol.

### BE-EDU-008 - Incidencias sensibles y convivencia
**Objetivo:** abrir un canal confiable para incidentes, violencia escolar o bullying.  
**Componentes:** soporte cases con confidentiality tiers, auditoria, permisos.  
**Alcance:**
- visibilidad restringida
- anonimato opcional en origen
- politicas de escalamiento
- adjuntos y evidencia
**Aceptacion:**
- Un reporte sensible nunca aparece en bandejas generales.
- Todo acceso queda auditado.
- El canal puede convivir con lineas oficiales o derivacion externa.

### BE-EDU-009 - Motor de notificaciones
**Objetivo:** dejar de resolver envios caso por caso.  
**Canales:** WhatsApp, email, push web/PWA, eventualmente SMS/voz.  
**Casos de uso:**
- estado de reclamo
- comunicado urgente
- encuesta pendiente
- recordatorio de evento
- link de pago
- carritos abandonados de tienda
- invitacion a portal
**Aceptacion:**
- Plantillas versionadas.
- Preferencias y opt-in por usuario/canal.
- Retry, rate limiting y trazabilidad.

## Fase C - Portal transaccional y growth

### BE-EDU-010 - Tienda escolar / marketplace por tenant
**Objetivo:** reaprovechar la capa de marketplace como tienda escolar real.  
**Items posibles:**
- cuotas y matriculas
- uniformes
- libros
- talleres
- viajes/salidas
- eventos con cupo
- cooperadora
**Componentes:** catalogo, orders, pricing, inventory, media.  
**Aceptacion:**
- Cada tenant tiene store propia.
- Acceso desde portal, widget y WhatsApp.
- Catalogo editable desde admin con preview.

### BE-EDU-011 - Abstraccion de pagos
**Objetivo:** no acoplar negocio a un gateway unico.  
**Componentes:** payment intents, links, estados, conciliacion.  
**Alcance:**
- orden
- link de pago
- estado pagado/pendiente/vencido
- callback idempotente
**Aceptacion:**
- El modulo soporta privados y casos de cooperadora/eventos.
- Si no hay gateway configurado, el sistema usa modo manual sin romper UX.

### BE-EDU-012 - Admisiones e inscripciones
**Objetivo:** abrir una segunda linea de valor, especialmente para colegios privados.  
**Componentes:** lead pipeline, agenda, documentos, seguimiento.  
**Alcance:**
- captacion desde landing, widget o WhatsApp
- estados de pipeline
- checklist documental
- asignacion a admisiones
**Aceptacion:**
- Todo lead queda trazado por canal.
- Conversion por fuente visible en analytics.

## Fase D - Analytics, IA y premium

### BE-EDU-013 - Event store, KPIs y heatmaps
**Objetivo:** una sola fuente de verdad para operacion y negocio.  
**Eventos minimos:**
- `conversation_started`
- `message_received`
- `case_created`
- `case_assigned`
- `case_resolved`
- `announcement_sent`
- `announcement_acknowledged`
- `survey_started`
- `survey_answered`
- `store_item_viewed`
- `cart_started`
- `order_created`
- `payment_link_sent`
- `portal_login`
- `location_shared`
- `incident_reported`
**Heatmaps recomendados:**
- por franja horaria
- por categoria
- por campus/sector
- por zona geografica si el evento trae geo
**Aceptacion:**
- Los cards, tablas, mapas, reportes y resumenes IA derivan de estas mismas agregaciones.
- Debe existir un endpoint de frescura: ultimo evento, ultima agregacion, razon de "sin datos".

### BE-EDU-014 - Resumenes IA y copilotos internos
**Objetivo:** que la IA sirva a directivos y operadores, no solo al usuario final.  
**Casos:**
- resumen diario para direccion
- top problemas de la semana
- borrador de comunicado
- clustering de reclamos repetidos
- recomendacion de FAQ o accion
**Aceptacion:**
- Siempre con trazabilidad y source links internos.
- No inventar datos; resumir agregados existentes.

### BE-EDU-015 - RAG escolar multi-fuente
**Objetivo:** que el bot y el copiloto respondan con fuentes del colegio.  
**Fuentes:**
- reglamentos
- calendario
- horarios
- requisitos de inscripcion
- aranceles
- menus
- instructivos
- formularios
**Aceptacion:**
- Ingesta por tenant.
- Filtrado por tenant/campus/categoria.
- Citas internas visibles en panel.

### BE-EDU-016 - Multimodal, voz y adjuntos
**Objetivo:** sumar valor real, no demo.  
**Casos:**
- certificado medico por foto
- comprobante de pago
- autorizacion firmada
- nota de voz familiar
- llamada con IA + handoff humano
**Aceptacion:**
- Clasificacion y borrador de caso.
- Politicas de retencion y acceso a adjuntos.
- Voz browser/mobile en un epic posterior, no mezclado con AI gateway.

### BE-EDU-017 - Integraciones y conectores
**Objetivo:** conectar sin hipotecar el core.  
**Prioridad:**
1. import/export CSV/XLSX
2. email/calendario
3. LMS/ERP/SIS
4. conectores provinciales o institucionales si existen convenios
**Nota:** para Mendoza, pensar primero import/export y sincronizaciones asistidas; no asumir APIs oficiales estables de GEI/GEM.
**Aceptacion:**
- Cola de jobs, retries, auditoria y mapeo de errores.
- Feature flags por tenant.

### BE-EDU-018 - Sync y soporte offline parcial
**Objetivo:** permitir operacion basica en conectividad pobre.  
**Alcance backend:**
- cola de eventos
- replay seguro
- versionado de recursos
- sync state
**Aceptacion:**
- Solo para PWA publica/portal donde sea seguro.
- Nada de cachear datos sensibles sin politica clara.

## Endpoints sugeridos

### Nucleo escolar
- `GET /api/schools/me`
- `GET /api/schools/campuses`
- `GET /api/schools/levels`
- `GET /api/schools/academic-years`

### Directorio y roles
- `GET /api/directory/staff`
- `POST /api/directory/staff`
- `GET /api/directory/families`
- `GET /api/directory/students`
- `POST /api/routing/rules`
- `GET /api/routing/rules`

### Portal
- `POST /api/portal/bootstrap`
- `GET /api/portal/me`
- `GET /api/portal/cases`
- `GET /api/portal/announcements`
- `GET /api/portal/store`
- `GET /api/portal/orders`

### Operacion
- `POST /api/cases`
- `PUT /api/cases/{id}/assign`
- `PUT /api/cases/{id}/status`
- `GET /api/cases/queue`
- `POST /api/incidents/confidential`

### Campanas
- `POST /api/announcements`
- `POST /api/surveys`
- `POST /api/votes`
- `GET /api/campaigns/results`

### Comercio
- `POST /api/store/items`
- `POST /api/orders`
- `POST /api/payments/link`
- `POST /api/admissions/leads`

### Analiticas
- `GET /api/analytics/kpis`
- `GET /api/analytics/heatmaps`
- `GET /api/analytics/freshness`
- `POST /api/analytics/ai-summary`

## Observabilidad minima obligatoria

- `request_id` por accion.
- Logs con `tenant_id`, `campus_id`, `channel`, `actor_type`, `actor_id masked`, `case_id`, `route`, `status`.
- Dashboard interno de frescura: ultimo evento, ultimo resumen, stream online/offline.
- Alertas si las agregaciones quedan atrasadas o si un tenant deja de emitir eventos.
- Auditoria separada para acciones IA y para accesos a casos sensibles.

## Test plan obligatorio

### Unit tests
- scope por tenant/campus
- routing rule engine
- visibility de casos sensibles
- portal bootstrap correcto
- announcement ack tracking
- survey/vote aggregation
- analytics event aggregation
- payment link idempotencia
- admission lead dedupe
- consent record lifecycle
- AI gateway structured output parse
- tool calls retry/idempotencia

### Integration tests
- seed `Colegio Demo Mendoza`
- alta de familia y alumno
- deep link desde WhatsApp al portal
- creacion de reclamo y autoasignacion
- publicacion de comunicado y acuse
- encuesta respondida y agregado visible
- item de tienda comprado
- lead de admision creado desde widget
- analytics actualizadas
- resumen IA generado desde datos reales

### Manual/staging checks
- tenant con datos ve analytics coherentes
- tenant sin datos ve estado vacio explicado
- role low privilege no ve casos sensibles
- family app y admin no comparten cookie/token accidentalmente

## Corte recomendado para Jules / Codex (PR slices)

| Slice | Alcance | Dependencia |
| --- | --- | --- |
| BE-S1 | AI gateway base + fake provider tests | Auth listo |
| BE-S2 | `tenant_type` + `school_profile` + `campus` | BE-S1 no bloquea |
| BE-S3 | staff roles + assignments + routing rules | BE-S2 |
| BE-S4 | family/student auth + portal bootstrap | BE-S2, Auth |
| BE-S5 | evolucion ticket -> support_case + categorias escolares | BE-S3 |
| BE-S6 | announcements + ack + calendar | BE-S4 |
| BE-S7 | surveys + votes + exports | BE-S4 |
| BE-S8 | confidential incidents + audit tiers | BE-S3, BE-S5 |
| BE-S9 | store items + orders + manual payment intents | BE-S4 |
| BE-S10 | admissions leads + pipeline | BE-S4 |
| BE-S11 | analytics event taxonomy + KPIs + freshness | BE-S5 a BE-S10 |
| BE-S12 | IA summaries + RAG base | BE-S1, BE-S11 |
| BE-S13 | connectors import/export | Modelos base listos |
| BE-S14 | multimodal adjuntos / voz posterior | AI gateway + policies |

## Lo que backend no debe hacer

- No diseñar UX del portal ni menus visuales.
- No renderizar reglas de negocio en copy hardcodeado.
- No calcular heatmaps pesados en el browser.
- No abrir pagos reales en el mismo sprint que tienda basica.
- No hacer scraping o conectores frágiles con sistemas escolares externos.
- No usar la palabra "marketplace" internamente si el modulo se vende como "tienda escolar"; resolver con dictionary/domain mapping.

## Formato de entrega exigido al agente

1. Resumen de cambios reales.
2. Archivos modificados.
3. Archivos nuevos.
4. Endpoints nuevos o versionados.
5. Migraciones y cambios de esquema.
6. Variables de entorno nuevas.
7. Tests agregados y resultado.
8. Seeds/fixtures nuevas.
9. Riesgos y blockers concretos.
10. Items diferidos con razon exacta.

No devolver brainstorming. No marcar "implementado" sin tests y rutas funcionando.