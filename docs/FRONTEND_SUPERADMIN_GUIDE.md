# Frontend Super Admin Implementation Guide

This document outlines the API endpoints and recommended frontend structures to implement the "Super Admin" dashboard for full tenant management.

## 1. Overview

The Super Admin panel should provide a CRUD interface for Tenants (`TenantProfile`).
Key features:
*   **List Tenants:** Paginated table with status, plan, and creation date.
*   **Create Tenant:** Form to provision a new tenant + admin user.
*   **Edit Tenant:** Modify details like Name, Plan, Domain, and Active Status.
*   **Deactivate/Activate:** Toggle tenant access.
*   **Impersonate:** "Login as" the tenant admin to debug issues.

## 2. API Endpoints

Base URL: `/api/admin/tenants` (Requires `super_admin` role)

### 2.1 List Tenants
**GET** `/api/admin/tenants?page=1&per_page=20`

**Response:**
```json
{
  "tenants": [
    {
      "id": 1,
      "slug": "municipio",
      "nombre": "Municipio Inteligente",
      "tipo": "municipio",
      "plan": "enterprise",
      "status": "active", // "active" or "inactive"
      "is_active": true,
      "created_at": "2023-01-01T12:00:00"
    }
  ],
  "total": 50,
  "pages": 3,
  "current_page": 1
}
```

### 2.2 Create Tenant
**POST** `/api/admin/tenants`

**Payload:**
```json
{
  "slug": "mi-nuevo-cliente",
  "nombre": "Cliente Nuevo S.A.",
  "tipo": "pyme", // "pyme" or "municipio"
  "email_admin": "admin@cliente.com",
  "plan": "pro" // "free", "pro", "full", "enterprise"
}
```

**Response (201 Created):**
```json
{
  "message": "Tenant creado",
  "id": 15,
  "slug": "mi-nuevo-cliente"
}
```

### 2.3 Get Tenant Detail
**GET** `/api/admin/tenants/<slug>`

**Response:**
```json
{
  "id": 15,
  "slug": "mi-nuevo-cliente",
  "nombre": "Cliente Nuevo S.A.",
  "tipo": "pyme",
  "plan": "pro",
  "is_active": true,
  "owner_email": "admin@cliente.com",
  "whatsapp_sender_id": "whatsapp:+549..."
}
```

### 2.4 Update Tenant
**PUT** `/api/admin/tenants/<slug>`

**Payload (send only changed fields):**
```json
{
  "nombre": "Cliente Nuevo Renombrado",
  "plan": "enterprise",
  "is_active": true,
  "whatsapp_sender_id": "whatsapp:+54911111111"
}
```

### 2.5 Deactivate Tenant (Soft Delete)
**DELETE** `/api/admin/tenants/<slug>`

**Response:**
```json
{ "message": "Tenant deactivated successfully" }
```

### 2.6 Activate Tenant
**POST** `/api/admin/tenants/<slug>/activate`

**Response:**
```json
{ "message": "Tenant activated successfully" }
```

### 2.7 Impersonate (Login As)
**POST** `/api/admin/tenants/<slug>/impersonate`

**Response:**
```json
{
  "token": "JWT_TOKEN_FOR_TENANT_ADMIN",
  "redirect_url": "/portal/mi-nuevo-cliente/admin"
}
```
*Action:* Frontend should store this token (e.g. in `localStorage` or `cookie`) and redirect the user to the `redirect_url`.

---

## 3. Frontend Implementation Recommendations (React/Next.js)

### 3.1 Types (TypeScript)

```typescript
export interface Tenant {
  id: number;
  slug: string;
  nombre: string;
  tipo: 'pyme' | 'municipio';
  plan: string;
  status: 'active' | 'inactive';
  is_active: boolean;
  created_at: string;
  owner_email?: string;
}

export interface CreateTenantDTO {
  slug: string;
  nombre: string;
  tipo: 'pyme' | 'municipio';
  email_admin: string;
  plan?: string;
}
```

### 3.2 UI Components Suggested

1.  **TenantTable:**
    *   Columns: ID, Name, Slug, Type, Plan, Status (Badge Green/Red), Actions.
    *   Actions: "Edit" (pencil icon), "Impersonate" (login icon), "Toggle Status" (switch).

2.  **TenantModal (Create/Edit):**
    *   Form fields:
        *   **Name:** Text Input
        *   **Slug:** Text Input (Disabled on Edit usually, or restricted)
        *   **Type:** Select (Pyme/Municipio) - Disabled on Edit
        *   **Plan:** Select (Free, Pro, Full, Enterprise)
        *   **Admin Email:** Text Input (Create only)
        *   **WhatsApp Sender:** Text Input (Optional)

3.  **Impersonation Flow:**
    *   Button "Acceder como Admin".
    *   `onClick`: Call API -> Get Token -> `login(token)` -> `router.push('/dashboard')`.

### 3.3 State Management
Use a standard query hook (like React Query or SWR) to fetch the list.
*   `useTenants(page, perPage)`
*   Invalidate cache on Create/Update actions.

## 4. Notes
*   **Security:** These endpoints require the user to have `rol: "super_admin"`. Ensure the frontend checks `user.role` before rendering the "Tenants" menu item.
*   **Validation:** The backend enforces unique slugs. Handle 409 errors in the Create form.
