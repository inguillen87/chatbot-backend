# Frontend Handoff - Empleados, Roles y Asignacion Operativa

Fecha: 2026-05-14

## Objetivo

La seccion de empleados debe permitir que un admin tenant cree equipo operativo y lo limite por categoria, zona, canal y permisos. Esto aplica a municipios, pymes y colegios.

Backend ya entrega contratos para:

- listar empleados del tenant,
- exponer categorias/zonas/canales reales o configurados,
- guardar scope operativo por empleado,
- sugerir asignacion de tickets,
- auto-asignar tickets en modo preview o aplicado.

Importante: `/empleados` es una ruta visual del frontend. Para datos usar siempre `/api/empleados` o `/api/v2/*`.

## Endpoints que frontend debe usar

### Categorias para selector legacy

```txt
GET /api/empleados/categorias
```

Respuesta:

```json
{
  "categorias": [
    { "value": "alumbrado", "label": "Alumbrado" }
  ]
}
```

Regla UI:

- No mostrar "No hay categorias disponibles" si este endpoint trae items.
- Si no trae items, pedir `GET /api/v2/employee-coverage` antes de ocultar el selector.

### Crear empleado simple

```txt
POST /api/empleados
```

Payload recomendado:

```json
{
  "name": "Mesa de alumbrado",
  "email": "alumbrado@junin.gob.ar",
  "password": "temporal-segura",
  "categorias": ["alumbrado"],
  "scope": {
    "zonas": ["centro"],
    "channels": ["web", "whatsapp"],
    "permisos": ["tickets_read", "tickets_update"]
  }
}
```

Backend sincroniza esto con `User.accesibilidad.employee_scope`, asi que tambien aparece en contratos v2.

### Crear empleado tenant avanzado

```txt
POST /api/admin/employees
```

Headers:

```txt
Authorization: Bearer <token>
X-Tenant-Slug: <tenant_slug>
```

Payload recomendado:

```json
{
  "name": "Coordinador de reclamos",
  "email": "reclamos@junin.gob.ar",
  "password": "temporal-segura",
  "roles": ["empleado", "coordinador"],
  "categories": ["alumbrado", "baches"],
  "scope": {
    "categorias": ["alumbrado", "baches"],
    "zonas": ["centro", "norte"],
    "channels": ["web", "whatsapp"],
    "permisos": ["tickets_read", "tickets_update", "tickets_assign"]
  }
}
```

Respuesta incluye:

- `employee.scope`
- `employee.roles`
- `coverage_endpoint`
- `routing_endpoint`

### Coverage matrix

```txt
GET /api/v2/employee-coverage
GET /api/admin/tenants/{tenant_slug}/employees/coverage
```

Usar para renderizar:

- dimensiones soportadas,
- categorias sin cubrir,
- zonas sin cubrir,
- canales sin cubrir,
- empleados y carga abierta.

No ocultar categorias si no hay empleados. La UI debe mostrar las categorias como "sin responsable".

### Routing y auto-asignacion

```txt
GET /api/v2/employee-routing
PATCH /api/v2/employees/{employee_id}/routing-scope
POST /api/v2/employee-routing/auto-assign
```

Para auto-asignacion:

1. Primero enviar `dry_run: true`.
2. Mostrar preview de tickets, empleado sugerido, score y razones.
3. Aplicar con `dry_run: false` solo si el admin confirma.

## UX esperado

La pantalla `/empleados` deberia tener:

- selector de categorias visible aunque no haya empleados,
- chips de roles,
- chips de categorias, zonas, canales y permisos por empleado,
- contador de tickets abiertos por empleado,
- bloque "Categorias sin responsable",
- bloque "Tickets sin asignar",
- accion "Previsualizar asignacion automatica",
- accion "Actualizar alcance" por empleado.

## Antireglas

- No inventar categorias en frontend.
- No inventar empleados ni roles.
- No ocultar el selector si backend tiene `coverage.categorias`.
- No tratar `channels` como `canales` en salida; backend acepta ambos en entrada, pero devuelve `channels`.
- No aplicar auto-asignacion sin preview.

## QA minimo frontend

- Login como `mauricio@junin.com`.
- Abrir `/empleados`.
- Ver categorias reales en selector.
- Crear empleado con categoria `alumbrado` y canal `whatsapp`.
- Confirmar que aparece en directorio interno.
- Confirmar que `GET /api/v2/employee-routing` lo muestra en `employees`.
- Ejecutar auto-assign `dry_run:true` y mostrar preview sin modificar DB.
- En mobile, formulario sin overflow horizontal.
