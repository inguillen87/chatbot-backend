# Window provider syntax error

Al integrar el chat mediante `window-provider.js` puede mostrarse en la consola el mensaje:

```
Uncaught SyntaxError: Invalid or unexpected token (at window-provider.js:72104:12)
```

Este error suele indicar que el script se cargó incompleto o con caracteres corruptos. Comprueba que la URL sea correcta y que el servidor devuelva `Content-Type: application/javascript`.

Si se obtiene el script dentro de un `<iframe>` u otro contenedor con políticas de seguridad, añade el atributo `allow="geolocation"` igual que en el iframe del chat para habilitar la obtención de la ubicación.
