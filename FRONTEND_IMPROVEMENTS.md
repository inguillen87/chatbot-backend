# Frontend Improvements & Integration Guide

This document outlines the recent backend updates supporting the comprehensive platform improvement plan, including enhanced chat previews, integration previews, order management notifications, and the **new CRM/Memory system**.

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
    "dispatch_email": "deposito@municipio.gov.ar",
    "dispatch_phone": "+54911...",
    "send_buyer_email": true,
    "send_dispatch_email": true,
    "send_dispatch_whatsapp": true
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

---

## 4. CRM & Customer History (New)

The system now supports a unified **Customer Memory** feature.

### Endpoint: `GET /api/admin/tenants/<slug>/contacts`

Lists all resolved contacts with their metrics.

**Response:**
```json
{
  "contacts": [
    {
      "id": "uuid...",
      "name": "Juan Perez",
      "phone": "+54911223344",
      "type": "customer",
      "total_orders": 5,
      "ltv": 150000.00,
      "last_interaction": "2024-01-30T10:00:00Z"
    }
  ]
}
```

### Endpoint: `GET /api/admin/tenants/<slug>/contacts/<id>/history`

Returns the interaction history and structured data for a specific contact.

**Response:**
```json
{
  "contact": { ... },
  "snapshot": {
    "summary": "Cliente frecuente de vinos tintos. Prefiere delivery por la tarde.",
    "last_intent": "compra_vino",
    "suggested_actions": ["ofrecer_promo_malbec"]
  },
  "orders": [ ... ],
  "tickets": [ ... ],
  "interactions": [
    { "channel": "whatsapp", "direction": "inbound", "content": "Quiero comprar", "ts": "..." }
  ]
}
```

**Frontend Task:**
- Create a **"Clientes / CRM"** section in the admin panel.
- Show the list of contacts with key metrics (LTV, Orders).
- Create a Detail View for each contact showing their history and the AI-generated "Memory Snapshot".

---

## 5. Loyalty Points (New)

### Endpoint: `GET /api/admin/tenants/<slug>/loyalty/ledger`

Shows points transactions.

**Response:**
```json
{
  "transactions": [
    { "contact": "Juan Perez", "amount": +100, "reason": "purchase", "ref": "ORD-123" }
  ]
}
```
