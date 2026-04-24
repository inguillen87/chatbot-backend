# Frontend Guide: Advanced Employee Management

This guide details how to implement the "Employee Management" section in the Tenant Admin panel, specifically for assigning specific categories (rubros/districts) to employees. This allows large organizations (municipalities, enterprises) to delegate work effectively.

## 1. List Available Categories

Before creating or editing an employee, you need to know which categories are available in the tenant.

**Endpoint:** `GET /api/admin/tenants/<slug>/ticket-categories`

**Response:**
```json
[
  { "id": 1, "nombre": "Alumbrado Público", "tipo": "ticket" },
  { "id": 2, "nombre": "Recolección de Residuos", "tipo": "ticket" },
  { "id": 3, "nombre": "Distrito Centro", "tipo": "zona" } // Example of using categories as zones
]
```

## 2. Create Employee with Assignments

When creating a new employee, you can immediately assign them to specific categories.

**Endpoint:** `POST /api/admin/employees`

**Payload:**
```json
{
  "name": "Roberto Gomez",
  "email": "roberto.gomez@municipio.gov.ar",
  "password": "temporary-password",
  "roles": ["empleado"], // or "admin"
  "categories": [1, 3] // IDs from the list above
}
```

**Response:** `201 Created`

## 3. Update Employee Assignments

To modify the access of an existing employee (e.g., move them to a different district).

**Endpoint:** `POST /api/admin/employees/<user_id>/categories`

**Payload:**
```json
{
  "category_ids": [2, 4] // The NEW complete list of assigned IDs
}
```

## 4. UI Recommendations

- **Multi-Select Dropdown:** Use a multi-select component (like `Select` from shadcn/ui or `react-select`) populated by the category list.
- **Role Selector:** Allow choosing between "Admin" (full access) and "Empleado" (restricted to assigned categories).
- **"Districts" vs "Topics":** If the tenant uses categories for districts, label the field accordingly in the UI (e.g., "Zonas Asignadas").
