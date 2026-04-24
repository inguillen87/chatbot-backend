# Frontend Integration Tasks (Urgent)

This document outlines the critical frontend changes required to consume the latest backend updates, specifically addressing the user's feedback regarding "Widget Styles," "Professional Demos," "Cart Reset," and "Hierarchy."

## 1. Widget Isolation (Fixing "Loses Styles")

The backend now explicitly sends a flag to enable Shadow DOM isolation for the widget to prevent CSS conflicts with the host page.

*   **Endpoint:** `GET /api/public/tenants/<slug>/widget-config`
*   **New Field:** `attributes["data-shadow-dom"]: "true"`
*   **Action Required:**
    *   In the widget loader script (`iframe.js` or similar), check for `data-shadow-dom="true"`.
    *   If true, mount the widget inside a Shadow Root:
        ```javascript
        const host = document.createElement('div');
        document.body.appendChild(host);
        const shadow = host.attachShadow({ mode: 'open' });
        // Mount your React/Vue app or iframe here
        shadow.appendChild(widgetContainer);
        ```
    *   **Note:** You must ensure your widget's internal CSS is injected into this Shadow Root, not the global `document.head`.

## 2. Cart Persistence (Fixing "Cart Resets to Zero")

The backend supports persistent anonymous sessions via the `X-Anon-Id` header. The "cart reset" issue occurs because the frontend is likely relying solely on cookies, which may be blocked or cleared.

*   **Action Required:**
    1.  Generate a UUID on first load if one doesn't exist (e.g., `uuidv4()`).
    2.  Store it in `localStorage.setItem('chatboc_anon_id', uuid)`.
    3.  **Crucial:** Send this UUID in the headers of **every** API request to `/api/market/...`, `/api/pwa/...`, and `/api/public/...`:
        ```json
        {
          "X-Anon-Id": "your-uuid-here"
        }
        ```
    *   **Result:** The cart items will persist even if the user refreshes the page or re-opens the browser.

## 3. Professional Category Hierarchy (Fixing "Giant Sausage List")

The backend now enforces a strict 2-root hierarchy ("Soluciones para Sector Público" vs "Soluciones para Empresas").

*   **Endpoint:** `GET /api/rubros?format=tree`
*   **Action Required:**
    *   Update the "Demos" page to fetch this endpoint with `format=tree`.
    *   Render the nested structure:
        *   **Root Level:** Display as Main Sections (Tabs or Cards).
        *   **Level 1 (Children):** Display as Categories (e.g., "Alimentación", "Retail").
        *   **Level 2 (Children of Children):** Display as actual clickable Demo links (e.g., "Bodega", "Ferretería").
    *   **Fixing "Undefined" URLs:**
        *   The `/api/rubros` response items now include a `demo` object.
        *   **Use `item.demo.slug` (or `item.demo.key`)** to construct the URL: `/demo/${item.demo.slug}`.
        *   Do *not* rely on `item.clave` if `item.demo` is present.

## 4. Professional Demos & Content

The backend has been seeded with rich content for the following demos. Ensure your routing (`/demo/<slug>`) connects to these specific slugs:

*   **Municipality Demo (`municipio`):**
    *   **Products:** "Entrada Teatro", "Bono Hospital".
    *   **Dashboard:** Fetch `/gov/analytics/heatmap` to display the "Mapa de Calor" requested by the user. If empty, the endpoint returns safe defaults, but seeded incidents should appear if `MunicipioTicket` records exist (seed script populates them).
    *   **Analytics:** Use `/gov/analytics/scorecards` for top-level metrics.

*   **Winery Demo (`bodega`):**
    *   **Catalog:** "Malbec Reserva", "Cabernet Sauvignon", "Caja Degustación".
    *   **Action:** Ensure the "Catalog" tab is visible by default or highlighted.

*   **Hardware Store Demo (`ferreteria`):**
    *   **Catalog:** "Taladro Percutor", "Set de Destornilladores".

## 5. UX/UI Improvements (Strict Requirements)

The user has explicitly requested high-end behavior for the widget:

*   **Minimize Animation:** The widget must **expand downwards** when opening and **collapse downwards** when minimizing. It should NOT disappear upwards or just vanish. Use CSS transitions on `height` and `transform: translateY`.
*   **Auto-Scroll:** The chat history must **always** scroll to the bottom automatically when a new message arrives (user or bot). The user should never have to manually scroll down to see the latest response.
*   **WhatsApp Integration:**
    *   The `widget-config` endpoint returns `marketplace.whatsapp_share_url`. Use this for the "Share" button in the demo.

## FAQ: Logic vs. Visuals

*   **Backend Responsibility:**
    *   Supplying the *structure* of the menu (`/api/rubros?format=tree`).
    *   Supplying the *colors and text* for the widget (`/api/public/tenants/.../widget-config`).
    *   Supplying the *data* for heatmaps (`/gov/analytics/heatmap`) and products (`/api/market/.../catalog`).
    *   Persisting the cart state via `X-Anon-Id`.

*   **Frontend Responsibility:**
    *   **Rendering** the menu tree as tabs/cards.
    *   **Implementing** the CSS animations (Shadow DOM, transitions).
    *   **Drawing** the Heatmap using a library like Leaflet or Google Maps, consuming the JSON from the backend.
    *   **Managing** the `localStorage` for `X-Anon-Id`.
