# Frontend API Fixes & Backend Enhancements

## 1. Map/Heatmap Filters (Fix for 404/CORS)
The following endpoints have been added/fixed to support the heatmap filters:

*   **`GET /api/pyme/estados`**
    *   **Query Params:** `tenant_slug` (required)
    *   **Response:** `{"estados": ["abierto", "cerrado", ...]}`
    *   **Fix:** Added CORS headers and tenant resolution.

*   **`GET /api/pyme/categorias`**
    *   **Query Params:** `tenant_slug` (required)
    *   **Response:** `{"categorias": [{"id": 1, "nombre": "Ventas", ...}]}`
    *   **Fix:** Added CORS headers and tenant resolution.

## 2. Order Visibility in Admin Panel (Fix for "Sales" Section)
Previously, orders created via the Chatbot (`PymePedido`) were not visible in the new Admin Panel "Sales" section because they were missing `tenant_id` and were not synced to the new `Order` model.

*   **Sync Mechanism:** Backend now automatically creates a corresponding `Order` record whenever a `PymePedido` is generated.
*   **API Endpoint:** `GET /api/admin/tenants/<slug>/orders` now returns a unified list of both Legacy (`PymePedido`) and New (`Order`) records.
*   **Frontend Action:** No changes needed on frontend if it uses the standard Admin Panel endpoints. Just verify the "Sales" list now populates.

## 3. Catalog Data Quality (Intelligent Processor)
The PDF/Excel catalog processor has been significantly enhanced:

*   **Price Parsing:** Improved support for Argentine format (e.g. `$ 10.410` is correctly parsed as 10,410.00, not 10.41).
*   **Metadata Inference:** The AI now aggressively infers **Brand** (`marca`) and **Category** (`categoria`) from product names/descriptions if missing in the source file.
*   **Persisted Data:**
    *   `precio_monetario`: Stored as float for correct sorting/filtering.
    *   `extra_metadata`: Full JSON structure (including inferred fields) is saved to the database.

## 4. Order Listing (Alias)
*   **`GET /api/orders`** is now an alias for the admin order list, requiring authentication. This resolves the 405 error if the frontend was hitting this route.
