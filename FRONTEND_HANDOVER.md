# Frontend Handover Notes

## 1. Subscription Plans Update
The subscription plans returned by `GET /auth/plans` (and related endpoints) have been reordered to prioritize the highest value plan.
- **Order:** `Full` (First) -> `Pro` -> `Gratis` (Last).
- **Prices:** The backend correctly serves the updated prices:
    - **Plan Full:** $350.000 (ID: `2c9380849763daeb0197658791ee00b1`)
    - **Plan Pro:** $300.000 (ID: `2c9380849764e81a01976585767f0040`)
    - **Plan Demo:** Gratis
- **Action Required:** Ensure the frontend renders the list in the order provided by the API and does not force a local sort.

## 2. Persistent Marketplace (Cart & Orders)
The backend now supports fully persistent, database-backed Shopping Carts and Orders, enabling a robust multi-tenant marketplace experience.
- **Models Added:** `MarketCart`, `MarketCartItem`, `MarketOrder`, `MarketOrderItem`.
- **Behavior:**
    - The `X-Anon-Id` header is used to persist carts for anonymous users.
    - Upon login, anonymous carts are automatically merged/adopted by the authenticated user.
    - Orders are now stored in `market_order` tables with status tracking (`pending`, `paid`, etc.).
- **Endpoints:** The existing `routes/market.py` endpoints now write to these tables. No URL changes are required, but error handling might be more robust (500s will occur if tables are missing, which is now fixed).

## 3. New Integration & Notification Modules
- **IntegrationAccount:** A new model exists to store credentials for third-party integrations (MercadoLibre, TiendaNube).
- **NotificationLog:** All system notifications (WhatsApp, Email, Telegram) are logged to the `notification_log` table.
- **Integrations API:**
    - `POST /api/integrations/<provider>/connect`: Generate OAuth URL.
    - `POST /api/integrations/webhooks/<provider>`: Handle external events (MercadoLibre orders).
- **Admin Order API:**
    - `GET /api/admin/tenants/<slug>/orders`: List/Filter orders.
    - `POST /api/admin/tenants/<slug>/orders`: Create manual order (JSON payload: `contact_name`, `items`: `[{name, price, quantity}]`).
    - `PUT /api/admin/tenants/<slug>/orders/<id>`: Update order status.
- **Notification Settings API:**
    - `GET /api/admin/tenants/<slug>/notifications`: Get current config.
    - `PUT /api/admin/tenants/<slug>/notifications`: Update `owner_phone`, `telegram_chat_id`, etc.
- **Push Notifications:**
    - Telegram and WhatsApp push notifications to the owner are now supported via `services/notification_dispatcher.py`.
    - Backend logic reads from the tenant configuration managed via the API above.

## 4. Mirror Catalog Strategy
- **New Fields:** `CatalogoItem` now has `checkout_type` (e.g., 'mercadolibre') and `external_url`.
- **Behavior:**
    - When rendering products in the Chatbot or Web Catalog, check `checkout_type`.
    - If `checkout_type` is 'mercadolibre' or 'tiendanube', render a **URL Button** (e.g., "Ver en ML") pointing to `external_url` instead of an "Add to Cart" button.
    - This allows preserving the traffic within Chatboc while delegating the payment to the external platform preferred by the seller.
