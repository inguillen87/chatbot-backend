# User Portal API Specification

This API powers the User Portal, WhatsApp interactions, and Widget flows.
Base URL: `/api/v1/portal/:tenant_id`

## Endpoints

### 1. Dashboard Content
`GET /api/v1/portal/:tenant_id/content`
Returns aggregated data (News, Events, Loyalty, Activities) to minimize round trips.
Supports optional authentication to return user-specific data (Loyalty points, Notifications).

### 2. News
`GET /api/v1/portal/:tenant_id/news`
Query Params: `page`, `limit`
Returns paginated news items.

### 3. Events
`GET /api/v1/portal/:tenant_id/events`
Query Params: `page`, `limit`, `status` (upcoming, past)
Returns events with registration status.

### 4. Catalog
`GET /api/v1/portal/:tenant_id/catalog`
Returns available products and services.

### 5. Notifications
`GET /api/v1/portal/:tenant_id/notifications`
`POST /api/v1/portal/:tenant_id/notifications/:id/read`
Manages user notifications. Requires authentication.

### 6. User Profile
`GET /api/v1/portal/:tenant_id/profile`
Returns user details and loyalty points. Requires authentication.

## Implementation Details
- **Tenant Context**: Resolved via the `:tenant_id` (slug) in the URL.
- **Authentication**: Bearer Token (JWT) expected for protected endpoints.
- **Data Source**:
    - News/Events: `MunicipioPost` table.
    - Catalog: `CatalogoItem` table.
    - Loyalty: Calculated from `EncEncuesta` rewards or `recompensas_service`.

## 7. Real-time Updates (Socket.IO)
To ensure the User Portal updates instantly when an Admin posts content, the backend emits Socket.IO events to the tenant's room.

**Room Name:** `tenant_slug` (e.g., "municipio", "ferreteria")

**Events Emitted:**
*   `tenant_content_update`: Generic signal that something changed. Payload: `{ "type": "news_update" }`
*   `news_update`: Signal that news items have changed. Payload: New/Updated Post object.
*   `events_update`: Signal that events have changed. Payload: New/Updated Post object.
*   `catalog_update`: Signal that catalog items have changed. Payload: `{}` or Item object.

**Triggers:**
*   `POST /municipal/posts` (Create/Update News/Events).
*   `POST /api/admin/market/catalog` (Create Product).
*   `PUT /api/admin/market/catalog/:id` (Update Product).
*   `DELETE /api/admin/market/catalog/:id` (Delete Product).
*   `POST /catalogo/upload` (Bulk Upload).

## 8. Catalog Management & CDN (Backend Tasks)
To avoid spending LLM tokens for routine updates, the backend should support a **tenant-scoped catalog file** stored on the CDN and a public link for sharing via WhatsApp, widget chat, and email.

- [ ] **CDN Upload + Storage:** When an admin uploads/replaces a catalog file, persist it to the CDN under a tenant-specific path (e.g., `cdn/<tenant_slug>/catalogo.<ext>`), and store the resulting public URL in the tenant config or catalog metadata.
- [ ] **Canonical Public Links:** Always return backend canonical links instead of direct CDN URLs (e.g. `https://chatboc.ar/{tenant_slug}/catalogo` and `https://api.chatboc.ar/api/public/tenants/{tenant_slug}/catalog/download?format=pdf`).
- [ ] **Public Catalog Link Endpoint:** Add/extend an endpoint to return the tenant’s public catalog URL and metadata (banner/descripcion) for frontend display and sharing.
- [ ] **Catalog File Metadata:** Store optional metadata (title/description/banner image) to allow richer link previews in WhatsApp/widget chat.
- [ ] **Permissions & Validation:** Ensure only authenticated admins can upload/replace catalogs, validate file type/size, and keep access read-only for the public CDN URL.
- [ ] **R2 + Cloudinary Fallback:** On download, redirect to R2 when available and fall back to Cloudinary if missing or unavailable.
- [ ] **Versioned Paths:** Store catalog artifacts under versioned paths (e.g., `tenants/{tenant_slug}/catalog/v{N}/catalog.pdf|json|xlsx|csv`) and resolve the published version in the download endpoint.
- [ ] **Storage Abstraction:** Implement simple provider methods (`put`, `exists`, `public_url`) for R2 primary and Cloudinary fallback to keep the download redirect logic consistent.
