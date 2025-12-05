# Integración Frontend - Multi-Tenant SaaS

Este documento describe los endpoints y estructuras de datos para la gestión multi-tenant en Chatboc.

## 1. Administración de Tenants

Endpoints para crear y configurar nuevos clientes (municipios o pymes).

### 1.1 Crear Tenant
**POST** `/api/admin/tenants`

Crea un nuevo tenant utilizando una plantilla predefinida.

**Body:**
```json
{
  "nombre": "Municipalidad de Mendoza",
  "slug": "mendoza",
  "tipo": "municipio",
  "template_key": "municipio_default",
  "plan": "full",
  "auto_assign_whatsapp_number": true,
  "owner_email": "admin@mendoza.gob.ar",
  "owner_password": "securePass123!"
}
```

**Respuesta (201 Created):**
```json
{
  "slug": "mendoza",
  "widget_token": "token_seguro_...",
  "id": 123
}
```

### 1.2 Obtener Configuración Completa
**GET** `/api/admin/tenants/<slug>/config`

Devuelve la configuración completa del tenant para edición en el panel.

**Respuesta:**
```json
{
  "tenant": {
    "slug": "mendoza",
    "nombre": "Municipalidad de Mendoza",
    "tipo": "municipio",
    "plan": "full",
    "logo_url": "...",
    "whatsapp_sender_id": "..."
  },
  "menu": { ...JSON menú... },
  "contacts": { ...JSON contactos... },
  "links": { ...JSON links... },
  "widget": { ...JSON widget... }
}
```

### 1.3 Actualizar Configuración
**PUT** `/api/admin/tenants/<slug>/config`

Permite actualizar la configuración. Se pueden enviar solo los campos a modificar.

**Body:**
```json
{
  "tenant": { "nombre": "Muni Mendoza" },
  "menu": { ...nuevo menú... }
}
```

### 1.4 Asignar WhatsApp
**POST** `/api/admin/tenants/<slug>/assign-whatsapp-number`

Asigna un número disponible del pool de Twilio al tenant.

**Respuesta:**
```json
{
  "assigned": true,
  "phone_number": "+54...",
  "sender_id": "..."
}
```

## 2. Endpoints Públicos (Widget / Portal)

Endpoints consumidos por el widget o el portal ciudadano/cliente. No requieren autenticación de admin, pero usan el `slug` en la URL.

### 2.1 Menú
**GET** `/api/public/tenants/<slug>/menu?channel=widget`

Devuelve la estructura del menú.

**Ejemplo Respuesta:**
```json
{
  "version": 1,
  "main_menu": [
    { "id": "reclamos", "label": "Reclamos", "type": "submenu" }
  ],
  "submenus": {
    "reclamos": [ ... ]
  }
}
```

### 2.2 Contactos
**GET** `/api/public/tenants/<slug>/contacts`

### 2.3 Links / Trámites
**GET** `/api/public/tenants/<slug>/links`

### 2.4 Configuración de Widget
**GET** `/api/public/tenants/<slug>/widget-config`

Devuelve colores, mensajes de bienvenida, avatar, etc.

## 3. Empleados y Roles

Gestión de usuarios internos del tenant.

### 3.1 Crear Empleado
**POST** `/api/admin/employees`
Header: `X-Tenant: <slug>`

**Body:**
```json
{
  "email": "juan@mendoza.gob.ar",
  "name": "Juan Perez",
  "password": "..."
}
```

### 3.2 Asignar Rol
**POST** `/api/admin/employees/<id>/roles`
Body: `{ "role": "empleado" }`

### 3.3 Asignar Categorías (Tickets)
**POST** `/api/admin/employees/<id>/categories`
Body: `{ "category_ids": [1, 5, 8] }`

El frontend debe listar las categorías disponibles para el tenant (`GET /api/public/tenants/<slug>/menu` o similar contiene las categorías en los items del menú) para permitir la selección.
