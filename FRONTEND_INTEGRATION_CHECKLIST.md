# Frontend Integration Checklist & Mock Replacement Guide

This document lists every mocked component in the frontend and maps it to the newly deployed backend endpoints. Use this to finalize the integration.

---

## 1. CRM & Customer History (CustomerHistoryPanel)

**Current Status:** Mocks `setData({...})` with fake orders and summary.
**Target Endpoint:** `GET /api/admin/tenants/<slug>/contacts/<contact_id>/history`

### Tasks
- [ ] Replace `setTimeout` mock with `fetch/axios` call to the endpoint.
- [ ] Map response data to UI state:
    - `summary.total_spent` -> `contact.ltv_monetary` (or calculate from `orders`)
    - `summary.total_orders` -> `contact.total_orders` (or `orders.length`)
    - `recent_orders` -> `orders` array.
    - `snapshot` -> `snapshot.summary_text` (New AI feature!)

**Payload Example (Response):**
```json
{
  "contact": { "id": "...", "name": "Juan", "ltv": 15000 },
  "orders": [
    { "id": "...", "created_at": "2024-01-01", "total": 5000, "status": "confirmed" }
  ],
  "snapshot": {
    "summary": "Cliente frecuente, prefiere pagos con QR.",
    "suggested_actions": ["Ofrecer promo vinos"]
  }
}
```

---

## 2. Analytics Dashboard (AnalyticsPage)

**Current Status:** Mocks `generateMockData()` with random numbers for KPIs and Heatmaps.
**Target Endpoint:** `GET /api/admin/analytics/summary` (or legacy `/api/municipal/stats` for simple stats).

### Tasks
- [ ] Call `GET /api/admin/analytics/summary?from=...&to=...` on mount.
- [ ] Map KPIs:
    - `total_interactions` -> `stats.interactions_count`
    - `top_categories` -> `stats.top_intents`
- [ ] Map Heatmap:
    - `heatmap_points` -> `stats.geo_heatmap` (returns `{lat, lng, weight}`)

---

## 3. Order Management (OrderList)

**Current Status:** May be using legacy `PymePedido` mocks or partial API.
**Target Endpoint:** `GET /api/admin/orders`

### Tasks
- [ ] Switch to the unified `Order` endpoint which returns both legacy and new persistent orders.
- [ ] Implement "Mark as Shipped" using `PATCH /api/admin/orders/<id>` with body `{"status": "shipped"}`.

---

## 4. Chat Customization (ChatCustomizer)

**Current Status:** Local state or partial config.
**Target Endpoint:** `GET /api/admin/tenants/<slug>/config`

### Tasks
- [ ] Load initial state from `configs.widget.default`.
- [ ] Save changes via `PUT /api/admin/tenants/<slug>/config` (updates `theme_json` and `TenantConfig`).
- [ ] Ensure "Live Preview" uses the local state before saving.

---

## 5. Integration Preview (MercadoLibre)

**Current Status:** Static mock or non-existent.
**Target Endpoint:** `GET /api/admin/tenants/<slug>/integrations/mercadolibre/preview`

### Tasks
- [ ] Add a "Preview Sync" button in the Integrations card.
- [ ] Display the returned `items` list (showing `new` vs `update` status).
- [ ] Show summary stats (`total_found`, `new_items`) in a modal.

---

## 6. Catalog Import Wizard

**Current Status:** File upload might be mocked or point to old endpoint.
**Target Endpoints:**
1. `POST /api/admin/catalog/import` (Upload file) -> Returns `upload_id` + `preview_data`.
2. Display `preview_data` in editable table.
3. `POST /api/admin/catalog/import/<upload_id>/commit` (Finalize) -> Saves to DB + Qdrant.

### Tasks
- [ ] Implement the 3-step wizard (Upload -> Preview -> Commit).
- [ ] Handle validation errors returned in the preview step.
