# Solución de problemas de geolocalización

Si en la consola del navegador aparece el mensaje:

```
[Violation] Permissions policy violation: Geolocation access has been blocked because of a permissions policy applied to the current document.
```

significa que la página no tiene permisos para solicitar la ubicación. Comprueba lo siguiente:

1. Si se usa un `<iframe>`, añade `allow="geolocation"` en la etiqueta:

```html
<iframe src="..." allow="geolocation"></iframe>
```

2. Si tu servidor define el encabezado `Permissions-Policy`, incluye `geolocation=(self)` o el dominio que corresponda. Esto habilita la API de geolocalización dentro de la página.
   En Flask puedes agregarlo con:

   ```python
   @app.after_request
   def add_permissions_policy(resp):
       resp.headers.setdefault("Permissions-Policy", "geolocation=(self)")
       return resp
   ```
3. Si cargas el `window-provider.js` con una etiqueta `<script>`, coloca el atributo `allow="geolocation"` en el contenedor que lo aloja (por ejemplo otro `<iframe>`). Ejemplo:

   ```html
   <iframe src="about:blank" id="loader" allow="geolocation"></iframe>
   <script>
     const doc = document.getElementById('loader').contentWindow.document;
     doc.write('<script src="https://www.chatboc.ar/window-provider.js"<\/script>');
   </script>
   ```


Además, Google recomienda reemplazar `google.maps.places.AutocompleteService` por `google.maps.places.AutocompleteSuggestion`. Consulta la [guía de migración](https://developers.google.com/maps/documentation/javascript/places-migration-overview) para actualizar tu código.

Para guardar la ubicación de un ticket en el backend utiliza la ruta:

```
PUT /tickets/<tipo>/<ticket_id>/ubicacion
```

Donde `<tipo>` es `municipio` o `pyme` y debes enviar un token válido. Si se devuelve `"Ticket no encontrado"` asegúrate de que el `ticket_id` exista y pertenezca al usuario autenticado.

Si el ticket se creó de forma anónima y acabas de registrar al usuario, envía también el header `Anon-Id` con el mismo valor utilizado al crear el ticket. El endpoint vinculará automáticamente ese ticket al nuevo usuario al recibir la ubicación.

Los mensajes relacionados con `ethereum` son generados por extensiones del navegador y no afectan al funcionamiento de la aplicación.
