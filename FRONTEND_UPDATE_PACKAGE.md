# Frontend Update Package: Demo System & Widget Enhancements

This document contains all the necessary instructions, logic, and assets for the frontend team to implement the new interactive demo system, persistent carts, and widget enhancements.

## 1. Overview of Changes

The backend has been refactored to support:
1.  **Hierarchical Categories:** The `/api/rubros` endpoint now returns a nested structure (Root -> Sub-categories).
2.  **Generic Demos:** Demo data has been renamed to generic industry terms (e.g., "Bodega Demo", "Fintech Demo") to be more professional.
3.  **Persistent Sessions:** Anonymous user carts now persist across reloads using a client-generated `X-Anon-Id` header.
4.  **Enhanced Widget:** The widget now supports rotating "Call to Action" (CTA) tooltips and theme customization via `/api/public/tenants/:slug/widget-config`.

---

## 2. Routing & Page Structure

### New Routes Required
*   `/demo/:slug`: A dedicated landing page for specific industry demos (e.g., `/demo/bodega`, `/demo/fintech`).
    *   **Behavior:** When accessing this route, the frontend should:
        1.  Fetch widget config for the tenant `slug`.
        2.  Render the landing page with the specific branding found in the config.
        3.  Open the chat widget automatically (or show the CTA).

### Landing Page Update (`/`)
*   **Category Menu:** Instead of a flat list, render a hierarchical menu using the `padre_id` field from `/api/rubros`.
    *   **Roots:** Items with `padre_id: null` (e.g., "Municipios", "Locales Comerciales").
    *   **Children:** Items with `padre_id: <root_id>`.

---

## 3. API Integration Guide

### A. Cart Persistence (Critical)
**Logic:** The backend no longer relies solely on cookies for anonymous sessions.
**Action:**
1.  On first app load, check `localStorage` for `chatboc_anon_id`.
2.  If missing, generate a UUID v4 and store it.
3.  **Send this UUID in the `X-Anon-Id` header for ALL API requests.**

```javascript
// Example Interceptor
const getAnonId = () => {
  let id = localStorage.getItem('chatboc_anon_id');
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem('chatboc_anon_id', id);
  }
  return id;
};

axios.interceptors.request.use(config => {
  config.headers['X-Anon-Id'] = getAnonId();
  return config;
});
```

### B. Widget Configuration & CTAs
**Endpoint:** `GET /api/public/tenants/:slug/widget-config`
**Response (New Fields):**
```json
{
  "cta_messages": [
    { "text": "🔥 Ver ofertas", "action": "open_catalog", "payload": "" },
    { "text": "🚚 Seguimiento", "action": "trigger_intent", "payload": "estado_pedido" }
  ],
  "theme_config": {
    "mode": "system", // or "light", "dark"
    "light": { "primary": "#0066ff", "secondary": "#ffffff", ... },
    "dark": { "primary": "#0052cc", "secondary": "#1a1a1a", ... }
  }
}
```
**Implementation:**
*   **CTA Bubble:** Render a small bubble above the widget launcher. Rotate through the `text` in `cta_messages` every 5-7 seconds.
*   **Click Handler:** When the bubble is clicked, open the widget and immediately send the `action` to the chat.

### C. File Downloads & Resources
**Endpoint:** `GET /api/media/<path>`
**Logic:**
*   Chat messages may now contain buttons with `type: "url"`.
*   **Action:** When clicked, open the URL in a new tab.
*   **Attachments:** If a response includes an `adjuntos` array (PDFs, Images), render them as download cards.

---

## 4. Demo Data Mapping (Generic Names)

The backend has migrated to generic names. Ensure the frontend does **not** hardcode old names like "Bodega Cuatro Fincas". Use the dynamic `nombre` returned by the API.

| Old Key | New Generic Key | Display Name |
| :--- | :--- | :--- |
| `bodega` | `bodega` | Bodega Demo |
| `almacen` | `almacen` | Almacén Demo |
| `ferreteria` | `ferreteria` | Ferretería Demo |
| `municipio` | `municipio` | Municipio Inteligente |
| ... | ... | ... |

**Note:** The API `/api/rubros` will return these updated names automatically.

---

## 5. Checklist for Frontend Dev

- [ ] **Auth:** Implement `X-Anon-Id` header generation and transmission.
- [ ] **Menu:** Refactor main menu to support 2-level hierarchy (`padre_id`).
- [ ] **Widget:** Add "CTA Bubble" component to the chat widget.
- [ ] **Theming:** Apply colors from `theme_config` (support Dark Mode).
- [ ] **Routing:** Ensure `/demo/:slug` loads the correct tenant context.
- [ ] **Chat:** Handle `type: "url"` buttons (open in new tab).
- [ ] **Assets:** Ensure images/PDFs from `/api/media/` load correctly (handle CORS if needed).
