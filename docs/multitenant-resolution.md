# Resolución de tenant en Chatboc

Este backend permite identificar al municipio o pyme activo a partir de varios indicios para admitir escenarios multi-tenant (widget web, APIs públicas o dominios propios). El middleware centraliza la detección y los blueprints de carrito y otros recursos exponen los encabezados necesarios vía CORS.

## Fuentes de resolución

El middleware `tenant_context` intenta resolver el tenant en el siguiente orden:

1. Encabezado `X-Tenant` con el slug normalizado.
2. Encabezado `X-Tenant-Id` o parámetro `tenant_id` para búsquedas directas por ID numérico.
3. Parámetro de query `tenant` (slug).
4. Segmento de la URL (`/municipio/<slug>`, `/pyme/<slug>`, `/p/<slug>`, `/t/<slug>`, etc.).
5. Dominio mapeado en `TENANT_DOMAIN_MAP` configurado en la aplicación.

Si ninguna fuente coincide se deja `g.tenant_profile` en `None` y el handler puede devolver un mensaje de error acorde.

## Encabezados permitidos en CORS

Las rutas del carrito (`/carrito`) aceptan encabezados específicos para identificar al tenant y al widget, incluyendo `X-Tenant`, `X-Tenant-Id`, `X-Widget-Token` y `X-Whatsapp-Dst`. Esto permite que el frontend público y los canales externos envíen el contexto correcto sin violar CORS.

## Ejemplo de uso

- **Widget público**: enviar `X-Tenant: demo-municipio` o `X-Widget-Token` junto con la petición.
- **APIs autenticadas**: cuando se conoce el ID interno, adjuntar `X-Tenant-Id` para evitar ambigüedad.
- **Rutas con slug**: acceder a `/municipio/<slug>/...` o `/p/<slug>/...` resuelve el tenant sin necesidad de encabezados adicionales si el dominio ya está mapeado.
