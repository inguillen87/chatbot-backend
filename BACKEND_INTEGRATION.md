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
