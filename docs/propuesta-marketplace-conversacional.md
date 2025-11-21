# Propuesta integral de mejoras para Chatboc

Este documento resume la hoja de ruta solicitada para evolucionar Chatboc hacia una plataforma de e-commerce conversacional multi-tenant con catálogo avanzado, pagos, importación asistida por IA y recomendaciones inteligentes. Cada apartado destaca el objetivo de negocio, los cambios propuestos y consideraciones de implementación.

## 1. Contexto multi-tenant y acceso anónimo
- Mantener el contexto de tenant por `slug` para aislar catálogos y datos.
- Resolver el tenant automáticamente por canal:
  - **WhatsApp**: mapear el número oficial de destino al `slug` antes de listar catálogo o crear pedidos.
  - **Widget web**: aceptar `data-tenant`, `X-Tenant`/`tenant` o usar el dominio como respaldo si ya existe el mapeo.
- Responder 404 ante tokens o números no reconocidos para evitar filtraciones.

## 2. Catálogo con modalidades de producto (venta, donación, puntos)
- Extender `CatalogoItem` con un campo `modalidad` (`"venta" | "donacion" | "puntos"`).
- Convenciones de precio:
  - Venta: precio monetario normal.
  - Donación: `modalidad="donacion"` y precio `0` (pedido sin pago monetario).
  - Puntos: precio con moneda `"PTS"` para reutilizar el subtotal de puntos.
- Checkout:
  - Canje: validar saldo de puntos y deducirlos de forma atómica al confirmar.
  - Donación: pedidos $0 con flag por ítem; confirmar sin pasarela de pago.

## 3. Sistema de puntos por participación ciudadana
- Crear `RecompensasService` para acreditar, consultar y redimir puntos.
- Tabla/historial de transacciones por usuario (motivo, fecha, saldo resultante) configurable por tenant (p. ej. en `TenantProfile.configuracion`).
- Ganancias de puntos por encuestas, reclamos, propuestas, sondeos, compras y referidos.
- Redención integrada al checkout de productos en `PTS`, con validación de saldo y registro de canje.

## 4. Integración de pagos con MercadoPago
- Crear preferencia al confirmar pedidos con monto monetario, guardando `preference_id` y links.
- Registrar pedidos internos en estado "Pendiente de pago" vinculados al `preference_id`.
- Redirigir a `init_point`/deep link o QR para web y WhatsApp.
- Webhook `mercadopago_webhook` ampliado para eventos `payment`; marcar pedido como pagado/aprobado o rechazado/expirado.
- Preparar credenciales por tenant en `TenantProfile.configuracion` sin romper el flujo unificado actual.

## 5. Importación automatizada de catálogos (Excel/PDF/imagen)
- Detección automática de columnas y plantillas de mapeo persistentes por tenant.
- Soportar Excel/CSV y PDF con extracción de tablas; aceptar imágenes incrustadas o URLs de fotos.
- Caso FCA MZA 2024.xlsx:
  - Nombre: `BRAND + VARIETAL`.
  - Precio unitario: `$ BOTTLE` (ARS asumido).
  - Precio por caja: `$ BOX` guardado como `precio_por_caja` o en metadata.
  - Unidad: `UNIT/BOX` (unidades por caja); `BOX/PALLET` como metadato logístico.
- Reutilizar configuraciones y actualizar productos existentes por SKU o nombre.

## 6. Reconocimiento de pedidos desde archivos/imagen
- Aceptar Excel/CSV/PDF/imagen enviados por web o WhatsApp y convertirlos en ítems del carrito.
- **Extracción primaria**: OpenAI Vision API para OCR/interpretación de tablas.
- **Fallback**: bibliotecas locales (Tesseract/pdfplumber) cuando Vision no esté disponible.
- Matching con catálogo por SKU exacto o búsqueda difusa; reportar ítems ambiguos/no encontrados.
- Generar carrito preliminar y compartir enlace mágico para revisión y confirmación.
- Mostrar recomendaciones de upsell/cross-sell tras la importación.

## 7. IA para kits, maridajes y descuentos inteligentes
- Modelo `Bundle/Kit` con precio promocional; creación manual y futura generación automática.
- Reglas iniciales:
  - Maridajes por categoría (p. ej., Malbec → carnes rojas/quesos duros).
  - Complementos por rubro y temporalidad.
  - Upsell de umbral (envío gratis, descuentos por volumen) y cupones contextuales.
- Evolución hacia co-ocurrencia y personalización por historial cuando haya datos suficientes.

## 8. Robustez, escalabilidad y mantenimiento
- Filtrado por tenant en cada capa con índices adecuados; roles: admins importan/manejan promos, empleados gestionan pedidos, usuarios finales consumen su catálogo.
- Modularizar en servicios: catálogo/importación, pedidos/checkout, recompensas, OCR, recomendaciones.
- Tareas pesadas (importación, OCR, creación de preferencia) en Celery/Redis con notificaciones de progreso.
- Logging/monitoreo detallado para pagos, importaciones y OCR; alertas ante fallos de webhook o deducción de puntos.
- Preparar sesiones compartidas (Redis) para despliegue horizontal y rate limiting al llamar APIs externas.

## 9. Consideraciones de experiencia de usuario
- Web: redirección automática a MercadoPago y recomendaciones debajo del carrito importado.
- WhatsApp: mensajes con links/QR de pago, notificaciones al aprobar pagos y preguntas de desambiguación.
- Confirmaciones diferenciadas según tipo de pedido: compra, donación o canje de puntos.

## 10. Próximos pasos sugeridos
1. Crear modelos/migraciones para `modalidad` en productos, historial de puntos y bundles.
2. Exponer `RecompensasService` y ampliar checkout para puntos y MercadoPago.
3. Implementar pipeline de importación con mapeo automático y plantillas reutilizables.
4. Añadir endpoint/tarea para reconocimiento de pedidos vía OpenAI Vision (con fallback local) y generación de carrito pre-cargado.
5. Integrar reglas de recomendaciones iniciales y superficies de UI/WhatsApp para upsell.
