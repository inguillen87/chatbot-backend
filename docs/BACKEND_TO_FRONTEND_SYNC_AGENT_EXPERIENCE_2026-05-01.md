# Backend to Frontend Sync - Agent Experience 2026-05-01

Fecha: 2026-05-01

Objetivo: mejorar la experiencia del chatbot/agente IA para primera visita, demos comerciales y widget sin hardcodear copy en React. Backend mantiene rutas legacy y suma campos aditivos sobre contratos existentes.

## Regla de arquitectura

No crear una app paralela. Frontend debe leer `experience_blueprint`, `workspace`, `builder_config` y `quick_menu` desde backend. Si falta un campo, degradar con estado vacio, no inventar textos por municipio/pyme dentro de componentes.

## Backend listo

### LLM multimodal

`services/chatbot_prompts.py` ya incluye reglas compartidas para que municipio y pyme traten `uploaded_file_info`, `datos_interpretados_archivo`, `transcribed_text` y `ubicacion_compartida` como contexto accionable.

Frontend puede confiar en estos flujos:

- Nota de voz: backend responde sobre la transcripcion y no pide que el usuario escriba de nuevo.
- Imagen/documento: backend usa descripcion, categoria o texto extraido para avanzar hacia ticket, pedido, presupuesto o handoff.
- Ubicacion: backend confirma zona/direccion y la usa para ticket, envio o retiro.
- Alta intencion: backend puede ofrecer ticket, pedido, checkout, humano o lead.

### Demo session

Endpoint:

`POST /api/v2/demo/session`

Contrato existente:

`demo.session.v2`

Campos nuevos aditivos:

```json
{
  "workspace": {
    "subtitle": "...",
    "first_visit": {},
    "sample_conversations": [],
    "trust_signals": [],
    "lead_capture": {},
    "media_capabilities": {},
    "conversion_ctas": {},
    "animation_tokens": {},
    "chat_bootstrap": {}
  },
  "chat_bootstrap": {},
  "experience_blueprint": {
    "version": "2026-05-agent-experience-v2",
    "agent_persona": {},
    "first_visit": {},
    "quick_actions": [],
    "sample_conversations": [],
    "trust_signals": [],
    "lead_capture": {},
    "media_capabilities": {},
    "conversion_ctas": {},
    "animation_tokens": {},
    "empty_states": {},
    "agent_copilot": {},
    "component_pack": {}
  },
  "chat_seed": {
    "starter_prompts": [],
    "sample_conversations": [],
    "chat_bootstrap": {}
  }
}
```

### Demo chat bootstrap

`POST /api/v2/demo/session` ahora devuelve `chat_bootstrap` top-level, dentro de `workspace` y dentro de `chat_seed`.

Contrato:

```json
{
  "contract_version": "demo.chat_bootstrap.v1",
  "endpoint": "/ask/pyme",
  "fallback_endpoint": "/ask",
  "method": "POST",
  "headers": {
    "X-Chat-Session-Id": "demo_session_id",
    "X-Demo-Session-Id": "demo_session_id",
    "X-Tenant-Slug": "tenant-slug"
  },
  "query": {
    "tenant_slug": "tenant-slug"
  },
  "payload": {
    "pregunta": "",
    "tipo_chat": "pyme",
    "tenant_slug": "tenant-slug",
    "rubro": "bodega",
    "rubro_clave": "bodega",
    "demo_session_id": "demo_session_id",
    "demo_mode": true
  },
  "context": {
    "sector": "empresas",
    "rubro": "bodega",
    "tenant_slug": "tenant-slug",
    "tenant_tipo": "pyme"
  },
  "start_event": {
    "type": "demo_chat_start",
    "tenant_slug": "tenant-slug",
    "tipo_chat": "pyme",
    "rubro": "bodega"
  },
  "initial_prompt": "Quiero hacer una consulta",
  "supports": {
    "text": true,
    "image": true,
    "audio": true,
    "location": true,
    "file": true
  }
}
```

Regla frontend:

- Elegir rubro -> llamar `POST /api/v2/demo/session` -> abrir chat usando `chat_bootstrap.endpoint`, `headers`, `query` y `payload`.
- Para municipio el endpoint esperado es `/ask/municipio`; para pyme es `/ask/pyme`; si frontend no reconoce algo, usar `fallback_endpoint`.
- No reconstruir `tipo_chat`, `tenant_slug`, `rubro` ni `X-Chat-Session-Id` localmente.

### Widget config

Endpoints:

`GET /api/public/widget-config?tenant={slug}`

`GET /api/public/tenants/{slug}/widget-config`

Contrato:

`public.widget_config.v1`

Campos nuevos dentro de `widget` y `builder_config`:

```json
{
  "widget": {
    "experience_blueprint": {},
    "first_visit": {},
    "sample_conversations": [],
    "trust_signals": [],
    "lead_capture": {},
    "media_capabilities": {},
    "conversion_ctas": {},
    "animation_tokens": {}
  },
  "builder_config": {
    "experience_blueprint": {},
    "first_visit": {},
    "sample_conversations": [],
    "trust_signals": [],
    "lead_capture": {},
    "media_capabilities": {},
    "conversion_ctas": {},
    "animation_tokens": {}
  }
}
```

## Campos para UX frontend

### first_visit

Usar como primera pantalla dentro del widget/demo cuando el usuario aun no envio mensajes.

```json
{
  "headline": "Hola, soy tu asistente comercial.",
  "subheadline": "Puedo mostrar productos, responder dudas y ayudarte a comprar.",
  "primary_action": { "id": "browse_catalog", "label": "Ver catalogo", "intent": "ver_catalogo" },
  "secondary_action": { "id": "talk_sales", "label": "Hablar con ventas", "intent": "derivar_humano" },
  "steps": []
}
```

### sample_conversations

Renderizar como chips/tarjetas que prellenan el input o disparan quick action.

```json
{
  "id": "sample_order",
  "title": "Pedido guiado",
  "user_message": "Quiero comprar 3 unidades y coordinar envio.",
  "assistant_goal": "Armar pedido, validar contacto y preparar checkout.",
  "intent": "crear_pedido"
}
```

### trust_signals

Renderizar como fila compacta de confianza. No usar hero gigante.

```json
{
  "id": "human_backup",
  "label": "Humano disponible",
  "detail": "El agente sabe cuando derivar a ventas o soporte."
}
```

### lead_capture

Disparar cuando haya alta intencion: checkout, pedido, derivar humano, reclamo, consulta estado.

Endpoint:

`POST /api/public/lead-capture`

Campos:

```json
{
  "enabled": true,
  "title": "Recibir propuesta o continuar compra",
  "fields": [],
  "trigger_intents": ["crear_pedido", "derivar_humano", "checkout_intent"],
  "endpoint": "/api/public/lead-capture",
  "success_message": "Listo, dejamos tu consulta preparada para seguimiento."
}
```

### media_capabilities

Usar como fuente de verdad para el composer del chat. No mostrar botones de imagen/audio/ubicacion/archivo si el modo viene deshabilitado.

```json
{
  "version": "media.capabilities.v1",
  "composer": {
    "placeholder": "Escribi, habla o adjunta algo para que el agente te ayude.",
    "actions": [
      { "id": "attach_image", "type": "image", "icon": "image", "label": "Imagen" },
      { "id": "record_audio", "type": "audio", "icon": "mic", "label": "Audio" },
      { "id": "share_location", "type": "location", "icon": "map-pin", "label": "Ubicacion" },
      { "id": "attach_file", "type": "file", "icon": "paperclip", "label": "Archivo" }
    ],
    "states": ["idle", "recording", "uploading", "transcribing", "thinking", "needs_confirmation", "success", "handoff"]
  },
  "input_modes": {
    "text": { "enabled": true, "chat_endpoint": "/ask", "payload_key": "pregunta" },
    "image": {
      "enabled": true,
      "upload_endpoint": "/archivos/upload/chat_attachment",
      "upload_response_key": "attachmentInfo",
      "chat_payload_key": "attachmentInfo"
    },
    "audio": {
      "enabled": true,
      "chat_endpoint": "/ask",
      "multipart_field": "audio_file",
      "max_seconds": 120
    },
    "location": {
      "enabled": true,
      "chat_endpoint": "/ask",
      "payload_key": "location",
      "fields": ["lat", "lon", "latitude", "longitude", "lng", "address", "accuracy"]
    },
    "file": {
      "enabled": true,
      "upload_endpoint": "/archivos/upload/chat_attachment",
      "upload_response_key": "attachmentInfo",
      "chat_payload_key": "attachmentInfo"
    }
  }
}
```

Flujo recomendado:

1. Imagen/archivo: subir a `/archivos/upload/chat_attachment`, leer `attachmentInfo`, mandar luego `/ask` con `attachmentInfo`.
2. Audio/nota de voz: mandar multipart a `/ask` con campo `audio_file`; backend transcribe y responde como chat normal.
3. Ubicacion: mandar `/ask` JSON con `location` usando `lat/lon` o `latitude/longitude`; backend normaliza aliases.
4. Texto: mandar `/ask` con `pregunta`.
5. En todos los casos enviar `X-Chat-Session-Id`; cuando haya tenant, enviar `X-Tenant-Slug`.

### conversion_ctas

Renderizar como barra contextual debajo del ultimo mensaje o dentro de cards de accion. Backend manda labels, intents y endpoints.

```json
{
  "version": "conversion.ctas.v1",
  "actions": [
    {
      "id": "create_order",
      "label": "Crear pedido",
      "intent": "crear_pedido",
      "endpoint": "/ask",
      "show_when": ["catalog_item_selected", "price_question", "high_intent"],
      "style": "primary"
    },
    {
      "id": "capture_lead",
      "label": "Recibir propuesta",
      "intent": "lead_capture",
      "endpoint": "/api/public/lead-capture",
      "show_when": ["demo_interest", "pricing_interest", "abandoned_checkout"],
      "style": "accent"
    }
  ],
  "rules": {
    "max_visible": 3,
    "prefer_backend_labels": true,
    "fallback_behavior": "hide_missing_actions",
    "preserve_context_on_click": true
  }
}
```

### animation_tokens

Usar para microinteracciones profesionales del widget/demo sin acoplarse a CSS backend.

```json
{
  "version": "chat.motion.v1",
  "respect_reduced_motion": true,
  "motion_level": "polished",
  "events": [
    { "id": "launcher_open", "trigger": "widget.open", "pattern": "spring_scale", "duration_ms": 180 },
    { "id": "recording", "trigger": "audio.recording", "pattern": "level_meter", "duration_ms": 0 },
    { "id": "uploading_media", "trigger": "media.uploading", "pattern": "progress_shimmer", "duration_ms": 0 },
    { "id": "location_shared", "trigger": "location.shared", "pattern": "pin_drop", "duration_ms": 220 },
    { "id": "lead_success", "trigger": "lead.created", "pattern": "success_check", "duration_ms": 260 }
  ]
}
```

### empty_states

Usar para offline, sin mensajes, esperando humano y limite demo. Evita textos locales.

### component_pack

No es obligatorio copiar nombres exactos de componentes, pero si respetar secciones:

- `hero`
- `first_visit`
- `quick_actions`
- `sample_conversations`
- `media_composer`
- `channels`
- `conversion_ctas`
- `trust_signals`
- `lead_capture`
- `upsell`

## Tareas frontend recomendadas

1. Widget: si no hay mensajes, renderizar `builder_config.first_visit` antes que una pantalla vacia.
2. Widget/demo: renderizar `sample_conversations` como ejemplos clickeables que prellenan input y conservan `intent`.
3. Widget/demo: mostrar `trust_signals` debajo del primer bloque, compacto y profesional.
4. Demo workspace: consumir `workspace.first_visit`, `workspace.sample_conversations` y `experience_blueprint.component_pack`.
5. Lead capture: al detectar intent de alta intencion, abrir form con `lead_capture.fields` y enviar a `lead_capture.endpoint`.
6. Empty/offline: reemplazar copy local por `experience_blueprint.empty_states`.
7. Inbox agente: usar `experience_blueprint.agent_copilot.suggestions` como botones sugeridos para operador humano.
8. Composer multimedia: leer `media_capabilities.composer.actions`, usar iconos reales y tooltips; ocultar accion si `input_modes[type].enabled` es false.
9. Audio UX: estado `recording -> transcribing -> thinking`; enviar multipart `audio_file` a `/ask`.
10. Imagen/archivo UX: vista previa, progreso, cancelacion y retry; enviar primero `/archivos/upload/chat_attachment` y luego `/ask` con `attachmentInfo`.
11. Ubicacion UX: permiso claro del navegador, preview de direccion/mapa y retry; enviar `location`.
12. Conversion CTAs: renderizar maximo `conversion_ctas.rules.max_visible` acciones, con labels del backend.
13. Animaciones: mapear `animation_tokens.events` a microinteracciones; respetar `prefers-reduced-motion`.
14. Landing/demo: usar los mismos campos que widget para que elegir rubro -> empezar chat sea una experiencia continua.
15. No hardcodear labels por rubro en React. Si falta copy, mostrar estado neutro o pedir backend.

## Tests backend verdes

- `tests.test_demo_experience_contract`
- `tests.test_api_v2_foundation`
- `tests.test_public_resolver`
- `tests.test_public_resolver_quick_menu`
- `tests.test_pwa_public_cart_url`
