# Frontend Handoff - Widget, Portal Ciudadano y Seguimiento Junin - 2026-05-14

## Objetivo

Renderizar una experiencia publica premium para municipio Junin sin inventar datos:

- Widget embebible en una pagina externa.
- Chat primero, acciones compactas despues.
- Portal ciudadano accesible como anonimo.
- Registro progresivo sin perder reclamos, carrito, historial ni encuestas.
- Seguimiento de reclamos, pedidos, canjes y puntos con datos reales del backend.

## Contratos backend listos

### Widget externo

Usar:

```txt
GET /api/public/tenants/municipio/widget-config?tenant_slug=municipio
GET /api/public/widget-commerce-session?tenant_slug=municipio
GET /api/public/widget-user/tenant-history?tenant_slug=municipio
POST /api/public/widget-user/register?tenant_slug=municipio
POST /api/public/widget-user/link-session?tenant_slug=municipio
GET /api/pwa/public/cart/summary?tenant=municipio
```

Headers que frontend debe mantener estables:

```txt
X-Chat-Session-Id: <chat_session_id corto>
X-Anon-Id: <anon_id persistente>
Origin: <host externo real>
```

Reglas:

- Persistir `anon_id` en localStorage/cookie y reenviarlo en todos los endpoints publicos.
- Persistir `chat_session_id` por conversacion/widget.
- No usar `demo_session_id` como `chat_session_id`.
- Si backend devuelve `session.chat_session_id` con `sid_...`, usarlo desde ese momento.

### Registro del usuario del widget

`POST /api/public/widget-user/register` ahora requiere:

```json
{
  "name": "Vecino Junin",
  "phone": "+549261...",
  "email": "opcional@example.com"
}
```

Respuesta OK trae:

- `profile.user_id`
- `tenant_follow.linked`
- `merge.municipio_tickets`
- `merge.market_carts`
- `merge.chat_contexts`
- `portal.view_url`

Si falta contacto:

- `reason_code=validation_failed`
- `required_fields=["name","email_or_phone"]`
- `field_errors`

Si el email ya existe:

- `status=verification_required`
- frontend debe pedir login/verificacion, no asumir que la cuenta quedo vinculada.

## UX requerida frontend

### Widget

- Primer nivel: conversacion real.
- Segundo nivel: botones compactos para historial, carrito/canjes, portal y WhatsApp.
- No mostrar carrito si `cart.enabled=false`.
- No mostrar catalogo/canjes si `catalog.enabled=false`.
- No mostrar PDFs como accion principal.
- No mostrar botones duplicados o ajenos al flujo.
- Composer debe soportar texto, imagen, audio y ubicacion si backend/config lo habilita.

### Portal anonimo

`/portal/:tenant` debe abrir aunque no haya login:

- Mostrar historial desde `tenant-history` usando `X-Anon-Id` y `X-Chat-Session-Id`.
- Mostrar reclamos, pedidos, mensajes, encuestas y cart si existen.
- Mostrar CTA de registro progresivo cuando `profile.can_register=true`.
- Despues de registrarse, llamar `link-session` y refrescar `tenant-history`.

### Seguimiento de reclamos

Para cada item `kind=claim` usar `detail_endpoint`.

Render esperado:

- Estado actual con chip claro.
- Timeline de eventos si backend lo envia.
- Adjuntos si existen.
- Mapa solo si hay lat/lng reales.
- CTA para agregar comentario/foto solo si backend publica endpoint.
- No inventar tiempos de reparacion ni empleados asignados.

### Canjes y puntos

- Si cart trae items con puntos, mostrarlo como beneficios/canjes, no como ecommerce generico.
- Mostrar balance de puntos solo si backend lo envia.
- Si falta identidad para canjear, pedir registro o telefono/email.
- No inventar puntos, ranking ni premios.

## Pagina externa de prueba

Backend dejo una pagina de contrato:

```txt
docs/widget_junin_external_contract_test.html
```

Uso local recomendado:

```powershell
cd docs
python -m http.server 8765
```

Abrir:

```txt
http://127.0.0.1:8765/widget_junin_external_contract_test.html
```

Configurar `API base` a:

```txt
http://127.0.0.1:5000
```

Esa pagina no reemplaza el widget final: solo valida contratos, CORS, sesiones, registro, historial y carrito desde un origin externo.

## Checklist frontend

- Widget externo no rompe estilos del host.
- No hay overflow mobile.
- Dark/light mode conserva contraste.
- La sesion anonima sobrevive refresh.
- Registro no borra historial.
- Portal no muestra admin ni configuracion interna.
- Seguimiento usa solo `detail_endpoint` y datos reales.
- Errores JSON se muestran como estados humanos, no como stack/HTML.
- Si un endpoint devuelve `reserved_public_slug`, no reintentar en loop.
- Si `registration_required`, abrir formulario minimo: nombre + telefono/email.
