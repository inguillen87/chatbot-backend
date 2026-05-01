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
- Quick menu educativo completo desde backend.
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
