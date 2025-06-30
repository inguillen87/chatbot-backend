# Problemas comunes al integrar el widget

¿No ves el widget? Verifica que el código esté correctamente pegado antes de la etiqueta `</body>`. Asegúrate de que tu plataforma (Tiendanube, Shopify, etc.) permita la inserción de scripts o iframes de terceros. En Tiendanube, por ejemplo, puedes necesitar usar la opción de "Editar Código Avanzado".

## Problemas de geolocalización o portapapeles

Asegúrate de que tu sitio se sirva a través de HTTPS, ya que muchas funciones del navegador, incluida la geolocalización, lo requieren. Si tu página está incrustada en otro iframe, el iframe contenedor **debe** tener el atributo `allow="clipboard-write; geolocation"`.

## Conflictos de estilos o scripts

El widget está diseñado para minimizar conflictos. Si experimentas problemas, intenta cargar el script del widget al final del `<body>`. Si usas el método iframe, los estilos están completamente aislados.

## Errores 403 o 405 al llamar a la API

Si la consola del navegador muestra un **403 Forbidden** al invocar `POST /ask/municipio`, normalmente significa que el token no se envió correctamente. El backend acepta el token en los encabezados `Authorization`, `X-Token` o `X-Entity-Token`, también como parámetro `?token=` o dentro del JSON. Revisa que el iframe o script incluya el token de la empresa antes de hacer la consulta.

Un **405 Method Not Allowed** indica que se usó un verbo no admitido. Algunas instalaciones antiguas sólo permiten `PUT /perfil` para actualizar los datos. Para leer la información del usuario utiliza `GET /me` o, si sólo necesitas validar el token, `GET /token-info`.

## ¿Aún necesitas ayuda?

No dudes en contactar a nuestro equipo de soporte. [info@chatboc.ar](mailto:info@chatboc.ar) está disponible para ayudarte a integrar Chatboc exitosamente.
