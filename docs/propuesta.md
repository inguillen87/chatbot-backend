# Propuesta y resumen de funcionalidades de Chatboc

Este documento resume las principales capacidades del backend de Chatboc. Puede utilizarse como base para crear infografías o material promocional.

## Características principales

- **Agente IA 24/7**: atención automatizada y continua para consultas frecuentes.
- **Soporte para múltiples sectores**: endpoints optimizados para pymes y municipios (`/ask/pyme` y `/ask/municipio`).
- **Registro y autenticación integrados**: creación de usuarios desde el widget o el panel, con opciones de inicio de sesión por Google.
- **Gestión de catálogo**: subida de archivos (PDF, Excel, imágenes), búsqueda vectorial y manejo de carrito.
- **Tickets y reclamos**: creación, historial, encuestas de satisfacción y mapa de incidentes.
- **CRM básico**: administración de clientes, tags de segmentación, métricas y envíos de campañas simuladas.
- **Módulo municipal**: incidentes abiertos, estadísticas, métricas y posibilidad de integración con sistemas externos (ej. SIGEM).
- **Gestión de empleados**: altas, bajas y asignación de categorías de tickets.
- **Subida y descarga de archivos**: adjuntos en chats o tickets con control de formatos.
- **Notificaciones por correo, SMS y WhatsApp** al actualizar tickets o pedidos.
- **Soporte de geolocalización**: guardar ubicación en tickets y obtener métricas basadas en la zona.
- **Funcionalidades opcionales**: dictado por voz en el chat, personalización del globito de atención y perfiles inteligentes según el estado de sesión.

## Consideraciones técnicas

- Validación de roles y permisos mediante decoradores (`require_role`, `require_municipio_access`).
- Variables de entorno para configurar CORS, Google Maps, límites de resultados y zona horaria.
- El token de la empresa puede enviarse por encabezados, parámetros de URL o campos del formulario para facilitar la integración del widget.
- Documentación adicional en la carpeta `docs/` con guías de integración, troubleshooting y ejemplos de código.

Chatboc combina IA y herramientas de gestión para brindar una atención eficiente tanto a municipios como a empresas privadas.
