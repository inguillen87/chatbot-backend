# Anon ID Debugging Checklist

Esta guía rápida resume los pasos para verificar los problemas más comunes con tickets anónimos y el uso del `Anon-Id`.

## Pasos de validación

- **Anon‑Id constante en cada request**
  - Confirma que el valor del `Anon-Id` no cambie a lo largo del flujo.
- **Ticket creado con el mismo Anon‑Id**
  - El `Anon-Id` almacenado en la base de datos para ese ticket debe coincidir con el recibido.
- **Ruta de `ubicacion` bien formada**
  - Revisa que la URL que actualiza la ubicación siga el formato esperado.
- **Headers sin mezclar**
  - Evita enviar simultáneamente un token de autenticación y un `Anon-Id` que correspondan a usuarios diferentes.
- **Vigencia de cookie/localStorage**
  - Verifica que la cookie o el almacenamiento local que guarda el `Anon-Id` no expire durante el proceso.

Si todo lo anterior se ve correcto y el problema persiste, verifica los logs del backend. Este proyecto registra automáticamente:

1. **Ticket ID recibido**
2. **Anon‑Id recibido en el header**
3. **Anon‑Id registrado en la base** para ese ticket
4. **Estado actual del ticket**

Estos registros permiten identificar discrepancias y facilitan encontrar el origen del error.
