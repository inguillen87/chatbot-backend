# Problemas comunes al integrar el widget

¿No ves el widget? Verifica que el código esté correctamente pegado antes de la etiqueta `</body>`. Asegúrate de que tu plataforma (Tiendanube, Shopify, etc.) permita la inserción de scripts o iframes de terceros. En Tiendanube, por ejemplo, puedes necesitar usar la opción de "Editar Código Avanzado".

## Problemas de geolocalización o portapapeles

Asegúrate de que tu sitio se sirva a través de HTTPS, ya que muchas funciones del navegador, incluida la geolocalización, lo requieren. Si tu página está incrustada en otro iframe, el iframe contenedor **debe** tener el atributo `allow="clipboard-write; geolocation"`.

## Conflictos de estilos o scripts

El widget está diseñado para minimizar conflictos. Si experimentas problemas, intenta cargar el script del widget al final del `<body>`. Si usas el método iframe, los estilos están completamente aislados.

## Error 404 al cargar `favicon.ico`

Algunos navegadores intentan solicitar `favicon.ico` automáticamente. Si tu sitio no incluye ese archivo, la consola mostrará `GET /favicon.ico 404`. Puedes ignorar el aviso o colocar un ícono en la ruta indicada mediante:

```html
<link rel="icon" href="/favicon.ico" />
```

## Errores de SVG "Expected length, \"undefined\""

Mensajes como `Error: <circle> attribute cx: Expected length, "undefined"` indican que las coordenadas del ícono se enviaron vacías. Revisa que las variables usadas para `cx` y `cy` sean numéricas antes de renderizar el SVG. Suele ocurrir cuando un componente React recibe `undefined` en sus props.

## Errores 403 o 405 al llamar a la API

Si la consola del navegador muestra un **403 Forbidden** al invocar `POST /ask/municipio`, normalmente significa que el token no se envió correctamente. El backend acepta el token en los encabezados `Authorization`, `X-Token` o `X-Entity-Token`, también como parámetro `?token=` o dentro del JSON. Revisa que el iframe o script incluya el token de la empresa antes de hacer la consulta.

Un **405 Method Not Allowed** indica que se usó un verbo no admitido. Algunas instalaciones antiguas sólo permiten `PUT /perfil` para actualizar los datos. Para leer la información del usuario utiliza `GET /me` o, si sólo necesitas validar el token, `GET /token-info`.

## ¿Aún necesitas ayuda?

No dudes en contactar a nuestro equipo de soporte. [info@chatboc.ar](mailto:info@chatboc.ar) está disponible para ayudarte a integrar Chatboc exitosamente.
