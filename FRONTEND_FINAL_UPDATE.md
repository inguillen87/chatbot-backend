# Frontend API Update & Confirmation

This document outlines critical updates and fixes applied to the Backend API. Please update your integration logic accordingly.

## 1. Role Identifier Update
The **Super Admin** role identifier has been standardized in the database and authentication logic.
*   **Old/Potential Value:** `superadmin`
*   **New Correct Value:** `super_admin` (with underscore)

**Action:** If you are checking `user.role` or `user.rol` in the frontend to display the "Super Admin" link or dashboard, please ensure you check for `super_admin`.

## 2. Order Management API Fix
The endpoints for managing orders were previously failing due to a parameter mismatch. This has been fixed.
*   **Endpoints:**
    *   `GET /api/admin/tenants/<slug>/orders`
    *   `POST /api/admin/tenants/<slug>/orders`
    *   `PUT /api/admin/tenants/<slug>/orders/<id>`
*   **Status:** Confirmed working. You can now fetch, create, and update orders successfully.

## 3. Health Check Endpoint
A new endpoint is available for monitoring application status.
*   **Endpoint:** `GET /api/health`
*   **Response:** `{"status": "ok", "db": "connected"}`
*   **Usage:** You can use this for a "System Status" indicator in the admin panel if desired.

## 4. Integration Logic Reminder
*   **MercadoLibre:** The OAuth flow and Webhook processing are active. Ensure the "Connect" button in the frontend redirects to the URL returned by `/api/integrations/mercadolibre/connect`.
*   **Buttons:** Remember to implement the **Mirror Catalog** logic: if `item.checkout_type == 'mercadolibre'`, show a link button, not an add-to-cart button.
