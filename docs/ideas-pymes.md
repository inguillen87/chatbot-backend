# Ideas para pymes

Este documento resume funcionalidades pensadas para negocios y emprendimientos que utilicen este backend.

## Consultas frecuentes

- Utiliza el script `faq_loader.py` para cargar preguntas y respuestas prearmadas en la base de datos.
- Las preguntas se almacenan por rubro y pueden modificarse para adaptarse a cada empresa.
- El módulo `services/faq_matcher_spacy.py` busca automáticamente la mejor coincidencia en las FAQs cuando se recibe una consulta.

## Notificaciones automáticas

- Al actualizar un ticket con `TicketService.crear_comentario` se envían avisos por correo y SMS al cliente.
- Para pedidos realizados con `PedidoService.crear_pedido` también se disparan notificaciones opcionales por WhatsApp.

## Panel de métricas

- El endpoint `GET /crm/analytics` devuelve estadísticas de clientes y tickets asociados a la empresa.
- También puede consultarse `GET /metricas` para ver el uso reciente de preguntas y la fecha del último acceso.
- Para obtener un resumen general de tickets por rubro y tipo, se puede llamar a `GET /estadisticas/reclamos`.
