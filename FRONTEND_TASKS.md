# Frontend Improvements & Fixes

This document outlines necessary frontend adjustments to resolve reported crashes and improve the user experience for Pymes and Integrations.

## 1. Widget Crash Fix (`registerExtensionNoiseFilters.js`)

**Issue:** Users reported a crash with `TypeError: Cannot read properties of undefined (reading 'value')`. This often occurs when iterating over `cta_messages` or initializing the socket with undefined tokens.

**Backend Fix:** The API `/api/public/tenants/<slug>/widget-config` has been patched to always return:
- `cta_messages`: `[]` (empty list) if no messages are configured.
- `theme`: `{}` (empty object) if not set.
- `features`: `{}` (empty object) if not set.

**Frontend Task:**
- **Defensive Check:** Ensure the widget initialization code checks if `cta_messages` exists and is an array before calling `.map()` or `.filter()`.
- **Default Values:** If `widgetToken` or `entityToken` is missing in the config, the frontend should handle it gracefully (e.g., fallback to slug or show a console warning instead of crashing).

## 2. Integration Page Improvements

**Issue:** The integration page (`/cuatro-fincas/integracion`) was reported as missing features ("vista previa", "atributos").

**Tasks:**
- **Preview Widget:** Add a preview component that renders the Chat Widget using the current configuration (similar to the public landing page).
- **Attribute Mapping:** For MercadoLibre/TiendaNube, ensure the UI displays the mapping status (e.g., "Category X mapped to Y").
- **Error Handling:** When clicking "Connect", if the backend returns `503` (Platform not configured) or `403` (Plan required), show a user-friendly toast notification instead of a generic "Connection failed".

## 3. Order Tracking (Voice & Web)

**Issue:** Voice orders were failing to register completely.

**Backend Fix:** The voice agent now correctly associates the Pyme ID with the order.

**Frontend Task:**
- **Tracking Page:** Ensure the order tracking page (`/pyme/pedidos/<nro>`) correctly displays the "Tenant Branding" (logo, colors) returned by the API.
- **Rich Receipts:** Verify that WhatsApp receipts allow the user to click a link to view their order status.

## 4. Seeding Tool (Frontend Snippet)

If you need to re-enable the "Seed Demo Data" button for admins:

```javascript
async function seedDemoData(encuestaId) {
  try {
    const response = await fetch(`/api/admin/encuestas/${encuestaId}/seed-demo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    const result = await response.json();
    alert(`Seeding complete: ${result.message}`);
  } catch (error) {
    console.error('Seeding failed:', error);
    alert('Error seeding data');
  }
}
```

## 5. Widget UX/UI Cleanup (High Priority)

Based on user feedback from production, the current widget has too many non-functional controls and leaves too little clean space for conversation.

### 5.1 Remove non-functional top chips
- Remove (or hide behind a feature flag) the chips:
  - `widget`
  - `whatsapp`
  - `voice`
- Condition to render: only show each chip when its handler is actually enabled and wired to a valid action.

### 5.2 Remove dead CTA blocks
- Remove the standalone `WhatsApp` button shown inside the body when no URL/action is configured.
- **Do not remove composer tools from the bottom bar** (`Adjuntos`, `GPS/Ubicación`, `Audio`, `emoji`) because they are core chat inputs.
- If a tool is not available for the tenant, show a disabled state with tooltip explaining why, instead of removing all input affordances.
- Rule: remove decorative/duplicated CTAs, but preserve core composer actions.

### 5.3 Recover chat viewport
- Reduce vertical chrome (header + utility bars) and reserve more height for message list.
- Keep composer always visible, but compact.
- Add a min usable viewport target for 768p screens so the message area remains dominant.
- Convert the large “horario de atención” block into compact, dismissible info (`once_per_session`) or tooltip.

### 5.4 Onboarding flow copy (from backend contract)
- Initial message should guide by categories first (reclamos / trámites / información / catálogo).
- Ask for user name as optional personalization, not as a hard blocker before showing options.
- Avoid hardcoded generic identity text (e.g. always saying `Municipio Inteligente`) when tenant-specific name exists.

## 6. Socket.IO 500 Troubleshooting Checklist (Frontend + Backend)

User-reported error:
- `Socket.IO connection error: xhr poll error`
- `GET /api/socket.io/?EIO=... 500`

### Frontend checks
- Ensure Socket.IO client uses:
  - `path: "/api/socket.io"`
  - Transport fallback (`polling` + `websocket`) with sane reconnect backoff.
- Log and surface handshake payload (`channel`, `token`, `tenant_slug`) for debugging.
- If token is absent, connect as anonymous web channel only.

### Backend checks (already aligned in this repo)
- Socket server now defaults to `threading` for safer compatibility, and supports override via `SOCKETIO_ASYNC_MODE` when infra is prepared for another worker mode.
- Keep reverse proxy forwarding `/api/socket.io` without stripping upgrade headers.
