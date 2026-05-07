# Backend to Frontend Sync - Realtime Voice 2026-05-07

Objetivo: mejorar llamadas de WhatsApp/telefono con voz nativa Realtime, sin volver a armar STT -> LLM -> TTS. La integracion se hizo sobre la base existente: Twilio Voice/Media Streams en `/twilio/voice/inbound` y `/twilio/voice/stream`, mas sesiones WebRTC del widget en `/api/public/realtime/session`.

## Decisiones backend

- Modelo recomendado por defecto: `gpt-realtime-2`.
- Fallback operativo: `gpt-realtime-1.5`.
- Voz default: `marin`, configurable por tenant con `openai_realtime_voice`.
- No se depreca V1 ni se duplica app: se mejora el stream actual y se agregan contratos publicos.
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
  "fallback_model": "gpt-realtime-1.5",
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
    "server_vad": true,
    "tool_calling": true,
    "whatsapp_followup": true,
    "post_call_receipt": true,
    "human_handoff": true
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

1. En demo/widget/landing, mostrar opcion "Llamar ahora" o "Probar llamada IA" cuando `realtime_voice` venga habilitado.
2. Mostrar badges de confianza: "Voz en tiempo real", "Interrupciones naturales", "Resumen por WhatsApp", "Derivacion humana".
3. Para colegios, usar starters:
   - "Justificar inasistencia"
   - "Consultar secretaria"
   - "Hablar con el colegio"
4. Para pymes, usar starters:
   - "Consultar disponibilidad"
   - "Tomar pedido por voz"
   - "Hablar con ventas"
5. Para municipios, usar starters:
   - "Iniciar reclamo"
   - "Consultar tramite"
   - "Hablar con operador"
6. Si el modelo devuelve/solicita herramienta desde WebRTC, frontend debe convertirlo en action-event o mantener la UI en estado "derivando/registrando" hasta que backend confirme.

## Backend implementado

- `services/realtime_voice_profiles.py`: perfiles, tools y contrato por vertical.
- `services/voice_stream_service.py`: stream Twilio -> OpenAI Realtime con `gpt-realtime-2`, herramientas por vertical y `crear_caso_escolar`.
- `services/realtime_session_service.py`: sesiones WebRTC legacy actualizadas a modelo realtime actual y voz configurable.
- `routes/public_resolver.py`: contrato publico de capacidades y widget config enriquecido.
- Tests: `tests/test_realtime_voice_profiles.py` y cobertura publica en `tests/test_public_resolver_widget_config_contract.py`.
