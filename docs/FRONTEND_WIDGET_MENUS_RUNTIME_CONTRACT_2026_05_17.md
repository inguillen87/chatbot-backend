# Frontend Widget Menus Runtime Contract

Fecha: 2026-05-17

## Objetivo

El widget web y el sandbox WhatsApp deben usar los menus publicados por backend para empresas, gobiernos y colegios. Frontend no debe inventar textos, rutas, acciones, disponibilidad humana, montos, catalogos ni resultados operativos.

## Endpoint de chat

Usar siempre `workspace.chat_bootstrap.endpoint` o `chat_bootstrap.endpoint`.

Endpoints soportados:

- `/api/ask`
- `/api/ask/pyme`
- `/api/ask/municipio`

El backend no debe publicar `/ask/...` como endpoint visual de demo. Esas rutas pueden existir como compatibilidad interna, pero `chat_bootstrap.endpoint` y `same_origin_endpoint` deben venir bajo `/api/ask/...`. No probar bases alternativas despues de una respuesta de limite.

## Request para acciones de menu

Cada click de menu se envia como POST al endpoint de `chat_bootstrap`:

```json
{
  "pregunta": "",
  "tenant_slug": "qa-colegio-sandbox",
  "tipo_chat": "pyme",
  "demo_mode": true,
  "action_id": "create_school_case",
  "education_context": { "is_education": true }
}
```

Headers obligatorios si existen en contrato:

```json
{
  "X-Chat-Session-Id": "sid_...",
  "X-Demo-Session-Id": "eyJ...",
  "X-Tenant-Slug": "qa-colegio-sandbox",
  "X-Anon-Id": "anon..."
}
```

## Lectura de mensajes

Orden recomendado:

1. `messages[].content`
2. `assistant_message.content`
3. `message_body`
4. `respuesta`
5. `respuesta_usuario`
6. `message`

Si backend responde 200 y `messages[]` esta vacio, mostrar `message_body` y registrar warning compacto. No descartar botones por falta de `messages[]`.

## Lectura de menus

Leer acciones desde:

1. `workspace.primary_actions`
2. `workspace.operational_menu.primary_actions`
3. `workspace.education|government|gobierno|municipio|pyme|business|commerce.primary_actions`
4. `workspace.verticals.<active_vertical>.actions`
5. `workspace.default_menu.items`
6. `workspace.quick_menu`
7. `quick_menu`
8. `botones`
9. `options_list`
10. `experience_blueprint.conversion_ctas.actions`

Para cada item usar:

- label: `label`, `texto`, `title`, `cta_label`
- action id: `action_id`, `intent`, `action`, `id`, `key`
- descripcion: `description`, `descripcion`, `detail`

Deduplicar por `action_id` o `intent`. Renderizar solo `enabled !== false`.

## URLs y recursos

Si un item trae `url`, abrir como link externo:

```tsx
<a href={resource.url} target="_blank" rel="noreferrer">
```

No convertir `/api/v2/demo/catalog-assets/...` ni `/media/demo_catalogs/...` en rutas SPA. No enviar un PDF como mensaje de chat.

## Limites de demo

Si backend responde:

- `reason_code: "demo_message_limit_reached"`
- `reason_code: "anonymous_trial_limit_reached"`
- `reason_code: "anonymous_message_limit_reached"`

Frontend debe:

- bloquear composer y botones operativos,
- mantener historial visible,
- mostrar card comercial desde `upgrade`,
- no reintentar en `/ask`, `/api/ask/pyme` ni otra base,
- mostrar `request_id` solo como trazabilidad compacta.

## Comportamiento por rubro

### Colegios

Acciones principales:

- `create_school_case`
- `justify_absence`
- `talk_secretary`

`talk_secretary` puede devolver:

```json
{
  "success": true,
  "request_id": "req_...",
  "fuente": "education_widget_live_handoff",
  "data": {
    "ticket_id": 123,
    "chat_id": "P-123456",
    "status": "esperando_agente_en_vivo",
    "live_chat": {},
    "school_case": {}
  }
}
```

Frontend no inventa horarios ni disponibilidad si `data.live_chat` no lo publica.

### Gobiernos

Acciones minimas:

- `crear_reclamo`
- `consultar_estado_reclamo`
- `consultar_tramite`
- `derivar_humano`

Frontend renderiza el menu y envia `action_id`. La creacion de reclamos, estado y derivacion se decide por backend.

### Empresas

Acciones minimas:

- `consultar_producto`
- `crear_pedido`
- `cotizar_envio`
- `capturar_lead_comercial`

Si backend responde `amount_validated: false`, frontend no muestra monto final confirmado.

## Marketplace e inventario Pro

Documento completo: `docs/FRONTEND_TO_BACKEND_MARKETPLACE_INVENTORY_PRO_2026_05_17.md`.

Regla para demos:

- `demo_mode=true` no confirma stock real, inventario, precio final ni disponibilidad comercial.
- El catalogo demo puede mostrar recursos ilustrativos y descargables.
- Botones de pedido en demo muestran el flujo, pero la confirmacion comercial real queda deshabilitada o marcada como demo.

Regla para tenants pagos:

- Admin, widget, WhatsApp, chat profesional, carrito y pedidos usan el mismo catalogo backend.
- Frontend renderiza `stock`, `stock_quantity`, `stock_status`, `available_to_sell`, `inventory`, `catalog_version` y `request_id`.
- Frontend no calcula stock ni precio final.
- Pedido confirmado requiere validacion backend en el ultimo paso.

Endpoints a consumir:

- `GET /api/admin/tenants/{tenant_slug}/catalog`
- `GET /api/admin/tenants/{tenant_slug}/catalog/items`
- `PATCH /api/admin/tenants/{tenant_slug}/catalog/items/{item_id}`
- `POST /api/admin/catalog/import`
- `GET /api/admin/catalog/import/{upload_id}`
- `PUT /api/admin/catalog/import/{upload_id}`
- `POST /api/admin/catalog/import/{upload_id}/commit`
- `GET /api/v2/tenants/{tenant_slug}/catalog/quality`

Importacion:

- `mode=upsert`: actualiza por SKU y crea faltantes.
- `mode=replace`: reemplaza catalogo del tenant.
- `mode=stock_only`: actualiza solo stock por SKU y no crea productos nuevos.

UX esperada:

- Pantalla tenant admin `Catalogo e inventario`.
- Cards: publicados, listos para vender, sin precio, sin stock, bajo stock, agotados, ultima importacion.
- Tabla editable: SKU, nombre, categoria, precio, stock, estado, visible, ultima actualizacion.
- Wizard de CSV/XLSX: subir, mapear columnas, preview, confirmar, resultado.
- Chat/widget muestra `stock_unknown` como "stock a confirmar", no como disponible.

## QA frontend

1. Cambiar Empresas -> Colegios -> Gobiernos no conserva `tenant_slug`, `widget_token` ni `chat_bootstrap` anterior.
2. Todo click de menu envia `action_id`.
3. Ningun click cae a `/ask` si `chat_bootstrap.endpoint` existe.
4. Limite de demo no dispara retry.
5. `messages[]`, `message_body` y botones se renderizan aunque uno de los campos venga ausente.
6. PDFs abren desde `/api/v2/demo/catalog-assets/...` sin pasar por React Router.
7. Colegios no muestra copy municipal.
8. Gobiernos no muestra copy comercial.
9. Empresas no muestra monto final si no fue validado por backend.
10. Widget mantiene navegacion por teclado y lector de pantalla.
11. Tenant pago puede actualizar stock por `PATCH` y por import `stock_only`.
12. Widget/WhatsApp no confirma compra si `stock_status` es `stock_unknown` u `out_of_stock`.
