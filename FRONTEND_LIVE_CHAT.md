# Chat en vivo / Derivación a humano (Widget + WhatsApp + Panel)

Este documento resume cómo integrar el **chat en vivo con agentes** para **municipio** y **pyme**, reutilizando el backend actual. Incluye flujo, endpoints y eventos de socket.

## Objetivo

Cuando el usuario solicita “hablar con un humano” desde el bot (widget web o WhatsApp), se crea un ticket con estado **`esperando_agente_en_vivo`** y categoría **“Atención en Vivo”**. Desde el panel de tickets, un admin/empleado puede tomar el caso y continuar la conversación en tiempo real.

---

## Estados y categorías

* **Estado del ticket:** `esperando_agente_en_vivo`, `en_vivo`, `en_proceso`.
* **Categoría:** `Atención en Vivo`.

> Sugerencia UI: crear una sección/columna tipo “Solicitudes en vivo” filtrando por `estado=esperando_agente_en_vivo`.

---

## Horarios de atención (live chat)

El backend expone el horario configurado y si hay disponibilidad en ese momento.

**Endpoint:** `GET /live-chat/schedule`

**Respuesta ejemplo:**
```json
{
  "enabled": true,
  "available": false,
  "description": "lunes a viernes de 09:00 a 13:00 hs",
  "days": ["lunes", "martes", "miércoles", "jueves", "viernes"],
  "start_time": "09:00",
  "end_time": "13:00",
  "timezone": "America/Argentina/Buenos_Aires"
}
```

> El bot añade automáticamente un aviso si está fuera de horario.

---

## Flujo general

1. **Bot → derivación a humano**
   * El bot responde con un ticket de chat en vivo y devuelve:
     * `data.ticket_id`
     * `data.status = "esperando_agente_en_vivo"`
     * `data.live_chat` (info del horario)

2. **Panel admin/empleado**
   * Recibe el ticket por socket (`new_ticket` + `live_chat_request` para municipio).
   * El operador **toma el ticket** y se une a la sala de chat.

3. **Conversación en vivo**
   * Usuario (web/WhatsApp) y agente intercambian mensajes.
   * Las respuestas del agente actualizan el ticket y notifican por WhatsApp si corresponde.

---

## Widget Web (usuario final)

### Enviar mensajes durante el chat en vivo
El widget debe mandar las respuestas del usuario por el **mismo endpoint `/ask`**, incluyendo:

```json
{
  "pregunta": "Necesito ayuda con mi ticket",
  "ticket_id": 123,
  "tipo_ticket": "municipio"
}
```

El backend detecta `ticket_id` + `tipo_ticket` y guarda el mensaje en la sala de chat en vivo.

### Adjuntos / fotos / videos

1. Subir el archivo al endpoint de uploads del widget.
2. Enviar a `/ask` el `attachmentInfo` que devuelve el upload.

El backend se encarga de asociarlo al ticket y emitir el mensaje.

---

## WhatsApp (usuario final)

Cuando hay un ticket de chat en vivo abierto, el webhook de WhatsApp **redirige automáticamente** los mensajes del usuario al ticket correspondiente:

* Busca un ticket en estados `esperando_agente_en_vivo`, `en_vivo` o `en_proceso`.
* Guarda el mensaje y adjuntos en el ticket.
* Emite `new_chat_message` al panel del agente.

---

## Panel Admin / Empleado

### Eventos de Socket.IO

**Conexión:**
* URL: `ws(s)://<backend>/api/socket.io`
* Auth: `{ token: <JWT>, channel: "web" }`

**Eventos a escuchar:**
* `new_ticket` → nuevo ticket (incluye chat en vivo).
* `ticket_update` → cambios de estado.
* `new_comment` → nuevos comentarios en tickets.
* `live_chat_request` → (municipio) solicitud de chat en vivo en tiempo real.
* `new_chat_message` → nuevo mensaje en un chat en vivo.

**Unirse a salas específicas de ticket:**
```
socket.emit("join", { room: `ticket_${tipo}_${ticketId}` })
```

### Obtener historial de mensajes
* Municipio: `GET /tickets/chat/<ticket_id>/mensajes`
* Pyme: `GET /tickets/chat/pyme/<ticket_id>/mensajes`

Soporta `?ultimo_mensaje_id=<id>` para polling incremental.

### Enviar mensaje del agente

* **Texto / adjuntos:**
  * `POST /tickets/<tipo>/<ticket_id>/responder`
  * `Content-Type: application/json` (texto)
  * `Content-Type: multipart/form-data` (archivos)

### Asignar ticket a un agente

* `POST /tickets/<tipo>/<ticket_id>/assign`
* Permite que admin/empleado tome el ticket.

---

## Notas importantes

* **WhatsApp en Pyme:** habilitable con `ENABLE_PYME_WHATSAPP_CHAT=true`.
* **Adjuntos:** los comentarios guardan `archivo_adjunto_id` y el panel puede mostrar la URL del archivo.
* **Emojis y texto enriquecido:** se preservan como texto en los comentarios.

---

## Checklist rápido para el frontend

- [ ] Mostrar botón “Hablar con un humano” cuando el bot lo indique.
- [ ] Cambiar a modo “chat en vivo” cuando `status = esperando_agente_en_vivo`.
- [ ] Enviar mensajes del usuario con `ticket_id` + `tipo_ticket`.
- [ ] Unirse a la sala `ticket_${tipo}_${ticketId}` para recibir mensajes en tiempo real.
- [ ] Mostrar/filtrar tickets en estado `esperando_agente_en_vivo`.
- [ ] Mostrar horario desde `/live-chat/schedule`.

