# Propuesta Integral de Mejoras para el Marketplace Conversacional Chatboc

Esta propuesta resume las evoluciones solicitadas para convertir Chatboc en una plataforma de e-commerce conversacional inteligente, multi-tenant y global, con capacidades de catálogo avanzado, pagos, reconocimiento de pedidos y recomendaciones potenciadas por IA.

## 1. Contexto multi-tenant y acceso anónimo
- Mantener el _tenant context_ por `slug` para aislar catálogos y datos por municipio/PYME.
- Resolución automática del tenant según canal:
  - **WhatsApp**: mapear número de destino oficial → `slug` y aplicar `tenant_context` antes de listar catálogo o crear pedidos.
  - **Widget web**: incluir `data-tenant` o `X-Tenant`/`tenant` en el snippet; usar dominio como respaldo si ya existe el mapping.
- Respuesta 404 ante tokens o números no reconocidos para evitar filtraciones.

## 2. Catálogo con modalidades de producto (venta, donación, puntos)
- Extender `CatalogoItem` con un campo `modalidad` (`"venta" | "donacion" | "puntos"`).
- Convenciones de precio:
  - Venta: precio monetario normal.
  - Donación: `modalidad="donacion"` y precio `0` (pedido sin pago monetario).
  - Puntos: precio expresado con moneda `"PTS"` para el costo en puntos, reutilizando la lógica del carrito que ya separa subtotales en puntos.
- Checkout:
  - Canje: validar saldo de puntos y deducirlos de forma atómica al confirmar el pedido.
  - Donación: pedidos $0 con flag por ítem; se notifica y confirma sin pasarela de pago.

## 3. Sistema de puntos por participación ciudadana
- Crear `RecompensasService` para acreditar, consultar y redimir puntos.
- Definir tabla/historial de transacciones de puntos por usuario (motivo, fecha, saldo resultante) con configuración opcional por tenant (p. ej. en `TenantProfile.configuracion`).
- Ganancias de puntos por eventos: encuestas, reclamos, propuestas, sondeos, compras, referidos.
- Redención: integrada al checkout de productos en `PTS`, con validación de saldo y registro de canje.

## 4. Integración de pagos con MercadoPago
- **Creación de preferencia**: al confirmar pedidos con monto monetario, enviar ítems y totales a MercadoPago y guardar `preference_id`/links.
- **Registro de pedido**: crear pedido interno en estado "Pendiente de pago" vinculado al `preference_id`.
- **Redirección**: devolver `init_point`/deep link o QR para web y WhatsApp.
- **Webhook**: ampliar `mercadopago_webhook` para eventos `payment`; validar estado `approved` y marcar pedido como pagado (o rechazado/expirado).
- **Multi-tenant futuro**: permitir credenciales MP por tenant en `TenantProfile.configuracion` sin romper el flujo unificado actual.

## 5. Importación automatizada de catálogos (Excel/PDF/imagen)
- Reforzar el importador existente con detección automática de columnas (similaridad de encabezados) y plantillas de mapeo persistentes por tenant.
- Soportar Excel/CSV (pandas/openpyxl) y PDF con extracción de tablas; aceptar imágenes incrustadas o URLs de fotos.
- Caso de uso FCA MZA 2024.xlsx:
  - Nombre: concatenar `BRAND + VARIETAL`.
  - Precio unitario: `$ BOTTLE` (ARS asumido).
  - Precio por caja: `$ BOX` guardado como `precio_por_caja` o en metadata.
  - Unidad: `UNIT/BOX` (unidades por caja); `BOX/PALLET` como metadato logístico.
- Guardar la configuración de mapeo para reutilizar en próximas cargas y actualizar productos existentes por SKU o nombre.

## 6. Reconocimiento de pedidos desde archivos/imagen
- Aceptar Excel/CSV/PDF/imagen enviados por web o WhatsApp y convertirlos en ítems de carrito del tenant actual.
- **Extracción primaria**: usar OpenAI Vision API para OCR/interpretación de tablas con calidad tipo ChatGPT.
- **Fallback**: bibliotecas locales (p. ej. Tesseract/pdfplumber) cuando Vision no esté disponible.
- Matching con catálogo por SKU exacto o búsqueda difusa por nombre; reportar ítems ambiguos/no encontrados para corrección.
- Generar carrito preliminar y compartir enlace mágico en web/WhatsApp para revisión y confirmación.
- Mostrar recomendaciones de upsell/cross-sell tras la importación.

## 7. IA para kits, maridajes y descuentos inteligentes
- Definir modelo `Bundle/Kit` opcional con precio promocional; permitir creación manual y futura generación automática.
- Reglas iniciales de recomendaciones:
  - Maridajes por categoría (ej. Malbec → carnes rojas/quesos duros).
  - Complementos de rubro (ferretería, farmacia, bodegas) y temporalidad/estacionalidad.
  - Upsell de umbral (envío gratis, descuentos por volumen) y cupones contextuales.
- Evolución hacia análisis de co-ocurrencia y personalización por historial cuando haya datos suficientes.

## 8. Robustez, escalabilidad y mantenimiento
- Garantizar filtrado por tenant en cada capa con índices adecuados; roles: admins importan/manejan promos, empleados gestionan pedidos, usuarios finales solo su catálogo.
- Modularizar en servicios: catálogo/importación, pedidos/checkout, recompensas, OCR, recomendaciones.
- Tareas pesadas (importación, OCR, creación de preferencia) delegadas a Celery/Redis y notificaciones de progreso.
- Logging/monitoreo detallado para pagos, importaciones y OCR; alertas ante fallos de webhook o deducción de puntos.
- Preparar sesiones compartidas (Redis) para despliegue horizontal y rate limiting al llamar APIs externas.

## 9. Consideraciones de experiencia de usuario
- Web: redirección automática a MercadoPago y sección de recomendaciones debajo del carrito importado.
- WhatsApp: mensajes con links/QR de pago, notificaciones al aprobar pagos, y preguntas de desambiguación en ítems dudosos.
- Confirmaciones diferenciadas según tipo de pedido: compra, donación o canje de puntos.

## 10. Próximos pasos sugeridos
1. Crear modelos/migraciones para `modalidad` en productos, historial de puntos y bundles.
2. Exponer servicios `RecompensasService` y ampliar flujo de checkout para puntos y MercadoPago.
3. Implementar pipeline de importación con mapeo automático y plantillas reutilizables.
4. Añadir endpoint/tarea para reconocimiento de pedidos vía OpenAI Vision (con fallback local) y generación de carrito pre-cargado.
5. Integrar reglas de recomendaciones iniciales y superficies de UI/WhatsApp para upsell.
