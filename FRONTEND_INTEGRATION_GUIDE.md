# Frontend Integration Guide

This guide documents the API endpoints and patterns implemented to support the new unified commerce features.

## 1. Dispatch Configuration

**Purpose:** Configure where order notifications are sent (Warehouse/Logistics).

**Endpoint:** `PUT /api/admin/tenants/:slug/config`

**Payload:**
```json
{
  "tenant": {
    "dispatch_email": "deposito@empresa.com",
    "dispatch_phone": "+5491122334455"
  }
}
```

**Notes:**
- `dispatch_email`: Receives a "Pick List" HTML email with order details.
- `dispatch_phone`: Receives a WhatsApp/SMS alert (if configured).

## 2. Widget Customization (Chat Customizer)

**Purpose:** Customize the look and feel of the chat widget beyond basic colors.

**Endpoint:** `PUT /widget-settings`

**Payload:**
```json
{
  "theme_config": {
    "mode": "light",
    "animation": "slide_up",
    "font_family": "Inter, sans-serif",
    "light": {
      "primary": "#FF5733",
      "secondary": "#FFFFFF"
    },
    "dark": {
      "primary": "#C70039",
      "secondary": "#1F2937"
    }
  },
  "cta_messages": [
    "¡Hola! ¿En qué te puedo ayudar?",
    "🔥 Oferta especial hoy"
  ],
  "bubble_shape": "square" // or "round"
}
```

**Notes:**
- `theme_config`: JSON object storing advanced style properties. Frontend can store any valid JSON here.
- `cta_messages`: Array of strings for rotating call-to-action bubbles.

## 3. Catalog Mapping Preview

**Purpose:** Preview how a file will be parsed before saving it to the database.

**Endpoint:** `POST /api/admin/catalogo/importar`

**Form Data:**
- `archivo`: (File) The PDF/Excel/CSV file.
- `preview`: `true` (Boolean/String "true").
- `mapa_columnas`: (Optional JSON string) `{"sku": "Codigo", "nombre": "Descripcion"}`.

**Response (Preview Mode):**
```json
{
  "ok": true,
  "preview": true,
  "total_detected": 150,
  "items": [
    {
      "sku": "A100",
      "nombre": "Producto Ejemplo",
      "precio_unitario": 1200.50,
      "categoria": "General"
    },
    ... (first 100 items)
  ]
}
```

**Usage:**
1. Call with `preview=true` to show the "After" state in the UI wizard.
2. User confirms mapping.
3. Call again *without* `preview` (or `preview=false`) to commit changes to the database.

## 4. Order Automation & Idempotency

**Purpose:** Ensure orders are reliably created and processed without duplicates.

**Endpoint:** `POST /api/checkout` (or triggered via `mercadopago_webhook`)

**Persistence:**
- Orders are automatically persisted to the `PymePedido` table.
- Use `PymePedido` endpoints (`GET /api/pedidos`) to list them in the admin panel.

**Idempotency:**
- The system checks for an `idempotency_key` (derived from cart hash or session ID).
- If a duplicate request arrives (e.g., user double-clicks "Pay"), the existing order is returned instead of creating a duplicate.

**Notifications:**
- **Customer:** Receives HTML receipt via email.
- **Warehouse:** Receives "Pick List" if `dispatch_email` is set.
- **Admin:** Receives summary email.
