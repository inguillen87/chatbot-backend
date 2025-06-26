# Ideas para municipios

Este documento resume funcionalidades útiles para cualquier municipio que
utilice este backend. Se listan los módulos y endpoints disponibles para
implementar cada idea.

## Mapa de incidentes

- Endpoint `GET /tickets/<tipo>/mapa` devuelve los tickets abiertos con
  latitud y longitud utilizando
  `TicketService.obtener_tickets_abiertos_con_ubicacion`.
- Con esos datos puede renderizarse un mapa interactivo para identificar zonas
  con más reclamos y planificar mejor la respuesta.
- Ejemplo de respuesta del endpoint:

  ```json
  [
    {
      "id": 12,
      "latitud": -34.6,
      "longitud": -58.4,
      "categoria": "Iluminación",
      "estado": "abierto",
      "direccion": "Av. Siempreviva 123"
    }
  ]
  ```

Con cualquier librería de mapas (Leaflet, Google Maps, etc.) puedes generar
marcadores con esa información y mostrar la ubicación de los reclamos.

Ejemplo de consulta con `curl`:

```bash
curl https://midominio.com/tickets/municipio/mapa
```

La respuesta puede cargarse en el visor de mapas que utilice el municipio.

## Consulta de incidentes abiertos

El endpoint `GET /municipal/incidents` permite listar todos los tickets
abiertos asociados al municipio del usuario autenticado (administrador o
empleado). Esto facilita revisar los reclamos pendientes desde un panel o
integrar la información con otras herramientas.

Ejemplo de respuesta:

```json
[
  {
    "id": 8,
    "nro_ticket": 1020,
    "asunto": "Arreglo de luminaria",
    "categoria": "Iluminación",
    "estado": "nuevo",
    "fecha": "2024-05-01T10:00:00",
    "pregunta": "El poste de la esquina está apagado",
    "detalles": null,
    "direccion": "Av. Siempreviva 456",
    "latitud": -34.61,
    "longitud": -58.38,
    "archivo_url": null
  }
]
```

## Integración con sistemas municipales

- El módulo `services/integracion_municipal.py` contiene un stub de la función
  `enviar_ticket_a_sigem`.
- Puede reemplazarse para integrar SIGEM, GDE u otras plataformas locales.
  Se recomienda manejar las credenciales en variables de entorno.
- Para activarlo, invoca `enviar_ticket_a_sigem` al crear un ticket y maneja las credenciales mediante variables de entorno.


## Encuestas de satisfacción

- Tras la resolución de un ticket, los vecinos pueden valorar la atención con
  `POST /tickets/<tipo>/<id>/encuesta`.
- El endpoint `GET /tickets/<tipo>/<id>/encuesta` muestra la puntuación y el
  comentario almacenados para evaluar la calidad del servicio.

Ejemplo de envío:

```bash
curl -X POST https://midominio.com/tickets/municipio/123/encuesta \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"puntuacion":5,"comentario":"¡Muy buen servicio!"}'
```

