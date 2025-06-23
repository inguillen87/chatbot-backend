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

Además, Google recomienda reemplazar `google.maps.places.AutocompleteService` por `google.maps.places.AutocompleteSuggestion`. Consulta la [guía de migración](https://developers.google.com/maps/documentation/javascript/places-migration-overview) para actualizar tu código.

Para guardar la ubicación de un ticket en el backend utiliza la ruta:

```
PUT /tickets/<tipo>/<ticket_id>/ubicacion
```

Donde `<tipo>` es `municipio` o `pyme` y debes enviar un token válido. Si se devuelve `"Ticket no encontrado"` asegúrate de que el `ticket_id` exista y pertenezca al usuario autenticado.

Los mensajes relacionados con `ethereum` son generados por extensiones del navegador y no afectan al funcionamiento de la aplicación.
