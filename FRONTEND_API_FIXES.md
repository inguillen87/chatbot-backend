# Frontend API Fixes & Updates

This document summarizes the recent backend fixes and API behavior updates to support the frontend integration.

## 1. Catalog Upload Route Aliases
To resolve the 404/405 errors during catalog upload, the backend now supports multiple route variations. You can use any of the following:

*   **Standard:** `/api/pymes/<id>/catalog-upload/subir_catalogo`
*   **Alternative:** `/api/pymes/<id>/catalog_upload/subir_catalogo`

Both routes support `POST` and `OPTIONS` (CORS preflight) methods directly.

## 2. Integration Connection (Numeric IDs)
The backend now accepts numeric indices for the `integration_type` parameter in the connection endpoint, mapping them to the correct provider slugs.

**Endpoint:** `GET /api/admin/tenants/<slug>/integrations/<integration_type>/connect`

*   `0` -> `mercadolibre`
*   `1` -> `tiendanube`
*   `2` -> `whatsapp`

*Note: While string slugs (e.g., `mercadolibre`) are preferred, the backend will now correctly process legacy calls using numbers.*

## 3. Document Intelligence Preview (Generic Context)
The preview endpoint now allows `pyme_id=0` to support contexts where the specific tenant ID is not yet resolved (e.g., during an initial setup wizard).

**Endpoint:** `POST /api/pymes/0/document-intelligence/preview`

This request will no longer return `403 Forbidden` provided a valid auth token is present.

## 4. Fulfillment Configuration
A new endpoint is available to manage dispatch settings (used for order notifications).

**Endpoint:** `GET /api/fulfillment-config` / `PUT /api/fulfillment-config`

**Payload:**
```json
{
  "dispatch_email": "depositos@empresa.com",
  "dispatch_phone": "+5491112345678",
  "send_buyer_email": true,
  "send_dispatch_email": true,
  "send_dispatch_whatsapp": true
}
```

## 5. Summary of Fixes
*   **Fixed:** `404 Not Found` on catalog upload (Route aliases added).
*   **Fixed:** `405 Method Not Allowed` on catalog upload (CORS/OPTIONS handled).
*   **Fixed:** `400 Bad Request` on integration connect (Numeric types mapped).
*   **Fixed:** `403 Forbidden` on file preview (Generic ID 0 allowed).
