# Frontend Updates

## New Pyme-specific Categories Endpoint

A new endpoint has been created to fetch categories specifically for Pyme tenants.

**Endpoint:** `/api/pyme/<tenant_slug>/categorias`

**Method:** `GET`

**Description:** This endpoint should be used to retrieve the list of categories for a Pyme tenant. It replaces the previous usage of the `/api/municipal/categorias` endpoint for Pyme tenants, which was causing errors.

**Example Usage:**

```
GET /api/pyme/servill/categorias
```

**Response:**

```json
{
  "categorias": [
    {
      "id": 1,
      "nombre": "Indumentaria",
      "tenant_id": 1
    },
    {
      "id": 2,
      "nombre": "Calzado",
      "tenant_id": 1
    }
  ],
  "categories": [
    {
      "id": 1,
      "nombre": "Indumentaria",
      "tenant_id": 1
    },
    {
      "id": 2,
      "nombre": "Calzado",
      "tenant_id": 1
    }
  ]
}
```
