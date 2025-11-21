# Informe de revisión rápida del flujo PWA/widget

Resumen de hallazgos al revisar el código que maneja catálogo público, carrito y checkout desde el widget/PWA. Se señalan problemas observados en la implementación actual y recomendaciones técnicas para abordarlos.

## Hallazgos principales

1. **Carrito público sin autenticación ni control de sesión robusto**
   - Las operaciones de catálogo y carrito en el PWA (`/api/pwa/public`) dependen únicamente de la cookie de sesión (`tenant_public_carts`) y no verifican identidad alguna, por lo que cualquier visitante puede agregar/editar ítems y el carrito se comparte por navegador sin restricción de usuario real. 【F:routes/pwa_public.py†L200-L345】
   - El resumen de carrito expone opciones de checkout con `mercadopago_ready` en `True` aun sin haber validado autenticación o disponibilidad real del gateway. 【F:routes/pwa_public.py†L226-L245】

2. **Checkout no pide login para compras monetarias**
   - El endpoint `/api/checkout/crear-preferencia` arma el pedido con el usuario resuelto (puede ser anónimo) y solo fuerza autenticación cuando el total es en puntos. No hay validación para exigir login en compras con dinero ni para garantizar que el carrito pertenezca al usuario autenticado. 【F:routes/checkout.py†L50-L144】
   - Si no hay `MERCADOPAGO_ACCESS_TOKEN` o el total es cero, el pedido pasa a estado `confirmado` sin procesar pago, lo que puede dar compras “gratuitas” inadvertidas. 【F:routes/checkout.py†L157-L199】

3. **Webhook de MercadoPago con token embebido y sin validaciones**
   - El webhook usa un `ACCESS_TOKEN` hardcodeado de prueba y no referencia a configuración segura. Tampoco valida firma, origen ni idempotencia, dejando abierto a ejecuciones no autenticadas o repetidas. 【F:routes/mercadopago_webhook.py†L16-L100】

4. **Procesamiento de nota de pedido desde archivo es frágil**
   - El endpoint `/api/pedidos/from-file` confía en extracción automática (`extract_table_from_file`) y en Excel/CSV, pero no normaliza errores comunes (archivos grandes, extensiones desconocidas) ni retorna mapeo claro de filas rechazadas más allá de `no_encontrados`. Además, agrega ítems al carrito en sesión aun si faltan campos, sin feedback granular. 【F:routes/pedidos_from_file.py†L98-L163】

5. **Resolución de tenant y urls del widget**
   - El PWA asume que el middleware ya colocó `g.tenant_profile`; si no está, responde 404 genérico. No hay redirección ni mensaje específico para widgets con slug mal armado, lo que puede explicar accesos rotos desde “catálogos” en el menú. 【F:routes/pwa_public.py†L13-L107】

## Recomendaciones

- Requerir autenticación (o al menos correo/teléfono verificado) antes de permitir checkout monetario; ligar el carrito a un `user_id` en base de datos en lugar de solo sesión.
- Validar disponibilidad real de MercadoPago antes de marcar `mercadopago_ready` y bloquear la ruta de checkout si falta `MERCADOPAGO_ACCESS_TOKEN`.
- Mover tokens de MercadoPago a variables de entorno y agregar verificación de firma, idempotencia y estado permitido en el webhook.
- Mejorar manejo de errores en carga de notas de pedido: validar tamaño/tipo de archivo antes de procesar, devolver lista de líneas con errores y no mutar el carrito si la extracción falla.
- Endurecer resolución de tenant: devolver errores explícitos de URL/slug, registrar accesos sin tenant y ofrecer ruta de fallback para el widget con enlaces correctos.

## Pasos técnicos sugeridos (front + back)
- **Catálogo y widget**: incluir `tenant_slug` en URLs generadas desde el widget; agregar guardas en `routes/pwa_public.py` para que, si falta `g.tenant_profile`, devuelva JSON de error indicando slug inválido y cómo reconstruir la URL.
- **Checkout**: en `routes/checkout.py`, bloquear preferencias sin contacto; crear validación `requires_contact_or_auth` y reutilizarla para anónimos. Marcar `mercadopago_ready=False` si no hay token por tenant.
- **Webhook**: parametrizar tokens por tenant y validar firma/`topic`/`action`; registrar eventos idempotentes y notificar por WhatsApp/widget tras actualizar pedido.
- **Importación de archivos**: mover la validación de archivo (extensión/tamaño) antes de `extract_table_from_file`; devolver `rows_with_errors` y `skipped_rows`; no agregar al carrito si el parsing devolvió errores críticos.
- **Pruebas y métricas**: agregar tests unitarios para (a) checkout anónimo sin contacto (debe fallar), (b) webhook con firma inválida (debe rechazar), (c) importación con CSV inválido (debe devolver errores sin mutar carrito). Medir y loggear slugs inválidos para detectar widgets mal configurados.
