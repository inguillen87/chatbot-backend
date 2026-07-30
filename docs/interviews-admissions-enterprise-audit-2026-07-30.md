# Auditoria enterprise: entrevistas, admisiones y evaluaciones

Fecha: 2026-07-30

## Veredicto ejecutivo

La auditoria encontro que el checkout no tenia un dominio operativo de entrevistas, admisiones o evaluaciones. Durante este slice se implemento localmente un core P0 tenant-scoped con programa/version publicada e inmutable, caso, sesion, consentimiento versionado, evidencia por referencia y cierre exclusivo en `awaiting_human_review`.

No es correcto vender el estado actual como funnel de admisiones listo para produccion. El core backend esta feature-gated y apagado por defecto; todavia faltan los adaptadores WhatsApp/widget/voice, UI, asignaciones, rubricas/revision humana completa, analitica de lifecycle, migracion aplicada y prueba de staging.

## Matriz real versus placeholder

| Superficie | Estado comprobado | Evidencia | Brecha enterprise |
| --- | --- | --- | --- |
| Estructura escolar | Real | `models_education.py`: School, Campus, AcademicLevel, Shift, CourseSection, Student | Falta gobierno fino de datos y ciclo de admision |
| Tutor y relacion con alumno | Real; limite de acceso endurecido localmente | `models_education.py`; `routes/education_routes.py` | La prueba de telefono no existe; solo queda habilitada atestacion institucional explicita |
| Casos escolares | Real como alias de ticket | `SchoolCaseAlias`; `services/education_case_service.py` | No es un applicant/admission/interview record |
| Admisiones en taxonomia | Parcial | `services/education_contracts.py:236-240` | Solo categoria/cola; no hay pipeline ni campos estructurados |
| Intake WhatsApp | Real para crear ticket generico | `routes/whatsapp_webhook.py:2390-2479` | Un detalle nuevo crea el ticket; no valida nivel, vacante, consentimiento, contacto ni etapa |
| Intake widget | Real para crear ticket generico | `services/pymes.py`; `tests/test_education_widget_flow.py` | Misma brecha: no crea candidatura ni entrevista |
| Llamada realtime | Real para crear caso escolar | `services/voice_stream_service.py:2128-2245` | La herramienta persiste ticket/alias, no sesion, transcript-evidence ni scorecard |
| Core assessment/interview v1 | Implementado y validado localmente; apagado por defecto | `models_interviews.py`; `services/interview_service.py`; `routes/v2/interviews.py` | Sin adaptadores de canal, UI ni evidencia remota de staging |
| QA de llamadas | Stub | `services/call_qa_service.py:15-26` devuelve score fijo 85 | No debe alimentar evaluaciones ni KPIs |
| Encuestas adaptativas | Real y reutilizable | `routes/v2/surveys.py:1537-1600`; `services/encuestas_service.py:1553-2329` | No modela entrevistador, asignacion, evidencia, rubrica o decision |
| UI de admisiones | Placeholder | `EducationAdmissionsPage.tsx`; `EducationContractPendingCard.tsx` | Muestra "Pantalla lista" aunque la configuracion esta pendiente |
| Feature gate de admisiones | Riesgoso | `src/config/featureFlags.ts:14` usa default `true` | Debe ser fail-closed hasta que exista contrato backend probado |
| Shell educativo | Contrato frontend sin route encontrada | `useEducationShellData.ts:24` llama `/api/v1/education/shell` | No se encontro endpoint backend en este checkout |
| Endpoints `/education/admissions/*` | Ausentes | La pagina los anuncia; busqueda backend sin resultados | Contrato todavia inexistente |
| Interview/Admission/Rubric/Scorecard/Decision models | Core Program/Version/Case/Session/Evidence implementado; Rubric/Scorecard/Decision ausentes | `models_interviews.py` | Decision final queda deliberadamente fuera del API P0; falta workflow humano posterior |

## P0 de seguridad encontrado y corregido localmente

Antes de esta auditoria, tres endpoints permitian una cadena explotable: lookup anonimo de tutor por telefono/email/documento, "verificacion" por simple coincidencia de telefono y vinculacion anonima con un alumno arbitrario. Ademas, `/family/context?guardian_id=` permitia a una identidad autenticada leer otro tutor y sus alumnos dentro del mismo tenant.

La correccion local aplica estas invariantes:

- Todo lookup, verify, link y family-context requiere autenticacion.
- El tenant se deriva de la identidad autenticada; el body/query no puede cambiarlo.
- Familia significa rol canonico `usuario` (incluye alias `cliente`) o `lead`, salvo que el usuario sea propietario real del tenant en `TenantProfile`.
- Una familia solo puede resolver el `Guardian.user_id` vinculado explicitamente. Email y telefono nunca crean ni infieren el vinculo.
- Un operador requiere capabilities separadas: `education.guardians.read`, `education.guardians.verify` y `education.guardians.link`.
- Admin y owner siguen tenant-bound. El bypass de owner usa propiedad server-side, no un rol declarado por el cliente.
- La verificacion por telefono sin challenge falla cerrada. Los metodos admitidos son atestaciones institucionales explicitas con referencia opaca.
- La referencia se persiste solo como HMAC-SHA256. La respuesta declara `phone_ownership_verified: false`.
- Vincular alumno exige tutor ya verificado y capability de link.
- Operacion y AuditEvent se confirman en una sola transaccion. Si falla el commit de auditoria, todo se revierte y responde 503.
- El audit no guarda telefono, documento, email, evidencia raw ni IP.

Archivos modificados:

- `routes/education_routes.py`
- `services/education_access_policy.py`
- `tests/test_education_routes.py`

Esto esta validado localmente, no desplegado ni certificado contra produccion.

## Arquitectura objetivo reutilizable

Conviene crear un core multi-vertical y adaptadores finos, no tres implementaciones distintas.

### Entidades minimas

- `AssessmentProgram`: define el proceso (admision escolar, intake ciudadano, seleccion laboral, calificacion comercial).
- `AssessmentProgramVersion`: snapshot inmutable de preguntas, reglas, templates, rubrica y politica de consentimiento.
- `AssessmentCase`: sujeto, tenant, org-unit, etapa, owner, SLA y fuente.
- `InterviewSession`: canal, estado, agenda, entrevistador/agente, timestamps y version de programa.
- `InterviewAssignment`: cola, responsable, reasignacion y motivo.
- `InterviewEvidence`: referencias a audio, imagen, archivo, transcript y hash/provenance; nunca blobs duplicados.
- `Rubric` y `RubricCriterion`: criterios versionados, escala, peso y reglas de completitud.
- `Scorecard`: puntuacion por criterio, evidencia citada, autor humano o recomendacion AI y revision.
- `AIRecommendation`: salida estructurada separada de la decision, modelo/prompt/version, confianza, limites y citas de evidencia.
- `DecisionReview` y `Decision`: aprobacion humana, motivo, doble control cuando aplique y evento auditable.
- `ConsentRecord` y `RetentionPolicy`: finalidad, canal, version, vencimiento, revocacion y borrado.

### Estados minimos

- Programa: `draft -> published -> retired`.
- Caso: `new -> consent_pending -> ready -> scheduled -> in_progress -> awaiting_human_review -> decision_pending -> closed` con `withdrawn` y `cancelled`.
- Sesion: `scheduled -> active -> completed`, con `interrupted`, `no_show` y `void`.
- Decision: `draft -> under_review -> finalized`, sin salto directo desde una recomendacion AI.

Cada transicion debe validar tenant, capability, version esperada e idempotency key, y producir AuditEvent/outbox durable.

### Politica AI obligatoria

Para admision educativa, empleo y acceso a servicios publicos, la AI puede resumir, extraer datos, detectar faltantes y proponer una scorecard. No debe emitir ni ejecutar automaticamente rechazo, admision, contratacion, elegibilidad o sancion.

Controles minimos:

- esquema estructurado y validado por backend;
- evidencia citada por criterio;
- campos prohibidos y minimizacion de datos sensibles;
- decision final humana con capability dedicada;
- explicacion y motivo de override;
- evaluaciones de precision, sesgo, drift y false negatives por version;
- retencion especial para menores, audio y documentos;
- fallback humano cuando la confianza o calidad de evidencia sea insuficiente.

## Barra competitiva

La paridad con plataformas omnicanal como respond.io, Jelou o ChatBot.com no se logra agregando prompts. El baseline funcional es inbox unificado, routing/ownership, lifecycle, templates aprobables, automatizaciones, CRM, API/webhooks, analitica y permisos auditables.

La diferenciacion defendible para Chatboc puede ser un motor de intake/entrevista gobernado y multi-vertical que conserve evidencia y supervision humana. Las comparaciones de precios, features exactos y releases de competidores deben verificarse en un benchmark web separado antes de usarlas comercialmente.

## Core P0 local y siguiente integracion

El core generico hasta revision humana ya esta implementado localmente, sin decision automatica. El siguiente cambio debe ser chico, vertical y demostrable: conectar admision escolar desde intake real al mismo `AssessmentCase`, sin crear tres dominios divergentes.

Backend propuesto:

1. Implementado: `models_interviews.py` con Program, ProgramVersion, Case, Session y Evidence; el P0 termina en `awaiting_human_review`.
2. Implementado: migration Alembic lineal con claves tenant, constraints de estado, idempotencia e inmutabilidad de versiones publicadas.
3. Implementado parcialmente: `services/interview_access_policy.py` con capabilities separadas para program, case, session y evidence; assignment/score/finalize no pertenecen a este P0.
4. Implementado: `services/interview_service.py` con maquina de estados, validacion, auditoria atomica e idempotencia.
5. Implementado y feature-gated: `routes/v2/interviews.py` con contratos v2 y errores estructurados.
6. Adaptador `routes/v2/education_admissions.py`: convierte intake de admisiones en AssessmentCase sin duplicar dominio.
7. Registro de blueprints en la factory de app.

Frontend propuesto:

1. `src/types/interviews.ts` y `src/api/interviews.ts`.
2. Reemplazar `EducationContractPendingCard` en `EducationAdmissionsPage.tsx` por un inbox real con estados de loading/empty/error/locked/ready.
3. Vista de caso con timeline, consentimiento, evidencia, asignacion y proxima accion.
4. Activar `admissions_enabled` solo con capability backend y contrato compatible; default local `false` mientras no exista.

Reutilizacion segura:

- Reusar el editor/runtime de encuestas para preguntas y branching.
- No guardar respuestas de admision como simples `EncRespuesta`: se necesita version, sujeto, entrevistador, consentimiento y evidencia.
- Reusar adjuntos, realtime, tickets/handoff, outbox y auditoria; el ticket puede ser una vista operativa, no la fuente canonica de admision.

## Criterios de aceptacion del slice

- Ningun endpoint funciona sin auth y tenant autorizado.
- Tests IDOR same-tenant y cross-tenant por cada recurso sensible.
- Employee sin capability recibe 403; cada capability tiene test positivo y negativo.
- Crear caso es idempotente y no duplica ante retry.
- La sesion queda fijada a una ProgramVersion publicada.
- No se inicia entrevista sin consentimiento aplicable.
- Evidencia conserva provenance/hash y no entra raw al AuditEvent.
- AI output invalido no muta estado.
- AI no puede finalizar decision.
- Fallo de audit/outbox revierte la transicion.
- WhatsApp, widget y voice producen el mismo caso estructurado y no tres tickets divergentes.
- UI no muestra estado "listo" si el endpoint falta o la capability esta cerrada.
- Suite backend, frontend, contrato y E2E de una admision completa pasan antes de habilitar el flag.

## Validacion ejecutada en esta auditoria

```text
.codex-venv\Scripts\python.exe -m py_compile routes\education_routes.py services\education_access_policy.py tests\test_education_routes.py
.codex-venv\Scripts\python.exe -m pytest -q tests\test_education_routes.py
Resultado: 17 passed

.codex-venv\Scripts\python.exe -m pytest -q tests\test_education_routes.py tests\test_education_widget_flow.py tests\test_education_kb_service.py tests\test_education_kb_api.py tests\test_rubros_education_profile.py tests\test_rubros_endpoint.py tests\test_rubros_demo_mode.py
Resultado: 30 passed

.codex-venv\Scripts\python.exe -m pytest -q tests\test_interviews_v2.py -p no:cacheprovider
Resultado: 12 passed

.codex-venv\Scripts\python.exe -m pytest -q tests\test_interviews_migration.py -p no:cacheprovider
Resultado: 1 passed
```

Warnings observados, no causados por este slice: usos legacy de `Query.get()`, warning de orden de DROP por ciclo `tenant_profile/user`, Qdrant compatibility warning y falta de permiso para `.pytest_cache`.

## Estado de entrega del core P0

El core backend esta implementado y probado localmente. La bandera `ENABLE_ASSESSMENT_INTERVIEWS_V1` usa opt-in estricto, queda en `false` en `.env.example` y Render, y toda version publicada es inmutable; los casos fijan version/hash y las sesiones requieren consentimiento explicito de esa version. La evidencia acepta unicamente referencias opacas, hash y provenance allowlisted; el audit no conserva contenido ni referencias de storage/mensaje. Completar una sesion solo produce `awaiting_human_review`: no existe endpoint ni campo capaz de admitir, rechazar, contratar o decidir elegibilidad.

Esto no esta desplegado ni certificado en produccion. No hay adaptadores WhatsApp/widget/voice hacia el nuevo dominio, UI administrativa/CRM para operarlo, outbox de integracion, migration aplicada en Render, ni smoke/E2E de staging. Hasta completar esas capas y la revision de privacidad para menores, la feature debe permanecer apagada.
