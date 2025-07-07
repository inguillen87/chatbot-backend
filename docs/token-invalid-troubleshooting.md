# Validar token y registrar usuarios

Si el bot responde con "token inválido" o no se pueden registrar usuarios en `/chatuserregisterpanel`, revisá lo siguiente:

1. **Comprobar el token con `/token-info`**

   ```bash
   curl -H "Authorization: Bearer TU_TOKEN" https://tu-servidor.chatboc.ar/token-info
   ```

   La respuesta debe indicar la empresa y el rubro asociados al token. Si recibís `401 Unauthorized` o un mensaje que el token no existe, generá uno nuevo desde el panel de administración.

2. **Enviar el token en la cabecera `X-Entity-Token`**

   Asegurate de que tu widget o cliente HTTP incluya el token en las cabeceras de cada solicitud:

   ```http
   X-Entity-Token: TU_TOKEN
   ```

   Nunca incrustes el token del administrador directamente en el código público. Cargalo desde el backend de tu sitio o utilizá un proxy seguro.

3. **Ruta de registro**

   El registro de usuarios finales debe realizarse con:

   ```http
   POST /chatuserregisterpanel
   ```

   Si el servidor responde `404 Not Found`, verificá que la ruta esté habilitada en tu instalación. Algunas implementaciones antiguas usaban un nombre distinto para este endpoint.

4. **Cookie de sesión**

   Si en los logs aparece `Flask session cookie 'session' NOT received`, revisá la configuración de dominio y CORS para que el navegador pueda enviar la cookie correctamente. Sin la cookie, el backend tratará al usuario como anónimo.

Estas verificaciones suelen resolver los mensajes de "token inválido" y el fallo al registrar usuarios.
