# Frontend Update Guide: Pyme Order Tracking & External Catalogs

This document outlines the API changes and integration requirements for the new Pyme features: Public Order Tracking and External Marketplace Links.

## 1. Public Order Tracking

A new public API endpoint allows fetching order details using the order ticket number (`nro_pedido`). This supports the creation of a "Tracking Page" where customers can view the status of their order in real-time.

### Endpoint
`GET /api/public/pyme/pedidos/<nro_pedido>`

**Parameters:**
* `nro_pedido` (Path): The unique order identifier (e.g., `PED-20251230122421-A6A490`).

### Response Example

```json
{
  "id": 4,
  "nro_pedido": "PED-20251230122421-A6A490",
  "estado": "pendiente", // pendiente, confirmado, en_proceso, enviado, entregado
  "asunto": "Pedido de Marcelo",
  "monto_total": 31500.0,
  "fecha_creacion": "2025-12-30T12:24:21.123456",
  "nombre_cliente": "Marcelo",
  "email_cliente": "marcelo@example.com",
  "telefono_cliente": "+549261...",
  "direccion": "Av. San Martín 123",
  "detalles": [
    {
      "nombre_producto": "Kit premium",
      "cantidad": 1,
      "precio_unitario_original": 31500.0,
      "subtotal_con_descuento": 31500.0,
      "moneda": "ARS",
      "presentacion": "Caja regalo",
      "sku": "DEMO-002"
    }
  ],
  // Tenant Branding Info (for rendering the header/theme)
  "pyme_nombre": "Servill Indumentaria",
  "tenant_slug": "servill",
  "tenant_logo": "https://res.cloudinary.com/...",
  "tenant_theme": {
    "primaryColor": "#1b325f",
    "secondaryColor": "#ffffff"
  }
}
```

### Frontend Implementation Requirements
*   **Route:** Create a frontend route (e.g., `/pyme/pedidos/:nro_pedido`) that consumes this endpoint.
*   **UI:** Render the order status, total, item list, and shipping details.
*   **Branding:** Use `tenant_logo`, `pyme_nombre`, and `tenant_theme` to style the page according to the Pyme's brand.
*   **States:** visually represent the `estado` (e.g., a progress bar or timeline).

---

## 2. External Marketplace Links (Mirror Catalog)

The catalog API now supports "Mirror Catalog" functionality, where items can redirect to external e-commerce sites (MercadoLibre, TiendaNube) instead of adding to the internal cart.

### Endpoint
`GET /api/pwa/public/catalog` (Existing endpoint, updated response)

### Updated Response Object

Each product object in the list now includes:

*   `checkout_type`: String. Values: `"chatboc"` (default), `"mercadolibre"`, `"tiendanube"`.
*   `external_url`: String. The URL to the external product page. Null if `checkout_type` is `"chatboc"`.

### Example

```json
[
  {
    "catalogo_item_id": 101,
    "nombre": "Jean Clásico",
    "precio_unitario": 45000.0,
    "stock": "Disponible",
    "imagen_url": "https://...",
    // New fields
    "checkout_type": "mercadolibre",
    "external_url": "https://articulo.mercadolibre.com.ar/MLA-..."
  },
  {
    "catalogo_item_id": 102,
    "nombre": "Remera Básica",
    "precio_unitario": 18000.0,
    "checkout_type": "chatboc", // Standard internal cart behavior
    "external_url": null
  }
]
```

### Frontend Implementation Requirements
*   **Product Card:** Check `checkout_type`.
    *   If `"chatboc"`: Show "Agregar al Carrito" (+ / - buttons).
    *   If `"mercadolibre"`: Show a "Ver en MercadoLibre" button (yellow styling recommended) that opens `external_url` in a new tab.
    *   If `"tiendanube"`: Show a "Ver en Tienda" button that opens `external_url`.

---

## 3. General Notes

*   **Tenant Isolation:** The backend now strictly separates configuration. Ensure you are passing the correct `tenant_slug` or `widget_token` in headers/query params when fetching public configs to get the correct branding and catalog.
*   **Tracking Links:** The backend-generated messages now point to `APP_BASE_URL/pyme/pedidos/<ticket>`. Ensure the frontend router handles `/pyme/pedidos/:ticket`.
