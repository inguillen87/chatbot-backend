# Frontend Guide: Live Chat & Real-Time Updates

## Overview
This document outlines the WebSocket events and API interactions required to support:
1.  **Live Chat (Human Handoff):** Real-time bidirectional communication between Tenant Agents and Users (Widget/WhatsApp).
2.  **Order Notifications:** Ensuring Pyme orders appear instantly in the Admin Panel.

## 1. Live Chat (WebSocket)

The backend uses `Socket.IO` to stream chat messages. Both the **Admin Panel** and **Web Widget** (if supporting live mode) should connect to the same socket namespace.

### Connection
- **Endpoint:** `/api/socket.io`
- **Auth:** JWT Token required for Admins. Anonymous/Public tokens for Widget users.

### Events

#### A. Listening for Messages (`new_chat_message`)
The backend emits this event whenever a new message is added to a ticket/chat, whether by an Admin, a User (via Widget), or via WhatsApp webhook.

**Event Name:** `new_chat_message`

**Payload:**
```json
{
  "ticket_id": 123,
  "message": {
    "id": 456,
    "comentario": "Hola, necesito ayuda con mi pedido.",
    "texto": "Hola, necesito ayuda con mi pedido.",
    "fecha": "2023-10-27T10:30:00Z",
    "user_id": 789,
    "es_admin": false,
    "autor": "vecino", // or "municipio" / "pyme"
    "autor_nombre": "Juan Perez",
    "origen": "whatsapp" // "chat", "email", etc.
  }
}
```

**Frontend Action:**
- Append the `message` object to the local chat history state for the given `ticket_id`.
- If the chat window for `ticket_id` is open, scroll to bottom.
- If closed, show a notification/badge.

#### B. Sending Messages (`send_chat_message`)
**Admins** use this event to send replies.

**Event Name:** `send_chat_message`

**Payload:**
```json
{
  "token": "JWT_TOKEN",
  "room": "municipio_1", // or specific ticket room if applicable
  "ticket_id": 123,
  "ticket_type": "municipio", // or "pyme"
  "message": "En breve lo revisamos."
}
```

**Backend Action:**
- Saves comment to DB.
- Emits `new_chat_message` to everyone in the room (including sender, for confirmation).
- Dispatches WhatsApp/Email notification to the user.

## 2. Order Real-Time Updates

For Pyme Orders to appear in the "Sales" section instantly:

### Events to Listen
- **Event Name:** `ticket_update` (Legacy) or `order_update` (Future)
- Currently, the backend emits `ticket_update` for generic updates. Ensure the Admin Panel refreshes the order list or appends the new order when this event is received with `type: "new_order"` or similar payload if available.

### Polling Fallback
If sockets disconnect, the Admin Panel should poll `GET /api/admin/orders` every 30-60 seconds or on window focus.

## 3. Order Sync Fixes (Backend Note)
The backend has been patched to ensure:
- **Legacy Cart Orders:** Now automatically create a corresponding `Order` record, ensuring they appear in the V2 Admin Panel API.
- **Notifications:** WhatsApp notifications now support multiple comma-separated numbers in `TenantProfile.dispatch_phone`.
