# Frontend handoff — Realtime Avatar + UX/UI Web 4.0

## Objetivo
Habilitar en el widget web una experiencia multimodal en tiempo real (chat + llamada + videollamada con avatar robótico) con soporte inclusivo para usuarios que no quieren/no pueden escribir o enviar notas de voz.

## Estado backend entregado
El backend ya expone metadata para integrar **OpenAI Realtime (gpt-realtime-1.5)** en la configuración pública del widget.

### 1) Nuevos canales en `support_channels`
En `GET /api/public/widget-config?tenant=<slug>` ahora llegan:
- `support_channels.voice_call`
- `support_channels.video_call`

Campos relevantes:
- `enabled`
- `provider = "openai_realtime"`
- `model = "gpt-realtime-1.5"` (o override por tenant)
- `features` (barge-in, captions, accesibilidad, transferencia humano)

### 2) Nuevos atributos para el script/widget
En `widget.attributes` ahora llegan:
- `data-realtime-model`
- `data-realtime-voice-enabled`
- `data-realtime-video-enabled`
- `data-avatar-enabled`
- `data-avatar-type`
- `data-avatar-persona`

Con esto frontend puede activar/desactivar UX sin hardcode.
Además, `builder_config.enterprise_iteration` expone endpoints/QA checklist para planificar releases frontend enterprise.

### 3) Nuevo endpoint backend para sesión realtime
`POST /api/public/realtime/session`

Body recomendado:
```json
{
  "tenant_slug": "mi-tenant",
  "channel": "voice"
}
```

Respuesta:
- `session`: payload de OpenAI Realtime Sessions (incluye credenciales efímeras/client secret).
- `model`: modelo efectivo (default `gpt-realtime-1.5`).
- `avatar`: metadata de avatar/persona para el runtime visual.

Fallbacks:
- Si `OPENAI_API_KEY` no está configurada => `503 openai_api_key_missing`.
- Si `channel=video` y tenant no lo habilitó => `400 video_realtime_disabled`.
- Si `widget_token` inválido => `403 widget_token_invalid`.
- Si excede límite por tenant/ip/token => `429 rate_limit_exceeded`.
- Headers de rate limit disponibles: `X-RateLimit-Limit`, `X-RateLimit-Window`.

### 4) Trazabilidad de acciones en tiempo real
`POST /api/public/realtime/action-event`

Body recomendado:
```json
{
  "tenant_slug": "mi-tenant",
  "widget_token": "...",
  "channel": "voice",
  "action": "crear_reclamo",
  "session_id": "rt_sess_123",
  "status": "ok",
  "details": {"ticket": "M-1234"}
}
```

Uso esperado:
- Emitir evento al confirmar acciones de negocio ejecutadas por la experiencia realtime.
- Alimentar analytics por canal/acción para paridad chat vs voz vs video.

---

## Trabajo que debe ejecutar Frontend (prioridad alta)

## A. Realtime Voice Call (MVP)
1. Mostrar botón primario `Llamar ahora` cuando:
   - `support_channels.voice_call.enabled === true`
2. Crear estado de sesión realtime:
   - `idle -> connecting -> live -> reconnecting -> ended`
3. Implementar VAD/barge-in visual:
   - animación de “escuchando”
   - cortar TTS cuando el usuario habla
4. Accesibilidad:
   - botón mute/unmute
   - subtítulos live on/off
   - teclado: `Space` push-to-talk opcional

## B. Realtime Video + Avatar Robot
1. Mostrar botón `Videollamada con asistente` cuando:
   - `support_channels.video_call.enabled === true`
2. Renderizar componente avatar:
   - tipo por `data-avatar-type` (default `robot`)
   - persona por `data-avatar-persona`
3. Overlay de captions + transcript en vivo
4. Fallback automático:
   - si falla video/webcam -> pasar a voice-only realtime

## C. Integración con flujos de negocio existentes
El canal realtime debe poder disparar las mismas acciones de negocio que chat:
- crear reclamos
- crear pedidos
- consultas generales
- derivar humano

UX recomendado:
- timeline único (texto + voz + eventos)
- cards de confirmación al ejecutar acción (`Reclamo #1234 creado`)
- botón “Enviar resumen por WhatsApp / Email” al finalizar llamada

## D. Widget UX/UI Web 4.0
1. Navegación por modos:
   - Chat
   - Llamada
   - Video Avatar
2. Microinteracciones:
   - estado de latencia de red
   - “AI pensando” con animation tokens existentes
3. Personalización tenant:
   - tomar colores/logo de `attributes`
   - no hardcodear temas
4. Inclusivo:
   - contraste AA
   - captions de alto contraste
   - controles grandes táctiles

---

## Heatmaps + analytics (plan frontend inmediato)

## 1) Perfil
Agregar módulo "Perfil ciudadano/cliente" con segmentación:
- sexo
- rango etario
- barrio/distrito
- canal (chat/voz/video/whatsapp)

## 2) Encuestas
En vistas de encuestas:
- heatmap por ubicación
- filtros por categoría y demografía
- serie temporal (últimos 7/30/90 días)

## 3) Mapas
En mapa principal:
- capas por categoría (reclamos/pedidos/encuestas)
- clustering por zoom
- toggle densidad/calor

## 4) Estadísticas / Analytics
Dashboard unificado con tabs:
- Conversión por canal
- Tiempos de resolución
- Derivaciones a humano
- Satisfaction score

KPIs mínimos nuevos:
- `% interacciones por voz`
- `% interacciones por video/avatar`
- `Tasa finalización sin escribir`
- `Tasa accesibilidad (uso captions/voice-only)`

---

## Contrato sugerido para eventos frontend realtime
Emitir eventos analytics homogéneos:
- `realtime_session_started`
- `realtime_session_failed`
- `realtime_mode_switched` (chat->voice, voice->video)
- `avatar_rendered`
- `accessibility_caption_enabled`
- `business_action_executed` (`crear_reclamo`, `crear_pedido`, etc.)

Payload base sugerido:
```json
{
  "tenant": "slug",
  "channel": "chat|voice|video",
  "session_id": "uuid",
  "user_segment": {
    "sexo": "f|m|x|na",
    "edad_rango": "18-24",
    "barrio": "centro",
    "distrito": "norte"
  }
}
```

---

## Criterios de aceptación
- El widget habilita voz realtime en <= 2 clics.
- La videollamada con avatar tiene fallback a voz si falla video.
- Los flujos de reclamos/pedidos/consultas funcionan igual en chat/voz/video.
- Hay métricas por canal y segmentación geodemográfica en analytics.
- El modo accesible (captions + voice-only) está disponible y visible.


## E. Sprint frontend incremental (iteración recomendada)
1. Semana 1:
   - Integrar `realtime/session` + UI de conexión/errores.
   - Activar fallback automático a chat cuando falle session bootstrap.
2. Semana 2:
   - Integrar `realtime/action-event` al ejecutar acciones (`crear_reclamo`, `crear_pedido`, `derivar_humano`).
   - Mostrar timeline unificado de eventos (audio + acciones).
3. Semana 3:
   - Completar dashboards con filtros persistentes (`categoria/barrio/distrito/sexo/rango_edad/canal`).
   - Añadir QA automático de accesibilidad (captions, teclado, contraste).


## F. Nuevo módulo frontend: Realtime Hub para encuestas/votaciones/sondeos
Backend disponible:
- `GET /admin/analytics/realtime-hub?tenant_id=<id>&scope=<municipio|pyme>&window_minutes=30`

Incluye:
- Totales realtime (`events`, `survey_responses`, `survey_comments`, `live_chat_comments`)
- Top canales y eventos
- Señales de sentimiento (`positive|neutral|negative`)
- Puntos geográficos + hotspots
- Recomendaciones ejecutivas para equipos políticos y empresarios

UX sugerida:
- Tab “Realtime Hub” dentro de Analytics
- Cards de alertas para sondeos/votaciones con actividad alta
- Tabla de comentarios en vivo (encuestas + chat)
- Mapa de calor en tiempo real con filtro por canal/segmento


### Contrato heatmap/segmentación para frontend (actualizado)
Para que frontend muestre **categorías, edades y género** en mapas/estadísticas, usar `GET /admin/analytics/heatmap` y leer:

- `segments.categoria[]`
- `segments.rango_edad[]`
- `segments.sexo[]`
- `segments.barrio[]`, `segments.distrito[]`, `segments.canal[]`
- `segments_filters_applied` (eco de filtros activos)
- `geo_layers` (capas Leaflet + OSM con color por categoría e intensidad por votos/peso)

Filtros soportados en query params:
- `categoria` o `categorias`
- `sexo` o `genero`
- `rango_edad`, `barrio`, `distrito`, `canal`

Payload esperado (resumen):
```json
{
  "geo_layers": {
    "provider": "leaflet",
    "tiles": {
      "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      "attribution": "© OpenStreetMap contributors"
    },
    "categories": [
      {
        "categoria": "seguridad",
        "color": "#EF4444",
        "event_count": 12,
        "total_weight": 46,
        "intensity": 1.0,
        "points": [{"lat": -34.60, "lng": -58.38, "weight": 8}]
      }
    ],
    "legend": {"mode": "category_weight", "min_weight": 0, "max_weight": 46}
  },
  "segments": {
    "categoria": [{"label": "seguridad", "count": 12}],
    "sexo": [{"label": "f", "count": 7}],
    "rango_edad": [{"label": "25-34", "count": 5}]
  }
}
```

Notas de integración:
- Usar `segments` para barras/tortas/filtros de demografía.
- Usar `geo_layers.categories[].color` para leyenda fija por categoría en mapa.
- `total_weight` refleja volumen de votos/score (`votos`, `cantidad_votos`, `vote_count`, `puntaje`, etc.).


## G. Troubleshooting rápido (producción)
- Si el navegador muestra `socket.io websocket error` o `GET /api/socket.io ... 400`, forzar fallback de frontend a `polling` usando `socket_transport_hint` del endpoint de schedule (`/api/live-chat/schedule`).
- Si `POST /api/ask/pyme` retorna `409`, mostrar mensaje UX claro de conflicto de sesión/flujo y ofrecer botón de reintentar con nueva sesión.
- Si `/api/live-chat/schedule` falla, usar fallback `/api/{tenant_slug}/live-chat/schedule` y degradar en UI a estado "horario no disponible" sin romper chat.


## Hotfix UX/Operación (marzo 2026)

### Socket fallback obligatorio (Render/Gunicorn)
- Si `/api/live-chat/schedule` o `/api/<slug>/live-chat/schedule` devuelve `socket_transport_hint: polling` o `socket_transports: ["polling"]`, inicializar Socket.IO con `transports: ["polling"]` y **no forzar websocket**.
- Mostrar estado de conexión no bloqueante (el chat HTTP debe seguir funcionando sin socket).

### Selector de demo por categorías (no lista plana)
- Si `fuente=demo_selector` y `demo_selector_mode=segment_categories`, renderizar 2 CTAs grandes:
  - `demo_segment:empresas`
  - `demo_segment:gobiernos`
- Si `demo_selector_mode=segment_rubros`, mostrar solo rubros del segmento elegido + botón `demo_segment:all` para volver.

### Realtime call/video visibles por contrato
- Mostrar botones de `call` y `video` cuando `support_channels.voice_call.enabled=true` y `support_channels.video_call.enabled=true`.
- Leer siempre desde `widget.support_channels` y `widget.attributes (data-realtime-*)`; no hardcodear por tenant.
