# CODEX BACKEND — chatboc.ar

## 1. Rol de este codex

Actuar como guía operativa para transformar el backend actual de chatboc.ar sin romper contratos principales y priorizando:
- seguridad;
- trazabilidad;
- multi-tenancy correcto;
- unificación de IA;
- operabilidad en gobierno/empresa.

## 2. Restricciones no negociables

1. Mantener Flask como framework principal en la primera etapa.
2. No romper `/ask/*`, `/tickets/*`, `/catalogo/*` salvo para agregar headers/campos nuevos compatibles.
3. Todo acceso IA nuevo debe pasar por `services/ai_gateway.py`.
4. Todo tool call debe quedar auditado.
5. Toda consulta RAG debe filtrar por `tenant_id`.
6. Ningún secreto simétrico debe publicarse vía JWKS.
7. `store=false` por defecto en Responses API para canales ciudadanos/enterprise sensibles.
8. Ninguna tool destructiva se ejecuta sin RBAC y policy check.

## 3. Estructura objetivo incremental

```text
backend/
  routes/
    ask_routes.py
    ticket_routes.py
    catalog_routes.py
    auth_routes.py
    ai_routes.py
    rag_routes.py
    moderation_routes.py
    analytics_routes.py
    audit_routes.py
    realtime_routes.py
  services/
    ai_gateway.py
    ai_stream_service.py
    prompt_registry.py
    tool_registry.py
    policy_engine.py
    moderation_service.py
    audit_service.py
    usage_metering_service.py
    rag_ingestion_service.py
    rag_query_service.py
    citation_service.py
    widget_token_service.py
    realtime_session_service.py
    call_qa_service.py
  workers/
    batch_embeddings_worker.py
    reindex_worker.py
    moderation_backfill_worker.py
    analytics_rollup_worker.py
  schemas/
    ai.py
    rag.py
    moderation.py
    analytics.py
  models/
    prompt_version.py
    ai_request_log.py
    ai_tool_call_log.py
    policy_decision_log.py
    source_document.py
    source_chunk.py
    usage_event.py
    realtime_session.py
  tests/
    unit/
    integration/
    contract/
```

## 4. Epic A — identidad, JWT/JWKS y sesiones

### Objetivo
Eliminar el riesgo de falsificación de tokens y ordenar el esquema de identidad panel/widget/chat/voz.

### Tareas
- migrar firma de HS256 a RS256 o ES256;
- exponer sólo la parte pública en `/auth/.well-known/jwks.json`;
- crear tabla `signing_keys` con `kid`, `alg`, `public_jwk`, `private_ref`, `status`, `created_at`, `expires_at`;
- implementar rotación segura;
- separar claims por tipo de token.

### Token types
- `panel_access`
- `panel_refresh`
- `widget_access`
- `chat_access`
- `realtime_client_session`
- `service_internal`

### Claims mínimas
```json
{
  "iss": "chatboc.ar",
  "aud": "chatboc-widget",
  "sub": "user_or_anon_id",
  "tenant_id": "uuid",
  "scope": ["chat:read", "chat:write"],
  "channel": "widget",
  "origin": "https://cliente.gob.ar",
  "session_id": "uuid",
  "jti": "uuid",
  "iat": 0,
  "nbf": 0,
  "exp": 0,
  "kid": "key-id"
}
```

### Endpoints
- `GET /auth/.well-known/jwks.json`
- `POST /auth/keys/rotate` (superadmin)
- `POST /auth/widget/bootstrap`
- `POST /auth/widget/refresh`
- `POST /auth/realtime/client-secret`
- `POST /auth/revoke`

### Reglas
- widget access TTL corto: 5–15 min;
- refresh token separado, rotativo;
- cookies separadas panel/widget;
- allowlist de origin por tenant;
- rate limit por IP + `anon_id` + `tenant_id`.

## 5. Epic B — AI Gateway unificado sobre Responses API

### Objetivo
Eliminar llamadas ad-hoc a OpenAI dispersas por el código.

### Archivo central
`services/ai_gateway.py`

### Interface objetivo
```python
class AIRequest(BaseModel):
    tenant_id: str
    channel: Literal["widget", "panel", "voice", "worker"]
    task: str
    user_id: str | None = None
    anon_id: str | None = None
    session_id: str | None = None
    model: str
    instructions_key: str
    prompt_variables: dict
    input_items: list
    tools: list = []
    response_schema: dict | None = None
    stream: bool = False
    background: bool = False
    max_output_tokens: int | None = None
    metadata: dict = {}

class AIResponse(BaseModel):
    text: str | None
    json: dict | None
    citations: list
    tool_calls: list
    usage: dict
    raw_response_id: str | None
    finish_reason: str | None
```

### Modos soportados
- sync
- semantic streaming
- background job
- structured output JSON
- tool loop
- multimodal image input

### Reglas de integración
- usar `responses.create` como default;
- `store=false` por defecto;
- usar structured outputs cuando el caller espere JSON;
- no parsear JSON “a mano”;
- registrar latencia, tokens, cache hit, modelo y tool calls.

### Adaptadores iniciales
- `/ask`
- `/ask/municipio`
- `/ask/pyme`
- clasificación de tickets
- análisis de imagen
- generación de resúmenes
- respuestas sugeridas a operadores

## 6. Epic C — Prompt registry y versionado

### Objetivo
Sacar prompts hardcodeados del flujo de negocio.

### Componentes
- tabla `prompt_versions`
- service `prompt_registry.py`
- fallback a prompts en código sólo durante transición

### Modelo mínimo
- `prompt_key`
- `version`
- `channel`
- `tenant_override`
- `model_default`
- `instructions`
- `tool_profile`
- `response_schema`
- `status` (`draft`, `active`, `deprecated`)
- `created_by`

### Reglas
- cada task del gateway referencia un `instructions_key`;
- versionado explícito;
- rollback simple;
- exportable a Git para trazabilidad;
- opcional: mapear a prompt objects de OpenAI en entornos no sensibles.

## 7. Epic D — Tool registry y MCP proxy

### Objetivo
Convertir acciones del agente en funciones verificables, auditables y seguras.

### Archivo central
`services/tool_registry.py`

### Tipos de herramientas
1. functions locales:
   - crear ticket
   - actualizar estado
   - buscar catálogo
   - consultar mapa/geo
   - transferir a humano
2. conectores internos:
   - ERP
   - CRM
   - GIS
   - expediente
3. MCP remoto detrás de proxy propio

### Contrato mínimo
```python
class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict
    scopes: list[str]
    risk_level: Literal["low", "medium", "high"]
    channel_allowlist: list[str]
    tenant_allowlist: list[str] | None
    requires_human_approval: bool = False
```

### Policy check previo a ejecutar
- RBAC por rol y tenant;
- canal permitido;
- política del tenant;
- validación estricta contra schema;
- idempotencia donde aplique.

### Decisión fuerte
No exponer “web search”, “computer use” o MCPs arbitrarios directo al ciudadano final.  
Todo eso pasa por:
- proxy;
- allowlist;
- auditoría;
- aprobación humana cuando el riesgo sea medio/alto.

## 8. Epic E — Moderación y policy gates

### Objetivo
Tener controles antes y después del modelo.

### Flujo
1. moderar input;
2. aplicar reglas tenant/canal;
3. consultar modelo;
4. moderar output;
5. decidir `allow`, `redact`, `block`, `handoff`, `warn`.

### Casos
- insultos/abuso
- autolesión
- doxxing / datos sensibles
- instrucciones ilegales
- spam/flood
- imágenes sensibles

### Endpoints
- `GET /api/policies`
- `POST /api/policies`
- `PUT /api/policies/{id}`
- `GET /api/moderation/events`
- `POST /api/moderation/review/{id}`

### Storage
- `policy_sets`
- `policy_rules`
- `moderation_events`

### Reglas
- input y output moderados por canal;
- voz: moderar transcripciones y también mensajes textuales intermedios;
- para streaming: bufferizar hasta umbral seguro antes de mostrar al usuario final.

## 9. Epic F — Auditoría y explainability operativa

### Objetivo
Reconstruir cualquier decisión del sistema.

### Tablas
- `ai_request_logs`
- `ai_response_logs`
- `ai_tool_call_logs`
- `policy_decision_logs`
- `rag_retrieval_logs`
- `user_feedback_logs`

### Hash chain
Cada fila crítica:
- `prev_hash`
- `hash`

### Campos obligatorios
- `tenant_id`
- `channel`
- `actor_type` (`user`, `anon`, `agent`, `system`)
- `actor_id`
- `request_id`
- `session_id`
- `task`
- `prompt_key`
- `prompt_version`
- `model`
- `tool_name`
- `decision`
- `token_usage`
- `latency_ms`

### Endpoints
- `GET /api/audit/logs`
- `GET /api/audit/logs/{request_id}`
- `POST /api/audit/export`

## 10. Epic G — Streaming semántico

### Objetivo
Dejar de hacer polling o pseudo-streaming rudimentario.

### Transporte
SSE desde backend Flask:
- `GET /api/ai/stream?request_id=...`
o
- `POST /api/ai/respond?stream=true`

### Eventos frontend
- `response.started`
- `response.delta`
- `response.reasoning_summary`
- `tool.started`
- `tool.completed`
- `citation.added`
- `moderation.warning`
- `response.completed`
- `response.error`
- `usage.reported`

### Reglas
- incluir `correlation_id`;
- heartbeats cada N segundos;
- cierre limpio;
- reintento idempotente;
- persistencia parcial de chunks sólo si el tenant lo permite.

## 11. Epic H — RAG unificado multi-fuente

### Objetivo
Extender el catálogo actual a una plataforma de conocimiento única.

### Fuentes soportadas
- catálogo pyme
- normativa municipal
- FAQs
- procedimientos
- expedientes
- adjuntos de tickets
- páginas internas curadas

### Nuevas entidades
- `source_documents`
- `source_chunks`
- `ingestion_jobs`
- `retrieval_profiles`

### Metadata por chunk
- `tenant_id`
- `source_type`
- `source_id`
- `document_id`
- `chunk_id`
- `title`
- `url_or_path`
- `access_level`
- `language`
- `version`
- `embedding_model`
- `hash`

### Query pipeline
1. normalizar consulta;
2. clasificar intención;
3. retrieval profile;
4. hybrid retrieval;
5. reranking opcional;
6. respuesta con citas internas;
7. log completo.

### Endpoints
- `POST /api/rag/ingest`
- `POST /api/rag/reindex`
- `POST /api/rag/query`
- `GET /api/rag/sources`
- `GET /api/rag/sources/{id}`
- `GET /api/rag/chunks/{chunk_id}`

### Reglas
- filtros por tenant obligatorios;
- filtros por ACL cuando el usuario es autenticado;
- chunking versionado;
- embeddings batch para grandes cargas;
- sin mezclar colecciones cross-tenant.

## 12. Epic I — Multimodal imagen→ticket

### Objetivo
Crear tickets a partir de fotos con validación estructurada.

### Endpoint
- `POST /tickets/{tipo}/crear_desde_imagen`

### Salida JSON requerida
```json
{
  "category": "bache",
  "subcategory": "calzada",
  "priority": "media",
  "confidence": 0.86,
  "summary": "Bache visible en calle asfaltada",
  "suggested_location_text": "Av. ... y ...",
  "evidence_tags": ["calle", "asfalto", "bache"],
  "needs_human_review": false
}
```

### Flujo
- subir imagen a storage;
- moderar imagen;
- analizar con modelo multimodal;
- devolver borrador editable;
- recién después crear ticket real.

### Regla crítica
No auto-crear ticket si:
- confianza < umbral;
- hay PII visual sensible;
- la clasificación es ambigua.

## 13. Epic J — Voz: telco + browser

### Objetivo
Unificar telefonía actual y nueva voz web.

### Subflujos
#### J1. PSTN / Twilio
Mantener puente actual:
- Twilio inbound
- media stream
- backend WS → OpenAI Realtime WS
- function calling
- handoff humano

#### J2. Browser voice
Agregar:
- `POST /api/realtime/session`
- `POST /api/realtime/call`
- WebRTC session bootstrap
- policy checks previos
- RTC token/session TTL corto

#### J3. QA de llamadas
Post llamada:
- transcripción completa;
- diarización si el caso lo justifica;
- score de calidad;
- resumen;
- next actions.

### Tablas
- `voice_calls`
- `voice_turns`
- `voice_handoffs`
- `call_quality_scores`

### Reglas
- guardar transcript sólo si la política del tenant lo permite;
- etiquetar consentimiento;
- política distinta para ciudadano vs operador;
- fallback a STT/TTS textual si falla speech-to-speech.

## 14. Epic K — Analytics y costos

### Objetivo
Visibilidad operativa y financiera real.

### KPIs
- conversaciones por canal
- tickets creados por IA
- FRT
- ART
- SLA breach
- deflection rate
- CSAT
- costo por conversación
- costo por ticket
- costo por modelo
- costo por tenant
- ratio tool success/failure
- ratio retrieval hit/no-hit

### Endpoints
- `GET /api/analytics/kpis`
- `GET /api/analytics/costs`
- `GET /api/analytics/models`
- `GET /api/analytics/voice`
- `GET /api/analytics/rag`

## 15. Epic L — Jobs batch y background

### Objetivo
Aprovechar asíncrono donde tiene sentido.

### Background mode
Usar para:
- reportes extensos;
- resúmenes administrativos largos;
- análisis complejos no interactivos.

No usar para:
- flujos ciudadanos sensibles con requerimiento de no retención.

### Batch API
Usar para:
- embeddings masivos;
- reclasificación nocturna;
- moderación backfill;
- evaluaciones masivas;
- enriquecimiento de catálogos.

### Workers
- `batch_embeddings_worker`
- `nightly_eval_worker`
- `catalog_cleanup_worker`
- `audit_rollup_worker`

## 16. Epic M — Evals en CI/CD

### Objetivo
Liberar cambios de IA con control.

### Suites mínimas
- `municipio-faq`
- `municipio-ticket-triage`
- `pyme-catalog-search`
- `voice-handoff`
- `policy-safety`
- `multimodal-ticket`

### Gate
No mergear si:
- cae score global;
- suben refusals incorrectos;
- suben hallucinations en RAG;
- empeora costo/latencia fuera de umbral.

## 17. PR plan sugerido

### PR-01
Auth/JWKS/rotación + tests de token

### PR-02
`ai_gateway.py` + adapter Responses sync

### PR-03
Streaming SSE + contrato de eventos

### PR-04
Moderation service + policy engine

### PR-05
Audit logs + usage metering

### PR-06
Prompt registry + prompt migration

### PR-07
RAG unified ingest/query + citations

### PR-08
Image to ticket

### PR-09
Realtime browser sessions

### PR-10
Analytics + costs + dashboards API

### PR-11
Batch/background workers

### PR-12
Eval suite + release gate

## 18. Criterios de aceptación backend

- todos los endpoints legacy siguen respondiendo;
- Responses API está detrás del gateway;
- JWT widget/panel ya no depende de secreto compartido expuesto;
- toda respuesta IA genera audit trail;
- toda consulta RAG devuelve `citations[]`;
- streaming semántico emite eventos consistentes;
- voz browser se inicia con sesión controlada;
- costos por tenant/modelo quedan visibles;
- existe suite de evals mínima.

## 19. Qué no hacer en el backend

- no parsear JSON del modelo con regex o `json.loads` sobre texto libre;
- no ejecutar tool calls si el schema no valida;
- no mezclar tenant context en caché;
- no usar memoria de OpenAI como fuente principal de historial sensible;
- no dejar prompts críticos hardcodeados en rutas;
- no abrir MCP externos sin proxy/allowlist;
- no usar computer use para acciones críticas autenticadas.