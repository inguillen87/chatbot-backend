# Ubicacion fluida post registro

Este instructivo explica cómo asegurar que un usuario que inicia el chat de forma anónima pueda compartir su ubicación sin problemas luego de registrarse.

## Flujo recomendado

1. **El usuario solicita una acción que necesita GPS**:
   - Si no está autenticado, el frontend debe mostrar el formulario de registro y bloquear la función hasta completar el alta.
2. **Registro exitoso**:
   - Guarda el nuevo token e información del usuario en el estado global de React.
   - Asocia el ticket existente al `user.id` (usa el header `Anon-Id` si el ticket era anónimo) o crea uno nuevo vinculado al usuario.
3. **Solicitud automática de ubicación**:
   - Con el usuario y el ticket ya vinculados, ejecuta `navigator.geolocation.getCurrentPosition`.
   - Envía las coordenadas al backend usando el endpoint de ubicación correspondiente.
4. **Usuarios ya logueados**:
   - Solo se dispara la solicitud de GPS y se manda al backend. El flujo no cambia.

## Consideraciones

- Mantén el atributo `allow="geolocation"` tanto en el iframe del widget como en el script que carga el chat.
- Si `/ubicacion` responde que no encuentra el ticket o usuario, refresca la sesión e intenta nuevamente sin interrumpir al usuario.
- Muestra un mensaje claro si la obtención de la ubicación falla o el usuario la cancela.

Con este flujo el usuario experimentará el mismo comportamiento al compartir su ubicación ya sea que estuviera logueado desde un inicio o que se registre durante la conversación.

