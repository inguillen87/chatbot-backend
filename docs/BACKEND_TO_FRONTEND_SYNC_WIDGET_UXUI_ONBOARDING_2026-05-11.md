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
    "chat_header_policy": "use_chat_bootstrap_from_demo_session"
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
- `public.widget_onboarding.v1`.
- `widget.ui_hints.v1`.
- `suppress_global_widget=false` fuera de integracion.
- `media_capabilities`, `conversion_ctas`, `animation_tokens` y `onboarding` top-level.
- Tests backend para contrato plataforma/tenant.
