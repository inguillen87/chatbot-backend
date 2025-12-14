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
    3.  **Crucial:** Send this UUID in the headers of **every** API request to `/api/market/...` and `/api/pwa/...`:
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
    *   **Do not** render the flat list anymore.

## 4. Professional Demos & Content

The backend has been seeded with rich content for the following demos. Ensure your routing (`/demo/<slug>`) connects to these specific slugs:

*   **Municipality Demo:**
    *   **Slug:** `municipio`
    *   **New Content:** "Entrada Teatro", "Bono Hospital" (Products), Voting Surveys, News.
    *   **Dashboard Preview:** Fetch `/api/v1/portal/municipio/content` to show the graphs.

*   **Winery Demo (Bodega):**
    *   **Slug:** `bodega`
    *   **New Content:** "Malbec Reserva", "Cabernet Sauvignon", "Caja Degustación" (Catalog).
    *   **Action:** Ensure the "Catalog" tab is visible by default or highlighted.

*   **Hardware Store Demo (Ferretería):**
    *   **Slug:** `ferreteria`
    *   **New Content:** "Taladro Percutor", "Set de Destornilladores" (Catalog).

## 5. UX/UI Improvements

*   **Widget Animation:** The user requested the widget to "go down" (minimize) instead of disappearing or moving up. This is a CSS transition in the frontend widget container.
*   **WhatsApp Integration:**
    *   The `widget-config` endpoint returns `marketplace.whatsapp_share_url`. Use this for the "Share" button in the demo.
