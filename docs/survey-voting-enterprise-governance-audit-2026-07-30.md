# Auditoria enterprise de encuestas y votaciones

Fecha de corte: 2026-07-30  
Repositorios auditados:

- Backend: `chatbot-backend`
- Frontend: `chatboc-frontend`

Esta auditoria describe el codigo local observado. No certifica Render, PostgreSQL,
Redis, Cloudflare Turnstile, Clerk ni ningun otro proveedor en produccion.

## Veredicto ejecutivo

El producto ya tiene una base valiosa para encuestas enterprise: aislamiento por
tenant en el dominio principal, control de ventana publica, revision optimista del
instrumento, recepcion exactly-once, unicidad respaldada por indice, rate limiting
y Turnstile compartidos, modo `source_anonymous`, retencion y un outbox durable de
efectos.

Todavia no debe presentarse como una plataforma de votacion gobernada, electoral o
de resultado certificable. Faltan invariantes centrales: version publicada
inmutable, revision y aprobacion con separacion de funciones, padron/elegibilidad,
credencial de voto de un solo uso, anonimato no enlazable, quorum, cierre atomico,
resultado final persistido y firmado, auditoria de lifecycle y un anclaje externo
real. Ademas, la superficie actual de transparencia tiene una falla de autorizacion
cross-tenant y publica identificadores simulados como si fueran anclajes reales.

La posicion defendible hoy es **encuestas y participacion ciudadana con controles
avanzados en desarrollo**. La posicion objetivo P0 es **consultas y votaciones
internas gobernadas**. Una eleccion publica oficial exige, ademas de este roadmap,
revision legal, threat model formal, auditoria externa y pruebas de infraestructura
real.

## Remediacion local posterior al hallazgo

El corte A de contencion del anchor fue implementado en este mismo worktree despues
de levantar la fotografia inicial:

- Hash, creacion, listado, simulacion y prueba resuelven primero tenant/encuesta;
  publish/verify ya no buscan un `snapshot_id` global sin contexto.
- El endpoint compatible `/publish/{snapshot_id}` conserva conectividad, pero solo
  ejecuta una simulacion local y devuelve `anchor_status=simulated`,
  `published=false` y `externally_verified=false`.
- Referencias historicas `SIM-*` se degradan al serializar como `simulated`; otros
  claims legacy sin verificador se exponen como `unverified`.
- La prueba distingue `local_proof_valid` de `verified`; la clave compatible
  `valido` permanece en `false` para no presentar inclusion local como verificacion
  externa.
- Frontend y backend usan `surveys.anchor.v2`, rango `desde/hasta`, rutas reales y
  copy explicita de "evidencia local, no publicacion blockchain".

Esto cierra la fuga y la sobreafirmacion en codigo local. No crea una publicacion
externa real, no corrige la falta de release/cierre final y no constituye evidencia
de despliegue.

## Matriz real contra requisito enterprise

| Dominio | Estado real | Evidencia principal | Falta para nivel enterprise |
| --- | --- | --- | --- |
| Borrador | Solido como base | `services/encuestas_service.py:3180-3293`; builder durable V2 | Mantener historial de versiones y diff gobernado |
| Revision optimista | Parcial | `models.py:2791-2804`; `services/encuestas_service.py:3296-3405` | La revision no representa una release publicada inmutable |
| Review / approval | Ausente | `EncEncuesta.estado` solo es string libre en `models.py:2763` | Estados, transiciones, comentario, actor y four-eyes |
| Publicacion | Parcial y mutable | `services/encuestas_service.py:3525-3606` | Release canonica, hash, aprobacion, actor, idempotencia y auditoria |
| Inmutabilidad publicada | Ausente | `services/encuestas_service.py:2590-2651, 2940-2968, 3296-3405` | Congelar contenido y configuracion al publicar; toda correccion crea release nueva |
| Ventana de participacion | Presente | `services/encuestas_service.py:4190-4290` | Atarla a la release y al reloj/cierre autoritativo |
| Elegibilidad | Ausente | No hay padron, regla ni credencial en `models.py:2734-3254` | Snapshot de electores/regla y credencial de un solo uso |
| Unicidad | Parcial | `models.py:2895-2964`; `services/encuestas_service.py:4302-4409` | Identidad canonica verificada, aliases y redencion atomica |
| Exactly-once HTTP | Fuerte | `models.py:2967-3036`; `services/encuestas_service.py:5960-6345` | Vincular receipt a `release_id` y credencial, no al instrumento mutable |
| Anonimato de origen | Buena base | `models.py:2734-2804, 2895-2964`; `services/encuestas_service.py:4429-4498, 6184-6225` | Separar identidad/credencial del voto y evitar enlazabilidad operacional |
| Secreto de voto | Ausente | La huella pseudonima y el contenido viven en la misma respuesta | Ballot store separado, token ciego/opaque y politicas de acceso |
| Quorum | Ausente | No existe denominador, regla ni resultado de quorum | Regla versionada y evaluacion incluida en el cierre |
| Cierre | Insuficiente | `services/encuestas_service.py:3609-3620` | Estado previo, idempotencia, drain de inflight, manifest final y actor/motivo |
| Resultado final | Ausente | `services/encuestas_analytics_service.py:3134-3150` usa solo encuesta publica activa | Endpoint final durable disponible despues del cierre |
| Resultados live | Parcial | `services/encuestas_analytics_service.py:3134-3520` | Version ligada a release, politicas de visibilidad y supresion por celdas pequenas |
| Auditoria lifecycle | Ausente | Publicar/cerrar solo escriben log de aplicacion | Eventos tenant-scoped append-only y fail-closed para acciones gobernadas |
| Antiabuso | Buena base, no elegibilidad | `services/public_survey_intake.py:85-389` | Proxy trust certificado, riesgo adaptativo y no confundir CAPTCHA/IP con derecho a voto |
| RBAC analytics/export | Buena base local | `services/survey_access_policy.py`; `routes/encuestas_analytics.py` | Completar auditoria de finalizacion/bytes y revision remota |
| RBAC lifecycle | Critico | V2 admite `empleado` en `routes/v2/surveys.py:1666-2100` | Capabilities por accion y separacion de funciones |
| Exportacion | Parcial fuerte | `routes/encuestas_analytics.py:176-241` | Manifest/digest de archivo y evento de finalizacion, no solo autorizacion |
| Transparencia / Merkle | Contenido localmente; no verificable externamente | `services/encuestas_anchor_service.py`; `routes/encuestas_anchor.py` | Cierre exhaustivo, firma y recibo real de red |
| Contrato frontend/backend | Alineado localmente para `surveys.anchor.v2` | `chatboc-frontend/src/api/encuestas.ts`; `TransparencyTab.tsx` | Certificar build desplegado y mantener contract tests |
| Privacidad en UI publica | Incompleto | `chatboc-frontend/src/types/encuestas.ts:432-469`; `SurveyForm.tsx:870-935` | Mostrar politica, pedir consentimiento y enviar version exacta |
| Datos demo | Riesgo de mezcla | `services/encuestas_service.py:3545-3598` | Namespace separado o exclusion dura del tally oficial |

## Hallazgos P0

### GOV-01 - La publicacion no crea una version inmutable

`publicar_encuesta` cambia `estado`, crea/reutiliza un link y confirma. No crea una
release ni guarda el JSON canonico o su digest. La encuesta publicada se puede
editar antes de la primera respuesta; despues de recibir respuestas aun se pueden
cambiar titulo, descripcion, tipo, fechas, recompensa, politica de identidad,
unicidad, anonimato, resultados live y comentarios. Tambien se pueden cambiar el
texto/obligatoriedad/minimos/maximos de preguntas y el texto/valor de opciones.

Esas ediciones semanticas no incrementan `structure_revision` en el camino
"non-destructive". Por lo tanto, una respuesta historica puede terminar mostrada
bajo una pregunta u opcion diferente sin que el receipt ni el cliente detecten el
cambio.

**Invariante requerido:** cada respuesta nueva referencia una `SurveyRelease`
publicada e inmutable. Cualquier correccion crea una release nueva y nunca reescribe
la semantica de votos ya emitidos.

### GOV-02 - No hay workflow ni separacion de funciones

El lifecycle efectivo es `borrador -> publicada -> cerrada`, sin review, rechazo,
aprobacion, programacion gobernada ni actor persistido por transicion. Las rutas
legacy exigen admin/superadmin, pero V2 permite crear, editar, publicar y cerrar a
`empleado` (`routes/v2/surveys.py:1666-2100`). Un mismo operador puede redactar,
publicar y cerrar.

**Invariante requerido:** tenant antes que capability; para modo gobernado, el
autor no puede aprobar su propia release. Publicar y cerrar requieren permisos
especificos y un `expected_release_digest`.

### GOV-03 - Unicidad no equivale a elegibilidad y contiene bypasses

- `politica_unicidad="libre"` devuelve `None` como huella. Una encuesta que exige
  login pero conserva esa politica admite multiples respuestas del mismo usuario.
- DNI y telefono son valores declarados por el participante. La respuesta de error
  los llama "identidad verificada", aunque no existe prueba de posesion ni padron.
- `por_dni_o_phone` concatena los identificadores presentes. Cambiar la combinacion
  de campos puede producir otra huella para la misma persona.
- Cookie e IP son controles antiabuso; no acreditan derecho a votar.

**Invariante requerido:** una credencial opaca emitida contra un snapshot de
elegibilidad se redime exactamente una vez en transaccion. Las distintas aliases
de identidad se resuelven antes de emitirla.

### GOV-04 - El cierre no produce un resultado final verificable

`cerrar_encuesta` no exige que la encuesta este publicada, no tiene idempotency key,
motivo, actor persistido, control de submissions en vuelo ni snapshot final. Solo
asigna `cerrada` y una fecha. Los resultados live cargan mediante
`get_public_encuesta`; al cerrar devuelven `403 survey_not_published`. El propio
journey lo consagra en
`tests/product_flow/test_government_claim_and_live_vote_journeys.py:875-909`.

**Invariante requerido:** cierre atomico sobre una release, corte autoritativo,
conteos reconciliados, quorum, backlog de efectos, manifest canonico firmado y
endpoint final que siga disponible segun la politica de visibilidad.

### GOV-05 - El anclaje actual cruza tenants y presenta una simulacion como publicada

`publish_snapshot` carga solo por `snapshot_id` y asigna
`anchor_status="published"` junto con `tx_id="SIM-..."`. No envia ni confirma una
transaccion externa. `generate_merkle_proof` tambien carga solo por snapshot.

Las rutas reciben `encuesta_id` y `current_user`, pero los descartan al publicar y
verificar (`routes/encuestas_anchor.py:96-170`). Un usuario con rol admitido puede
operar o consultar un snapshot ajeno si conoce sus IDs. Ademas, el snapshot acepta
un rango manual, no exige encuesta cerrada ni demuestra que incluya el universo
completo de votos aceptados.

**Contencion inmediata:** validar `tenant_id + encuesta_id + snapshot_id` en cada
operacion; retirar `published` para simulaciones; usar `simulated`/`unverified` y no
mostrar escudo de verificacion. La publicacion real solo llega a `confirmed` con
receipt de proveedor y verificaciones de chain, contract, payload y confirmaciones.

**Estado local posterior:** contencion implementada. El riesgo residual es que el
snapshot sigue siendo un corte local, con rango manual y sin receipt externo.

### GOV-06 - La pestaña de transparencia no habla el contrato del backend

El frontend crea en `/{id}/snapshots` con `{rango}`, publica en
`/{id}/snapshots/{snapshotId}/publicar` y verifica con POST. El backend expone
`/snapshot` singular con `desde/hasta`, `/publish/{snapshot_id}` y GET
`/{snapshot_id}/verify?respuesta_id=...`. El tipo frontend espera
`etiqueta/creado_at/publicado_at/resumen`; el backend entrega
`root_hash/total_respuestas/desde_at/hasta_at/anchor_status`.

La UI afirma "estado verificable" aunque el contrato esta roto y el backend solo
genera una referencia simulada. Debe ocultarse o marcarse experimental hasta que
el contrato y la semantica sean verdaderos.

**Estado local posterior:** rutas, payloads, tipos y UI alineados; la accion se
presenta como simulacion/referencia local y nunca como publicacion verificada.

### GOV-07 - El modo `source_anonymous` no esta completo de punta a punta

El backend implementa consentimiento, version de politica, HMAC dedicado,
minimizacion y retencion. El `PublicResponsePayload` frontend no incluye
`privacy_consent` ni `privacy_policy_version`, y `SurveyForm` no presenta ni envia
esos campos. Una encuesta que requiera consentimiento puede fallar aunque el
backend este bien configurado.

Ademas, el mapa publico redondea centroides pero no suprime celdas con conteo bajo;
una celda de una persona puede seguir siendo identificable. El modo anonimo de
origen tampoco es secreto de voto: la huella pseudonima queda junto al ballot.

### GOV-08 - Los datos demo no pueden compartir el tally oficial

La publicacion puede disparar `auto_seed_demo` si el runtime lo habilita. Para una
votacion gobernada, los datos sinteticos deben vivir en otra encuesta/namespace o
estar marcados de forma no removible y excluidos por construccion del universo
oficial, receipts, quorum, snapshots y exports.

## Contrato objetivo

### Modos de assurance explicitos

1. `survey`: opinion abierta; unicidad opcional; nunca se presenta como representativa.
2. `controlled_consultation`: identidad autenticada/OTP y una participacion por sujeto.
3. `governed_vote`: release aprobada, elegibilidad, credencial de un uso, ballot
   pseudonimo, cierre y resultado firmado.
4. `official_election`: fuera de alcance hasta auditoria externa, requisitos legales
   y certificacion de infraestructura.

El modo define validaciones fail-closed. No debe ser solo una etiqueta visual.

### State machine propuesto

```text
draft -> in_review -> approved -> scheduled -> published -> closing -> closed -> archived
           |              |                       |
           +-> rejected --+                       +-> suspended
```

- `draft`: editable; cada guardado incrementa revision.
- `in_review`: congelado para revision; cambios vuelven a `draft`.
- `approved`: contiene digest canonico y aprobador. No se edita.
- `scheduled/published`: responde una release exacta; no el registro mutable.
- `closing`: bloquea nuevos ingresos y resuelve receipts ya aceptados por el gate.
- `closed`: manifest y resultado final persistidos, firmados e idempotentes.
- `suspended`: pausa excepcional auditada; no equivale a cerrar ni borra votos.

Transiciones ilegales deben fallar con `409`, `reason_code` estable y sin cambios
parciales. En modo `governed_vote`, `created_by != approved_by`; la politica puede
exigir ademas `approved_by != published_by` y doble aprobacion.

### Entidades minimas

#### `SurveyRelease`

- `tenant_id`, `survey_id`, `release_no`, `status`.
- JSON canonico del instrumento y configuracion completa.
- `instrument_digest`, algoritmo y `canonicalization_version`.
- Snapshot de privacidad, elegibilidad, quorum y visibilidad de resultados.
- `created_by`, `submitted_by`, `approved_by`, `published_by` y timestamps.
- Unique `(survey_id, release_no)` y digest inmutable despues de review.

`EncRespuesta` y `SurveyResponseReceipt` deben referenciar `release_id`; el payload
hash incluye el digest de release.

#### `SurveyEligibilitySnapshot` y credenciales

- Fuente/regla versionada, cantidad elegible y digest del universo.
- Identificador canonico protegido por HMAC con `key_version`; nunca DNI plano en el
  ballot.
- Credencial opaca aleatoria de un solo uso, almacenada solo como hash.
- Unique `(release_id, subject_hash)` para emision y `(release_id, credential_hash)`
  para redencion.
- Estado `issued/redeemed/revoked/expired`, motivo y evento de auditoria.

La emision conoce identidad; el ballot store conoce la credencial redimida, pero no
la identidad directa. Acceso a ambos dominios requiere permisos distintos.

#### `SurveyFinalization`

- `release_id`, cutoff, estado, motivo y actor.
- `accepted_count`, `rejected_count`, receipts pendientes y efectos pendientes.
- Regla, numerador, denominador y resultado de quorum.
- Tally canonico por `question_ref/option_ref`, no por texto mutable.
- Digest de release, snapshot de elegibilidad y ballot set.
- Manifest canonico, firma Ed25519/KMS, `key_version` y clave publica verificable.
- Estado de anclaje externo separado: `not_requested/submitted/confirmed/failed`.

### Capabilities

- `survey.draft.write`
- `survey.review`
- `survey.approve`
- `survey.publish`
- `survey.close`
- `survey.results.certify`
- `survey.anchor.publish`
- `survey.export`
- `survey.pii.read`

La ruta siempre valida tenant antes de capability. Los grants vienen del usuario
persistido, no de headers del cliente. Los eventos gobernados fallan cerrados si la
auditoria no se puede persistir.

## Implementacion P0 por etapas

### P0-A - Contencion inmediata

1. Corregir scope de anchor en servicio y rutas.
2. Sustituir el falso estado `published` de `SIM-*` por `simulated/unverified`.
3. Deshabilitar la UI de publicar/verificar snapshots hasta alinear contrato.
4. Retirar `empleado` de publicar/cerrar o exigir capabilities dedicadas.
5. Para `tipo=votacion`, bloquear publicacion con `politica_unicidad=libre`, datos
   demo activos o identidad autodeclarada presentada como verificada.
6. Reparar el journey que importa `_public_response_rate_buckets`; las pruebas no
   deben acoplarse a un simbolo privado retirado.

Gate de salida: tests cross-tenant y de semantica del anchor en verde; ninguna UI
afirma verificacion externa sin receipt real.

### P0-B - Release inmutable y workflow

1. Crear tablas de release y lifecycle events con constraints.
2. Canonicalizar y hashear el instrumento completo.
3. Implementar review/approve/reject/publish con revision esperada e idempotencia.
4. Referenciar release desde receipts y respuestas nuevas.
5. Convertir toda edicion post-publicacion en nueva release/borrador.

Gate de salida: publish-vs-edit concurrente tiene un unico ganador; ningun dato de
una release publicada cambia; cada ballot prueba que instrumento vio.

### P0-C - Elegibilidad, unicidad y anonimato

1. Introducir snapshots de elegibilidad y credenciales de un uso.
2. Agregar verificacion real segun tenant: login, OTP o fuente autoritativa.
3. Resolver aliases antes de emitir credencial.
4. Separar PII/eligibilidad de ballots y limitar capacidades.
5. Mantener Turnstile/rate limit como defensa adicional, nunca como elegibilidad.

Gate de salida: dos requests concurrentes con la misma credencial aceptan un solo
ballot; variar DNI/telefono/cookie/IP no permite otro voto del mismo sujeto.

### P0-D - Cierre, quorum y resultado final

1. Implementar `closing` con cutoff e idempotency key.
2. Reconciliar receipts aceptados, ballots y outbox antes de certificar.
3. Calcular quorum sobre el snapshot versionado de elegibilidad.
4. Persistir y firmar el manifest final.
5. Exponer endpoint final publico/privado segun politica, incluso en `closed`.
6. Aplicar supresion de celdas pequenas (por ejemplo `k >= 5`, configurable) y
   eliminar texto libre/segmentos sensibles de vistas publicas.

Gate de salida: submit-vs-close no pierde ni agrega ballots fuera del corte; el
resultado se reproduce byte a byte desde la release y el ballot set congelado.

### P0-E - Frontend y operacion

1. Pantalla de diff de release, checklist de readiness y aprobacion.
2. Mostrar digest, actores, permisos y estado real del lifecycle.
3. Consentimiento versionado de privacidad de punta a punta.
4. Cierre con doble confirmacion, motivo y preview de quorum.
5. Resultado final y certificado verificable, diferenciando live/final/simulado.
6. Namespace visual y tecnico separado para datos demo.

Gate de salida: tests de contrato y un E2E real recorren draft, review, approval,
publish, credential, vote, close, final result y verify.

## Archivos exactos propuestos

### Backend

Nuevos:

- `services/survey_governance_policy.py`
- `services/survey_release_service.py`
- `services/survey_eligibility_service.py`
- `services/survey_finalization_service.py`
- `routes/v2/survey_governance.py`
- migraciones Alembic separadas para release/lifecycle, eligibility y finalization

A modificar:

- `models.py`
- `services/encuestas_service.py`
- `services/public_survey_intake.py`
- `services/encuestas_analytics_service.py`
- `services/encuestas_anchor_service.py`
- `services/survey_access_policy.py`
- `routes/v2/surveys.py`
- `routes/encuestas_admin.py`
- `routes/encuestas_anchor.py`
- `routes/encuestas_analytics.py`

### Frontend

Nuevos sugeridos:

- `src/components/surveys/SurveyReleaseReview.tsx`
- `src/components/surveys/SurveyPublicationReadiness.tsx`
- `src/components/surveys/SurveyCloseDialog.tsx`
- `src/components/surveys/SurveyFinalResults.tsx`
- `src/components/surveys/SurveyPrivacyConsent.tsx`

A modificar:

- `src/types/encuestas.ts`
- `src/api/encuestas.ts`
- `src/components/surveys/SurveyEditor.tsx`
- `src/components/surveys/SurveyForm.tsx`
- `src/components/surveys/TransparencyTab.tsx`
- `src/pages/admin/encuestas/[id].tsx`
- `src/pages/admin/encuestas/index.tsx`
- `src/pages/e/[slug].tsx`

## Bateria de tests requerida

### Backend unit/integration

- `tests/test_survey_lifecycle_state_machine.py`
  - todas las transiciones validas e invalidas;
  - rechazo vuelve a draft;
  - closed no reabre sin evento excepcional.
- `tests/test_survey_release_immutability.py`
  - canonicalizacion estable;
  - texto, opcion, politica, fechas y privacidad no mutan release;
  - nueva correccion crea release distinta.
- `tests/test_survey_release_rbac.py`
  - tenant antes que capability;
  - empleado sin grant recibe 403;
  - four-eyes y revocacion.
- `tests/test_survey_eligibility_credentials.py`
  - no elegible, expirado, revocado, doble redencion y carrera concurrente.
- `tests/test_survey_ballot_secrecy.py`
  - ballot sin DNI/telefono/user/IP;
  - roles de PII no implican acceso al ballot linkage.
- `tests/test_survey_close_atomicity.py`
  - cierre de draft rechazado;
  - close idempotente;
  - carreras submit/close y replay.
- `tests/test_survey_final_results_contract.py`
  - resultado final disponible en closed;
  - quorum y tally reproducibles;
  - firma y digest fallan si se altera el manifest.
- `tests/test_survey_anchor_tenant_scope.py`
  - publish/verify cross-tenant y mismatch survey/snapshot siempre 404/403.
- `tests/test_survey_anchor_truthfulness.py`
  - `SIM-*` nunca es confirmed/published;
  - receipt, chain y payload deben verificarse.
- `tests/test_survey_public_small_cell_privacy.py`
  - celdas `< k`, segmentos raros y texto libre no salen al publico.
- `tests/test_survey_demo_data_isolation.py`
  - datos sinteticos nunca entran en quorum, tally, finalizacion o export oficial.
- `tests/test_survey_governance_migrations.py`
  - constraints, FKs, uniques, downgrade/upgrade y datos existentes.
- `tests/test_survey_postgres_concurrency.py`
  - publish-vs-edit, approve-vs-edit, redeem-vs-redeem y close-vs-submit en Postgres.

### Frontend

- `src/api/surveyGovernanceApi.test.ts`
- `src/components/surveys/SurveyReleaseReview.test.tsx`
- `src/components/surveys/SurveyPublicationReadiness.test.tsx`
- `src/components/surveys/SurveyFinalResults.test.tsx`
- `src/components/surveys/SurveyPrivacyConsent.test.tsx`
- `src/components/surveys/TransparencyTab.test.tsx`
- E2E: `e2e/survey-governed-vote.spec.ts`

Casos UI obligatorios: permiso faltante, revision obsoleta, politica cambiada,
consentimiento requerido, no elegible, credencial usada, cierre en curso, quorum no
alcanzado, resultado final, anchor simulado y anchor confirmado.

### Pruebas externas antes de declarar produccion

- Migracion y concurrencia contra PostgreSQL administrado real.
- Redis compartido con dos procesos y failover.
- Turnstile valido, invalido, timeout y replay desde el dominio real.
- Proveedor de identidad/OTP y revocacion.
- Firma con KMS/HSM, rotacion de clave y verificacion con clave publica.
- Anclaje externo: submit, confirmaciones, reorg, receipt falso y proveedor caido.
- Restore de backup y retencion/borrado.
- Smoke de navegador sobre build desplegado y dos tenants reales aislados.

## Riesgo residual y posicionamiento

Jelou, respond.io y otras plataformas omnicanal son referencias utiles para
automatizacion conversacional, operacion y UX. La ventaja diferenciadora de Chatboc
no debe basarse en afirmar "votacion segura" antes de tiempo, sino en unir canales,
CRM y participacion con gobernanza demostrable. Despues de P0, el mensaje comercial
puede ser consultas y votaciones internas auditables. La etiqueta electoral o
certificada debe permanecer bloqueada hasta completar pruebas externas y auditoria
independiente.
