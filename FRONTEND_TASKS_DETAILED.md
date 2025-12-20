# Frontend Tasks Detailed List

Based on the implemented Backend API for the Multi-tenant Modules (Pedidos, Portal, Notificaciones, Integraciones).

## 1. Routing & Tenant Context
*   **Tenant Resolution**: Ensure the frontend router captures the `tenant_slug` from the URL (e.g., `/pyme/:slug/*`, `/portal/*` if using subdomains, or `/portal/:slug/*`).
*   **Context Provider**: Store the current tenant slug and type (pyme/municipio) in a React Context to be accessible by all components.
*   **Auth**: Send `Authorization: Bearer <token>` in all authenticated requests.

## 2. Module: Pedidos (Market/E-commerce)
**Public Views (Client)**
*   **Catalog**: Fetch from `GET /api/market/{slug}/catalog`. Render products with images, prices (money/points), and stock status.
*   **Cart**:
    *   Fetch `GET /api/market/{slug}/cart`.
    *   Add Item: `POST /api/market/{slug}/cart/add` (`{product_id, quantity}`).
    *   Update Item: `POST /api/market/{slug}/cart/update` (`{product_id, quantity}`).
    *   Clear: `POST /api/market/{slug}/cart/clear`.
*   **Checkout**:
    *   Call `POST /api/market/{slug}/checkout/start` with contact info.
    *   **MercadoPago**: If response contains `checkout_options.init_point` (and `mercadopago_ready: true`), redirect user to that URL.
    *   **Points**: If total is in points, handle success directly.

**Admin Views (Pyme Admin)**
*   **Order List**: Fetch `GET /api/admin/market/orders`. Display status, total, contact.
*   **Order Detail/Update**:
    *   Allow changing status (e.g., to "shipped", "completed").
    *   Call `PUT /api/admin/market/orders/{id}` with `{status: "new_status"}`.

## 3. Module: Integrations (Admin)
*   **Integrations Page**:
    *   Fetch status: `GET /api/admin/tenants/{slug}/integrations`.
    *   Display "Connect" button for Disconnected services.
    *   Display "Sync" button for Connected services (Catalog).
*   **Connect Flow**:
    *   Click "Connect" -> Call `GET /api/admin/tenants/{slug}/integrations/{type}/connect`.
    *   Redirect browser to the returned `redirect_url` (OAuth).
*   **Sync Action**:
    *   Click "Sync" -> Call `POST /api/admin/tenants/{slug}/integrations/{type}/sync`.
    *   Show progress/result (items imported).

## 4. Module: User Portal
*   **Dashboard**: Fetch `GET /api/v1/portal/{slug}/content`. Render widgets for News, Events, Notifications, Loyalty Summary.
*   **Loyalty Section**:
    *   Fetch `GET /api/v1/portal/{slug}/loyalty`. Display balance and transaction history table.
    *   **Redeem**: Implement button to call `POST /api/v1/portal/{slug}/redeem` with `{benefit_id}` (requires mapping benefits to catalog items).
*   **Surveys**:
    *   List pending surveys from `/content` or dedicated list.
    *   Render survey form dynamically based on JSON structure.
    *   Submit response to `POST /api/v1/portal/{slug}/surveys/{survey_slug}/responses`.

## 5. Notifications (Settings)
*   **Preferences**:
    *   In User Profile, allow toggling email/whatsapp notifications.
    *   Update via `PUT /api/v1/portal/{slug}/settings`.

## 6. General
*   **Error Handling**: Handle 403 (Unauthorized/Tenant Mismatch) gracefully by redirecting to login or home.
*   **Shadow DOM**: For embedded widgets, ensure styles are isolated (data-shadow-dom="true").

## 7. Widget / Chat Integration
*   **Conversational Sales Flow**:
    *   Implement bot logic to suggest products from the catalog based on user queries.
    *   Render "Add to Cart" buttons/actions within the chat interface.
*   **Checkout Options (CTA)**:
    *   When the user initiates purchase, provide choices:
        *   **Option A**: Internal Checkout (via Chatboc `/checkout/start`).
        *   **Option B**: "Pay on MercadoLibre" (link to external publication if available).
        *   **Option C**: "Pay on TiendaNube" (link to external product if available).
    *   Ensure all options trigger a notification to the owner (via backend).
