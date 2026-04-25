# Backend · vertical Educación para chatboc.ar

## 1. Objetivo

Montar la vertical educación sobre el backend actual sin romper contratos existentes ni reescribir el core.

La idea es agregar dominios, capacidades y reglas de seguridad específicas para colegios públicos y privados reutilizando:
- auth existente
- modelo multi-tenant
- tickets/chat en vivo
- catálogo + embeddings + Qdrant
- realtime web
- voz realtime
- analytics y eventos

## 2. Reglas de diseño

1. **additive first**
   - primero sumar tablas, servicios, endpoints y flags
   - no reemplazar en bloque rutas críticas existentes

2. **contracts stay stable**
   - conservar `/ask/*`, `/tickets/*`, `/catalogo/*`
   - crear alias y adapters nuevos para educación

3. **no full ERP**
   - no meter notas/boletines/actas complejas en la primera ola

4. **minor privacy first**
   - todo dato escolar debe modelar sensibilidad y permiso

5. **reuse existing engines**
   - ticket engine para casos
   - Qdrant para KB
   - socket events para presencia
   - voice stack para recepción

## 3. Qué del backend actual conviene reutilizar

### A. `routes/ticket.py`
Reusarlo para:
- administrativos
- inasistencias
- convivencia
- mantenimiento
- documentación
- cobranza
- admisiones

**No conviene crear otro motor paralelo de casos**.

### B. `routes/catalogo.py` + Qdrant
Reusarlo como:
- base de conocimiento escolar
- reglamentos
- manuales
- calendarios
- documentación de admisión
- instructivos de trámites
- FAQ de tesorería/secretaría

Se puede mantener el backend tal como está y exponer una capa de naming/UX “knowledge” para educación.

### C. `routes/chat.py`
Reusarlo para:
- asistente público del colegio
- asistente familiar verificado
- copiloto interno para staff
- admisiones privadas

### D. `socket_service.py`
Reusarlo para:
- inbox escolar
- presencia
- typing
- lectura de mensajes
- estado de casos por área

### E. `voice_routes.py` y `voice_stream_service.py`
Reusarlo para:
- central telefónica escolar
- handoff a humano
- turnos
- respuestas de recepción

## 4. Nuevo dominio de datos

## 4.1 Estructura institucional
Agregar entidades:

- `School`
  - `id`
  - `tenant_id`
  - `name`
  - `school_type` = `public` | `private`
  - `jurisdiction`
  - `brand_name`
  - `status`

- `Campus`
  - `id`
  - `school_id`
  - `name`
  - `address`
  - `phone`
  - `email`
  - `timezone`
  - `is_main`

- `AcademicLevel`
  - `id`
  - `school_id`
  - `code` = inicial / primaria / secundaria / superior
  - `name`

- `Shift`
  - `id`
  - `school_id`
  - `code` = manana / tarde / vespertino / jornada_completa
  - `name`

- `CourseSection`
  - `id`
  - `campus_id`
  - `academic_year`
  - `level_id`
  - `grade`
  - `division`
  - `shift_id`
  - `homeroom_staff_id`

## 4.2 Personas y relaciones
- `Student`
  - `id`
  - `school_id`
  - `campus_id`
  - `external_ref`
  - `first_name`
  - `last_name`
  - `document_type`
  - `document_number`
  - `birth_date`
  - `status`
  - `section_id`

- `Guardian`
  - `id`
  - `tenant_id`
  - `user_id` nullable
  - `first_name`
  - `last_name`
  - `document_number`
  - `email`
  - `phone`
  - `verification_status`
  - `preferred_channel`
  - `language`
  - `is_billing_contact`

- `StudentGuardianRelation`
  - `id`
  - `student_id`
  - `guardian_id`
  - `relationship_type`
  - `custody_scope`
  - `can_pickup`
  - `can_receive_billing`
  - `can_receive_sensitive_updates`
  - `status`

- `AuthorizedPickup`
  - `id`
  - `student_id`
  - `person_name`
  - `document_number`
  - `relationship`
  - `valid_from`
  - `valid_until`
  - `evidence_file_id`
  - `status`

## 4.3 Operación escolar
- `SchoolCase`
  - extender ticket actual o mapearlo
  - `school_case_type`
  - `sensitivity_level`
  - `student_id` nullable
  - `guardian_id` nullable
  - `campus_id`
  - `section_id`
  - `channel`
  - `sla_policy_id`

- `AttendanceRecord`
  - `id`
  - `student_id`
  - `date`
  - `status` = presente / ausente / tarde / retiro_anticipado
  - `source`
  - `recorded_by`
  - `notes`

- `AbsenceJustification`
  - `id`
  - `attendance_id`
  - `submitted_by_guardian_id`
  - `reason_type`
  - `free_text`
  - `attachment_file_id`
  - `status`
  - `reviewed_by`
  - `reviewed_at`

- `SchoolIncident`
  - `id`
  - `student_id` nullable
  - `case_id`
  - `severity`
  - `incident_type`
  - `requires_guardian_contact`
  - `requires_followup`
  - `restricted_visibility`

- `DocumentRequest`
  - `id`
  - `student_id` nullable
  - `guardian_id`
  - `request_type`
  - `status`
  - `template_id`
  - `due_at`
  - `delivery_channel`
  - `signed_url_expires_at`

- `BroadcastCampaign`
  - `id`
  - `tenant_id`
  - `school_id`
  - `audience_type`
  - `filters_json`
  - `channel_mix`
  - `message_version`
  - `status`
  - `ack_required`

- `BroadcastAck`
  - `id`
  - `campaign_id`
  - `guardian_id`
  - `student_id` nullable
  - `ack_at`
  - `channel`

### Sólo privados
- `AdmissionLead`
  - lead de admisión
- `BillingNotice`
  - estado de comunicación de cuota
- `PaymentAgreement`
  - acuerdos y compromiso de pago
- `VisitBooking`
  - visita / entrevista

## 5. Extensiones recomendadas al modelo actual

## 5.1 Tenant capabilities
Agregar a `TenantProfile` algo como:
- `vertical` = `municipio` | `pyme` | `educacion`
- `subvertical` = `escuela_publica` | `escuela_privada` | `red_escolar`
- `capabilities_json`
- `default_sensitivity_policy`
- `guardian_verification_mode`

## 5.2 Policy engine
Agregar un motor de políticas por tenant/school:
- qué puede responder la IA en modo anónimo
- qué requiere verificación de familiar
- qué temas requieren humano
- qué áreas reciben qué tipo de casos
- qué retention aplica por tipo de dato

## 5.3 Sensitivity tags
Cualquier mensaje, adjunto, ticket, resumen IA o export debe soportar:
- `public`
- `internal`
- `family`
- `sensitive`
- `critical`

## 6. Endpoints nuevos recomendados

## 6.1 Identidad y verificación familiar
- `POST /api/v1/education/guardians/lookup`
- `POST /api/v1/education/guardians/verify`
- `POST /api/v1/education/guardians/link-student`
- `GET /api/v1/education/me/family-context`

## 6.2 Estructura institucional
- `GET /api/v1/education/schools`
- `GET /api/v1/education/schools/{school_id}`
- `GET /api/v1/education/schools/{school_id}/campuses`
- `GET /api/v1/education/schools/{school_id}/sections`
- `POST /api/v1/education/schools/{school_id}/roster/import`

## 6.3 Casos escolares
Conviene reutilizar `/tickets/*` y sumar alias/filters:
- `GET /api/v1/education/cases`
- `POST /api/v1/education/cases`
- `GET /api/v1/education/cases/{id}`
- `POST /api/v1/education/cases/{id}/reply`
- `POST /api/v1/education/cases/{id}/assign`
- `POST /api/v1/education/cases/{id}/escalate`

Internamente idealmente mapean al motor de tickets existente.

## 6.4 Asistencia
- `POST /api/v1/education/attendance/bulk`
- `GET /api/v1/education/students/{id}/attendance`
- `POST /api/v1/education/attendance/{id}/justify`
- `POST /api/v1/education/attendance/{id}/approve`
- `POST /api/v1/education/attendance/{id}/reject`

## 6.5 Documentos
- `POST /api/v1/education/documents/requests`
- `GET /api/v1/education/documents/requests`
- `GET /api/v1/education/documents/requests/{id}`
- `POST /api/v1/education/documents/requests/{id}/generate`
- `POST /api/v1/education/documents/requests/{id}/deliver`

## 6.6 Campañas y comunicados
- `POST /api/v1/education/campaigns`
- `GET /api/v1/education/campaigns`
- `GET /api/v1/education/campaigns/{id}/stats`
- `POST /api/v1/education/campaigns/{id}/send`
- `POST /api/v1/education/campaigns/{id}/ack`

## 6.7 Base de conocimiento escolar
Alias sobre catálogo + RAG:
- `POST /api/v1/education/knowledge/ingest`
- `GET /api/v1/education/knowledge/sources`
- `POST /api/v1/education/knowledge/query`

## 6.8 Sólo privados
- `POST /api/v1/education/admissions/leads`
- `GET /api/v1/education/admissions/leads`
- `POST /api/v1/education/admissions/visits`
- `POST /api/v1/education/billing/notices`
- `GET /api/v1/education/billing/notices`
- `POST /api/v1/education/billing/notices/{id}/send`

## 7. Eventos realtime recomendados

- `education.case.created`
- `education.case.assigned`
- `education.case.status.changed`
- `education.case.message.created`
- `education.case.message.read`
- `education.case.typing`
- `education.attendance.justification.received`
- `education.document.ready`
- `education.campaign.ack.received`
- `education.guardian.verified`
- `education.billing.notice.delivered`
- `education.admission.lead.created`

## 8. AI/LLM: cómo subir de nivel el backend para educación

## 8.1 AI gateway único
Todo debe pasar por un gateway interno:
- prompts versionados
- policy evaluation
- moderation input/output
- tool registry
- tenant/school context
- logging y hashing
- structured outputs

## 8.2 Policy packs por vertical
Crear packs específicos:
- `edu_public_public_widget`
- `edu_public_verified_guardian`
- `edu_private_public_widget`
- `edu_private_verified_guardian`
- `edu_staff_assistant`
- `edu_billing_private`
- `edu_admissions_private`

Cada pack define:
- temas permitidos
- temas prohibidos
- si requiere verificación
- si deriva a humano
- tono y formato
- tools habilitadas

## 8.3 Tool registry específico
Tools de alto valor:
- `lookup_school_calendar`
- `lookup_document_requirements`
- `lookup_student_context` (sólo staff/familiar verificado)
- `create_school_case`
- `create_document_request`
- `submit_absence_justification`
- `check_billing_status` (sólo privados y roles válidos)
- `schedule_visit`
- `handoff_to_staff`

## 8.4 RAG escolar
Reutilizar embeddings + Qdrant, pero endurecer:
- filtros por tenant + school + campus + audience
- `source_id`, `chunk_id`, `document_type`, `valid_from`, `valid_until`
- citations internas obligatorias
- reindex batch
- invalidación por cambio de calendario/reglamento/arancel

## 8.5 Moderación y temas sensibles
Temas que no deben salir en modo público:
- datos personales de menores
- sanciones
- salud
- morosidad individual
- conflictos sensibles
- decisiones disciplinarias
- información académica individual

Para esos casos:
- respuesta segura
- pedido de verificación
- handoff a humano
- registro auditado

## 9. Seguridad y cumplimiento técnico

## 9.1 Acceso
- RBAC por rol
- scopes por school/campus/section
- restricciones por tipo de caso
- verificación fuerte de familiar para datos individuales

## 9.2 Tokens
- separar panel / widget / familiar / voz
- TTL cortos para widget
- JWKS público real
- rotación
- claims con `tenant_id`, `school_id`, `role`, `aud`, `channel`

## 9.3 Auditoría
Registrar:
- actor
- tenant
- school
- student_id cuando aplique
- prompt hash
- output hash
- tools llamadas
- decisiones de policy/moderation
- export events
- access to sensitive records

## 9.4 Retención
Agregar políticas por tipo:
- conversación pública institucional
- conversación familiar
- ticket sensible
- documento
- audio
- trazas de auditoría

## 10. Jobs de background recomendados

- importación de padrón / roster
- normalización de secciones
- campañas batch
- reindexación RAG
- generación de documentos
- recordatorios automáticos
- alertas de backlog
- conciliación de entregas por canal
- resumen diario/semanal para dirección

## 11. Métricas backend clave

- first response time por área
- time to resolution por tipo de caso
- % autoservicio
- % campañas leídas
- % justificativos procesados en SLA
- backlog por sede/turno
- costo IA por escuela/canal
- tasa de verificación familiar exitosa
- escalaciones a humano
- latencia p95 chat/texto/voz

## 12. Orden de implementación backend

## Slice 1
- tenant capabilities educación
- modelos school/campus/section
- guardian verification skeleton
- taxonomía de school cases
- políticas básicas

## Slice 2
- roster import
- family context
- KB escolar alias sobre catálogo
- create case + reply + assign

## Slice 3
- attendance + justifications
- document requests
- broadcast campaigns + ack

## Slice 4
- private admissions + billing notices

## Slice 5
- voice front desk
- image to ticket
- analytics avanzados

## 13. Qué rechazo explícitamente

- crear otro motor de inbox
- crear otro motor de búsqueda vectorial
- crear otro servicio de identidad paralelo
- construir un microservicio por módulo escolar
- resolver sensibilidad de menores sólo con prompts
- sacar directo información individual sin verificación
- meter el vertical educación dentro del mismo JSON gigante sin modelos claros

## 14. Resultado esperado

Al terminar la ola fundacional, el backend debe poder hacer esto:

1. servir un colegio con identidad y roles correctos
2. responder consultas públicas institucionales
3. verificar familias
4. abrir y gestionar casos escolares
5. procesar justificativos y solicitudes documentales
6. mandar campañas con trazabilidad
7. sostener privados con admisiones y cobranzas livianas
8. hacerlo sin romper municipios, pymes ni contratos actuales
