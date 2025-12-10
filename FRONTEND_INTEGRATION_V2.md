# Frontend Integration Guide V2: Demos & Widget Enhancements

This guide outlines the changes required in the frontend application to support the new interactive demos, persistent sessions, and enhanced widget configuration.

## 1. Cart Persistence (Critical)

**Requirement:** Anonymous users must retain their cart items across page reloads and browser sessions.

**Implementation:**
*   **Generate `X-Anon-Id`:** The frontend must generate a UUID v4 (e.g., using `crypto.randomUUID()` or a library like `uuid`) when the user first visits the site.
*   **Store `X-Anon-Id`:** Store this UUID in `localStorage` (e.g., key `chatboc_anon_id`).
*   **Send Header:** Include the `X-Anon-Id` header in **every** API request to the backend (`/api/...`).

```javascript
// Example Axios Interceptor
const anonId = localStorage.getItem('chatboc_anon_id') || crypto.randomUUID();
localStorage.setItem('chatboc_anon_id', anonId);

axios.interceptors.request.use(config => {
  config.headers['X-Anon-Id'] = anonId;
  return config;
});
```

## 2. Interactive Demo Menus (Rubros)

**Requirement:** The demo landing page should display categories in a hierarchy (e.g., "Locales Comerciales" -> "Kioscos") instead of a flat list.

**API Endpoint:** `GET /api/rubros` (or `GET /api/public/rubros`)

**Response Structure (New):**
The API now returns a list where items may have a `padre_id`.
*   **Root Categories:** Items where `padre_id` is null (e.g., "Municipios", "Locales Comerciales").
*   **Sub-categories:** Items where `padre_id` matches a root category's `id`.

**Implementation:**
1.  Fetch rubros.
2.  Filter for root items.
3.  For each root item, filter for children (sub-categories).
4.  Render a nested menu or accordion.

## 3. Widget Enhancements

**Requirement:** The chat widget should support rotating "Call to Action" (CTA) tooltips and respect the tenant's theme preference (Dark/Light).

**API Endpoint:** `GET /api/public/tenants/<slug>/widget-config`

**Response Fields (New):**
*   `cta_messages` (Array of Objects): List of messages to rotate in a bubble above the widget launcher.
    *   `text` (string): The message to display (e.g., "🔥 Ver ofertas").
    *   `action` (string): The action to trigger on click (e.g., "open_catalog", "trigger_intent").
    *   `payload` (string): Payload for the action.
*   `theme_config` (Object):
    *   `mode` (string): "light", "dark", or "system".
    *   `light` (Object): Colors for light mode (`primary`, `secondary`, `background`, `text`).
    *   `dark` (Object): Colors for dark mode.

**Implementation:**
*   **CTA Tooltip:** Implement a tooltip component anchored to the widget launcher button. Cycle through `cta_messages` every 5-10 seconds. On click, open the widget and send the `action`/`payload` to the chatbot.
*   **Theming:** Read `theme_config`. If `mode` is "system", detect `prefers-color-scheme`. Apply the corresponding color palette to the widget header, bubbles, and buttons.

## 4. File Downloads & Direct Actions

**Requirement:** Demo flows will now send buttons that trigger direct file downloads (PDFs) or open external links.

**Response Handling:**
*   **Type `url`:** Buttons with `type: "url"` should open `url` in a new tab (`target="_blank"`).
*   **Attachments:** Responses may include an `adjuntos` array. Render these as downloadable cards (icon + title + download button).

```javascript
// Example Button Handling
if (button.type === 'url') {
  window.open(button.url, '_blank');
} else {
  // Standard chat action
  sendMessage(button.action_id);
}
```

## 5. Admin Panel Access

**Fix:** Administrative users (e.g., `mauricio@junin.com`) should now correctly access `/perfil` and `/employees`. The backend has been patched to resolve the 403 Forbidden errors. No frontend code changes are needed for this, but verification is recommended.
