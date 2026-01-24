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
