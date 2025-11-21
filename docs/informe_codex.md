# Informe para Codex: mejoras urgentes en widget, checkout y panel admin

Este documento consolida fallas detectadas en el widget web/WhatsApp y en el panel de administración, junto a propuestas accionables para que los equipos **codex_frontend** y **codex_backend** evolucionen la plataforma hacia un SaaS multitenant robusto.

## 1. Flujo de carrito y checkout (widget/WhatsApp, usuario anónimo)
### Problemas
- **Catálogo sin slug en URL**: el icono de bolsa abre `/productos` en lugar de una ruta aislada por tenant (`/<tenant>/productos`), rompiendo el aislamiento y provocando CORS si no hay contexto.
- **Checkout incompleto para anónimos**: se generan pedidos sin pedir datos de contacto ni exigir login al canjear puntos; el frontend no redirige a inicio de sesión cuando el backend devuelve saldo insuficiente.
- **Donaciones sin persistir**: cuando todos los ítems son donación, el cliente salta la llamada a `/checkout/crear-preferencia`, dejando la donación sin registro.
- **Carga de nota de pedido fallida**: la opción “Subir nota de pedido” devuelve `ApiError` y no importa ítems; probable error 500 o CORS en `/api/admin/catalogo/importar`.
- **Imágenes faltantes**: productos sin `imagen_url` dejan tarjetas vacías.

### Acciones frontend
- Propagar siempre `tenant_slug`/token en URLs desde el widget (ej. `/<tenant>/productos`) y ajustar el router del PWA para leerlo.
- Para canjes con puntos, mostrar modal “Necesitás iniciar sesión” y abrir login/registro.
- Antes de concluir compras/donaciones anónimas, pedir nombre, email y teléfono (como en el checkout PWA) y adjuntarlo al pedido.
- Registrar donaciones vía API aun con total 0, distinguiendo mensajes según tipo: compra, canje o donación.
- Añadir placeholder genérico cuando `imagen_url` sea nulo.

### Acciones backend
- En `/checkout/crear-preferencia`, persistir pedidos de donación/canje con total 0 y devolver ID/estado; exigir autenticación (401) si un anónimo intenta canjear puntos.
- Guardar donaciones como `PedidoConversacional` con flag `donacion=true` y datos de contacto enviados.
- Revisar `/api/admin/catalogo/importar` para respuestas JSON claras y CORS correcto.
- Asegurar que todas las rutas de productos/carrito/checkout lean `tenant_slug` o `tenant_id` y filtren en consecuencia; registrar tokens de widget en `TenantProfile`.

## 2. Panel de administración y cuentas
### Problemas
- Módulos “Integraciones” y “Encuestas” muestran UI vacía; endpoints no implementados.
- No hay forma de convertir un ciudadano en operador ni verificación de email en el registro.
- Al eliminar un operador, los tickets quedan huérfanos; no hay distribución automática por categoría.

### Acciones frontend
- Mostrar “Sin datos” en gráficos vacíos y ocultar secciones no implementadas.
- Permitir cambio de rol (ciudadano ↔ operador) desde la tabla de usuarios.
- Añadir paginación/búsqueda y validaciones de email en formularios.
- Solicitar confirmación y reasignación al eliminar empleados.

### Acciones backend
- Implementar asignación automática (round-robin o por carga) para tickets de la misma categoría y reasignación al eliminar empleados.
- Exponer endpoints para CRUD de categorías y reglas de puntos por tenant, con verificación de email/SMS para usuarios.
- Proveer exportaciones CSV y paginación en usuarios/tickets.

## 3. Integración MercadoPago
### Problemas
- En compras anónimas no se solicitan datos de contacto antes de redirigir a MercadoPago.
- El webhook actualiza pedidos pero no notifica al usuario en WhatsApp/widget.
- Las credenciales de MercadoPago son globales, sin configuración por tenant.

### Acciones propuestas
- Pedir nombre/email/teléfono antes de crear la preferencia y adjuntarlos al pedido.
- Tras el webhook, enviar mensaje automático al chat confirmando el pago.
- Guardar `mercadopago_access_token` por tenant y usarlo al generar preferencias.

## 4. Importación de archivos y OCR
- Usar Vision API como predeterminada y fallback a OCR local.
- Devolver siempre JSON con errores claros y lista de filas rechazadas; no mutar carrito si la extracción falla.
- Permitir guardar plantillas de mapeo de columnas para futuras importaciones.
- Devolver un `cart_id` preliminar y lista de ítems detectados para confirmación antes de generar pedido.

## 5. Bug de URL en el widget
- El enlace de catálogo del widget apunta a `/productos` sin slug; ajustar plantilla para construir la URL dinámicamente con `tenantSlug` (ej. `/<tenant>/productos`) o abrir el catálogo dentro del propio widget.

## 6. Tareas sugeridas para codex
### Frontend
- Refactorizar rutas de catálogo/carrito para incluir `tenant_slug` y aislar el catálogo.
- Añadir modal de login/contacto al usar puntos o cerrar compra sin autenticación.
- Implementar badges/mensajes diferenciados para donación, canje y venta.
- Placeholder de imagen para productos sin `imagen_url`.
- Manejar errores de importación con spinner y mensajes amigables.
- Menú “Catálogo” en WhatsApp con subopciones; ocultar “Subastas” hasta que exista funcionalidad.
- Paginación y validaciones en panel admin; permitir cambio de roles.
- Confirmar antes de eliminar empleados y mostrar su carga de tickets.

### Backend
- Reescribir `/checkout/crear-preferencia` para registrar pedidos con total 0 (donación/canje) y responder inmediato.
- Persistir donaciones; devolver 401 cuando un anónimo quiera canjear puntos.
- Asegurar multi-tenant en todos los endpoints y registrar tokens de widget en `TenantProfile`.
- Habilitar credenciales de MercadoPago por tenant y enviar notificaciones de pago al chat.
- Gestionar importación de pedidos/archivos con Vision API y CORS correcto; guardar plantillas de mapeo.
- Añadir asignación automática de tickets y re-asignación al eliminar empleados.
- Exponer configuración de categorías y reglas de puntos por tenant; validar registro con email/SMS.
- Proveer API de exportación de datos y usar tareas asíncronas para OCR/notificaciones pesadas.

## 7. Plan técnico inmediato (2 sprints)
- **Sprint 1 – Checkout seguro y rutas multitenant**: (a) En `routes/pwa_public.py` y en el widget, incluir siempre `tenant_slug` en links y derivar a catálogo filtrado; (b) en `routes/checkout.py`, validar contacto mínimo (nombre/email/teléfono) antes de MercadoPago y devolver 401 en canjes sin sesión; (c) mover credenciales de MercadoPago a modelo/config por tenant y bloquear `mercadopago_ready` si faltan.
- **Sprint 1 – Donaciones y notas de pedido**: (a) Crear endpoint específico para donaciones con total 0 que persista `PedidoConversacional`; (b) normalizar `/api/admin/catalogo/importar` y `/api/pedidos/from-file` para devolver JSON con filas fallidas y no mutar carrito ante error; (c) usar Vision API con fallback local.
- **Sprint 2 – Panel admin y tickets**: (a) agregar cambio de rol y verificación de email/SMS; (b) implementar asignación automática y reasignación al eliminar operadores; (c) exponer CRUD de categorías, rewards_rules y exportaciones CSV/paginación.
- **Sprint 2 – UX**: placeholder de imágenes, mensajes diferenciados (donación/canje/venta) y menú de catálogo en WhatsApp; ocultar módulos sin datos con placeholders amigables.

## 8. Checklist de implementación por endpoint
- `/api/pwa/public/*`: validar `g.tenant_profile`, devolver error claro si falta y registrar métricas de slugs inválidos.
- `/api/checkout/crear-preferencia`: exigir contacto para anónimos, 401 en canjes sin login, persistir pedido con total 0, obtener token MercadoPago por tenant.
- `/api/mercadopago/webhook`: validar firma/idempotencia, buscar pedido por `tenant_id`, notificar por WhatsApp/widget tras cambio de estado.
- `/api/pedidos/from-file` y `/api/admin/catalogo/importar`: validar tamaño/extensión antes de procesar, devolver `rows_with_errors`, evitar que el carrito se modifique si hay fallos graves, permitir plantillas de mapeo.
- `/api/admin/users/*`: cambio de rol, verificación email/SMS, exportación CSV y paginación; al borrar operador, reasignar tickets automáticamente.

Con estas acciones se corrigen las fallas actuales y se sienta la base para un SaaS multitenant competitivo con marketplace conversacional integrado.
