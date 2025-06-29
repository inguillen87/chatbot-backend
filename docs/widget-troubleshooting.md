# Problemas comunes al integrar el widget

¿No ves el widget? Verifica que el código esté correctamente pegado antes de la etiqueta `</body>`. Asegúrate de que tu plataforma (Tiendanube, Shopify, etc.) permita la inserción de scripts o iframes de terceros. En Tiendanube, por ejemplo, puedes necesitar usar la opción de "Editar Código Avanzado".

## Problemas de geolocalización o portapapeles

Asegúrate de que tu sitio se sirva a través de HTTPS, ya que muchas funciones del navegador, incluida la geolocalización, lo requieren. Si tu página está incrustada en otro iframe, el iframe contenedor **debe** tener el atributo `allow="clipboard-write; geolocation"`.

## Conflictos de estilos o scripts

El widget está diseñado para minimizar conflictos. Si experimentas problemas, intenta cargar el script del widget al final del `<body>`. Si usas el método iframe, los estilos están completamente aislados.

## ¿Aún necesitas ayuda?

No dudes en contactar a nuestro equipo de soporte. [info@chatboc.ar](mailto:info@chatboc.ar) está disponible para ayudarte a integrar Chatboc exitosamente.
