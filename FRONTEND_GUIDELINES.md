# Guía para el Equipo de Frontend

Este documento detalla los cambios recientes en el backend y proporciona instrucciones para la integración y resolución de problemas en el frontend.

## 1. Corrección del Redireccionamiento en el Login de Administrador

**Problema:** Al iniciar sesión como administrador de un tenant de tipo Pyme (ej. `servill`), el usuario era redirigido incorrectamente al perfil de `municipio`.

**Causa Raíz (Backend):** Hubo un error en la lógica de autenticación del backend (`/auth/admin/login`) que, bajo ciertas condiciones, devolvía el `tenant_slug` incorrecto (`municipio`) en la respuesta del login, sin importar cuál era el tenant real del usuario.

**Solución (Backend):** Se ha corregido el endpoint. Ahora, la respuesta de un login exitoso en `/auth/admin/login` siempre contendrá el `tenant_slug` correcto asociado al usuario.

**Acción Requerida (Frontend):**
El frontend debe utilizar el campo `tenant_slug` de la respuesta del login para construir la URL de redirección.

**Ejemplo de Respuesta del API (Corregido):**
```json
{
  "token": "ey...",
  "user": {
    "id": 10,
    "email": "info@servill.ar",
    "name": "SERVILL Indumentaria",
    "rol": "admin_pyme",
    "tenant_slug": "servill"  // <--- Usar este valor para el redirect
  }
}
```

**Lógica de Redirección Sugerida:**
Después de un login exitoso, redirigir al usuario a `/<tenant_slug>/perfil`. Para el ejemplo anterior, la URL sería `/servill/perfil`.

---

## 2. Actualización de Estado de Pedidos y Notificaciones

Se ha implementado la funcionalidad para notificar a los clientes a través de WhatsApp cuando el estado de su pedido cambia.

**Endpoint:**
`PUT /api/admin/tenants/<slug>/orders/<order_id>`

**Autenticación:**
Requiere el token JWT del administrador en el header `Authorization`:
```
Authorization: Bearer <token>
```

**Payload (Cuerpo de la Solicitud):**
Un objeto JSON que especifica el nuevo estado del pedido. Los estados válidos son `pending`, `confirmed`, `shipped`, `delivered`, `cancelled`.

**Ejemplo:**
```json
{
  "status": "shipped"
}
```

**Funcionalidad de Notificación:**
- Al llamar a este endpoint, el backend ahora intentará enviar una notificación de WhatsApp al cliente.
- La notificación se enviará al número de teléfono almacenado en `contact_phone` para ese pedido.
- **Nota:** Esta funcionalidad depende de que las credenciales de Twilio (para WhatsApp) estén configuradas en el entorno del servidor. Si no están configuradas, el backend registrará el intento pero no podrá enviar el mensaje.

---

## 3. Análisis de Errores en la Página de Estado del Ticket (`iframe.js`)

**Problema:** Se reportaron varios errores de JavaScript en la consola del navegador al visitar la página de estado de un ticket, causando que la interfaz no se renderice correctamente.

- `TypeError: Cannot read properties of undefined (reading 'length')` en `main.js`.
- `SyntaxError: The requested module './iframe.js' does not provide an export named 'x'` en `ProactiveBubble.js`.

**Análisis:**
Estos errores se originan exclusivamente en el código del frontend.

1.  **`TypeError: ... (reading 'length')`**: Este es un error común en JavaScript que ocurre cuando se intenta acceder a la propiedad `length` de una variable que es `undefined` o `null`. Generalmente, sucede cuando un componente intenta iterar sobre un array (ej. `miArray.map(...)`) que se esperaba de una API, pero la API devolvió `null` o el campo no estaba presente en la respuesta. El componente `XYe` en `main.js` debería añadir una guarda para manejar este caso.

    **Sugerencia:**
    ```javascript
    // Antes (causa el error si miArray es undefined)
    miArray.map(item => ...);

    // Después (solución)
    (miArray || []).map(item => ...);
    // o
    if (Array.isArray(miArray)) {
      miArray.map(item => ...);
    }
    ```

2.  **`SyntaxError: ... does not provide an export named 'x'`**: Este error indica un problema con los módulos de JavaScript (ESM). El archivo `ProactiveBubble.js` está intentando importar algo llamado `x` desde `./iframe.js`, pero `./iframe.js` no lo exporta. Esto puede ser un typo, un error de refactorización o una importación incorrecta.

**Acción Requerida (Frontend):**
- El equipo de frontend debe revisar los componentes `XYe` (en `main.js`) y `ProactiveBubble.js`.
- Asegurarse de que los datos recibidos de las APIs se manejen de forma segura, especialmente los arrays que podrían ser nulos.
- Corregir las declaraciones `import`/`export` entre los módulos `ProactiveBubble.js` y `iframe.js`.
- Estos no son problemas del backend; las APIs están proporcionando los datos correctamente.
