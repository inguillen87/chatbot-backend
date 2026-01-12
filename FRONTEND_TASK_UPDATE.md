# FRONTEND TASK UPDATE - 2024-01-13

## 1. Qdrant Search Enhancements
The backend search logic for Pymes has been upgraded to support natural language filters and improved response formatting.

*   **Endpoint:** `/catalogo/buscar` (and internal bot usage)
*   **New Filters Supported:**
    *   `en_promocion=True`: Filters items with active promotions (triggered by keywords like "oferta", "promo").
    *   `con_stock=True`: Filters items with positive stock (triggered by "stock", "disponible").
    *   `precio_max=FLOAT`: Filters items below a certain price (triggered by "menor a X").
*   **Response Format:**
    *   **WhatsApp:** Returns a clean bulleted list (e.g., `• *Vino*: $1000`) instead of a Markdown table.
    *   **Web/Widget:** Continues to support Markdown tables or interactive lists.
    *   **LLM Summary:** The bot now generates a natural language introduction (e.g., *"Encontré estos vinos que podrían interesarte..."*) before listing products.

## 2. Super Admin Panel Updates
The following API endpoints have been updated or verified to support the full Super Admin management flow.

*   **List Tenants:** `GET /api/admin/tenants`
    *   **Update:** Now includes `owner_email` in the response object for each tenant.
    *   *Usage:* Display the owner's email in the main tenants table for quick contact.

*   **Update Tenant:** `PUT /api/admin/tenants/<slug>`
    *   **Payload:** `{ "is_active": boolean, "plan": "string" }`
    *   *Behavior:*
        *   Updating `plan` triggers a cascade update to the owner user and associated employees.
        *   Updating `is_active` immediately suspends/reactivates the tenant access.

*   **Existing Endpoints Confirmed:**
    *   Create Admin: `POST /api/admin/tenants/<slug>/admin-user`
    *   Reset Password: `PUT /api/admin/tenants/<slug>/password`
    *   Configure WhatsApp: `PUT /api/admin/tenants/<slug>/whatsapp`
    *   Impersonate: `POST /api/admin/tenants/<slug>/impersonate`

## 3. Widget Integration Notes
*   **Shadow DOM:** Ensure `data-shadow-dom="true"` is set when embedding the widget to prevent style conflicts.
*   **Theme Config:** The public config endpoint returns `theme_config` which should be used to render Dark/Light modes correctly.
