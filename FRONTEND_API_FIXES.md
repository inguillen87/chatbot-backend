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

## Security & Auth Hardening (Feb 2026)

### 1. Token Isolation on Public Landing
The backend now enforces strict origin checks for the public landing page ().
- **Behavior:** If  is  (or ), any  cookie sent by the browser is **ignored** by the backend.
- **Impact:** This prevents "leaked" admin sessions (e.g., from ) from accidentally loading a private tenant context (like Junín) on the public marketing site.
- **Frontend Action:** Ensure the public widget uses  for its initial  calls to avoid sending unnecessary cookies, though the backend now handles this safely.

### 2. JSON Error Contract
All API errors now follow a unified JSON structure.
**Old:**  or
**New:**
```json
{
  "error": {
    "code": 400,
    "message": "Detailed error description here"
  }
}
```
- **Frontend Action:** Update error handling logic to read  as the primary error text.

### 3. Payload Normalization
- The backend now explicitly returns  if  or  are missing in .
- **Frontend Action:** Ensure  always includes a fallback  field (plain text) even if sending a complex  array, to prevent "Normalized payload produced no messages" errors.

### 4. Tenant Widget Config
- **Endpoint:** `GET /api/public/tenants/{tenant_slug}/widget-config`
- **Safety:** Returns only public styling (colors, texts). No internal IDs.
- **Usage:** Use this to hydrate the widget's theme before the chat session starts.

## Security & Auth Hardening (Feb 2026)

### 1. Token Isolation on Public Landing
The backend now enforces strict origin checks for the public landing page (chatboc.ar).
- **Behavior:** If Origin is https://chatboc.ar (or www.chatboc.ar), any auth_token cookie sent by the browser is **ignored** by the backend.
- **Impact:** This prevents "leaked" admin sessions (e.g., from app.chatboc.ar) from accidentally loading a private tenant context (like Junín) on the public marketing site.
- **Frontend Action:** Ensure the public widget uses credentials: "omit" for its initial fetch calls to avoid sending unnecessary cookies, though the backend now handles this safely.

### 2. JSON Error Contract
All API errors now follow a unified JSON structure.
**Old:** {"error": "bad_request", "detail": "..."} or {"error": "message"}
**New:**
```json
{
  "error": {
    "code": 400,
    "message": "Detailed error description here"
  }
}
```
- **Frontend Action:** Update error handling logic to read response.data.error.message as the primary error text.

### 3. Payload Normalization
- The backend now explicitly returns 400 Bad Request if question or messages are missing in /api/ask.
- **Frontend Action:** Ensure sendMessage always includes a fallback question field (plain text) even if sending a complex messages array, to prevent "Normalized payload produced no messages" errors.

### 4. Tenant Widget Config
- **Endpoint:** GET /api/public/tenants/{tenant_slug}/widget-config
- **Safety:** Returns only public styling (colors, texts). No internal IDs.
- **Usage:** Use this to hydrate the widget's theme before the chat session starts.
