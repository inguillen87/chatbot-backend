# Backend docs implementation status

Fecha: 2026-05-01

Este archivo resume la primera tanda ejecutada desde los MD de `/docs` para que backend y frontend se sincronicen sin adivinar contratos.

## Listo para frontend

### Educación

Contratos alineados con `02_BACKEND_VERTICAL_EDUCACION_CODEX.md`:

- `POST /api/v1/education/guardians/lookup`
- `POST /api/v1/education/guardians/verify`
- `POST /api/v1/education/guardians/link-student`
- `GET /api/v1/education/me/family-context`
- `GET /api/v1/education/schools/{school_id}`
- `GET /api/v1/education/schools/{school_id}/campuses`
- `GET /api/v1/education/schools/{school_id}/sections`
- `GET /api/v1/education/cases/{id}`
- `POST /api/v1/education/cases/{id}/reply`
- `POST /api/v1/education/cases/{id}/assign`
- `POST /api/v1/education/cases/{id}/escalate`

Las rutas legacy singulares (`/guardian/lookup`, `/guardian/verify`, `/family/context`) siguen funcionando.

### Educacion + WhatsApp colegios 2026-05-02

Mejora aditiva sobre demo, widget, panel y WhatsApp:

- `services/education_contracts.py` centraliza `education.profile.v1`, `education.quick_menu.v1`, `education.admin_menu.v1`, `education.whatsapp_playbook.v1` y `education.case_intake.v1`.
- `GET /api/v2/demo/catalog` agrega sector `educacion` sin remover `gobierno` ni `empresas`.
- `POST /api/v2/demo/session` acepta `sector: "educacion"` y devuelve `workspace.education`, `experience_blueprint.experience_type: "education"` y `chat_bootstrap.payload.vertical: "educacion"`.
- `GET /api/public/widget-config` y `/api/public/tenants/{slug}/widget-config` exponen `quick_menu` top-level, `education`, `builder_config.education` y tenant `vertical/subvertical`.
- `GET /api/v1/education/admin/menu` devuelve secciones del panel tenant para colegios.
- `GET /api/v1/education/whatsapp/playbook` devuelve menu, starters, media intelligence, routing rules y safety para WhatsApp escolar.
- `GET /api/v1/education/operations/summary` devuelve `education.operations_summary.v1` con KPIs escolares, breakdown por tipo/canal/estado/colegio y acciones recomendadas.
- `GET /api/v1/education/operations/heatmap` devuelve `education.operations_heatmap.v1` con puntos/celdas/hotspots de casos escolares geolocalizados.
- `GET /api/v1/education/cases` conserva array legacy por defecto y agrega filtros (`case_type`, `channel`, `sensitivity_level`, `status`, `assignee_id`, `unassigned`, alumno/familia/sede/curso). Con `envelope=1` devuelve `education.cases.list.v1`.
- `GET /api/v1/education/tenant/capabilities` suma `education_profile`, `admin_menu` y `whatsapp_playbook`.
- WhatsApp detecta tenants educativos, monta menu escolar de bienvenida, guarda `education_context`, pasa contexto al LLM, crea tickets escolares desde menu + detalle/media usando el servicio de tickets actual y los vincula a `SchoolCaseAlias` cuando puede resolver colegio/familia.
- `services/chatbot_prompts.py` suma reglas para que `/ask/pyme` actue como asistente escolar cuando llega `education_context`, sin abrir un flujo paralelo.
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_EDUCATION_WHATSAPP_2026-05-02.md`.

### Auth/widget

Contratos alineados con `01_chatboc_backend_codex.md`, `widget_integration_plan.md` y handoffs stage 4:

- `GET /auth/.well-known/jwks.json`
- `GET /auth/widget/jwks.json`
- `GET|POST /auth/widget/bootstrap`
- `POST /auth/widget/token`
- `POST /auth/widget-token`
- `POST /auth/widget/refresh`
- `POST /auth/widget-refresh`

Todos conservan `contract_version` donde ya existía contrato de widget.

### PWA/widget marketplace

Contratos alineados con `pwa_widget_review.md`:

- Si el tenant/slug explícito no resuelve, `/api/pwa/public/*` devuelve JSON accionable con:
  - `contract_version: pwa.public_tenant_resolution.v1`
  - `reason_code: tenant_resolution_failed`
  - hints de query params y headers aceptados
- El resumen de carrito ya no marca `mercadopago_ready=true` por defecto: depende de `tenant.configuracion.mercadopago_access_token`.
- `checkout_options` agrega:
  - `payment_required`
  - `requires_contact_or_auth`
  - `gateway_hint`
- `checkout_preview` agrega:
  - `payment_ready`
  - `contact_ready`

### Encuestas públicas

Contratos alineados con `FRONTEND_UXUI_ENCUESTAS_TASKS.md`:

- Errores públicos normalizados con:
  - `status_code`
  - `reason_code`
  - `retryable`
  - `action_hint`
  - `request_id`
- Se expone `X-Request-Id` en respuestas de error públicas.
- Respuestas exitosas de voto incluyen `request_id`.

## Verificación ejecutada

Se verificó sintaxis y tests focalizados con `venv\Scripts\pythonw.exe`:

- `tests.test_education_routes`
- `tests.test_widget_bootstrap`
- `tests.test_widget_token_endpoint`
- `tests.test_pwa_public_cart_url`
- `tests.test_pwa_public_catalog`
- `tests.test_public_encuestas_endpoint`

Nota: en esta máquina no hay `python.exe` ni `git` disponibles en PATH; se usó `pythonw.exe` del venv para compilar y correr tests por código de salida.
## Sync frontend 2026-05-01

Contratos implementados desde `FRONTEND_TO_BACKEND_SYNC_2026-05-01.md`:

- `POST /api/v2/demo/session` agrega `workspace.title`, `workspace.welcome_message`, `workspace.quick_replies`, `workspace.value_cards`, `workspace.handoff_labels` y `request_id`.
- `GET /api/v2/demo/catalog` devuelve `contract_version: demo.catalog.v2`, `sectors: ["gobierno", "empresas"]`, `rubros` y `sector_groups`.
- `GET /api/public/tenants/{slug}/widget-config` agrega `contract_version: public.widget_config.v1`, `tenant`, `widget`, `quick_menu`, `suppress_global_widget` e `integration_preview`, conservando campos legacy.
- `GET /api/v2/tickets` devuelve `contract_version: tickets.v2.list`, `{ items, pagination, summary, request_id }`; cada item agrega `sla_status`, `sla_state`, `assignee` y `assignee_name`.
- `GET /api/v2/analytics/overview` devuelve `contract_version: analytics.overview.v2` y `summary` estable con `conversations`, `open_tickets`, `overdue_tickets`, `response_time`, `survey_responses`, `nps`, `csat` y `handoff_rate`.
- `POST /api/v2/surveys/draft` acepta drafts incompletos y responde `contract_version: surveys.draft.v2`, `draft_id`, `status: draft`, `idempotency_key` y `request_id`.
- `GET /api/pwa/public/tenant-info` queda como endpoint canonico para resolver tenant del PWA/widget; success usa `contract_version: public.tenant_profile.v1`; los errores 404 incluyen `status_code`, `reason_code`, `retryable`, `action_hint`, `request_id` y hints de query params/headers.
- `POST /api/surveys/sync` responde `contract_version: surveys.sync.v1`, `synced`, `idempotency_key` y `request_id`.
- `POST /api/tickets/draft/sync` responde `contract_version: tickets.draft_sync.v1`, `ticket_id`, `idempotency_key` y `request_id`.

Pendientes honestos de siguiente ola:

- Inbox omnicanal premium con presencia real y eventos live socket.
- Webhooks v2 para recibir eventos de Mercado Pago sin pasar por rutas legacy.
- Puntos/recompensas con catalogo administrable completo y reglas por segmento.
- Quick menu educativo avanzado por permisos/rol fino; base colegios ya sale desde backend.

## Realtime voice / llamadas WhatsApp 2026-05-07

Mejora aditiva sobre la base existente de Twilio Voice/Media Streams y OpenAI Realtime:

- Default realtime actualizado a `gpt-realtime`; modelo configurable por env/tenant.
- `services/realtime_voice_profiles.py` centraliza contrato `realtime.voice_capabilities.v1`, perfiles por vertical, tools e instrucciones de voz.
- `GET /api/public/realtime/voice-capabilities` expone capacidades de llamadas para landing/widget/demo.
- `GET /api/public/widget-config` agrega `realtime_voice` y atributos `data-realtime-model`, `data-realtime-fallback-model`, `data-realtime-voice`, `data-realtime-transport` y `data-realtime-profile`.
- `/twilio/voice/stream` mantiene el stream actual pero selecciona tools por vertical: municipio, pyme o colegio.
- Colegios pueden crear `crear_caso_escolar` por llamada y vincularlo a `SchoolCaseAlias` cuando hay contexto escolar resoluble.
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_REALTIME_VOICE_2026-05-07.md`.
- Delivery hooks reales para notifications segun proveedor.

Verificacion sync ejecutada:

- `tests.test_api_v2_foundation`
- `tests.test_v2_tickets`
- `tests.test_v2_analytics_overview`
- `tests.test_v2_surveys`
- `tests.test_pwa_public_cart_url`
- `tests.test_offline_sync_contracts`

## SaaS P1 backend 2026-05-01

Contratos nuevos para destrabar secciones enterprise:

- `GET /api/v2/employee-coverage` y `GET /api/v2/tenants/{slug}/employee-coverage` devuelven `contract_version: employee.coverage.v1`, empleados, scopes, workload, coverage por categoria/zona/canal y alertas.
- `GET /api/v2/tenant-health` y `GET /api/v2/tenants/{slug}/health` devuelven `contract_version: tenant.health.v1`, health score, integraciones, colas, errores recientes, metricas y acciones recomendadas.
- `GET /api/v2/superadmin/executive-summary` devuelve `contract_version: superadmin.executive_summary.v1`, KPIs multi-tenant, health por tenant, top risky tenants y acciones recomendadas.
- `GET|POST /api/v2/notifications/hooks` devuelve/actualiza `contract_version: notifications.hooks.v1`, preferencias, triggers, delivery config, templates y delivery status.
- `GET /api/v2/notifications/delivery-status` devuelve `contract_version: notifications.delivery_status.v1`.
- `GET /api/v2/inbox/omnichannel` devuelve `contract_version: inbox.omnichannel.v1`, lista omnicanal, timeline, presencia basica y acciones.

Verificacion SaaS P1 ejecutada:

- `tests.test_v2_saas_contracts`

## Tenant admin profile + superadmin command center 2026-05-12

Mejora aditiva sobre SaaS P1/P2 para que cada PyME, colegio, municipio o rama de gobierno tenga un perfil operativo completo sin crear una app paralela:

- `GET /api/v2/tenant/admin-experience` y `GET /api/v2/tenants/{slug}/admin-experience` devuelven `contract_version: tenant.admin_experience.v1` con perfil, readiness, health, operaciones, freshness, leads/tickets, encuestas/votaciones, marketplace, modulos y seccion educativa cuando aplica.
- `tenant.readiness.v1` consolida checks de perfil, branding, widget, WhatsApp, equipo, catalogo, encuestas y SLA.
- `tenant.marketplace_ops.v1` expone conteos de productos, cobertura de imagenes, pedidos y capacidades de bulk import/imagenes/PDF catalog.
- `tenant.surveys_ops.v1` resume encuestas, votaciones live y respuestas para el panel tenant.
- `tenant.lead_capture.v1` unifica tickets/leads recientes de `TenantTicket`, `MunicipioTicket` y `PymeTicket`, con endpoints para inbox y leads legacy.
- `GET /api/v2/superadmin/command-center` devuelve `contract_version: superadmin.command_center.v1` con KPIs multi-tenant, ranking de riesgo, readiness por tenant, lead capture y contrato para crear tenants via `/api/admin/tenants`.
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_TENANT_ADMIN_PROFILE_2026-05-12.md`.
- QA de contrato 2026-05-12: `modules[]` siempre trae `secondary_endpoints/widgets`, `lead_capture.items[]` trae `ticket_id/intent/next_action`, `marketplace.summary` trae aliases de imagenes y `bulk_import_status`, `operations.freshness.summary.can_render_heatmap` es booleano, `education.admin_menu.panel_sections[]` trae `endpoint/secondary_endpoints/widgets` y `superadmin.command_center.tenants.*` trae aliases top-level `tenant_slug/display_name/health_score/status/risk_reason`.

Verificacion ejecutada:

- `tests/test_v2_saas_contracts.py` (`11 passed` tras QA tenant admin/superadmin).
- Suite ampliada con API v2 foundation, operational analytics, SaaS, realtime voice, public resolver, tracking, catalog quality y widget settings (`57 passed`).

## WhatsApp operations hub 2026-05-12

Mejora aditiva para que WhatsApp quede conectado con panel tenant, demo, widget, encuestas, noticias/eventos, promociones, catalogos, URLs y tracking de reclamos/pedidos:

- `services/whatsapp_experience.py` centraliza `whatsapp.experience.v1` sin duplicar los flujos existentes.
- `GET /api/v2/whatsapp/experience` y `GET /api/v2/tenants/{tenant_slug}/whatsapp/experience` devuelven estado del canal, reglas enterprise, ventana 24h, inteligencia conversacional, modulos de contenido, tracking y endpoints del panel admin.
- `GET /api/v2/tenant/admin-experience` agrega resumen `whatsapp` y el modulo `widget_whatsapp` apunta al nuevo endpoint operativo.
- `conversation_intelligence.inputs` declara soporte para texto, emojis, ubicacion, imagenes, notas de voz, archivos/PDF y video como adjunto.
- `conversation_intelligence.voice_calls` usa `realtime.voice_capabilities.v1` con `gpt-realtime` por defecto, WebRTC para browser, WebSocket server-side y puente Twilio/SIP para telefono.
- `tracking.courier_style_map` define contrato para mapa/timeline tipo courier con `pulse_current_step`, `route_progress` y `status_transition`, degradando a timeline si no hay coordenadas.
- `GET /api/public/tracking/experience` y `GET /tracking/api/experience` devuelven `tracking.experience.v1` para reclamos (`kind=claim&code=M-...&pin=...`) y pedidos (`kind=order&code=...`) con estado, hitos, timeline, mapa, acciones y contrato frontend.
- `content_modules` expone calidad de catalogo/imagenes, encuestas/votaciones, noticias/eventos, promociones y links configurables por tenant.
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_WHATSAPP_OPERATIONS_2026-05-12.md`.
- QA frontend 2026-05-12: se confirmaron alias tenant-aware, `request_id`/`X-Request-Id`, modulo `widget_whatsapp` con endpoint canonico, video con `analysis_ready:false`, voz gated por `voice_calls.enabled + native_speech_to_speech`, tracking publico JSON y fallback `timeline_only`.
- QA boton de prueba 2026-05-12: cuando el canal tiene numero configurado, `whatsapp.experience.v1.channel` publica `test_endpoint`, `test_method` y `test_label` apuntando al endpoint legacy real `POST /api/notifications/whatsapp/test`; si el canal no esta listo no se publica el boton.

Verificacion ejecutada:

- `tests/test_v2_saas_contracts.py` (`9 passed`).
- `tests/test_tracking_experience_contract.py` (`3 passed`).
- Suite ampliada con API v2 foundation, operational analytics, SaaS, realtime voice, public resolver y tracking experience (`42 passed`).

## Employee routing matrix 2026-05-12

Mejora aditiva para que el panel tenant pueda asignar reclamos/tickets/pedidos operativos por empleado, categoria, zona, canal y carga:

- `services/employee_routing.py` centraliza `employee.routing.v1` usando `User.accesibilidad.employee_scope`, `TenantTicket`, `MunicipioTicket` y `PymeTicket`.
- `GET /api/v2/employee-routing` y `GET /api/v2/tenants/{tenant_slug}/employee-routing` devuelven dimensiones, empleados, workload, cola sin asignar y recomendaciones con score/razones.
- `PATCH /api/v2/employees/{employee_id}/routing-scope` actualiza categorias, zonas, canales y permisos del empleado sin crear tablas nuevas; tambien sincroniza `ticket_categorias` legacy.
- `POST /api/v2/employee-routing/auto-assign` permite preview (`dry_run: true`) o aplicar asignacion (`dry_run: false`) sobre `TenantTicket`, `MunicipioTicket` y `PymeTicket`.
- `GET /api/v2/tenant/admin-experience` agrega resumen `employee_routing` y el modulo `employees` apunta tambien a `/api/v2/employee-routing`.
- Se actualizo handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_TENANT_ADMIN_PROFILE_2026-05-12.md`.

Verificacion ejecutada:

- `tests/test_v2_saas_contracts.py` (`10 passed`).
- Suite ampliada con API v2 foundation, operational analytics, SaaS, realtime voice, public resolver, tracking experience y employee routing (`43 passed`).

## Marketplace catalog quality 2026-05-12

Mejora aditiva sobre marketplace/catalogo existente para que PyMEs, colegios y municipios puedan vender/mejorar catalogos importados sin crear un CRUD paralelo:

- `services/catalog_quality.py` centraliza `catalog.quality.v1` y conserva la funcion legacy `evaluate_catalog_quality` usada por document intelligence.
- `GET /api/v2/catalog/quality` y `GET /api/v2/tenants/{tenant_slug}/catalog/quality` devuelven resumen de productos, ready-to-sell, productos sin imagen/precio/stock/descripcion, imports recientes, capabilities y contrato frontend.
- `tenant.marketplace_ops.v1` ahora incluye `quality.summary`, colas de calidad y endpoint `/api/v2/catalog/quality`.
- `GET /api/v2/tenant/admin-experience` mantiene el modulo `marketplace`, ahora con widget `catalog_quality`.
- `PATCH /api/admin/tenants/{slug}/catalog/items/{item_id}` acepta `imagen_url`, `image_url`, `gallery_urls`, `imagenes`, `images`, `promocion_info`, `external_url` y `checkout_type`, ademas de los campos previos.
- El importador legacy `/api/admin/catalogo/importar` y el flujo nuevo `/api/admin/catalog/import` siguen usando deteccion de imagenes por columnas (`imagen_url`, `image_url`, `foto`, `gallery_urls`, `imagenes`, etc.).
- Se actualizo handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_TENANT_ADMIN_PROFILE_2026-05-12.md`.

Verificacion ejecutada:

- `tests/test_catalog_quality.py`
- `tests/test_v2_saas_contracts.py`

## SaaS P2 commerce y operaciones 2026-05-01

Contratos nuevos para checkout, rewards e inbox accionable:

- Nota de arquitectura: las rutas v2 de commerce son fachadas HTTP; la logica compartida vive en `services/commerce_contracts.py` y `services/rewards.py` para evitar duplicar una app paralela al checkout/rewards existente.
- `GET /api/v2/payments/checkout-status`, `GET /api/v2/payments/capabilities` y `GET /api/v2/tenants/{slug}/payments/checkout-status` devuelven `contract_version: payments.checkout_status.v1`, tenant, gateway, `payment_ready`, `mercadopago_ready`, faltantes, capabilities y URLs de checkout.
- `POST /api/v2/payments/checkout-preview` y `POST /api/v2/tenants/{slug}/payments/checkout-preview` devuelven `contract_version: payments.checkout_preview.v1`, totales normalizados, `payment_required`, `payment_ready`, `contact_ready`, `checkout_options`, next steps e idempotency key.
- `POST /api/v2/payments/checkout-session`, `POST /api/v2/payments/preference` y `POST /api/v2/tenants/{slug}/payments/checkout-session` crean preference real de Mercado Pago con token por tenant y devuelven `contract_version: payments.checkout_session.v1`, `preference_id`, `init_point`, `external_reference`, `checkout_options` y `request_id`.
- `GET|POST /api/v2/payments/status` y `GET|POST /api/v2/tenants/{slug}/payments/status` devuelven `contract_version: payments.status.v1` buscando por `pedido_id`, `market_order_id`, `preference_id` o `external_reference`.
- `GET /api/v2/rewards/profile` y `GET /api/v2/tenants/{slug}/rewards/profile` devuelven `contract_version: rewards.profile.v1`, wallet real del usuario, reglas del tenant, beneficios canjeables e historial.
- `POST /api/v2/rewards/redeem` y `POST /api/v2/tenants/{slug}/rewards/redeem` devuelven `contract_version: rewards.redeem.v1`, canje real de puntos, `redemption_id`, balance actualizado e idempotencia por `Idempotency-Key`.
- `POST /api/v2/inbox/omnichannel/{ticket_id}/actions` y `POST /api/v2/inbox/omnichannel/actions` devuelven `contract_version: inbox.omnichannel.action.v1` y ejecutan `assign`, `reply`, `handoff`, `close`, `reopen` y `set_priority`.

Verificacion SaaS P2 ejecutada:

- `tests.test_v2_commerce_contracts`
- `tests.test_v2_saas_contracts`

## Agent Experience 2026-05-01

Mejora aditiva para primera visita, demo comercial y widget:

- `services/demo_experience_contract.py` queda como contrato compartido de experiencia para demo/widget.
- `POST /api/v2/demo/session` agrega `workspace.first_visit`, `workspace.sample_conversations`, `workspace.trust_signals`, `workspace.lead_capture`, `workspace.media_capabilities`, `workspace.conversion_ctas`, `workspace.animation_tokens`, `experience_blueprint` y `chat_seed.sample_conversations`.
- `POST /api/v2/demo/session` agrega `chat_bootstrap` top-level, dentro de `workspace` y dentro de `chat_seed` para que elegir rubro -> iniciar chat use endpoint, headers, query y payload definidos por backend.
- `GET /api/public/widget-config?tenant={slug}` y `GET /api/public/tenants/{slug}/widget-config` exponen `first_visit`, `sample_conversations`, `trust_signals`, `lead_capture`, `media_capabilities`, `conversion_ctas` y `animation_tokens` dentro de `widget` y `builder_config`.
- `media_capabilities` formaliza texto, imagen, audio/nota de voz, ubicacion y archivos usando endpoints existentes (`/ask` y `/archivos/upload/chat_attachment`), sin duplicar el flujo de chat.
- `conversion_ctas` define CTAs contextuales para ticket/pedido/checkout/handoff/lead con labels y endpoints desde backend.
- `animation_tokens` define microinteracciones para launcher, mensajes, audio, upload, ubicacion, handoff y lead success para que frontend anime sin hardcodear comportamiento.
- `POST /api/public/lead-capture` devuelve `contract_version: public.lead_capture.v1`, `request_id`, `lead_id`, `ticket_id`, `deduplicated`, `idempotency_key` y persiste `TenantTicket`, `ChatSessionContext.lead_profile` y evento analytics `lead_capture_created`.
- `services/chatbot_prompts.py` suma reglas multimodales compartidas para que el LLM use `uploaded_file_info`, `datos_interpretados_archivo`, `transcribed_text` y ubicacion como contexto accionable.
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_AGENT_EXPERIENCE_2026-05-01.md`.
- Se agrego handoff puntual para frontend: `docs/BACKEND_TO_FRONTEND_SYNC_CHAT_BOOTSTRAP_AND_FRESHNESS_2026-05-02.md`.

Verificacion Agent Experience ejecutada:

- `tests.test_demo_experience_contract`
- `tests.test_api_v2_foundation`
- `tests.test_public_resolver`
- `tests.test_public_resolver_quick_menu`
- `tests.test_pwa_public_cart_url`
- `tests.test_chatbot_prompts_multimodal`
- `tests/test_public_lead_capture.py`

## Operational Intelligence 2026-05-02

Mejora aditiva para analytics, tickets/reclamos, WhatsApp, chats en vivo, empleados, encuestas/votaciones y mapas:

- `services/operational_intelligence.py` agrega un agregador compartido sobre modelos existentes, sin crear app paralela.
- `GET /api/v2/analytics/operations/dashboard` y alias `GET /api/v2/analytics/operations` devuelven `contract_version: operations.dashboard.v1`.
- `GET /api/v2/analytics/operations/heatmap` devuelve `contract_version: operations.heatmap.v1`.
- `GET /api/v2/analytics/operations/action-center` devuelve `contract_version: operations.action_center.v1`.
- `GET /api/v2/analytics/operations/freshness` devuelve `contract_version: operations.freshness.v1` con estado por fuente (`fresh`, `stale`, `empty`) para dashboards y mapas degradables.
- El dashboard une `TenantTicket`, `MunicipioTicket`, `PymeTicket`, `AnalyticsEventV2`, `ChatSessionContext`, `TicketRealtimeState`, `EncEncuesta`, `EncRespuesta`, `PublicSurvey`, `PublicSurveyResponse` y empleados `User`.
- Heatmap combina capas `tickets`, `surveys` y `analytics_events`, con `points`, `cells`, `hotspots`, `bounds` y `render_contract` para MapLibre.
- `trends` compara el periodo actual contra el periodo anterior del mismo tamano.
- `next_best_actions` recomienda acciones proactivas: revisar vencidos, asignar tickets, cubrir empleados, impulsar votaciones, monitorear WhatsApp, revisar handoffs e inspeccionar hotspots.

## Landing UX/UI 2026-05-02

Contrato publico para redisenar landing y paginas aledanas desde backend, sin hardcodear copy ni marca en React:

- `services/landing_experience_contract.py` agrega `public.landing_experience.v1`.
- `GET /api/public/landing-experience` devuelve brand, logo rules, tokens de color/tipografia/layout, motion, navigation, hero, secciones, paginas aledanas, proof bar, FAQ y CTAs.
- Soporta plataforma default y modo white-label por `tenant`, `slug` o `widget_token`.
- Paginas cubiertas por contrato: `/demo`, `/pymes`, `/municipios`, `/colegios`, `/encuestas`, `/widget`.
- Frontend handoff: `docs/BACKEND_TO_FRONTEND_SYNC_LANDING_UXUI_2026-05-02.md`.

Verificacion Landing UX/UI ejecutada:

- `tests.test_landing_experience_contract`
- `tests.test_public_resolver_widget_config_contract`
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_OPERATIONS_2026-05-02.md`.
- Se agrego handoff puntual para frontend: `docs/BACKEND_TO_FRONTEND_SYNC_CHAT_BOOTSTRAP_AND_FRESHNESS_2026-05-02.md`.

Verificacion Operational Intelligence ejecutada:

- `tests.test_v2_operational_analytics`
- `tests.test_v2_analytics_overview`
- `tests.test_v2_saas_contracts`
- `tests.test_v2_tickets`
- `tests.test_v2_surveys`
- `tests.test_encuestas_dashboard_bundle_contract`
- `tests.test_encuestas_heatmap_fallback`
- `tests.test_estadisticas_heatmap`
- `tests.test_municipal_tickets_map_data`
- `tests.test_ticket_realtime_state`

## Demo Landing / Widget 2026-05-08

Mejora aditiva para que la experiencia inicial no quede colgada en "Cargando demos" y muestre los tres pilares comerciales:

- `services/demo_pillar_catalog.py` agrega `demo.pillars.v1` con `educacion`, `gobierno` y `empresas`.
- `GET /api/v2/demo/catalog` devuelve `pillars`, `sector_groups[].categories`, recursos PDF demo y prompts por categoria.
- `POST /api/v2/demo/session` acepta sector/pillar/categoria/rubro con aliases (`colegios`, `Soluciones para Empresas`, `rubro_slug`, `category_slug`) y usa el rubro default del pilar cuando frontend no manda `tenant_slug`.
- `/rubros/?format=tree` y `/api/rubros/?format=tree` en demo mode incluyen la raiz `Colegios e instituciones educativas`.
- `GET /api/public/realtime/voice-capabilities` conserva ruta canonica y agrega aliases publicos para compatibilidad de widget.
- Se agregaron PDFs demo en `data/demo_catalogs/{colegios,gobiernos,empresas}` y script regenerador `scripts/generate_demo_catalog_assets.py`.
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_DEMO_LANDING_WIDGET_2026-05-08.md`.

## Full Platform QA 2026-05-09

Validacion y mejoras aditivas sobre lo existente, sin crear app paralela:

- WhatsApp/webhook/voz/realtime/promocionar/funnel quedo verde localmente: 74 tests passed.
- Catalogo/Qdrant/import legacy/catalog mappings quedo verde localmente: 26 tests passed.
- Pedidos/market/rewards/order preview quedo verde localmente: 9 tests passed.
- Educacion/colegios/KB/rubros education profile quedo verde localmente: 12 tests passed.
- Se blindaron tests de analytics/Qdrant y educacion para usar `TestingConfig`/SQLite y evitar tocar Render/Postgres desde local.
- `POST /api/admin/catalogo/importar` conserva compatibilidad legacy con errores JSON y soporte de `column_map`, `plantilla` y `guardar_plantilla`.
- WhatsApp mapea opciones numericas a labels humanos antes de llamar al bot y envia bienvenida completa: template, sticker/media y saludo textual.
- Reclamos por WhatsApp pasan directo a categorias accionables cuando el usuario pide iniciar un reclamo.
- Taxonomia educativa mejora labels visibles: `Documentación`, `Agenda académica`, `Tesorería`.
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_FULL_PLATFORM_QA_2026-05-09.md`.

## Runtime + Marketplace 2026-05-11

Mejoras aditivas sobre runtime publico, demo/widget y marketplace:

- Sesiones demo (`demo_session_id`, `X-Demo-Session-Id`, `X-Demo-Session`) ahora resuelven owner/tenant/rubro para `/api/ask/*` sin login.
- Si el chat publico/demo tiene una excepcion interna, backend responde `chat.runtime_fallback.v1` con HTTP 200, `request_id` y acciones recuperables.
- CORS publico permite `X-Demo-Session`, `X-Demo-Session-Id`, `Idempotency-Key` y expone `X-Request-Id`/`X-Correlation-Id`.
- `GET /api/v2/demo/catalog` estabiliza orden y `tenant_slug` default para `gobierno`, `empresas` y `educacion`.
- Widget config publica `support_channels.live_chat.socket_enabled` y `realtime.socket_enabled`; por defecto no habilita Socket.IO si el backend/proxy no esta listo.
- `GET /api/public/realtime/voice-capabilities` devuelve `request_id` y JSON accionable incluso cuando no resuelve tenant.
- Marketplace serializa `image_url`, `gallery_urls`, `image_status` e `image_alt`.
- Crear/editar producto acepta aliases de imagen (`imagen_url`, `image_url`, `foto`, `thumbnail`, `gallery_urls`, `imagenes`, `images`).
- Nuevo `POST /api/admin/market/catalog/{product_id}/images` para subir/reemplazar imagen principal y galeria.
- Importacion CSV/Excel/TXT/PDF detecta columnas de imagen, devuelve `image_summary` y persiste imagenes al confirmar preview/Qdrant.
- TTS dejo de generarse automaticamente en web/demo; queda limitado a audio/voz/preferencia o `TTS_AUTO_GENERATE_FOR_TEXT=true`.
- Cohere fallback usa `COHERE_CHAT_MODEL=command-a-03-2025` y API v2 por defecto.
- Se agrego handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_RUNTIME_MARKETPLACE_2026-05-11.md`.

## Widget UX/UI Onboarding 2026-05-11

Mejora aditiva para el widget global de landing y la primera experiencia:

- `GET /api/public/widget-config` en hosts plataforma (`chatboc.ar`, `www.chatboc.ar`, localhost) sin tenant/token devuelve selector global `public.widget_onboarding.v1`.
- Selector global muestra tres pilares: `Colegios`, `Gobiernos`, `Empresas`, con `tenant_slug`, `sector` y `rubro` para iniciar `POST /api/v2/demo/session`.
- Widget tenant agrega `onboarding.mode=tenant_quick_menu` y mantiene `quick_menu` backend-driven.
- Se agrega `widget.ui_hints.v1` para UI compacta: maximo 3 quick replies visibles, acciones de composer como iconos, header liviano, botones extra colapsados.
- `widget.ui_hints.v1` incluye `accessibility` para dislexia, texto simple, alto contraste, controles grandes, captions, reduced motion y target tactil minimo.
- Se exponen top-level `onboarding`, `media_capabilities`, `conversion_ctas`, `animation_tokens` y `ui_hints` para que frontend no tenga que buscar dentro de objetos anidados.
- `suppress_global_widget` ahora es `false` fuera de integracion y `true` solo para preview/integracion.
- Handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_WIDGET_UXUI_ONBOARDING_2026-05-11.md`.

## Full Platform Runtime QA 2026-05-11

Cierre de blockers runtime reportados por frontend/prod:

- `POST /api/ask`, `/api/ask/pyme` y `/api/ask/municipio` quedan como aliases compatibles de `/ask/*` y degradan errores 500 a `chat.runtime_fallback.v1` con `request_id`.
- `POST /api/v2/demo/session` acepta aliases frontend en espanol: `pilar`, `categoria`, `categoria_slug`, `tenantSlug` y `tenant_key`.
- `GET /api/public/realtime/voice-capabilities` agrega `enabled`; si el tenant tiene voice apagado responde `200` degradable con `reason_code=voice_not_enabled` y `features.tool_calling=false`.
- `GET /api/public/widget-config` expone `visibility_rules.allow_websocket` para que frontend no intente Socket.IO cuando no esta disponible.
- `POST /api/archivos/upload/chat_attachment` queda como alias compatible de `/archivos/upload/chat_attachment`.
- Upload multimedia agrega `request_id` y headers CORS para `X-Widget-Token`, `X-Tenant-Slug`, `X-Demo-Session-Id` e `Idempotency-Key`.
- `POST /api/admin/catalogo/importar` mantiene errores JSON `{ codigo, mensaje }`; metodos `GET`, `PUT`, `PATCH`, `DELETE` devuelven `method_not_allowed` en JSON.
- Handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_FULL_PLATFORM_QA_2026-05-11.md`.

## Realtime Voice QA 2026-05-12

Alineacion con el QA frontend de voz realtime:

- `GET /api/public/realtime/voice-capabilities` mantiene HTTP 200 degradable y `request_id`.
- El contrato conserva `recommended_model: "gpt-realtime"` por defecto, con overrides por tenant/env.
- La respuesta de capabilities agrega `support_channels.voice_call.enabled` para que frontend use la misma regla que widget-config.
- Si `realtime_voice_enabled=false`, capabilities y widget-config devuelven `enabled:false`, `reason_code=voice_not_enabled` y `features.tool_calling=false`.
- `GET /api/public/widget-config` publica `support_channels.voice_call.enabled` y `realtime_voice.features.tool_calling` sincronizados.
- `POST /api/public/realtime/session` acepta `model`/`recommended_model`, `fallback_model`, `voice`, `transport`, `profile` y `active_vertical` desde el contrato publico.
- Badges/starters quedan backend-first: frontend debe usar los campos que lleguen y no inventar starters locales por vertical.

Verificacion ejecutada:

- `tests/test_realtime_voice_profiles.py`
- `tests/test_public_resolver_widget_config_contract.py`
- `tests/test_widget_settings.py`

## Widget onboarding QA 2026-05-12

Alineacion con el QA frontend del widget en landing:

- `GET /api/public/widget-config` sin tenant en host plataforma devuelve `public.widget_config.v1` con `tenant.slug=chatboc-platform`, `tenant.tipo=platform`, `public.widget_onboarding.v1` y `mode=platform_sector_selector`.
- La deteccion de host plataforma contempla deploy con proxy (`X-Forwarded-Host`, `X-Original-Host`, `X-Host`, `Origin`, `Referer`) para que `www.chatboc.ar` no caiga en un tenant default cuando Render recibe un host interno.
- `quick_menu` top-level y `onboarding.quick_menu` quedan sincronizados con tres opciones backend-first: Colegios, Gobiernos y Empresas.
- Cada opcion trae `label`, `sector`, `tenant_slug` y `rubro` para iniciar `POST /api/v2/demo/session`.
- `ui_hints` mantiene `widget.ui_hints.v1`, `max_visible_quick_replies=3` y composer compacto.
- `realtime.socket_enabled=false`, `visibility_rules.allow_websocket=false` y `support_channels.live_chat.socket_enabled=false` para que landing no conecte `/socket.io` ni muestre badge Live si no esta habilitado.
- `POST /api/v2/demo/session` acepta los payloads del selector para `educacion`, `gobierno` y `empresas`; devuelve `workspace.chat_bootstrap` con endpoint canonico, headers `X-Demo-Session-Id`, `X-Chat-Session-Id`, `X-Tenant-Slug`, media capabilities, conversion CTAs y animation tokens.
- Runtime fix 2026-05-12: el default `DEMO_WELCOME_MESSAGE` ya no reabre el selector legacy de rubros; los aliases publicos `GET /api/<slug>/live-chat/schedule`, `GET /<slug>/live-chat/schedule`, `/api/demo/live-chat/schedule` y `/demo/live-chat/schedule` devuelven JSON 200/CORS con `live_chat.schedule.v1` aun si el tenant demo todavia no existe.

Verificacion ejecutada:

- `tests/test_config_demo_mode_flag.py`
- `tests/test_tenant_leads_management.py::test_public_live_chat_schedule_aliases_never_404_for_demo_widget`
- `tests/test_public_resolver_widget_config_contract.py`
- `tests/test_api_v2_foundation.py`

## Demo landing/widget runtime compat 2026-05-12

Mejora aditiva para que bundles cacheados del frontend no rompan el flujo publico de demos:

- `POST /api/v2/demo/session` queda como ruta canonica de `demo.session.v2`.
- `POST /v2/demo/session`, `POST /api/v1/demo/session` y `POST /v1/demo/session` quedan como aliases degradables del mismo contrato.
- Los cuatro paths aceptan `OPTIONS`, devuelven JSON y exponen `X-Request-Id`.
- CORS publico acepta los headers usados por widget/demo (`X-Tenant-Slug`, `X-Widget-Token`, `X-Chat-Session-Id`, `X-Demo-Session-Id`, `X-Anon-Id`, `Anon-Id`, `Idempotency-Key`).
- `landing.public_experience.v1` se ajusto para que el copy visible sea comercial y no muestre lenguaje tecnico interno.

Verificacion ejecutada:

- `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_demo_session_canonical_and_legacy_aliases_delegate_to_v2_with_cors`
- `tests/test_public_resolver_widget_config_contract.py::PublicResolverWidgetConfigContractTest::test_landing_experience_visible_copy_is_commercial`

## Embedded widget commerce + user portal 2026-05-12

Mejora aditiva para que el script embebido de cada tenant sea una experiencia SaaS completa y white-label:

- `GET /api/public/widget-commerce-session` devuelve `public.widget_commerce_session.v1` con tenant, session, catalogo, carrito, checkout, portal, historial, accesibilidad y contrato frontend.
- `GET /api/public/widget-user/tenant-history` devuelve `public.widget_user_tenant_history.v1` filtrado por tenant y, cuando existe, por `anon_id`/`chat_session_id`.
- `POST /api/public/widget-user/register` y `POST /api/public/widget-user/link-session` devuelven contratos JSON degradables para asociar sesion anonima, carrito e historial sin perder contexto.
- El bundle reutiliza los endpoints existentes de catalogo, PWA cart, checkout, tracking y auth/widget; no crea un carrito paralelo.
- `GET /api/live-chat/schedule`, `/live-chat/schedule`, `/api/{tenant_slug}/live-chat/schedule` y `/{tenant_slug}/live-chat/schedule` degradan a `socket_enabled=false`, `socket_transport_hint=disabled` y `fallback_mode=http_chat`.
- `/api/ask/*` evita emitir el selector legacy cuando llegan marcadores de tenant/demo/chat bootstrap.

Verificacion ejecutada:

- `tests/test_public_tenant_catalog_alias.py`
- `tests/test_tenant_leads_management.py::test_public_live_chat_schedule_aliases_never_404_for_demo_widget`
- `tests/test_tenant_leads_management.py::test_public_api_live_chat_schedule_alias_includes_socket_hints`
- `tests/test_tenant_leads_management.py::test_tenant_live_chat_schedule_config_and_public_status`

## Production QA + Inbox 360 2026-05-12

Mejora aditiva para pasar de contratos desbloqueados a operacion monitoreable y drawer 360:

- Se agrega smoke local aislado `scripts/local_platform_smoke.py` con SQLite en memoria para validar contratos sin tocar produccion ni la base real.
- `GET /api/v2/inbox/omnichannel` mantiene `inbox.omnichannel.v1`, pero cada item ahora trae `detail_endpoint`, `attachments`, `sla`, `allowed_actions`, `next_steps`, `source_metadata`, `map.can_render` y `frontend_contract.render_as=inbox_360_drawer`.
- `GET /api/v2/inbox/omnichannel/{ticket_id}` devuelve `inbox.omnichannel.detail.v1` con el mismo item enriquecido para drawer 360.
- Las acciones existentes de inbox siguen en `/api/v2/inbox/omnichannel/{ticket_id}/actions` y usan el mismo payload enriquecido al responder.
- `GET /api/v2/platform/production-smoke`, `GET /api/v2/production-smoke` y `GET /api/v2/tenants/{tenant_slug}/production-smoke` devuelven `platform.production_smoke.v1`.
- El smoke protegido valida rutas criticas, widget onboarding, socket disabled, tenant admin experience, catalog quality, WhatsApp operations e inbox 360.
- `?fail_http=1` permite que monitores externos reciban HTTP 500 cuando haya falla critica; por defecto responde JSON 200 con `status`.
- Se actualizo handoff frontend: `docs/BACKEND_TO_FRONTEND_SYNC_TENANT_ADMIN_PROFILE_2026-05-12.md`.

Verificacion ejecutada:

- `test_venv\Scripts\python.exe scripts\local_platform_smoke.py`
- `tests/test_v2_saas_contracts.py`

## Public Demo Route QA 2026-05-12

Mejora aditiva para cerrar errores visibles de landing, rutas publicas, demo y widget embebido:

- Se agrego lista de slugs publicos reservados para que rutas como `casos`, `pymes`, `empresas`, `municipios`, `gobiernos`, `colegios`, `sectores`, `precios` y `opinar` no se intenten resolver como tenants reales.
- `GET /api/public/tenants/{slug}/catalog` y `/public/tenants/{slug}/catalog` degradan a JSON `public.catalog_resolution.v1` con `items=[]`, `cart.enabled=false`, `request_id` y CORS OK cuando el slug es reservado, el tenant no existe o falta owner de catalogo.
- `GET /api/public/tenants/{tenant_slug}/public-navigation` y `/public/tenants/{tenant_slug}/public-navigation` devuelven `tenant.public_navigation.v1` para que frontend pueda deshabilitar `Noticias`, `Eventos`, `Encuestas`, `Nuevo reclamo` o `Catalogo` sin navegar a paginas rotas.
- `GET /api/v2/demo/admin-preview` devuelve `demo.admin_preview.v1` para `educacion`, `gobierno` y `empresas`, con modulos, cards, timeline y catalogo.
- `GET /api/v2/demo/catalog-assets/{archivo}.pdf` sirve aliases de catalogos demo para colegios, gobiernos y empresas.
- `GET /api/public/tenants/{tenant_slug}/live-chat/schedule` queda cubierto por `live_chat.schedule.v1` degradable, igual que los aliases cacheados previos.

Verificacion ejecutada:

- `tests/test_public_tenant_catalog_alias.py`
- `tests/test_tenant_leads_management.py::test_public_live_chat_schedule_aliases_never_404_for_demo_widget`
- `tests/test_tenant_leads_management.py::test_public_api_live_chat_schedule_alias_includes_socket_hints`
- `tests/test_tenant_leads_management.py::test_tenant_live_chat_schedule_config_and_public_status`
- `tests/test_api_v2_foundation.py`
- `tests/test_public_resolver_widget_config_contract.py`
- `tests/test_widget_settings.py`

## Twilio Sandbox + Integraciones 2026-05-12

Mejora aditiva para que el panel tenant pueda probar WhatsApp Sandbox y el editor de catalogo no dependa solo de borrador local:

- `POST /api/v2/tenants/{tenant_slug}/whatsapp/sandbox-session` devuelve `whatsapp.sandbox_session.v1` con tenant, numero sandbox, join phrase, deeplink `wa.me`, contexto de demo, quick menu recibido por frontend y `request_id`.
- `POST /api/v2/whatsapp/sandbox-session` queda como alias tenant-aware; puede resolver `tenant_slug` desde header, query o body.
- El endpoint no envia mensajes reales; guarda ultimas sesiones de prueba en `tenant.configuracion.whatsapp_sandbox_sessions` para trazabilidad del panel.
- `GET /api/admin/tenants/{tenant_slug}/catalog` expone `draft_endpoint` y `links.draft_endpoint`.
- `PUT /api/admin/tenants/{tenant_slug}/catalog/draft` guarda `tenant.catalog_draft.v1` en `tenant.configuracion.catalog_draft` para persistir borradores entre dispositivos.
- Se mantiene `live_chat.schedule.v1` degradable y `socket_enabled=false` mientras Socket.IO no este publicado.

Verificacion ejecutada:

- `tests/test_v2_saas_contracts.py::V2SaasContractsTest::test_whatsapp_sandbox_session_returns_deeplink_contract`
- `tests/test_v2_saas_contracts.py::V2SaasContractsTest::test_admin_catalog_exposes_and_saves_draft_endpoint`
