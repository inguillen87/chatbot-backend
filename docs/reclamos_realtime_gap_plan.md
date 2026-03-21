# Plan inicial — Reclamos, tracking público y atención ciudadana en tiempo real

## Hallazgos visibles en las capturas

- El portal público del reclamo no siempre muestra el mapa aunque el panel tenant sí tenga latitud/longitud disponible.
- El módulo **Mesa de Ayuda / Atención Ciudadana** existe, pero hoy funciona como un flujo separado del chat operativo del ticket.
- El panel tenant de **Reclamos** y el portal público del ticket no comparten todavía un contrato realtime único para comentarios, estados y presencia.
- Se observan `403` sobre `GET /api/tickets/chat/<id>/mensajes`, señal de que el frontend público está entrando al chat sin reutilizar correctamente `anon_id` o `pin`.
- Los mensajes del reclamo viven en varios puntos (tracking público, comentarios del ticket, socket admin, soporte público), lo que genera sensación de producto “a medias”.

## Ajuste backend aplicado en esta iteración

- Se unificó la validación de acceso público al chat de tickets municipales para que los endpoints de chat reutilicen la misma lógica de `anon_id`, dueño autenticado y `consulta_pin`.
- El endpoint `POST /tickets/chat/<ticket_id>/responder_ciudadano` ahora acepta acceso público por `consulta_pin`/`anon_id`, además del usuario autenticado. Esto evita que el portal público quede bloqueado cuando el ciudadano entra desde el link de seguimiento del reclamo.

## Backend — siguientes pasos propuestos

1. **Unificar dominio de conversación**
   - Definir `ticket_conversation_id` único para:
     - comentarios del admin tenant,
     - mensajes del ciudadano en tracking público,
     - chat en vivo,
     - eventos automáticos de estado.
   - Emitir un solo evento websocket: `ticket_message_created`.

2. **WebSockets profesionales**
   - Rooms por ticket: `ticket:<tipo>:<id>`.
   - Rooms por tenant: `tenant:<slug>:tickets`.
   - Eventos mínimos:
     - `ticket_message_created`
     - `ticket_status_changed`
     - `ticket_assignment_changed`
     - `ticket_presence_changed`

3. **Timeline consistente**
   - Persistir mensajes, comentarios internos y cambios de estado con `origen` claro:
     - `public_tracking`
     - `admin_panel`
     - `whatsapp`
     - `email`
     - `system`
   - Exponer timeline unificada para panel y tracking.

4. **Notificaciones omnicanal**
   - Ciudadano:
     - email “tu reclamo está siendo procesado”
     - SMS opcional para estados críticos
     - WhatsApp para alta, asignación, en proceso y resolución
   - Operadores:
     - alerta por nuevo mensaje ciudadano
     - alerta por ticket sin respuesta SLA

5. **Maps**
   - Si el ticket tiene coordenadas, usarlas siempre como fuente de verdad para tracking público.
   - Si solo hay dirección, geocodificar y guardar lat/lon.
   - Agregar chequeo backend para detectar tickets con dirección pero sin coordenadas.

## Frontend — tareas concretas para pedir/proponer

1. **Tracking público**
   - Reutilizar `pin` en todas las llamadas a:
     - mensajes,
     - timeline,
     - historial/export,
     - soporte.
   - Mostrar fallback claro:
     - “Ubicación pendiente de geocodificación”
     - botón “Abrir en Maps” cuando haya coordenadas.

2. **Admin tenant / Reclamos**
   - Reemplazar polling manual por websocket con reconciliación inicial HTTP + delta realtime.
   - Mostrar badge de mensajes no leídos por ticket.
   - Refrescar mapa y timeline al llegar un nuevo mensaje o cambio de estado.

3. **Mesa de Ayuda**
   - No abrir un sistema paralelo: debe escribir sobre el mismo stream del ticket.
   - Diferenciar visualmente:
     - comentario público,
     - nota interna,
     - mensaje en vivo.

4. **UX de estados**
   - Estados recomendados:
     - `nuevo`
     - `esperando_agente_en_vivo`
     - `en_vivo`
     - `en_proceso`
     - `resuelto`
   - Cada cambio debe generar evento realtime y notificación opcional.

## Nota operativa

> Comentario intencional para seguimiento: mientras el frontend público siga consumiendo el chat sin propagar `pin`/`anon_id`, van a reaparecer `403` aunque el backend ya soporte acceso público válido.
