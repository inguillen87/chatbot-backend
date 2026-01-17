# Frontend Chat Integration Guide

This guide provides the necessary information for frontend developers to integrate a rich chat interface with the ChatBoc backend. The interface will allow admin users (Tenants) to manage tickets, chat with users (via WhatsApp/Web), and exchange media (images, audio).

## Base URL
All API endpoints are relative to the backend base URL (e.g., `https://api.chatboc.ar` or `http://localhost:5000`).

## Authentication
Most endpoints require a valid JWT token.
- **Header:** `Authorization: Bearer <token>`
- **Cookie:** The backend also sets a session cookie which works for browser-based requests if CORS is configured correctly.

---

## 1. Chat History
To display the conversation history for a specific ticket.

**Endpoint:** `GET /tickets/chat/<ticket_id>/mensajes`
**Auth:** Required

**Response (JSON):**
```json
{
  "estado_chat": "en_proceso",
  "mensajes": [
    {
      "id": 102,
      "fecha": "2023-10-27T10:30:00Z",
      "es_admin": false,
      "texto": "Hola, tengo un problema con...",
      "archivo_adjunto": null
    },
    {
      "id": 103,
      "fecha": "2023-10-27T10:32:00Z",
      "es_admin": true,
      "texto": "Hola, ¿podrías enviarme una foto?",
      "archivo_adjunto": null
    },
    {
      "id": 104,
      "fecha": "2023-10-27T10:35:00Z",
      "es_admin": false,
      "texto": "[Archivo adjunto: foto_bache.jpg]",
      "archivo_adjunto": {
        "id": 55,
        "url": "https://storage.googleapis.com/...",
        "nombre_original": "foto_bache.jpg",
        "mime": "image/jpeg"
      }
    }
  ]
}
```

**Notes:**
- `es_admin: true` -> Message sent by the agent/tenant (Right side).
- `es_admin: false` -> Message sent by the user (WhatsApp/Web) (Left side).
- If `archivo_adjunto` is present, display the media (Image, Audio player, or Download link).

---

## 2. Sending Messages (Text & Media)
To reply to a user. Supports sending text, files, or both.

**Endpoint:** `POST /tickets/<tipo>/<ticket_id>/responder`
- `<tipo>`: `municipio` or `pyme`
- `<ticket_id>`: ID of the ticket.

**Content-Type:** `multipart/form-data`

**Form Data:**
- `comentario`: (Text) The message content.
- `archivos`: (File) One or multiple files to upload.

**Example (JavaScript):**
```javascript
const formData = new FormData();
formData.append('comentario', 'Aquí está el documento solicitado.');
formData.append('archivos', fileInput.files[0]);

fetch('/tickets/municipio/123/responder', {
  method: 'POST',
  headers: { 'Authorization': 'Bearer ...' },
  body: formData
});
```

**Supported Media Types:**
- Images (`image/jpeg`, `image/png`, etc.)
- Audio (`audio/ogg`, `audio/mp3`, `audio/wav`) -> WhatsApp users will receive this as a voice note.
- Documents (`application/pdf`, etc.)

---

## 3. Real-Time Updates (WebSocket)
To receive new messages without refreshing.

**Library:** `socket.io-client` (v4.x)
**Endpoint:** `/api/socket.io`

**Connection:**
```javascript
import { io } from "socket.io-client";

const socket = io('https://api.chatboc.ar', {
  path: '/api/socket.io',
  auth: {
    token: 'YOUR_JWT_TOKEN' // Essential for subscribing to private rooms
  }
});

socket.on('connect', () => {
  console.log('Connected to ChatBoc socket');
  // Subscribe to updates
  socket.emit('subscribe_ticket_updates', { token: 'YOUR_JWT_TOKEN' });
});
```

**Event: `new_comment`**
Received when a new message is sent (by user or agent).

**Payload:**
```json
{
  "ticket_id": 123,
  "comment": {
    "id": 105,
    "comentario": "Gracias, recibido.",
    "fecha": "2023-10-27T10:40:00Z",
    "es_admin": false,
    "archivo_adjunto": null
  },
  "mensaje": "Gracias, recibido.",
  "actor": "neighbor" // or "agent"
}
```

**Event: `ticket_update`**
Received when ticket status or details change.

**Payload:**
```json
{
  "ticket_id": 123,
  "estado": "en_proceso",
  // ... full ticket snapshot
}
```

---

## 4. Media Handling Best Practices

### Images
- Use the `url` provided in `archivo_adjunto` to display a preview.
- Support a lightbox or modal for full-size viewing.

### Audio
- The backend stores audio files (e.g., from WhatsApp voice notes).
- Detect `mime` starting with `audio/` in `archivo_adjunto`.
- Render an HTML5 `<audio controls>` player source set to the `url`.

### Documents
- For PDFs or other files, render a download link/button with the `nombre_original`.

## 5. Status Management
To close or resolve a ticket from the UI.

**Endpoint:** `PUT /tickets/<tipo>/<ticket_id>/estado`
**Payload (JSON):**
```json
{
  "estado": "cerrado" // or "en_proceso", "nuevo"
}
```
