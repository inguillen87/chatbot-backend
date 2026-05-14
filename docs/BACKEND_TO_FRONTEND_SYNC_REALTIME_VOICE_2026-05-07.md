# Backend to Frontend Sync - Realtime Voice 2026-05-07

Objetivo: mejorar llamadas de WhatsApp/telefono con voz nativa Realtime, sin volver a armar STT -> LLM -> TTS. La integracion se hizo sobre la base existente: Twilio Voice/Media Streams en `/twilio/voice/inbound` y `/twilio/voice/stream`, mas sesiones WebRTC del widget en `/api/public/realtime/session`.

## Decisiones backend

- Modelo recomendado por defecto: `gpt-realtime-2`.
- Fallback operativo configurable: por defecto `gpt-realtime`.
- Voz default: `marin`, configurable por tenant con `openai_realtime_voice`.
- No se usa el endpoint deprecated `/v1/realtime/sessions`: el backend genera credenciales efimeras con `/v1/realtime/client_secrets`.
- El telefono usa audio `g711_ulaw` para Twilio Media Streams.
- El widget/browser sigue usando WebRTC desde `/api/public/realtime/session`.

## Contrato nuevo para frontend

Endpoint:

`GET /api/public/realtime/voice-capabilities`

Query params opcionales:

- `tenant`
- `tenant_slug`
- `slug`
- `widget_token`

Respuesta:

```json
{
  "contract_version": "realtime.voice_capabilities.v1",
  "provider": "openai_realtime",
  "recommended_model": "gpt-realtime-2",
  "fallback_model": "gpt-realtime",
  "voice": "marin",
  "active_vertical": "municipio|pyme|colegio|general",
  "native_speech_to_speech": true,
  "avoid_external_stt_tts_loop": true,
  "transports": {
    "browser": "webrtc",
    "server": "websocket",
    "phone_bridge": "twilio_media_streams",
    "sip_ready": true
  },
  "features": {
    "barge_in": true,
    "semantic_vad": true,
    "server_vad": false,
    "tool_calling": true,
    "whatsapp_followup": true,
    "post_call_receipt": true,
    "human_handoff": true
  },
  "support_channels": {
    "voice_call": {
      "enabled": true,
      "channel": "voice_call",
      "provider": "openai_realtime",
      "session_endpoint": "/api/public/realtime/session"
    }
  }
}
```

Tambien llega en:

- `GET /api/public/widget-config` como `realtime_voice`.
- `widget.support_channels.voice_call.capabilities`.
- `builder_config.enterprise_iteration.realtime`.

## Widget config agregado

Nuevos atributos de embed:

- `data-realtime-model`
- `data-realtime-fallback-model`
- `data-realtime-voice`
- `data-realtime-transport`
- `data-realtime-profile`
- `data-realtime-voice-enabled`
- `data-realtime-video-enabled`

Frontend deberia renderizar CTA de llamada solo si:

- `realtime_voice.features.tool_calling === true`
- `support_channels.voice_call.enabled === true`

Si voz esta apagada por tenant, backend responde HTTP 200 degradable:

```json
{
  "contract_version": "realtime.voice_capabilities.v1",
  "enabled": false,
  "reason_code": "voice_not_enabled",
  "features": {
    "tool_calling": false
  },
  "support_channels": {
    "voice_call": {
      "enabled": false
    }
  },
  "request_id": "req_123"
}
```

`POST /api/public/realtime/session` acepta los campos que frontend toma del contrato publico:

```json
{
  "tenant_slug": "municipio",
  "widget_token": "...",
  "channel": "voice",
  "model": "gpt-realtime-2",
  "fallback_model": "gpt-realtime",
  "voice": "marin",
  "transport": "webrtc",
  "profile": "realtime_voice_native",
  "active_vertical": "municipio"
}
```

Backend resuelve `model` desde tenant/env para evitar que un frontend viejo degrade a `gpt-realtime`; si frontend manda un modelo anterior, queda registrado como `requested_model_ignored`. `voice`, `transport`, `profile` y `active_vertical` se mantienen como metadata operativa.

## Verticales soportadas

Municipios:

- Crear reclamo por voz.
- Pedir solo datos faltantes.
- Separar descripcion de ubicacion.
- Enviar comprobante/resumen por WhatsApp.
- Derivar humano en urgencias o enojo.

PyMEs:

- Venta consultiva.
- Consultar producto/servicio.
- Crear pedido confirmado.
- Confirmar entrega/retiro y contacto.
- No inventar precios o stock.

Colegios:

- Crear `crear_caso_escolar` por llamada.
- Casos: secretaria, preceptoria, inasistencia, comunicados, agenda, documentacion, cobranza, admisiones, convivencia, mantenimiento, tecnologia, transporte y comedor.
- Vincula a `SchoolCaseAlias` cuando hay colegio/familia resoluble.
- Envia resumen por WhatsApp y permite sumar imagen/audio/archivo luego.
- Convivencia, salud, retiro de alumnos o temas sensibles deben mostrar handoff humano.

## Pedido para frontend

1. En demo/widget/landing, mostrar opcion "Llamar ahora" o "Probar llamada IA" solo cuando `realtime_voice.features.tool_calling === true` y `support_channels.voice_call.enabled === true`.
2. Badges y starters son backend-first: usar `trust_badges`, `badges`, `badge_labels`, `starter_messages`, `voice_starters` o `starters` si llegan. Si no llegan, no inventar starters por vertical.
3. Si el modelo devuelve/solicita herramienta desde WebRTC, frontend debe convertirlo en `POST /api/public/realtime/action-event` o mantener la UI en estado "derivando/registrando" hasta que backend confirme.

## Backend implementado

- `services/realtime_voice_profiles.py`: perfiles, tools y contrato por vertical.
- `services/voice_stream_service.py`: stream Twilio -> OpenAI Realtime con `gpt-realtime-2` por defecto, schema actual `output_modalities` + `audio`, herramientas por vertical y `crear_caso_escolar`.
- `services/realtime_session_service.py`: sesiones WebRTC actualizadas a `/v1/realtime/client_secrets`, modelo realtime actual y voz configurable.
- `routes/public_resolver.py`: contrato publico de capacidades y widget config enriquecido.
- `POST /api/public/realtime/session` usa `client_secrets.v2`, schema actual y respeta campos visuales/contextuales enviados por frontend (`voice`, `transport`, `profile`, `active_vertical`) sin permitir downgrade de modelo.
- Tests: `tests/test_realtime_voice_profiles.py`, `tests/test_public_resolver_widget_config_contract.py` y `tests/test_widget_settings.py`.
