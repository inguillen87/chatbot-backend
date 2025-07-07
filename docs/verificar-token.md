# Verificar la validez de tu token

Para comprobar si el token de empresa o municipio es correcto, puedes utilizar el endpoint `GET /token-info`.

```
GET https://tu-servidor.chatboc.ar/token-info
Authorization: Bearer TU_TOKEN
```

La respuesta indicará a qué empresa y rubro pertenece el token. Si obtienes un **401 Unauthorized** o la respuesta indica que el token no existe, genera uno nuevo desde el panel de administración o contacta a soporte.

Recuerda que el token corresponde al usuario administrador. Evita exponerlo en el frontend y guárdalo en tu servidor para pasarlo al widget de forma segura.
