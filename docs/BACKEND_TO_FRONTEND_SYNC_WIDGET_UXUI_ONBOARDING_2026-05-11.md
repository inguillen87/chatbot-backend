# Backend to Frontend Sync Widget UX/UI Onboarding 2026-05-11

Objetivo: que cualquier usuario que abre el widget desde la landing pueda elegir rubro/pilar, iniciar una demo conversacional y usar texto, imagen, nota de voz, ubicacion, archivo y llamadas sin llenar la pantalla de botones.

## 1. Widget sin tenant: selector de plataforma

Cuando el widget se abre en `chatboc.ar` sin `tenant`, `slug`, `widget_token` ni `whatsapp_destination_number`, backend responde un selector de plataforma en:

`GET /api/public/widget-config`

Shape:

```json
{
  "contract_version": "public.widget_config.v1",
  "tenant": {
    "slug": "chatboc-platform",
    "tipo": "platform",
    "nombre": "Chatboc",
    "white_label": false
  },
  "onboarding": {
    "contract_version": "public.widget_onboarding.v1",
    "mode": "platform_sector_selector",
    "title": "Que queres probar?",
    "entry_question": "Que tipo de organizacion queres simular?",
    "required_step": "select_sector",
    "autostart_after_selection": true,
    "selection_endpoint": "/api/v2/demo/session",
    "catalog_endpoint": "/api/v2/demo/catalog",
    "chat_header_policy": "use_chat_bootstrap_from_demo_session",
    "quick_menu": []
  },
  "quick_menu": [
    {
      "id": "select_educacion",
      "label": "Colegios",
      "intent": "select_demo_sector",
      "sector": "educacion",
      "tenant_slug": "colegio-demo",
      "rubro": "colegios"
    },
    {
      "id": "select_gobierno",
      "label": "Gobiernos",
      "intent": "select_demo_sector",
      "sector": "gobierno",
      "tenant_slug": "municipio",
      "rubro": "municipio"
    },
    {
      "id": "select_empresas",
      "label": "Empresas",
      "intent": "select_demo_sector",
      "sector": "empresas",
      "tenant_slug": "bodega",
      "rubro": "local_comercial_general"
    }
  ]
}
```

Regla frontend:

- Si `onboarding.mode === "platform_sector_selector"`, mostrar solo 3 opciones iniciales: Colegios, Gobiernos, Empresas.
- Al tocar una opcion, llamar `POST /api/v2/demo/session` con `sector`, `tenant_slug` y `rubro`.
- Luego usar `workspace.chat_bootstrap` como fuente de verdad para endpoint, headers y payload.
- No iniciar chat generico sin sector elegido salvo que el usuario escriba texto libre; en ese caso mostrar selector compacto como sugerencia.

## 2. Widget con tenant

Cuando hay tenant/widget token, backend devuelve:

```json
{
  "onboarding": {
    "contract_version": "public.widget_onboarding.v1",
    "mode": "tenant_quick_menu",
    "entry_question": "Que necesitas resolver?",
    "quick_menu": []
  },
  "quick_menu": [],
  "media_capabilities": {},
  "conversion_ctas": {},
  "ui_hints": {}
}
```

Regla frontend:

- Renderizar `quick_menu` del backend.
- No inventar labels locales.
- Si `quick_menu` viene vacio, mostrar solo input de texto y una tarjeta minima de bienvenida.

## 3. UX/UI obligatorio del widget

Backend entrega:

```json
{
  "ui_hints": {
    "contract_version": "widget.ui_hints.v1",
    "density": "compact",
    "max_visible_quick_replies": 3,
    "collapse_extra_quick_replies": true,
    "composer": {
      "single_row_actions": true,
      "icon_buttons_only": true,
      "show_labels_on_hover": true,
      "hide_disabled_actions": true,
      "send_button_always_visible": true
    },
    "toolbar": {
      "position": "composer",
      "avoid_header_action_overload": true,
      "show": ["attach_file", "share_location", "record_audio", "emoji"],
      "collapse": ["whatsapp", "voice_call", "video_call", "catalog"]
    }
  }
}
```

Tareas frontend:

- Sacar botones permanentes del header que achican el chat.
- Dejar header simple: marca, estado, cerrar.
- Mover adjuntar, ubicacion, audio y emoji al composer como icon buttons.
- Mostrar tooltips, no labels largos dentro de botones.
- Maximo 3 quick replies visibles; el resto en menu "Mas".
- No mostrar WhatsApp, llamada, video, catalogo y acciones comerciales a la vez. Deben estar colapsadas o aparecer por contexto.
- En mobile, widget full height y composer fijo abajo.
- En desktop, ancho recomendado 420px y alto 680px.

## 4. Capacidades multimodales

Backend publica `media_capabilities.input_modes`:

- `text`: usa `payload_key: pregunta`.
- `image`: subir a `/archivos/upload/chat_attachment`, luego enviar `attachmentInfo` al chat.
- `audio`: multipart `audio_file` contra `/ask`.
- `location`: enviar `location` con `lat/lon/lng/address/accuracy`.
- `file`: subir a `/archivos/upload/chat_attachment`, luego enviar `attachmentInfo`.

Regla frontend:

- Si el browser no soporta una capacidad, ocultarla sin mostrar error.
- Si falla upload/audio/location, dejar el texto funcionando.
- Emoji es texto; no requiere endpoint backend especial.
- Video call solo se muestra si `support_channels.video_call.enabled === true`.
- Voice call solo se muestra si `support_channels.voice_call.enabled === true` y hay endpoint realtime disponible.

## 5. Realtime y consola limpia

Backend entrega:

```json
{
  "realtime": {
    "socket_enabled": false,
    "socket_url": null,
    "fallback_mode": "polling_disabled"
  }
}
```

Regla frontend:

- No conectar `/socket.io` si `realtime.socket_enabled !== true`.
- No mostrar errores de extensiones del navegador como errores Chatboc.
- Si realtime esta apagado, mostrar "modo normal" solo dentro de debug o estado discreto.

## 6. Flujo recomendado al abrir landing widget

1. `GET /api/public/widget-config`.
2. Si `platform_sector_selector`, mostrar tres opciones.
3. Usuario elige sector.
4. `POST /api/v2/demo/session`.
5. Guardar `demo_session_id`.
6. Abrir chat con bienvenida del `workspace`.
7. Enviar mensajes con:

```json
{
  "headers": {
    "X-Demo-Session-Id": "demo_session_id",
    "X-Chat-Session-Id": "demo_session_id",
    "X-Tenant-Slug": "tenant_slug"
  }
}
```

8. Renderizar `workspace.quick_replies`, `workspace.value_cards`, `media_capabilities` y `conversion_ctas`.

## 7. Que no hacer en frontend

- No hardcodear "JUNI" ni municipio default en landing global.
- No mostrar 8 botones arriba del chat.
- No conectar Socket.IO por defecto.
- No mostrar CTA de llamada/video si backend lo marca disabled.
- No dejar el selector en "Cargando demos" sin fallback local.
- No mezclar colegios, gobiernos y empresas en la misma grilla despues de elegir un pilar.

## 8. Estado backend

Implementado:

- Selector plataforma en `/api/public/widget-config` para host `chatboc.ar` sin tenant.
- Deteccion de host plataforma compatible con proxy/deploy: `Host`, `X-Forwarded-Host`, `X-Original-Host`, `X-Host`, `Origin` y `Referer`.
- `public.widget_onboarding.v1`.
- `widget.ui_hints.v1`.
- `ui_hints.accessibility` con opt-ins white-label para dislexia, texto simple, alto contraste, controles grandes, captions, reduced motion y target tactil minimo.
- `suppress_global_widget=false` fuera de integracion.
- `media_capabilities`, `conversion_ctas`, `animation_tokens` y `onboarding` top-level.
- Tests backend para contrato plataforma/tenant.

## 9. QA frontend 2026-05-12 confirmado

Backend queda alineado con el QA de onboarding del widget:

- `GET /api/public/widget-config` sin tenant en host plataforma devuelve `tenant.slug: chatboc-platform` y `tenant.tipo: platform`.
- Si el request llega desde Vercel/proxy con host interno de Render pero `X-Forwarded-Host: www.chatboc.ar`, backend tambien devuelve el selector plataforma.
- `onboarding.contract_version` es `public.widget_onboarding.v1`.
- `onboarding.mode` es `platform_sector_selector`.
- `onboarding.selection_endpoint` es `/api/v2/demo/session`.
- `onboarding.catalog_endpoint` es `/api/v2/demo/catalog`.
- `quick_menu` top-level y `onboarding.quick_menu` traen las mismas opciones.
- Cada item de `quick_menu[]` trae `label`, `sector`, `tenant_slug` y `rubro`.
- `ui_hints.contract_version` es `widget.ui_hints.v1` y `max_visible_quick_replies` queda en `3`.
- `ui_hints.accessibility` queda disponible para que frontend tenga un unico acceso compacto a preferencias inclusivas sin llenar el header.
- `realtime.socket_enabled` y `visibility_rules.allow_websocket` quedan en `false` para landing global mientras Socket.IO no este publicado.
- `support_channels.live_chat.socket_enabled` queda en `false`; frontend no debe mostrar badge Live ni intentar `/socket.io`.
- `POST /api/v2/demo/session` acepta los payloads del selector para `educacion`, `gobierno` y `empresas`, y devuelve `workspace.chat_bootstrap` con `X-Demo-Session-Id`, `X-Chat-Session-Id` y `X-Tenant-Slug`.

Verificacion backend:

- `tests/test_public_resolver_widget_config_contract.py`
- `tests/test_api_v2_foundation.py`

## 10. Runtime fix widget demo 2026-05-12

Reporte de produccion:

- El widget abre profesional, pero despues aparece el mensaje legacy: "Bienvenido al showroom interactivo..." y vuelve a pedir elegir rubro.
- El frontend intenta `GET /api/colegio-demo/live-chat/schedule`, `/colegio-demo/live-chat/schedule`, `/api/demo/live-chat/schedule` y `/demo/live-chat/schedule`, recibiendo 404/CORS.

Backend aplicado:

- `DEMO_WELCOME_MESSAGE` default ya no menciona "showroom interactivo" ni "elegi el rubro".
- `GET /api/<slug>/live-chat/schedule` y `GET /<slug>/live-chat/schedule` responden `live_chat.schedule.v1` con CORS.
- Si el tenant demo todavia no existe en la base deployada, el schedule degrada a JSON 200 con `fallback_reason=tenant_not_found_schedule_fallback`, `socket_enabled=false`, `realtime=false`, `socket_transport_hint=disabled` y `fallback_mode=http_chat`.
- Los aliases `/api/demo/live-chat/schedule` y `/demo/live-chat/schedule` quedan cubiertos cuando frontend manda `tenant_slug=colegio-demo`.

Pedido frontend:

- Si todavia aparece "Bienvenido al showroom interactivo...", revisar fallback local/cacheado del bundle: backend ya no lo emite por default.
- No reabrir selector de rubros despues de `demo.session.v2`; usar `workspace.chat_bootstrap`, `workspace.quick_replies` y `workspace.education.quick_menu`.
- No hacer fallback directo a `https://chatbot-backend-2e14.onrender.com/...` para schedule; usar same-origin y degradar con el JSON `live_chat.schedule.v1`.

Verificacion backend:

- `tests/test_tenant_leads_management.py::test_public_live_chat_schedule_aliases_never_404_for_demo_widget`
- `tests/test_config_demo_mode_flag.py`

## 11. Demo landing/widget runtime compat 2026-05-12

Reporte de produccion:

- Bundles/cache viejos todavia podian llamar `POST /api/v2/demo/session`, `/v2/demo/session`, `/api/v1/demo/session` o `/v1/demo/session`.
- En algunos deploys esas rutas viejas devolvian 404/405 o preflight CORS no OK.

Backend aplicado:

- `POST /api/v2/demo/session` mantiene `demo.session.v2` como ruta canonica.
- `POST /v2/demo/session`, `POST /api/v1/demo/session` y `POST /v1/demo/session` delegan al mismo contrato canonico.
- Los cuatro paths aceptan `OPTIONS` y responden JSON con `X-Request-Id`.
- CORS publico expone `X-Request-Id` y acepta headers de widget/demo: `X-Tenant-Slug`, `X-Widget-Token`, `X-Chat-Session-Id`, `X-Demo-Session-Id`, `X-Anon-Id`, `Anon-Id` e `Idempotency-Key`.
- Si hay error, la respuesta sigue siendo JSON accionable, no HTML ni 405 sin CORS.
- `landing.public_experience.v1` quedo saneado para copy comercial visible: no muestra palabras tecnicas como backend, contrato, endpoint, fallback, 404 o deploy en textos de cliente.

Pedido frontend:

- Mantener el flujo nuevo local-first para landing/demo publico.
- Si todavia aparecen llamadas a `/api/v1/demo/session` o `/v1/demo/session`, invalidar cache/CDN/service worker; backend las tolera por compatibilidad pero ya no deberian ser necesarias.
- En copy visible de landing/demo/widget, usar lenguaje comercial: experiencia guiada, acciones listas, seguimiento, atencion automatizada, derivacion humana, catalogos y consultas.

Verificacion backend:

- `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_demo_session_canonical_and_legacy_aliases_delegate_to_v2_with_cors`
- `tests/test_public_resolver_widget_config_contract.py::PublicResolverWidgetConfigContractTest::test_landing_experience_visible_copy_is_commercial`

## 12. Embedded widget commerce + user portal 2026-05-12

Backend aplicado:

- `GET /api/public/widget-commerce-session` devuelve `public.widget_commerce_session.v1` para que el script embebido opere como widget completo: chat, catalogo, carrito, checkout, portal e historial.
- El contrato resuelve tenant por `tenant_slug`, `tenant`, `widget_token`, `X-Tenant-Slug`, `X-Widget-Token`, `X-Chat-Session-Id` y `X-Demo-Session-Id`.
- El bundle apunta a endpoints existentes: catalogo publico, carrito PWA, catalog quality, checkout preview/session, auth/widget bootstrap e historial.
- `GET /api/public/widget-user/tenant-history` devuelve `public.widget_user_tenant_history.v1` con items de reclamos/casos, pedidos, mensajes y resumen de carrito filtrados por tenant + `anon_id`/`chat_session_id` cuando existen.
- `POST /api/public/widget-user/register` y `POST /api/public/widget-user/link-session` devuelven JSON degradable para que frontend pueda conservar carrito/historial mientras se conecta identidad real.
- El carrito anonimo se conserva usando la logica existente de `X-Anon-Id` / `X-Chat-Session-Id`; no se creo un carrito paralelo.
- El bundle embebido expone `accessibility` con los mismos defaults inclusivos para portal/catalogo/carrito.
- `GET /api/live-chat/schedule`, `/live-chat/schedule`, `/api/{tenant_slug}/live-chat/schedule` y `/{tenant_slug}/live-chat/schedule` devuelven `live_chat.schedule.v1` con `socket_enabled=false`, `socket_transport_hint=disabled` y `fallback_mode=http_chat` cuando realtime/socket no esta publicado.
- `/api/ask/*` ya no debe emitir el selector legacy cuando llegan marcadores de contexto (`X-Demo-Session-Id`, `X-Chat-Session-Id`, `X-Tenant-Slug`, `tenant_slug`, `demo_session_id`, `chat_bootstrap.payload.rubro` o `chat_bootstrap.payload.rubro_clave`).

Pedido frontend:

- El script embebido debe consultar `GET /api/public/widget-commerce-session` apenas tenga `widget_token` o `tenant_slug`.
- Si `frontend_contract.render_as=embedded_tenant_operating_widget`, renderizar chat primero, y catalogo/carrito/portal como acciones compactas.
- Usar `portal.history_endpoint` para mostrar historial del visitante y mantener el carrito visible si `cart.items_count > 0`.
- Si `live_chat.socket_enabled=false`, no abrir Socket.IO ni mostrar badge Live.
- No volver al selector de rubro cuando ya exista `chat_bootstrap`, `tenant_slug`, `entityToken`, `X-Chat-Session-Id` o rubro efectivo.

Verificacion backend:

- `tests/test_public_tenant_catalog_alias.py::test_widget_commerce_session_returns_embedded_operating_contract`
- `tests/test_public_tenant_catalog_alias.py::test_widget_user_tenant_history_returns_cart_claims_and_orders`
- `tests/test_public_tenant_catalog_alias.py::test_widget_user_register_and_link_session_are_degradable_json`
- `tests/test_tenant_leads_management.py::test_public_live_chat_schedule_aliases_never_404_for_demo_widget`

## 13. Public demo route QA 2026-05-12

Backend aplicado:

- Los slugs publicos de marketing (`demo`, `casos`, `pymes`, `empresas`, `municipios`, `gobiernos`, `colegios`, `sectores`, `precios`, `opinar`, etc.) ya no se resuelven como tenants reales en endpoints publicos tenant-aware.
- `GET /api/public/tenants/{slug}/catalog` y `/public/tenants/{slug}/catalog` devuelven `public.catalog_resolution.v1` con `items: []`, `cart.enabled=false`, `request_id` y CORS OK cuando el slug es reservado o no resuelve tenant.
- `GET /api/public/tenants/{tenant_slug}/public-navigation` y `/public/tenants/{tenant_slug}/public-navigation` devuelven `tenant.public_navigation.v1` con items habilitados/deshabilitados para que frontend no navegue a 404.
- `GET /api/v2/demo/admin-preview?sector=educacion|gobierno|empresas&tenant_slug=...` devuelve `demo.admin_preview.v1` con modulos, cards, timeline y catalogo demo.
- `GET /api/v2/demo/catalog-assets/{archivo}.pdf` sirve aliases de catalogos demo para colegios, gobiernos y empresas.
- `GET /api/public/tenants/{tenant_slug}/live-chat/schedule` y `/public/tenants/{tenant_slug}/live-chat/schedule` quedan cubiertos por `live_chat.schedule.v1` degradable.

Pedido frontend:

- Mantener slugs de marketing como rutas publicas o redirects a `/demo`; no tratarlos como `tenant_slug`.
- Usar `public-navigation` para ocultar/deshabilitar botoneras publicas cuando el modulo no esta disponible.
- Usar `demo.admin_preview.v1` para la demo integrada de colegio/municipio/empresa en lugar de hardcodear cards y timeline.
- Si un catalogo publico responde `public.catalog_resolution.v1`, mostrar estado vacio limpio y no reintentar contra Render directo.
- Si un slug reservado responde `public.reserved_slug.v1`, redirigir o sugerir `/demo` sin mostrar error tecnico.

Verificacion backend:

- `tests/test_public_tenant_catalog_alias.py::test_reserved_public_slug_catalog_degrades_to_json`
- `tests/test_public_tenant_catalog_alias.py::test_public_navigation_contract_disables_unavailable_items`
- `tests/test_public_tenant_catalog_alias.py::test_reserved_public_slug_navigation_returns_reserved_json`
- `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_v2_demo_admin_preview_returns_sector_contract`
- `tests/test_api_v2_foundation.py::ApiV2FoundationTest::test_v2_demo_catalog_asset_alias_serves_pdf`
