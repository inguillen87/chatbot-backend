# Frontend Implementation Guide: V2 Features

This document outlines the API contracts and implementation details for the new backend features: **Catalog Upload Preview**, **CRM & Orders**, and **Widget Customization**.

Use this guide to update the frontend application (Admin Portal and Widget).

---

## 1. Catalog Upload & Preview (Document Intelligence)

**Objective:** Allow users to upload a raw file (PDF, Excel, CSV), see a structured preview of the data, and confirm the import.

### Step 1: Upload & Preview (Wizard Flow)
**Endpoint:** `POST /api/admin/catalog/import`
**Auth:** Bearer Token
**Content-Type:** `multipart/form-data`

**Request Body:**
- `file`: The file object (CSV, XLSX, PDF).

**Response (200 OK):**
```json
{
  "id": 45,
  "upload_id": 45,
  "filename": "catalogo.xlsx",
  "status": "ready_to_commit",
  "preview_data": [
    { "title": "Vino Malbec", "price": "5000", "sku": "V001" }
  ],
  "warnings": []
}
```

### Alternative: Raw Document Intelligence Preview (Legacy)
**Endpoint:** `POST /api/pymes/{pyme_id}/document-intelligence/preview`
**Auth:** Bearer Token
**Content-Type:** `multipart/form-data`

**Request Body:**
- `file`: The file object (CSV, XLSX, PDF).
- `maxRows`: (Optional) Integer, default 50.
- `headerRow`: (Optional) Integer, index of the header row (0-based).

**Response (200 OK):**
```json
{
  "pymeId": 123,
  "totalRows": 150,
  "columns": [
    { "key": "nombre", "name": "Nombre del Producto" },
    { "key": "precio", "name": "Precio Venta" },
    { "key": "sku", "name": "Código" }
  ],
  "rows": [
    { "nombre": "Vino Malbec", "precio": "5000", "sku": "V001" },
    { "nombre": "Cerveza IPA", "precio": "2500", "sku": "C002" }
  ]
}
```

### Step 2: Confirm & Process (Commit)
**Endpoint:** `POST /api/pymes/{pyme_id}/process-catalog-file`  *(Note: This route name may vary based on `upload_bp` registration, commonly `/subir_catalogo` inside the blueprint)*.
*Correction:* The backend blueprint `upload_bp` is registered. Use the route defined in `services/upload_processor.py`:
**Endpoint:** `POST /api/pymes/{pyme_id}/catalog-upload/subir_catalogo` (Verify exact path in `app.py`)

**Request Body:**
- `file`: The same file object.
- `processor`: (Optional) Slug of the processor to use (e.g., "vinoteca", "generico").

**Response (200 OK):**
```json
{
  "message": "Procesado correctamente",
  "items_count": 150,
  "upload_id": 45
}
```

---

## 2. Order Management (Pedidos)

**Objective:** Unified list of all orders (conversational + web cart) with status management.

### List Orders
**Endpoint:** `GET /api/orders`
**Auth:** Bearer Token (User must be owner/admin of the tenant)

**Response (200 OK):**
```json
[
  {
    "id": "PED-20240101-ABC",
    "status": "created",
    "channel": "whatsapp",
    "totals": { "total": 15000.00, "currency": "ARS" },
    "buyer": { "name": "Juan Perez", "phone": "+54911..." },
    "created_at": "2024-01-01T10:00:00Z"
  }
]
```

### Update Order Status
**Endpoint:** `PATCH /api/orders/{order_id}`
**Request Body:**
```json
{
  "status": "shipped"
}
```
*Valid statuses:* `created`, `confirmed`, `paid`, `preparing`, `shipped`, `cancelled`.

---

## 3. CRM & Customer Memory

**Objective:** View customer profile and interaction history.

### Get Customer Profile
**Endpoint:** `GET /crm/contacts/{contact_id}`
**Response:**
```json
{
  "contact": { "id": 1, "name": "Maria", "email": "maria@example.com" },
  "ltv": { "total_spent": 50000, "order_count": 5 },
  "snapshot": { "summary": "Cliente frecuente, le gustan las promociones de 2x1." }
}
```

---

## 4. Chat Widget Customization

**Objective:** Allow tenants to customize the look and feel of their web widget.

### Get Configuration
**Endpoint:** `GET /api/tenant/config` (or `/api/widget/config` for public consumption)

**Response:**
```json
{
  "theme_json": {
    "mode": "light",
    "light": { "primary": "#FF5733", "secondary": "#FFFFFF" },
    "dark": { "primary": "#C70039", "secondary": "#000000" }
  },
  "welcome_message": "Hola! En qué puedo ayudarte?",
  "avatar_url": "https://..."
}
```

### Update Configuration
**Endpoint:** `PUT /api/tenant/config`
**Request Body:**
```json
{
  "theme_json": { ... },
  "welcome_message": "Nuevo mensaje de bienvenida"
}
```

---

## 5. Integration Preview (MercadoLibre / TiendaNube)

**Endpoint:** `GET /api/admin/tenants/{slug}/integrations/{provider}/preview`

**Response:**
```json
{
  "provider": "mercadolibre",
  "total_found": 100,
  "new_items": 10,
  "updated_items": 5,
  "items": [
    { "external_id": "ML123", "title": "Producto ML", "status": "new", "price": 1000 }
  ]
}
```
*Use this data to show a "Sync Preview" modal before calling the actual Sync endpoint.*
