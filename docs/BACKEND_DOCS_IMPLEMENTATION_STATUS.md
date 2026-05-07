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

- Default realtime actualizado a `gpt-realtime-2`; fallback documentado `gpt-realtime-1.5`.
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
