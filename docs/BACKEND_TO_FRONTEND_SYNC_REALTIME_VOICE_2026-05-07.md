# Backend to Frontend Sync - Realtime Voice 2026-05-07

Objetivo: mejorar llamadas de WhatsApp/telefono con voz nativa Realtime, sin volver a armar STT -> LLM -> TTS. La integracion se hizo sobre la base existente: Twilio Voice/Media Streams en `/twilio/voice/inbound` y `/twilio/voice/stream`, mas sesiones WebRTC del widget en `/api/public/realtime/session`.

La continuidad PSTN/WhatsApp usa `channel.session_identity.v1`: un binding HMAC
por tenant/canal/proveedor y un UUID aleatorio. Voz no reconstruye IDs globales
con teléfono y sólo copia contexto WhatsApp cuando el binding del mismo tenant
e identidad queda verificado. Producción exige `CHANNEL_SESSION_IDENTITY_MODE=enforce`
antes de habilitar el lifecycle realtime; ver `docs/channel-session-identity-runbook.md`.

## Seguridad de Twilio Media Streams

- El webhook TwiML firma un envelope `v1` con `CallSid`, origen, destino, tenant, vertical, intent, sesion, modo demo, duracion maxima, timestamp y nonce.
- `VOICE_STREAM_SIGNING_SECRET` (minimo 32 bytes) es la clave preferida. Si falta, se deriva una clave separada por dominio desde `TWILIO_AUTH_TOKEN`; el token nunca se usa directamente ni se envia en TwiML.
- El WebSocket exige `connected`/`start` dentro de un numero y timeout acotados. Firma, TTL, campos de transporte, tenant y consumo one-time se validan antes de abrir OpenAI Realtime.
- El nonce se consume atomica y globalmente con Redis `SET NX EX`. El store se resuelve desde `VOICE_STREAM_REPLAY_REDIS_URL`, `SOCKETIO_MESSAGE_QUEUE_URL`, `SOCKETIO_REDIS_URL` o un `CELERY_BROKER_URL` explicitamente definido en el entorno.
- En runtime, un store ausente o caido rechaza el stream (`replay_store_missing` / `replay_store_unavailable`): no existe fallback local y no se abre OpenAI.
- El store en memoria solo puede habilitarse con `TESTING=true` y `VOICE_STREAM_REPLAY_ALLOW_IN_MEMORY_TEST_STORE=true`; no ofrece ni pretende ofrecer garantia entre procesos.
- Limites operativos por defecto: `VOICE_STREAM_ENVELOPE_TTL_SECONDS=120`, `VOICE_STREAM_PREFLIGHT_TIMEOUT_SECONDS=5`, `VOICE_STREAM_PREFLIGHT_MAX_EVENTS=3` y `VOICE_STREAM_PREFLIGHT_MAX_MESSAGE_BYTES=32768`.

## Consentimiento y lifecycle de llamadas v1

- `ENABLE_VOICE_CONSENT_LIFECYCLE_V1` es un opt-in estricto y queda `false` en
  Render hasta aplicar `20260730_voice_consent_v1`, configurar Redis y completar
  staging. Apagado, faltante o inválido significa cero `<Connect><Stream>` y cero
  conexión a OpenAI.
- Cada tenant debe declarar los tres campos de política, sin defaults implícitos:
  `voice_consent_policy={"version":"voice.consent.v1","ai_processing":"explicit_per_call","recording":"disabled"}`.
  Un objeto ausente o incompleto falla cerrado.
- El webhook firmado registra `received` y `consent_pending`, y pregunta mediante
  `Gather input="dtmf"`. No se habilita reconocimiento de voz para capturar la
  decisión. `1` se traduce en memoria a `granted`; `2` a `declined`. El dígito,
  teléfonos, audio y transcripciones nunca se guardan en el ledger ni se escriben
  en logs.
- Cada consentimiento queda unido a `tenant_id + CallSid + policy_version`, y
  `(provider, CallSid)` es globalmente único: un callback no puede re-vincular
  la identidad de una llamada a otro tenant. El
  WebSocket vuelve a cargar ese grant desde base de datos después de validar y
  consumir el envelope, y antes de resolver contexto o ejecutar `ws_connect`.
  HMAC no equivale a consentimiento. Además reclama atómicamente el CallSid con
  un evento único `stream:claimed`: aunque Twilio reintente el webhook y reciba
  otro envelope/nonce válido, una segunda conexión no puede abrir otro puente.
  La transacción vuelve a cargar y bloquear la política actual: conservar la
  misma versión pero cambiar `ai_processing` a `disabled` revoca el grant.
- Estados persistidos: `received`, `consent_pending`, `stream_authorized`,
  `completed` y `failed`. Los eventos son append-only e idempotentes; un terminal
  no se reabre por callbacks fuera de orden. `answered`, `ringing` o el ACK de una
  creación Twilio no se presentan como `connected` ni `completed`.
- Grabación queda deshabilitada dos veces: la política v1 sólo acepta
  `recording=disabled` y la base impide `recording_allowed=true` o
  `recording_enabled=true`. Este slice no implementa grabación.
- Timeout o decisión ausente se materializan como `declined`, igual que un
  rechazo explícito. Policy version distinta, tenant cruzado o persistencia
  caída también terminan sin puente de IA. Ninguna de estas negativas puede
  revertirse dentro de la misma llamada.
- La transferencia sólo emite `<Dial>` si la llamada ya tiene grant y el destino
  E.164 coincide exactamente con `human_handoff_number`/`telefono_atencion` del
  tenant. El endpoint no acepta un número libre como autoridad.
- El DTMF v1 se limita a consentimiento. No es todavía un IVR general. Los
  callbacks terminales actualizan el ledger, pero la certificación real
  PSTN/WhatsApp Business Calling sigue pendiente de staging con Twilio.

## Decisiones backend

- Modelo recomendado por defecto: `gpt-realtime`.
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
  "recommended_model": "gpt-realtime",
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
- `realtime_voice.phone_consent.rollout_enabled === true`
- `realtime_voice.transports.media_streams_ready === true`

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
  "model": "gpt-realtime",
  "fallback_model": "gpt-realtime",
  "voice": "marin",
  "transport": "webrtc",
  "profile": "realtime_voice_native",
  "active_vertical": "municipio"
}
```

Backend resuelve `model` desde tenant/env para evitar que un frontend viejo use un preview/beta; si frontend manda un modelo anterior, queda registrado como `requested_model_ignored`. `voice`, `transport`, `profile` y `active_vertical` se mantienen como metadata operativa.

## Verticales soportadas

Municipios:

- Crear reclamo por voz.
- Consultar estado de reclamo por voz.
- Consultar tramites/turnos/requisitos por voz.
- Pedir solo datos faltantes.
- Separar descripcion de ubicacion.
- Enviar comprobante/resumen por WhatsApp.
- Derivar humano en urgencias o enojo.

PyMEs:

- Venta consultiva.
- Consultar producto/servicio.
- Crear pedido confirmado.
- Consultar estado de pedido.
- Confirmar entrega/retiro y contacto.
- No inventar precios o stock.

Colegios:

- Crear `crear_caso_escolar` por llamada.
- Consultar `consultar_caso_escolar` por llamada.
- Casos: secretaria, preceptoria, inasistencia, comunicados, agenda, documentacion, cobranza, admisiones, convivencia, mantenimiento, tecnologia, transporte y comedor.
- Vincula a `SchoolCaseAlias` cuando hay colegio/familia resoluble.
- Envia resumen por WhatsApp y permite sumar imagen/audio/archivo luego.
- Convivencia, salud, retiro de alumnos o temas sensibles deben mostrar handoff humano.

## Pedido para frontend

1. En demo/widget/landing, mostrar opcion "Llamar ahora" o "Probar llamada IA" solo cuando `realtime_voice.features.tool_calling === true` y `support_channels.voice_call.enabled === true`.
2. Badges y starters son backend-first: usar `trust_badges`, `badges`, `badge_labels`, `starter_messages`, `voice_starters` o `starters` si llegan. Si no llegan, no inventar starters por vertical.
3. Si el modelo devuelve/solicita herramienta desde WebRTC, frontend debe convertirlo en `POST /api/public/realtime/action-event` o mantener la UI en estado "derivando/registrando" hasta que backend confirme.
4. Para videollamada, mostrarla como canal visual solo si `support_channels.video_call.enabled === true`; no prometer analisis de video en vivo hasta que backend publique una capacidad explicita `live_video_analysis: true`.
5. En llamadas web, mostrar captions/estado corto: "escuchando", "procesando", "registrando", "comprobante enviado". No mostrar menus largos ni logs tecnicos.

## Backend implementado

- `services/realtime_voice_profiles.py`: perfiles, tools y contrato por vertical; incluye crear/consultar para reclamos, pedidos y casos escolares.
- `services/voice_stream_service.py`: stream Twilio -> OpenAI Realtime con `gpt-realtime` por defecto, schema actual `output_modalities` + `audio`, herramientas por vertical, consultas de estado y `crear_caso_escolar`.
- `services/realtime_session_service.py`: sesiones WebRTC actualizadas a `/v1/realtime/client_secrets`, modelo realtime actual y voz configurable.
- `routes/public_resolver.py`: contrato publico de capacidades y widget config enriquecido.
- `POST /api/public/realtime/session` usa `client_secrets.v2`, schema actual y respeta campos visuales/contextuales enviados por frontend (`voice`, `transport`, `profile`, `active_vertical`) sin permitir downgrade de modelo.
- Tests: `tests/test_realtime_voice_profiles.py`, `tests/test_public_resolver_widget_config_contract.py` y `tests/test_widget_settings.py`.
