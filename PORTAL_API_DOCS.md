# Portal User API Documentation

Base URL: `/api/v1/portal/<tenant_slug>`

This API powers the User Portal / PWA. It provides all necessary content, settings, and interactions for the user.

## Endpoints

### 1. Dashboard Content
**GET** `/content`
Returns aggregated data for the home screen.
- **Response**: JSON object containing:
    - `news`: List of recent news.
    - `events`: List of upcoming events.
    - `notifications`: User-specific notifications (ticket updates) and general alerts.
    - `catalog`: Featured catalog items.
    - `loyaltySummary`: Points and status.
    - `config`: Theme colors and user preferences (e.g., dark mode).

### 2. Settings & Preferences
**GET** `/settings`
Returns user preferences and tenant theme.

**PUT** `/settings`
Update user preferences.
- **Headers**: `Authorization: Bearer <token>`
- **Body**:
  ```json
  {
    "theme_mode": "dark",
    "animations_enabled": true
  }
  ```
- `theme_mode` options: `"dark"`, `"light"`, `"system"`.

### 3. News & Events
**GET** `/news`
- Params: `page`, `limit`.
- Returns paginated news list.

**GET** `/events`
- Params: `page`, `limit`, `status` (`upcoming` or `past`).
- Returns paginated events list.

### 4. Catalog & Orders
**GET** `/catalog`
- Returns full product list available for the tenant.

**GET** `/orders`
- Returns list of past orders for the logged-in user.

**POST** `/orders`
- Create a new order.
- **Body**:
  ```json
  {
    "items": [
      { "product_id": 1, "quantity": 2, "price": 100 }
    ],
    "total": 200
  }
  ```

### 5. Integration Info
**GET** `/integration`
Returns details to populate an integration/setup page.
- **Response**:
  ```json
  {
    "slug": "demo",
    "portalUrl": "...",
    "widgetScript": "...",
    "qrCodeUrl": "...",
    "whatsappLink": "..."
  }
  ```

## Authentication
Most endpoints require authentication. Include the JWT token obtained during login/register in the header:
`Authorization: Bearer <token>`
