# Frontend to Backend - Widget Demo Runtime Hotfix

Fecha: 2026-05-17

## Objetivo

Corregir los demos del chatwidget para que catalogos, lista de precios, menu de tres puntos, acciones rapidas, tenant pages y carrito funcionen sin 404, sin mezclar tenants y sin convertir links publicados por backend en rutas internas de React.

## Backend ya cubre

- `/api/v2/demo/catalog-assets/<folder>/<file>.pdf` sirve PDFs demo.
- `/media/demo_catalogs/<folder>/<file>.pdf` queda como compatibilidad legacy para bundles viejos.
- `tenant_slug=bodega.chatboc.ar` resuelve como tenant canonico `bodega`.
- Si llega `tenant_slug` explicito y un `widget_token` viejo de otro tenant, gana el tenant explicito y backend no registra ese token viejo en el tenant nuevo.
- Estos endpoints aceptan slug canonico o slug dominio:
  - `GET /api/public/widget-commerce-session`
  - `GET /api/public/widget-user/tenant-history`
  - `GET /api/public/tenants/<slug>/public-navigation`
  - `GET /api/pwa/public/cart/summary`
  - `GET /api/pwa/public/cart/items`

## Frontend obligatorio

1. Links de recursos

Abrir `resource.url` exactamente como viene de backend. No usar router interno para PDFs.

Correcto:

```tsx
<a href={resource.url} target="_blank" rel="noopener noreferrer">
  Abrir catalogo
</a>
```

Incorrecto:

```tsx
navigate(resource.url)
```

Si algun cache o fixture trae `/media/demo_catalogs/...`, reemplazarlo antes de renderizar por `/api/v2/demo/catalog-assets/...`.

2. Canonicalizacion de tenant

En `/t/bodega.chatboc.ar`, el frontend puede mandar `tenant_slug=bodega.chatboc.ar`, pero debe adoptar la respuesta canonica de backend:

```ts
const canonicalTenantSlug = response.tenant?.slug ?? response.tenant_slug ?? routeTenantSlug;
```

Usar `canonicalTenantSlug` para endpoints posteriores, cart, history, widget session y chat.

3. Aislamiento de sesion por tenant/rubro

Al cambiar sector, rubro o tenant:

- limpiar `widget_token`, `widget_session_token`, `demo_session_id`, `chat_session_id`, `cart_session`, `tenant_history` del tenant anterior;
- crear nuevo `chat_session_id`;
- descartar respuestas cuyo `tenant.slug` no coincida con el tenant activo canonico;
- si hay mismatch, rebootstrap sin token cacheado.

4. Widget tokens

El token guardado solo vale para el tenant que lo emitio. No reenviar un token de `municipio` en `/t/bodega.chatboc.ar`.

Regla:

```ts
if (cachedToken?.tenantSlug !== activeCanonicalTenantSlug) {
  clearCachedWidgetToken();
}
```

5. Botones y menu de tres puntos

Todos los botones publicados por backend deben enviar una accion estructurada al endpoint de `workspace.chat_bootstrap.endpoint`.

Payload minimo:

```json
{
  "pregunta": "",
  "tenant_slug": "bodega",
  "tipo_chat": "pyme",
  "demo_mode": true,
  "action_id": "create_order"
}
```

No navegar localmente, no inventar respuestas y no simular resultado. Mostrar loading, bloquear doble click y renderizar la respuesta real del backend.

6. Endpoints operativos

Para cada request del widget enviar siempre:

```txt
tenant_slug=<canonicalTenantSlug>
tenant=<canonicalTenantSlug>
widget_token=<token del tenant activo, solo si existe>
chat_session_id=<session actual>
anon_id=<anon estable>
```

Si `widget-commerce-session` o `tenant-history` devuelven contrato JSON con `reason_code`, renderizar estado vacio o recuperacion comercial. No mostrar stack trace ni romper el widget.

## QA obligatorio frontend

1. En `/demo?sector=empresas`, abrir catalogo y lista de precios. Deben abrir `/api/v2/demo/catalog-assets/...`, no `/media/demo_catalogs/...` ni pantalla `Oops! Page not found`.
2. En `/t/bodega.chatboc.ar`, no debe haber 404 en:
   - `widget-commerce-session`
   - `widget-user/tenant-history`
   - `public-navigation`
3. Con un token cacheado de municipio, abrir `/t/bodega.chatboc.ar`. El frontend debe limpiar el token viejo y quedarse con tenant `bodega`.
4. Botones `Crear pedido`, `Preparar checkout`, `Hablar con ventas` deben hacer POST al chat bootstrap con `action_id`.
5. Cambiar Empresas -> Colegios debe limpiar sesion y no mostrar textos municipales ni comerciales en el saludo escolar.
6. El menu de tres puntos debe ser 100% navegable por teclado, con `aria-label`, foco visible y accion real al backend.

