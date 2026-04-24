# Frontend Super Admin Implementation Guide

This guide details the integration requirements for the enhanced Super Admin dashboard, focusing on tenant provisioning and user management.

## Base URL
`/api/admin/tenants`

## Authentication
All requests require the Super Admin JWT token in the `Authorization` header: `Bearer <token>`.

## 1. List Tenants
**Endpoint:** `GET /api/admin/tenants?page=1&per_page=20`

**Response:**
```json
{
  "tenants": [
    {
      "id": 1,
      "slug": "municipio",
      "nombre": "Municipio Demo",
      "tipo": "municipio",
      "plan": "full",
      "status": "active",
      "is_active": true,
      "created_at": "2024-01-01T10:00:00Z"
    }
  ],
  "total": 5,
  "pages": 1,
  "current_page": 1
}
```

## 2. Create Tenant
**Endpoint:** `POST /api/admin/tenants`

**Payload:**
```json
{
  "slug": "nuevo-cliente",
  "nombre": "Nuevo Cliente S.A.",
  "tipo": "pyme",
  "plan": "pro",
  "email_admin": "admin@cliente.com"
}
```
*Note: `tipo` can be 'pyme' or 'municipio'.*

## 3. Tenant Details & Updates
**Endpoint:** `GET /api/admin/tenants/<slug>`
**Endpoint:** `PUT /api/admin/tenants/<slug>`

**Update Payload:**
```json
{
  "nombre": "Nombre Actualizado",
  "plan": "full",
  "is_active": true,
  "whatsapp_sender_id": "whatsapp:+549..."
}
```

## 4. User Management (New)

### Create Admin User
Use this to provision the initial admin user if one wasn't created during tenant creation, or to link an existing user.

**Endpoint:** `POST /api/admin/tenants/<slug>/admin-user`

**Payload:**
```json
{
  "email": "nuevo_admin@cliente.com",
  "password": "SecurePassword123!",
  "name": "Juan Perez"
}
```

### Reset Admin Password
**Endpoint:** `PUT /api/admin/tenants/<slug>/password`

**Payload:**
```json
{
  "password": "NewPassword2025!"
}
```

### Configure WhatsApp Number
This updates the routing table (`WhatsappNumero`) so incoming messages map to this tenant.

**Endpoint:** `PUT /api/admin/tenants/<slug>/whatsapp`

**Payload:**
```json
{
  "number": "+5491112345678"
}
```
*Format must include country code (e.g., +549...).*

## 5. Actions
- **Deactivate:** `DELETE /api/admin/tenants/<slug>` (Soft delete)
- **Activate:** `POST /api/admin/tenants/<slug>/activate`
- **Impersonate:** `POST /api/admin/tenants/<slug>/impersonate` -> Returns `{ "token": "...", "redirect_url": "..." }`

## Implementation Checklist
1.  [ ] **Tenant List View:** Table displaying tenants with status badges and "Manage" buttons.
2.  [ ] **Create Modal:** Form to input Slug, Name, Type, Plan, and Admin Email.
3.  [ ] **Detail View:**
    *   **Overview Tab:** Edit Name, Plan, Active Status.
    *   **Users Tab:** Form to Create Admin / Reset Password.
    *   **Integrations Tab:** Input field for WhatsApp Number (calls `/whatsapp` endpoint).
    *   **Actions:** "Impersonate" button (opens portal in new tab with returned token).
