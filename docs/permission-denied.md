# Diagnóstico de "Permiso denegado"

Cuando el backend detecta que un usuario intenta acceder a un endpoint sin los permisos necesarios, registra un aviso similar a:

```
[WARNING] app - PERMISO DENEGADO | endpoint=ticket_bp.get_panel_por_categoria | user_id=<ID>
```

En estos casos la respuesta al frontend es un **403 Forbidden**. La causa más común es que el token o el rol del usuario no coinciden con los requeridos para la ruta.

Verificá en el panel de administración que el usuario tenga asignado el rol **admin** o **empleado** (también pueden aparecer como `admin_municipio`, `empleado_pyme`, etc.) y que su `tipo_chat` sea el correcto. Asegurate además de que el token de sesión no haya expirado antes de volver a intentar.
