# Backend to Frontend Sync - WhatsApp Operations 2026-05-12

Objetivo: convertir WhatsApp en un modulo operativo premium del panel tenant, demo y widget, sin crear una app paralela. Backend expone un contrato unico para que frontend renderice estado del canal, inteligencia conversacional, contenidos, tracking, voz realtime, reglas enterprise y setup.

## Backend implementado

Endpoint principal:

`GET /api/v2/whatsapp/experience`

Alias superadmin/tenant:

`GET /api/v2/tenants/{tenant_slug}/whatsapp/experience`

Tambien queda resumido dentro de:

`GET /api/v2/tenant/admin-experience`

Contrato:

```json
{
  "contract_version": "whatsapp.experience.v1",
  "tenant": {
    "slug": "colegio-demo",
    "tipo": "pyme",
    "vertical": "educacion"
  },
  "channel": {
    "provider": "twilio_whatsapp",
    "enabled": true,
    "number": "whatsapp:+100",
    "webhook": "/webhook/whatsapp",
    "status_webhook": "/twilio/whatsapp/status",
    "test_endpoint": "/api/notifications/whatsapp/test",
    "test_method": "POST",
    "test_label": "Probar canal",
    "reason_code": null
  },
  "enterprise_rules": {
    "configured": true,
    "enforce_template_outside_24h": true,
    "max_outbound_per_hour": 200,
    "quiet_hours": { "start": 22, "end": 7 },
    "blocked_keywords": []
  },
  "contact_window": {
    "active_24h": 1,
    "known_contacts": 1,
    "window_policy": "respond_freeform_inside_24h_use_templates_outside_window"
  },
  "conversation_intelligence": {
    "llm_strategy": {
      "primary": "llm_orchestrated_actions",
      "python_role": "execute_validate_persist",
      "avoid_keyword_only_flows": true
    },
    "inputs": {
      "text": { "enabled": true },
      "emoji": { "enabled": true },
      "location": { "enabled": true },
      "image": { "enabled": true },
      "audio_note": { "enabled": true },
      "file_pdf_doc": { "enabled": true },
      "video": { "enabled": true, "analysis_ready": false }
    },
    "voice_calls": {
      "enabled": true,
      "capabilities": {
        "contract_version": "realtime.voice_capabilities.v1",
        "recommended_model": "gpt-realtime",
        "native_speech_to_speech": true
      }
    }
  },
  "content_modules": {
    "catalog": {
      "enabled": true,
      "items": 1,
      "items_with_images": 1,
      "image_coverage_rate": 100,
      "endpoint": "/api/admin/tenants/colegio-demo/catalog/items",
      "bulk_import_endpoint": "/api/admin/catalogo/importar"
    },
    "surveys_votings": {
      "enabled": true,
      "endpoint": "/api/v2/surveys",
      "draft_endpoint": "/api/v2/surveys/draft"
    },
    "news_events": {
      "enabled": true,
      "endpoint": "/api/municipal/posts"
    },
    "promotions": {
      "enabled": true,
      "endpoint": "/api/whatsapp/promocionar"
    },
    "links": {
      "enabled": true,
      "endpoint": "/api/admin/tenants/colegio-demo/config"
    }
  },
  "tracking": {
    "claims": {
      "experience_endpoint": "/api/public/tracking/experience?kind=claim&code={code}&pin={pin}",
      "public_status_endpoint": "/tickets/public/status",
      "public_status_alias": "/api/tickets/public/status",
      "tracking_page_template": "/tracking/claim/{nro_ticket}"
    },
    "orders": {
      "experience_endpoint": "/api/public/tracking/experience?kind=order&code={code}",
      "tracking_page_template": "/tracking/order/{nro_pedido}",
      "payment_status_endpoint": "/api/v2/payments/status"
    },
    "courier_style_map": {
      "enabled": true,
      "render_contract": {
        "type": "tracking_map_timeline",
        "layers": ["origin", "current_status", "destination_or_claim_location", "timeline_events"],
        "animations": ["pulse_current_step", "route_progress", "status_transition"],
        "fallback_when_no_coordinates": "timeline_only"
      }
    },
    "milestones": {
      "claim": ["recibido", "validando", "asignado", "en_proceso", "resuelto", "cerrado"],
      "order": ["recibido", "confirmado", "pendiente_pago", "pagado", "preparando", "en_camino", "entregado"]
    }
  },
  "frontend_contract": {
    "render_as": "whatsapp_operations_hub",
    "primary_refresh_seconds": 30,
    "empty_state_behavior": "show_setup_checklist_and_safe_degradation"
  }
}
```

## UX/UI que frontend deberia construir

1. En el panel tenant, crear vista `WhatsApp Operations Hub` dentro de Widget/WhatsApp/Voz.
2. Arriba mostrar salud del canal: numero conectado, webhook, estado del proveedor, ventana 24h, reglas enterprise, alertas y boton de prueba.
3. Hacer cards compactas para capacidades conversacionales, no una botonera gigante: Texto, Audio, Imagen, Ubicacion, Archivo, Catalogo, Encuesta, Pedido, Reclamo, Voz.
4. En el widget/chat, reducir botones persistentes que achican la pantalla. Usar una barra inferior compacta con iconos, menu contextual y quick actions colapsables.
5. Para seguimiento de reclamos/pedidos, renderizar un tracking tipo courier:
   - Timeline vertical o horizontal.
   - Mapa si hay coordenadas.
   - Punto actual con pulso.
   - Barra de progreso de ruta.
   - Fallback a timeline si no hay ubicacion.
   - Usar `GET /api/public/tracking/experience` como contrato principal para widget/demo/WhatsApp.
6. En WhatsApp admin, mostrar modulos de contenido: catalogo, encuestas/votaciones, noticias, eventos, promociones y links.
7. Si `catalog.image_coverage_rate` es bajo, mostrar tareas accionables: subir imagen, reemplazar imagen, importar CSV/XLSX/PDF, generar catalogo PDF.
8. Para llamadas, mostrar CTA solo si `conversation_intelligence.voice_calls.enabled` y `capabilities.native_speech_to_speech` son true.
9. Para video, mostrarlo como adjunto recibido. No prometer analisis IA completo hasta que `analysis_ready` sea true.
10. En colegios, usar `education.whatsapp_playbook` para starters y rutas de secretaria, familia, inasistencias, documentacion, pagos, admisiones y convivencia.

## Comportamiento esperado por vertical

Municipios:

- Reclamos con ubicacion, foto, audio, estado y mapa.
- Consultas de tramites.
- Noticias, eventos y campanas.
- Estado de reclamo desde codigo/PIN o contacto.

PyMEs:

- Catalogo con imagenes, precios, variantes, stock y promociones.
- Pedido desde WhatsApp.
- Facturas, notas de pedido y comprobantes como adjuntos.
- Estado de pedido y pago.

Colegios:

- Consultas de familias y staff.
- Inasistencias, comunicados, documentacion, pagos, admisiones, transporte, comedor y convivencia.
- Encuestas/votaciones por curso o comunidad.
- Casos escolares con handoff humano cuando sea sensible.

## Reglas de frontend

- No hardcodear textos por municipio, pyme o colegio. Mostrar labels desde backend.
- Si `channel.enabled=false`, mostrar checklist de configuracion, no una pantalla rota.
- Si `socket_enabled=false` o no hay realtime, degradar a polling/timeline sin consola roja.
- No mostrar botones de llamada, audio, imagen o ubicacion si backend los deshabilita.
- Usar `request_id` en errores y soporte.

## QA frontend 2026-05-12 confirmado

- `GET /api/v2/whatsapp/experience` responde `whatsapp.experience.v1` con `request_id` y header `X-Request-Id`.
- `GET /api/v2/tenants/{tenant_slug}/whatsapp/experience` queda cubierto como alias tenant-aware.
- `GET /api/v2/tenant/admin-experience` incluye resumen `whatsapp` y el modulo `widget_whatsapp`.
- El modulo `widget_whatsapp` expone `label: "Widget/WhatsApp/Voz"` y `endpoint: "/api/v2/whatsapp/experience"`.
- Si el canal tiene numero configurado, `channel` expone `test_endpoint`, `test_method` y `test_label`; si falta configuracion, backend no publica esos campos y frontend no muestra boton de prueba.
- Video se mantiene como adjunto con `analysis_ready: false`.
- Voz se habilita solo con `conversation_intelligence.voice_calls.enabled` y `capabilities.native_speech_to_speech`.
- Tracking conserva `experience_endpoint` para claim/order y `fallback_when_no_coordinates: "timeline_only"`.
- `GET /api/public/tracking/experience` responde JSON con `tracking.experience.v1`, `request_id`, timeline, status, mapa/render contract y errores JSON accionables.

## Referencias tecnologia voz

- OpenAI Realtime WebRTC: https://platform.openai.com/docs/guides/realtime-webrtc
- OpenAI Realtime WebSocket: https://platform.openai.com/docs/guides/realtime-websocket
- OpenAI Realtime conversations: https://platform.openai.com/docs/guides/realtime-model-capabilities
- Modelo `gpt-realtime`: https://platform.openai.com/docs/models/gpt-realtime

## Verificacion backend

- `tests/test_v2_saas_contracts.py` cubre `whatsapp.experience.v1`, admin-experience y superadmin command center.
- `tests/test_realtime_voice_profiles.py` cubre modelo realtime default `gpt-realtime`, tools y verticales.
- `tests/test_public_resolver_widget_config_contract.py` cubre `GET /api/public/realtime/voice-capabilities`.
