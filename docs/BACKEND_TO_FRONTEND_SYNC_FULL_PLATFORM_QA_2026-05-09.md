# Backend to Frontend Sync - Full Platform QA 2026-05-09

Fecha: 2026-05-09

Objetivo: sincronizar frontend con la ola backend de estabilidad funcional sobre lo existente, sin crear app paralela ni duplicar V1/V2. Backend mantiene compatibilidad legacy donde sigue documentada y estabiliza contratos para demo, widget, WhatsApp, colegios, catalogos, pedidos, encuestas, tickets y analytics.

## Cambios relevantes para frontend

### WhatsApp, widget y primera conversacion

- El webhook de WhatsApp ahora mapea respuestas numericas contra `last_options_sent` antes de llamar al bot.
- Si una opcion tiene `category_name`, frontend/admin puede esperar que backend use ese label humano para iniciar la accion.
- La bienvenida WhatsApp envia secuencia consistente: template si aplica, sticker/media si aplica y saludo textual.
- Si no hay nombre confiable, backend pide el nombre y guarda `awaiting_user_name`.
- Nombres genericos como `Vecino/a` no se usan para personalizar.
- Los media relativos de bienvenida se resuelven contra `APP_BASE_URL`; en produccion la URL esperada es publica, no localhost.
- Reclamos por WhatsApp pasan directo a categorias accionables cuando el usuario dice "quiero hacer un reclamo".
- El menu canonico de reclamos viene desde backend: `Luminaria`, `Arbolado`, `Limpieza y riego`, `Arreglo de calle`, `Perdida de agua`, `Otros`, `Cancelar`.
- Consulta de estado con numero de ticket detectado pide PIN antes de exponer datos.

### Demo, landing y pilares

- Se mantiene el contrato de demos con tres pilares: `educacion`, `gobierno`, `empresas`.
- Frontend debe mostrar `Colegios`, `Gobiernos` y `Empresas` desde `GET /api/v2/demo/catalog`.
- `POST /api/v2/demo/session` acepta aliases de rubro/categoria/pilar y devuelve workspace/chat bootstrap.
- El widget debe iniciar viaje por pilar/rubro usando contratos de backend, no labels locales.

### Catalogos, Qdrant y pedidos

- Se conserva compatibilidad del endpoint legacy `POST /api/admin/catalogo/importar`.
- Ese endpoint devuelve JSON para errores comunes:
  - `archivo_requerido`
  - `formato_no_soportado`
  - `tenant_no_resuelto`
  - `method_not_allowed`
- Soporta `archivo`, `column_map`, `plantilla` y `guardar_plantilla`.
- Si frontend usa importacion de catalogo vieja, ya no deberia recibir HTML en errores.
- Catalogo/Qdrant/pedidos/market siguen sobre endpoints actuales; no hay que migrar a una pantalla paralela.

### Colegios / educacion

- Labels visibles de taxonomia educativa se normalizaron con acentos:
  - `documentacion` -> `Documentacion` en clave, `Documentación` en label.
  - `agenda_academica` -> `Agenda académica`.
  - `cobranza` -> `Tesorería`.
- Mantener keys sin acento para filtros/API.
- Mostrar siempre `taxonomy_label` cuando exista; no transformar labels en React.
- WhatsApp y panel deben consumir quick menu/cases/capabilities desde backend.

### Encuestas, votaciones, comentarios, tickets y analytics

- Los bloques locales quedaron verificados para encuestas publicas, votaciones, comentarios, tickets/reclamos/sugerencias, analytics v2 y operaciones.
- Mantener render de `request_id` cuando backend lo mande en error envelope.
- Para mapas/heatmaps, frontend debe degradar con estados vacios si no hay puntos; no inventar ubicaciones.

## Contratos que frontend deberia asumir

### Error legacy catalog import

```json
{
  "codigo": "formato_no_soportado",
  "mensaje": "No se pudo interpretar el formato del archivo. Proba con PDF, Excel o CSV."
}
```

### WhatsApp numeric option

```json
{
  "last_options_sent": [
    {
      "texto": "Luminaria",
      "id_accion": "1",
      "category_name": "Luminaria"
    }
  ]
}
```

Backend envia al bot `Luminaria`, no el texto crudo `"1"`.

### Education case list

```json
{
  "case_type": "documentacion",
  "taxonomy_label": "Documentación",
  "ticket_type": "pyme"
}
```

## Verificacion backend local ejecutada

- V2 tickets/surveys/analytics/saas/commerce: 29 passed.
- Encuestas/votaciones/comentarios/public surveys: 56 passed.
- Tickets/reclamos/sugerencias/status/timeline: 64 passed, 1 skipped.
- WhatsApp/webhook/media/audio/voice/realtime/promocionar/funnel: 74 passed.
- Catalogo/Qdrant/import legacy/catalog mappings: 26 passed.
- Pedidos/market/rewards/order preview: 9 passed.
- Educacion/colegios/KB/rubros education profile: 12 passed.

## Pendientes honestos

- No se enviaron mensajes reales de WhatsApp ni llamadas reales por Twilio en esta ola. Para eso backend necesita confirmacion explicita de numero/contacto, tenant y alcance.
- Qdrant real puede requerir credenciales/servicio activo; la suite local valida interfaz y fallback.
- OpenAI TTS local habia mostrado dependencia rota `jiter`; no bloqueo los tests porque se usan mocks/fallbacks, pero para probar audio real hay que reinstalar dependencias del venv.
- Para llamadas realtime reales, frontend debe seguir el handoff de realtime voice y no asumir que Socket.IO en landing existe si backend no expone ese transporte.
