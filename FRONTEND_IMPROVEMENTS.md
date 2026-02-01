# Frontend Improvements & Integration Guide

This document outlines the recent backend updates supporting the comprehensive platform improvement plan, including enhanced chat previews, integration previews, and order management notifications.

## 1. Chat Customization & Preview

The backend supports dynamic chat widget customization via the `TenantConfig` and `TenantProfile` models.

### Endpoint: `GET /api/admin/tenants/<slug>/config`

Returns the full configuration bundle, including the new dispatch fields and theme settings.

**Response Structure (partial):**
```json
{
  "tenant": {
    "slug": "municipio-demo",
    "nombre": "Municipio Demo",
    "logo_url": "...",
    "dispatch_email": "deposito@municipio.gov.ar", // NEW
    "dispatch_phone": "+54911...", // NEW
    "send_buyer_email": true, // NEW
    "send_dispatch_email": true, // NEW
    "send_dispatch_whatsapp": true // NEW
  },
  "configs": {
    "widget": {
      "default": {
        "primaryColor": "#3B82F6",
        "secondaryColor": "#ffffff",
        "welcomeMessage": "Hola! En qué puedo ayudarte?",
        "logoUrl": "...",
        "fontFamily": "Inter, sans-serif"
      }
    }
  }
}
```

### Endpoint: `PUT /api/admin/tenants/<slug>/config`

Updates the configuration. Frontend should include preview controls (color pickers, text inputs) that update the local state for "Live Preview" before sending this PUT request to save.

**Request Payload:**
```json
{
  "tenant": {
    "dispatch_email": "nuevo@deposito.com",
    "send_buyer_email": false
  },
  "configs": {
    "widget": {
      "primaryColor": "#FF5733",
      "welcomeMessage": "Bienvenido a la tienda!"
    }
  }
}
```

---

## 2. Integration Preview (MercadoLibre)

Before syncing the full catalog, users can now preview how items will be mapped.

### Endpoint: `GET /api/admin/tenants/<slug>/integrations/<type>/preview`

Supported types: `mercadolibre`

**Response Example:**
```json
{
  "success": true,
  "summary": {
    "total_found": 50,
    "new_items": 10,
    "updates": 5
  },
  "items": [
    {
      "external_id": "MLA123456",
      "title": "Remera Algodón",
      "original_price": 15000,
      "mapped_category": "Indumentaria",
      "mapped_price": 15000,
      "image_url": "https://...",
      "status": "active",
      "will_create_new": true
    }
  ]
}
```

**Frontend Task:**
- Display these items in a table/grid.
- Highlight "New" vs "Update".
- Allow users to uncheck items they don't want to import (logic would be client-side filter before calling Sync, or just informational).

---

## 3. Order Notifications & Dispatch

New fields have been added to the `TenantProfile` to manage order notifications.

- **`dispatch_email`**: Email address for the warehouse/dispatch center.
- **`dispatch_phone`**: Phone number for the warehouse (for WhatsApp notifications).
- **`send_buyer_email`**: Toggle to enable/disable confirmation emails to the buyer.
- **`send_dispatch_email`**: Toggle to enable/disable new order emails to the warehouse.
- **`send_dispatch_whatsapp`**: Toggle to enable/disable new order WhatsApp messages to the warehouse.

**Frontend Task:**
- Add a "Configuración de Pedidos y Envíos" section in the Tenant Settings / Integrations page.
- Expose these fields as inputs and toggles.
- Use `GET/PUT /api/admin/tenants/<slug>/config` to read/write these values.

## 4. Order Management

Orders created via the Chatbot or Web Checkout are stored in `PymePedido`.
Notifications are dispatched automatically by the backend upon creation (`NotificationDispatcher`).

- **Buyer**: Receives Email (if enabled) and WhatsApp (if phone provided).
- **Dispatch**: Receives Email (if enabled & configured) and WhatsApp (if enabled & configured).
- **Admin**: Receives Email (legacy owner notification).

Ensure the "Pedidos" section in the admin panel lists these records (endpoint `GET /api/pedidos` or similar exists).
