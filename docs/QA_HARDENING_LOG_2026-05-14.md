# QA Hardening Log - 2026-05-14

## Objetivo

Registrar cada prueba realista que se haga sobre Chatboc y convertirla en una mejora concreta o en una tarea pendiente clara. La regla es simple:

- Si durante QA aparece algo roto y es acotado, se corrige en el momento.
- Si requiere una tanda mayor, se documenta aca con evidencia, riesgo y criterio de cierre.
- No se mezclan responsabilidades: backend arregla contratos, persistencia, sesiones, acciones, datos y errores; frontend arregla UX, render, estados visuales y performance.

## Estado Actual

### Corregido

- `POST /ask/municipio` ya no debe persistir `demo_session_id` JWT como `chat_session_context.chat_session_id`.
- Cuando no llega `X-Chat-Session-Id`, backend debe derivar un `sid_...` estable desde `demo_session_id`.
- Demo chat con `demo_session_id` invalido debe responder JSON accionable, no 500.
- Webhook WhatsApp municipio crea reclamos por texto, foto, ubicacion y audio transcripto.
- Foto enviada antes de confirmar reclamo queda asociada al `MunicipioTicket` como adjunto.
- Reclamo con ubicacion persiste `latitud` y `longitud`.
- Pedido PYME Cuatro Fincas persiste `detalles[]` con lineas reales, total, cliente y telefono.
- Pedido PYME prioriza nombre real de WhatsApp/contacto sobre placeholders como `Vecino/a`.
- Demo session publica y endpoints SaaS exponen `whatsapp_sandbox` con limite de 10 mensajes, inputs soportados y scripts por rubro.
- `GET/POST /api/v2/demo/whatsapp-sandbox` permite iniciar launcher WhatsApp demo sin login, con `demo_session_id` largo separado de `chat_session_id` corto.
- El launcher publico usa numero WhatsApp dedicado del tenant si existe; Twilio Sandbox queda como fallback con frase `join`.
- Tracking de pedidos usa `/tracking/order/{nro_pedido}` sin duplicar rutas.
- Twilio Sandbox `+14155238886` queda cubierto por QA reproducible con un tenant colegio `qa-colegio-sandbox`.
- WhatsApp colegio por sandbox crea caso escolar real desde menu + accion + audio/ubicacion.
- Al crear el caso escolar, el backend limpia `education_pending_case` para que el siguiente mensaje no quede pegado al caso anterior.
- Onboarding WhatsApp Tech Provider expone contrato backend-first para activar clientes sin mostrar Twilio Console.
- El contrato Tech Provider declara `manual_twilio_console_allowed=false`, `customer_sees_twilio_console=false` y `show_twilio_brand=false`.
- `POST /api/v2/tenants/:tenant_slug/whatsapp/tech-provider/provision` persiste plan dry-run sin llamar API live cuando `TWILIO_TECH_PROVIDER_LIVE_ENABLED=false`.
- `POST /api/v2/tenants/:tenant_slug/whatsapp/tech-provider/embedded-signup` persiste `waba_id`, `phone_number_id`, `session_id` y deja `next_action=register_whatsapp_sender_via_senders_api`.
- `GET /api/public/landing-experience` expone `conversion.journey` para guiar landing -> demo -> lead -> tenant -> WhatsApp sin rutas inventadas en frontend.
- `lead_capture` expone captura progresiva, estados de exito/validacion y payload keys para que frontend no postee formularios vacios.
- `POST /api/public/lead-capture` devuelve `frontend_contract`, `follow_up` y `next_actions` comerciales para superadmin/ventas.

### Evidencia Ejecutada

- Tests focalizados:
  - `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_demo_ask_municipio_does_not_persist_jwt_as_chat_session_id`
  - `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_ask_municipio_demo_session_creates_short_chat_context`
  - `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_ask_municipio_demo_session_without_header_reuses_stable_chat_context`
  - `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_ask_invalid_demo_session_returns_json_error`
  - `tests/test_pyme_order_extraction.py`
  - `tests/test_reclamo_flow_contact_merge.py`
  - `tests/test_education_routes.py`
- Tests widget/portal/realtime:
  - `tests/test_webauthn_support.py`
  - `tests/test_public_tenant_catalog_alias.py`
  - `tests/test_realtime_session.py`
  - `tests/test_widget_settings.py`
  - `tests/test_realtime_voice_profiles.py`
  - `tests/test_public_resolver_widget_config_contract.py`
  - `tests/test_voice_realtime_routes.py`
- Tests sandbox WhatsApp demo:
  - `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_public_whatsapp_sandbox_launcher_requires_no_auth_and_exposes_trial_contract`
  - `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_v2_demo_session_returns_workspace_contract`
  - `tests/test_v2_saas_contracts.py::V2SaasContractsTest::test_whatsapp_sandbox_session_returns_deeplink_contract`
  - `tests/test_v2_saas_contracts.py::V2SaasContractsTest::test_whatsapp_sandbox_setup_and_test_contracts_are_backend_first`
- Tests Tech Provider WhatsApp:
  - `tests/test_v2_saas_contracts.py::V2SaasContractsTest::test_twilio_tech_provider_onboarding_contract_hides_twilio_console`
  - `tests/test_v2_saas_contracts.py::V2SaasContractsTest::test_twilio_tech_provider_provision_dry_run_persists_plan_without_live_api`
- Tests conversion frontend/backend:
  - `tests/test_landing_experience_contract.py`
  - `tests/test_public_resolver_widget_config_contract.py::PublicResolverWidgetConfigContractTestCase::test_landing_experience_without_tenant_returns_platform_contract`
  - `tests/test_public_resolver_widget_config_contract.py::PublicResolverWidgetConfigContractTestCase::test_landing_experience_does_not_publish_frontend_visual_tokens_or_page_sections`
  - `tests/test_public_lead_capture.py`
- Resultado actualizado: `41 passed`, `3 subtests passed`.
- Resultado widget/portal/realtime actualizado: `48 passed`.
- Resultado sandbox WhatsApp demo focalizado: `4 passed`.
- Resultado contratos v2 foundation + SaaS: `46 passed`, `3 subtests passed`.
- Resultado SaaS completo con Tech Provider: `19 passed`.
- Resultado contratos publicos + SaaS + lead capture: `65 passed`, `3 subtests passed`.
- QA WhatsApp simulada con webhook Twilio firmado:
  - `18/18` requests respondieron `200`.
  - Delta creado: `3` tickets municipales, `1` pedido PYME, `1` ticket/caso escolar, `3` adjuntos.
  - Re-ejecutada despues del contrato sandbox publico; se mantuvo `18/18` OK.
  - Ticket texto+foto verificado con `foto_url_directa` y `archivos=1`.
  - Ticket ubicacion verificado con coordenadas.
  - Pedido Cuatro Fincas verificado con cliente `QA Bodega`, total y lineas.
  - Colegio sandbox verificado con `SchoolCaseAlias`: `ticket_type=pyme`, `case_type=inasistencia`, `channel=whatsapp`.
  - Contexto colegio verificado sin `education_pending_case` despues de crear el caso.

## Pendientes Detectados Para Siguiente Tanda

### P0 - Continuidad real frontend/backend en demo

Riesgo:
Si frontend no envia `X-Chat-Session-Id` o `chat_session_id` del contrato `/api/v2/demo/session`, backend ya estabiliza con `sid_...`, pero frontend deberia usar la sesion explicita para evitar ambiguedades y mejorar trazabilidad.

Criterio de cierre:

- Frontend envia `chat_session_id` en header `X-Chat-Session-Id` o payload para todos los turnos demo.
- Backend mantiene fallback `sid_...` desde `demo_session_id`.
- Logs de Render muestran una sola sesion por demo.

### P0 - Admin preview debe reflejar tickets/pedidos reales recientes

Riesgo:
La demo crea tickets/pedidos reales, pero si `GET /api/v2/demo/admin-preview` no consulta los ultimos casos creados, frontend puede mostrar un panel que no refleja la conversacion.

Criterio de cierre:

- Crear reclamo desde demo.
- Consultar admin preview.
- Ver el mismo ticket o un resumen trazable del ticket en `cards`, `timeline`, `modules` o `map.points`.
- Si no hay datos reales, no publicar metrica ni mapa.

### P1 - Heatmap y analytics operativas

Riesgo:
Hay servicios de analytics/heatmap, pero hay que validar endpoint por endpoint para evitar publicar puntos, metricas o series inventadas.

Criterio de cierre:

- Heatmap devuelve puntos solo desde tickets con coordenadas reales.
- Si no hay puntos, devuelve lista vacia y `enabled=false` o estado equivalente.
- Dashboard no muestra metricas sin fuente.
- Tests cubren caso con puntos y caso sin puntos.

### P1 - Encuestas/votaciones

Riesgo:
Existen endpoints/modulos, pero falta QA completa de crear, listar, responder, resultados y errores JSON.

Criterio de cierre:

- Responder encuesta publica crea respuesta real.
- Resultados agregan solo datos reales.
- Errores devuelven JSON con `request_id`.
- No se publican votos fake en demo/landing.

### P1 - Empleados, roles y asignacion

Riesgo:
La logica existe dispersa. Falta contrato SaaS duro para crear empleados, asignar tickets y filtrar bandejas por permisos.

Criterio de cierre:

- Crear empleado por tenant.
- Asignar ticket/reclamo a empleado.
- Listar bandeja por empleado.
- Validar permisos por rol.
- Tests cubren tenant isolation.

### P1 - Subida de catalogos PYME

Riesgo:
La venta depende de que subir catalogo sea profesional y confiable. Falta QA de upload real, parsing, indexacion, preview y busqueda/pedido.

Criterio de cierre:

- Subir catalogo PDF/CSV/XLSX.
- Backend parsea productos con precio, moneda, stock/categoria si existen.
- Catalogo queda consultable.
- Pedido desde WhatsApp usa productos reales del catalogo.
- Errores de parsing devuelven JSON accionable.

### P0 - Llamadas Realtime, no TTS/Gather por defecto

Riesgo:
Las llamadas no deben depender del loop legacy `Gather` + TTS. Ese camino agrega latencia, costo y peor experiencia. La ruta primaria debe ser voz nativa OpenAI Realtime; SIP directo es el destino tecnico, y Twilio Media Streams queda como puente operativo mientras se termina la configuracion de trunking.

Estado aplicado:

- `/voice/welcome` ahora deriva por defecto al stream realtime y solo usa `Gather` + TTS si `VOICE_LEGACY_GATHER_ENABLED=true`.
- `/twilio/voice/inbound` mantiene el contrato Twilio Media Streams hacia `/twilio/voice/stream`.
- El contrato `realtime.voice_capabilities.v1` declara `phone_primary=openai_realtime_sip`, `phone_bridge=twilio_media_streams` y TTS/STT externo como fallback only.
- `VoiceStreamService` deja de enviar el header beta fijo `realtime=v1`; si hace falta compatibilidad se puede setear `OPENAI_REALTIME_BETA_HEADER`.
- `POST /api/public/realtime/session` tambien deja `OpenAI-Beta` como opt-in por `OPENAI_REALTIME_BETA_HEADER`, para operar GA/latest por defecto.
- Se agrego politica multidioma `es/en/pt`: llamadas realtime y notas de voz detectan idioma, responden al usuario en su idioma y normalizan campos operativos al español para admin.

Criterio de cierre:

- Configurar Twilio Elastic SIP Trunk hacia OpenAI Realtime SIP para llamadas entrantes.
- Agregar webhook OpenAI `realtime.call.incoming` para aceptar/rechazar llamadas con modelo, voz, instrucciones y tools por tenant.
- Mantener Twilio Media Streams como fallback hasta validar Junin, bodega y colegio sandbox en llamadas reales.
- Registrar evento post-llamada y acciones trazables: ticket, pedido o caso escolar.

### P0 - Widget externo, portal ciudadano y seguimiento

Estado aplicado:

- `merge_anon_into_user` ahora adopta tambien `MarketCart` y `MarketOrder` por `anon_id`, `chat_session_id` y `contact_key=session:<id>`.
- `POST /api/public/widget-user/register` valida `name` + `email_or_phone`, crea/vincula usuario provisional, crea `TenantFollower`, migra historial/carrito/chat y devuelve contrato JSON con `merge`.
- `POST /api/public/widget-user/link-session` vincula la sesion si ya existe usuario provisional por `anon_id`; si no, responde JSON accionable con `registration_required`.
- `GET /api/public/widget-commerce-session` habilita catalogo/carrito para municipio solo si hay catalogo real asociado al tenant, no por placeholder.
- `demo_session_id` largo ya no queda publicado como `session.chat_session_id` en contratos publicos del widget; se deriva `sid_...` corto.
- Se agrego `docs/widget_junin_external_contract_test.html` como pagina externa de prueba por contrato.

Riesgo:

Frontend todavia debe implementar la experiencia visual completa: portal ciudadano anonimo, registro progresivo, seguimiento de reclamos, canjes de puntos y estado de carrito sin mezclar portal admin.

Criterio de cierre:

- Servir la pagina externa de prueba contra backend local o staging.
- Confirmar que el widget carga tenant `municipio`, mantiene `anon_id`/`chat_session_id`, abre historial, permite registro y no pierde carrito/reclamos.
- Frontend renderiza `/portal/:tenant` con historial anonimo antes de login y con CTA de registro progresivo.
- Seguimiento de reclamo muestra timeline, adjuntos y mapa solo con datos reales.

### P0 - WhatsApp Tech Provider productivo

Estado aplicado:

- `GET /api/v2/tenants/:tenant_slug/whatsapp/tech-provider` publica contrato `twilio.tech_provider.v1` para frontend.
- `POST /api/v2/tenants/:tenant_slug/whatsapp/tech-provider/provision` arma plan de provisioning, persiste estado y en modo live crea subcuenta Twilio con la Account API.
- En modo live el backend se detiene luego de crear subcuenta si no existe estrategia segura para guardar token/API key de la subcuenta.
- `POST /api/v2/tenants/:tenant_slug/whatsapp/tech-provider/embedded-signup` persiste resultado de Meta Embedded Signup y deja el siguiente paso trazable.
- Frontend handoff actualizado para no mostrar consola Twilio ni pasos manuales al cliente final.

Riesgo:

Activar WhatsApp productivo completo requiere cerrar secret store para credenciales de subcuenta, Messaging Service API, Senders API, polling de estado y webhooks por tenant. La automatizacion no puede saltear Login/OTP/aprobaciones de Meta ni avisos obligatorios del programa dentro de Embedded Signup; solo encapsula el flujo dentro del panel Chatboc y evita Twilio Console.

Criterio de cierre:

- Definir secret store para `subaccount_auth_token` o API Keys por subcuenta.
- Crear Messaging Service por subcuenta.
- Registrar/asociar sender WhatsApp despues de Embedded Signup.
- Polling de sender hasta aprobado/online.
- Guardar `messaging_service_sid`, `sender_sid`, `waba_id`, `phone_number_id` y estado productivo en `tenant.configuracion.twilio_tech_provider`.
- Enviar y recibir mensaje real por el numero del tenant sin que el cliente abra Twilio Console.

### P2 - Config local de media/cloud

Riesgo:
En local aparece `Cloudinary api_secret` faltante y Google credentials ausentes. El flujo cae a fallback local, pero la QA puede confundirse con errores de infraestructura.

Criterio de cierre:

- Documentar envs minimas para QA local.
- Silenciar degradaciones esperadas como warning controlado.
- Mantener error fuerte solo cuando el flujo realmente no pueda continuar.

### P1 - WhatsApp saludo inicial con sticker y nombre

Estado aplicado:

- El webhook resuelve nombre por prioridad: contexto guardado, usuario DB, contacto resuelto y `ProfileName` de WhatsApp.
- Se ignoran nombres genericos como `Vecino/a` para no personalizar mal.
- Si el bot pregunta el nombre y el usuario responde, se guarda en `profile_name`, `contact_cache` y `contexto_municipio.contacto_usuario.nombre`.
- Al capturar el nombre preguntado, se manda sticker de bienvenida personalizado una sola vez por sesion y se evita duplicarlo luego como header del menu diferido.
- Tests agregados en `tests/test_whatsapp_webhook.py`.

Criterio de cierre:

- Probar en sandbox real que el primer `hola` con nombre guardado envia template/sticker/saludo personalizado.
- Probar en sandbox real que, si no hay nombre, primero pregunta y luego al responder el nombre envia sticker + saludo personalizado.

### P0 - Demo municipio conversacional real

Evidencia:

- En la demo web, "Donde reporto baches con ubicacion?" respondia con estacionamiento.
- "Quiero iniciar un reclamo por alumbrado publico." caia en menu generico.
- El panel admin demo quedaba en cero aunque el usuario estuviera intentando crear un reclamo.

Estado aplicado:

- Nuevo runtime backend `services/demo_municipio_runtime.py` para demo gobierno/municipio.
- `POST /ask/municipio` en demo intercepta reclamos operativos antes del responder legacy.
- Crea o actualiza `MunicipioTicket` real con `canal_ingreso=web_demo_widget`.
- Guarda comentarios/evidencias en `TicketComentario` y `detalles.demo_runtime=true`.
- Soporta contrato de texto, imagen, audio, video, archivo, ubicacion, emoji, llamada y videollamada como capacidades declaradas.
- Licencia responde como tramite guiado sin crear ticket falso.
- Baches clasifica como `Baches y calzada`, no estacionamiento.
- `GET /api/v2/demo/admin-preview?sector=gobierno&tenant_slug=municipio` muestra cards, mapa y actividad solo con tickets reales de demo.

Criterio de cierre:

- En browser local, repetir flujo `/demo?sector=gobierno`: alumbrado, baches con ubicacion, foto y consultar estado.
- Confirmar que el frontend muestra el ticket, el PIN, los adjuntos y el mapa sin botones duplicados.
- Confirmar que Render ya no muestra 500 por JWT y que tampoco reaparece respuesta de estacionamiento para baches.

Tests:

```powershell
.\test_venv\Scripts\python.exe -m unittest tests.test_api_v2_foundation -v
.\test_venv\Scripts\python.exe -m unittest tests.test_api_v2_foundation tests.test_v2_saas_contracts tests.test_landing_experience_contract tests.test_public_resolver_widget_config_contract tests.test_public_lead_capture -v
```

Resultado local:

- 30 tests OK en `tests.test_api_v2_foundation`.
- 62 tests OK en suite combinada de demo/landing/SaaS/public resolver/lead capture.

### P0 - Smoke local repetible de demo, widget, perfil e integracion

Estado aplicado:

- `scripts/local_platform_smoke.py` usa SQLite in-memory y no toca Render, Twilio, WhatsApp ni la DB real.
- El seed ahora separa owners por pilar: colegio, municipio y bodega.
- El smoke valida:
  - `/api/v2/health`.
  - `/api/public/widget-config` como selector platform.
  - `/api/v2/demo/session` para educacion, gobierno y empresas.
  - `POST /api/ask/municipio` con `demo_session_id`, `X-Chat-Session-Id`, ubicacion y foto.
  - `GET /api/v2/demo/admin-preview` filtrado por `chat_session_id`, con ticket real y mapa real.
  - `/api/public/realtime/voice-capabilities`.
  - `/api/v2/tenant/admin-experience`, `/api/v2/whatsapp/experience`, `/api/v2/catalog/quality`, `/api/v2/inbox/omnichannel`, `/api/v2/platform/production-smoke`.

Comando:

```powershell
.\test_venv\Scripts\python.exe scripts\local_platform_smoke.py
```

Resultado local:

- 13 checks OK.
- El reclamo demo de gobierno crea `MunicipioTicket` real con `canal_ingreso=web_demo_widget`.
- El admin preview devuelve `session_activity.has_session_data=true`, `cards[0].value=1` y `map.enabled=true`.

### P0 - QA WhatsApp aislada por defecto

Estado aplicado:

- `scripts/qa_whatsapp_flows.py` ahora setea `TESTING=1` antes de importar `app`, evitando `eventlet` en Windows.
- Por defecto usa `LocalWhatsappQAConfig` con SQLite in-memory.
- Solo usa DB configurada si se setea explicitamente `QA_WHATSAPP_USE_CONFIGURED_DB=1`.
- Seedea mappings locales para:
  - Junin: `+1 (743) 264-3718`.
  - Cuatro Fincas: `+1 (856) 485-8589`.
  - Twilio Sandbox colegio: `+1 (415) 523-8886`.
- Usa fake Twilio client: no envia mensajes reales.
- Usa fake storage para adjuntos: no toca Cloudinary, R2 ni `static/uploads`.
- Usa fake transcripcion para audio de municipio/colegio.

Comando seguro:

```powershell
$env:TWILIO_AUTH_TOKEN='local-token'
$env:TWILIO_ACCOUNT_SID='AC_LOCAL_TEST'
.\test_venv\Scripts\python.exe scripts\qa_whatsapp_flows.py
```

Resultado local:

- Junin texto, imagen, ubicacion y audio: 200 OK.
- Cuatro Fincas pedido y confirmacion: 200 OK.
- Colegio sandbox menu, seleccion de inasistencia y audio/ubicacion: 200 OK.
- Delta validado: 5 contextos, 3 reclamos municipio, 1 ticket pyme/colegio, 1 pedido pyme, 3 adjuntos, 1 alias de caso escolar.

Uso contra DB configurada:

```powershell
$env:QA_WHATSAPP_USE_CONFIGURED_DB='1'
$env:TWILIO_AUTH_TOKEN='<token real o staging>'
.\test_venv\Scripts\python.exe scripts\qa_whatsapp_flows.py
```

No usar este modo contra produccion sin snapshot/ventana de QA, porque escribe contextos, tickets, pedidos y adjuntos.

## Regla De Trabajo Para Proximas Pruebas

Cada recorrido nuevo debe terminar con una de estas salidas:

- Fix aplicado y verificado.
- Test agregado.
- Entrada nueva en este log con prioridad, evidencia y criterio de cierre.
- Handoff para frontend si el problema es visual o de interaccion.

## Comandos Base De Verificacion

```powershell
$env:FLASK_SKIP_GLOBAL_APP='1'
$env:PYTHONIOENCODING='utf-8'
.\test_venv\Scripts\python.exe -m pytest tests\test_api_v2_foundation.py tests\test_pyme_order_extraction.py tests\test_reclamo_flow_contact_merge.py -q
```

```powershell
$env:FLASK_SKIP_GLOBAL_APP='1'
$env:ENABLE_RUNTIME_SCHEMA_SYNC='0'
$env:ENABLE_RUNTIME_TENANT_INIT='0'
$env:STARTUP_RUNTIME_BOOTSTRAP='0'
$env:WHATSAPP_AUDIO_ENABLED='0'
$env:PYTHONIOENCODING='utf-8'
.\test_venv\Scripts\python.exe scripts\qa_whatsapp_flows.py
```
