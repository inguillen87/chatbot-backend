# Arquitectura multi-tenant para marketplace municipal

Este memo resume por qué el modelo multi-tenant encaja con el marketplace municipal y qué implicancias UX/tecnológicas trae frente a un marketplace multi-vendedor centralizado.

## Diferencia de modelos
- **Multi-tenant (SaaS, estilo Shopify/Bagisto)**: cada municipio/empresa tiene su propia tienda con dominio/slug único, catálogo y carrito aislados. La infraestructura se comparte para reducir costos y actualizaciones, pero los datos permanecen separados.
- **Marketplace multi-vendedor centralizado (Amazon/Etsy)**: todos venden en un mismo sitio y comparten la UI principal. Requiere flujos de aprobación y administración más complejos para productos globales.
- **Conclusión**: el esquema "tienda por tenant" es el correcto para no mezclar productos entre municipios y reutiliza la lógica multi-tenant ya aplicada en encuestas.

## Flujo de usuario
- **Enlace/QR por tenant**: generar una URL única (`/market/{slug}/cart`) y un QR que apunte al catálogo público del tenant. Este enlace puede compartirse vía WhatsApp o desde el widget.
- **Catálogo público**: permite navegar productos (imágenes, precios, descripciones). El carrito queda limitado a ese tenant.
- **Autenticación sin fricciones**: incentivar login passwordless (OTP por teléfono o login vía WhatsApp) para evitar formularios largos. Verificación mínima (código SMS/WhatsApp) basta para confirmar identidad.
- **Checkout**: el carrito no mezcla ítems entre tenants. Al confirmar, se puede disparar flujo de pago interno o derivar a conversación de WhatsApp con el detalle del pedido.

## Administración de catálogo
- Altas/ediciones de productos con imágenes, PDFs, precios, descripciones y variantes locales (categorías, atributos propios por tenant).
- Stock y precios dinámicos con validación antes del pedido.
- Adjuntos informativos (ej. folletos en PDF) vinculados a los productos.

## Principios UX/UI
- Inspirarse en líderes (Amazon, Mercado Libre, Etsy, Shopify) y en hallazgos de Baymard Institute: navegación clara, filtros/buscador útiles y páginas rápidas.
- Optimizar para móvil (la mayoría llegará desde WhatsApp): menús sencillos, botones grandes y carga veloz.
- Checkout directo: acceso visible al carrito incluso antes de login; mensajes de confirmación claros ("Producto agregado al carrito").
- Progresive disclosure: opciones avanzadas sólo para administradores; clientes ven la información esencial.

## Diferencias respecto a encuestas compartibles
- Se mantiene el esquema de enlaces/QR por tenant y vista pública, pero el marketplace añade lógica de e-commerce: stock, precios dinámicos, cálculo de totales, checkout y pagos.
- Requiere más validaciones y manejo de estado (carrito, stock, pagos) que una encuesta estática.

## API Flask multi-tenant (backend)
- **Modelos clave** (todos con `tenant_id`):
  - `MarketCart` + `MarketCartItem`: carrito abierto por tenant + usuario/sesión, con snapshot de precio/puntos y modalidad (venta/canje/donación).
  - `MarketOrder` + `MarketOrderItem`: pedido confirmado a partir del carrito, guardando totales y contacto (nombre/teléfono) para pagos o seguimiento.
  - `CatalogoItem`: producto multi-tenant existente; se filtra por `tenant_id` y dueño (`municipio/pyme`).
- **Rutas nuevas** (`/api/market/<slug>/...`, siempre resolviendo el tenant por el slug de la URL):
  - `GET /catalog`: catálogo público del tenant (opciones `?q` y `?categoria`).
  - `GET /cart`: estado del carrito para ese tenant (session-first, pero si el usuario está logueado se asocia a su `user_id`).
  - `POST /cart/add` y `POST /cart/remove`: agregar/quitar ítems con `catalogo_item_id` y `cantidad`.
  - `POST /checkout/start`: valida que exista carrito y teléfono (`telefono`/`phone` o user logueado), crea `MarketOrder` y deja el carrito en estado `submitted` listo para integrar pago/whatsapp.
  - `GET /cart/url`: devuelve el link compartible `/market/<slug>/cart` usando la configuración pública del tenant.
- **Notas de tenencia y aislamiento**: todos los queries de catálogo y carrito incluyen `tenant_id` y dueño, evitando mezclar productos entre municipios.

## Consumo desde frontend
- **Catálogo**: consumir `GET /api/market/{slug}/catalog` y renderizar productos (precio, puntos, imagen, categoría). Respetar `tenant_slug` para los links/QR.
- **Carrito**: `GET /api/market/{slug}/cart` devuelve `items`, totales por moneda y `contacto` (nombre/teléfono si se conoce). Los endpoints de add/remove devuelven el mismo summary para actualizar la UI.
- **Checkout**: llamar a `POST /api/market/{slug}/checkout/start` con `telefono` (y opcional `nombre`) una vez que el usuario confirma. La respuesta entrega `order_id`, totales y `checkout_options` para enchufar MercadoPago o flujo de puntos.
