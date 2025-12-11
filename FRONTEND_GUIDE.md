# Frontend Integration Guide for Demo Stability & Engagement

This document outlines the frontend requirements to fully leverage the backend stability fixes and engagement features introduced in the `fix-demo-schema-and-cart-persistence` update.

## 1. Cart Persistence (Critical)

To prevent shopping carts from resetting on page reloads or when the widget is reopened, the frontend **must** ensure a stable anonymous identifier is sent with every request.

*   **Header Name:** `X-Anon-Id`
*   **Value:** A UUID v4 string generated on the client side.
*   **Logic:**
    1.  On app/widget initialization, check `localStorage` for an existing `anon_id`.
    2.  If not found, generate a new UUID v4.
    3.  Save it to `localStorage` (key: `chatboc_anon_id` or similar).
    4.  **Include this value in the `X-Anon-Id` header for ALL API requests**, especially to `/api/market/...` and `/api/auth/...`.

**Why?** The backend now prioritizes this header over the volatile Flask session cookie. If missing, the backend will generate a new session for every request if cookies are blocked (common in embedded widgets), causing data loss.

## 2. Widget Engagement Features (New)

The backend now serves configured engagement settings via `/api/widget/config` (or the bootstrap endpoint). The frontend should consume these to render the UI.

### Endpoint: `GET /api/public/tenants/<slug>/widget-config`

**New Fields in Response:**

*   `cta_messages` (Array of Objects): A list of rotating tooltips/call-to-action messages to display near the widget launcher.
    ```json
    [
      {
        "text": "📅 Ver agenda cultural",
        "action": "trigger_intent",
        "payload": "agenda_cultural"
      },
      {
        "text": "🛍️ Ver catálogo",
        "action": "open_catalog",
        "payload": ""
      }
    ]
    ```
    *   **Implementation:** Rotate through these messages every few seconds. On click, execute the defined `action`.

*   `theme_config` (Object): Color schemes for light/dark modes.
    ```json
    {
      "mode": "system",
      "light": {
        "primary": "#0066ff",
        "secondary": "#ffffff",
        "background": "#ffffff",
        "text": "#000000"
      },
      "dark": { ... }
    }
    ```
    *   **Implementation:** Apply these colors to the widget UI instead of hardcoded defaults.

*   `default_open` (Boolean): If `true`, the widget should automatically open (expand) upon the first load of the page to increase visibility.

## 3. Admin Permissions (Fixed)

*   **User:** `mauricio@junin.com` (and similar legacy admins).
*   **Fix:** The backend now correctly links legacy users to their tenants.
*   **Action:** Verify that accessing `/perfil` and `/api/admin/employees` now returns `200 OK` instead of `403 Forbidden`.
*   **New Fallback:** Administrative requests to `/api/*` will now automatically resolve the tenant context from the logged-in user if explicit headers (`X-Tenant`) are missing. This improves resilience for dashboard pages.

## 4. Demo Data Structure

*   The backend now seeds demo data with a hierarchical structure:
    *   **Roots:** "Municipios", "Locales Comerciales"
    *   **Subcategories:** "Gastronomía", "Indumentaria", etc.
*   **Action:** Ensure the category selector in the frontend can handle this hierarchy (parent/child relationship) if it displays categories. The API returns flat lists with `padre_id` or logical grouping; verify it renders correctly.
