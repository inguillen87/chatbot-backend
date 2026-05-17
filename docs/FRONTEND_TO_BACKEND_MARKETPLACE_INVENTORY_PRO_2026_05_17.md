# Frontend to Backend - Marketplace e Inventario Pro

Fecha: 2026-05-17

## Objetivo

Separar claramente demos publicas de operacion profesional paga.

- En demo, el catalogo sirve para mostrar la experiencia. No confirma stock real, inventario, precio final ni disponibilidad comercial.
- En tenant pago, el catalogo y el inventario son fuente operativa: admin, widget, WhatsApp, carrito, pedidos y chat profesional deben leer el mismo estado backend.

La regla central se mantiene: frontend no inventa stock, precio final, disponibilidad, cantidades, costos de envio ni confirmaciones de pedido.

## Estado backend disponible

La plataforma ya tiene base reutilizable:

- `CatalogoItem.cantidad` se usa como stock actual.
- `CatalogoItem.sku`, `precio`, `precio_monetario`, `moneda`, `categoria`, `marca`, `unidad`, `disponible`.
- Importadores CSV/XLSX/PDF:
  - `POST /api/admin/catalog/import`
  - `GET /api/admin/catalog/import/{upload_id}`
  - `PUT /api/admin/catalog/import/{upload_id}`
  - `POST /api/admin/catalog/import/{upload_id}/commit`
  - legacy: `POST /api/admin/catalogo/importar`
- Calidad de catalogo:
  - `GET /api/v2/catalog/quality`
  - `GET /api/v2/tenants/{tenant_slug}/catalog/quality`
- Admin de items:
  - `GET /api/admin/tenants/{tenant_slug}/catalog`
  - `GET /api/admin/tenants/{tenant_slug}/catalog/items`
  - `PATCH /api/admin/tenants/{tenant_slug}/catalog/items/{item_id}`

## Contrato admin catalogo

`GET /api/admin/tenants/{tenant_slug}/catalog`

Debe responder:

```json
{
  "contract_version": "tenant.catalog_admin.v1",
  "request_id": "req_...",
  "tenant_slug": "bodega",
  "catalog_version": "cat_1_20260517103000",
  "status": "published",
  "links": {
    "items_endpoint": "/api/admin/tenants/bodega/catalog/items",
    "item_patch_template": "/api/admin/tenants/bodega/catalog/items/{item_id}",
    "bulk_import_v2": "/api/admin/catalog/import",
    "stock_only_import_v2": "/api/admin/catalog/import",
    "quality_endpoint": "/api/v2/tenants/bodega/catalog/quality"
  },
  "inventory": {
    "contract_version": "catalog.inventory_ops.v1",
    "enabled": true,
    "catalog_version": "cat_1_20260517103000",
    "last_inventory_update_at": "2026-05-17T10:30:00Z",
    "columns": {
      "stock_columns": ["stock_quantity", "stock", "cantidad", "existencias", "inventory", "inventario", "available_quantity", "qty"],
      "supported_import_modes": ["upsert", "replace", "stock_only"]
    },
    "rules": {
      "demo_mode": false,
      "chat_confirms_stock_only_after_backend_validation": true,
      "frontend_must_not_invent_availability": true
    }
  },
  "frontend_contract": {
    "render_as": "tenant_catalog_inventory_admin",
    "primary_view": "catalog_and_inventory",
    "supports_inline_stock_edit": true,
    "supports_bulk_import": true,
    "supports_stock_only_import": true,
    "supports_quality_board": true
  }
}
```

## Contrato de item

`GET /api/admin/tenants/{tenant_slug}/catalog/items`

Cada item debe traer:

```json
{
  "catalogo_item_id": 123,
  "sku": "SKU-001",
  "nombre": "Producto",
  "precio_texto": "$ 1200",
  "price_numeric": 1200,
  "moneda": "ARS",
  "stock": "8",
  "stock_quantity": 8,
  "stock_status": "in_stock",
  "available_to_sell": true,
  "inventory": {
    "contract_version": "catalog.inventory_item.v1",
    "stock": "8",
    "stock_quantity": 8,
    "stock_status": "in_stock",
    "available_to_sell": true,
    "can_start_order": true,
    "can_confirm_order": true,
    "inventory_source": "catalogo_item",
    "updated_at": "2026-05-17T10:30:00Z"
  }
}
```

Estados de stock:

- `in_stock`: se puede iniciar y confirmar pedido si checkout backend vuelve a validar.
- `low_stock`: mostrar alerta de bajo stock.
- `out_of_stock`: no confirmar pedido.
- `stock_unknown`: se puede registrar interes o consulta, pero no confirmar disponibilidad.
- `not_available`: item no vendible/publicable.

## Edicion manual de stock

`PATCH /api/admin/tenants/{tenant_slug}/catalog/items/{item_id}`

Body soportado:

```json
{
  "stock_quantity": 18,
  "precio": "1500",
  "disponible": true,
  "inventory_source": "tenant_admin_inline_edit"
}
```

Respuesta:

```json
{
  "contract_version": "tenant.catalog_item_update.v1",
  "request_id": "req_...",
  "catalog_version": "cat_1_20260517103000",
  "item": {
    "catalogo_item_id": 123,
    "stock_quantity": 18,
    "stock_status": "in_stock",
    "available_to_sell": true,
    "inventory": {}
  }
}
```

Reglas frontend:

- Mostrar guardado optimista solo como pendiente.
- Confirmar el nuevo valor solo con la respuesta backend.
- Si cambia `catalog_version`, refrescar tabla, cards y chat/widget cache.

## Importacion CSV/XLSX

`POST /api/admin/catalog/import`

Multipart:

- `file`: CSV/XLSX/PDF/imagen.
- Query o tenant context autenticado.

Preview:

```json
{
  "contract_version": "catalog.import_preview.v1",
  "upload_id": 77,
  "columns": [],
  "rows_sample": [],
  "quality_summary": {},
  "inventory_summary": {
    "contract_version": "catalog.inventory_summary.v1",
    "total_rows": 50,
    "with_stock": 46,
    "stock_unknown": 4,
    "low_stock": 3,
    "out_of_stock": 2
  },
  "commit_endpoint": "/api/admin/catalog/import/77/commit",
  "frontend_contract": {
    "render_as": "catalog_import_preview",
    "editable_rows": true,
    "publish_requires_admin_confirmation": true
  }
}
```

Commit:

`POST /api/admin/catalog/import/{upload_id}/commit`

Body:

```json
{
  "mode": "upsert|replace|stock_only"
}
```

Modos:

- `upsert`: actualiza por `sku`; crea si no existe.
- `replace`: reemplaza catalogo del tenant.
- `stock_only`: actualiza solo cantidades por `sku`; no crea productos nuevos.

Respuesta:

```json
{
  "success": true,
  "contract_version": "catalog.import_commit.v1",
  "request_id": "req_...",
  "mode": "stock_only",
  "count": 45,
  "created": 0,
  "updated": 45,
  "stock_updated": 45,
  "skipped_rows": [
    { "sku": "NO-EXISTE", "reason_code": "sku_not_found_for_stock_only_import" }
  ],
  "catalog_version": "cat_1_20260517103000",
  "inventory_summary": {}
}
```

## UX/UI para frontend tenant admin

Pantalla: `Catalogo e inventario`.

Secciones minimas:

- Cards: productos publicados, listos para vender, sin precio, sin stock, bajo stock, agotados, ultima importacion.
- Tabla editable: SKU, nombre, categoria, precio, stock, estado, visible, ultima actualizacion.
- Edicion inline de stock y precio con estado `guardando`.
- Filtros: categoria, stock_status, sin precio, sin imagen, actualizado hace mas de X dias.
- Bulk upload wizard:
  1. Subir CSV/XLSX/PDF.
  2. Mapear columnas.
  3. Preview con filas creadas/actualizadas/error.
  4. Commit `upsert`, `replace` o `stock_only`.
  5. Resultado con `request_id`, `catalog_version` y filas omitidas.
- Acciones por item: editar, ocultar, subir imagen, ver en widget, copiar link, historial.
- Badges visibles: `Sin stock`, `Bajo stock`, `Stock sin validar`, `Precio faltante`, `No publicado`.

## UX para chat, widget y WhatsApp

Reglas:

- Catalogo descargable puede abrir link externo.
- Consultar producto, stock, precio, pedido y checkout siguen dentro del chat.
- El chat solo muestra disponibilidad publicada por backend.
- Si `stock_status === "stock_unknown"`, responder "stock a confirmar" y crear lead/pedido borrador, no pedido confirmado.
- Si `out_of_stock`, ofrecer alternativas backend o derivar humano.
- Si el usuario pide mas unidades que `stock_quantity`, backend debe bloquear o pedir validacion humana.
- Pedido confirmado exige validacion backend de stock en el ultimo paso.

## Gobiernos y colegios

No forzar lenguaje comercial cuando no corresponde.

Gobierno puede usar el mismo contrato para:

- cupos de turnos o entregas,
- kits municipales,
- materiales o recursos publicados,
- productos de economia local,
- stock de programas o beneficios.

Colegios pueden usarlo para:

- uniformes,
- libros,
- talleres,
- eventos,
- cuotas/matriculas como conceptos,
- certificados o recursos descargables.

Frontend debe leer labels del backend. Si el tenant es gobierno/colegio, usar `recursos`, `cupos`, `conceptos` o `catalogo escolar` cuando backend lo publique; no llamar todo "producto" por defecto.

## Demo vs Pro

Demo publica:

```json
{
  "demo_mode": true,
  "inventory_policy": {
    "real_stock": false,
    "confirm_orders": false,
    "message": "Esta demo no confirma stock real."
  }
}
```

Pro publica:

```json
{
  "demo_mode": false,
  "inventory_policy": {
    "real_stock": true,
    "confirm_orders": "backend_validated",
    "catalog_version": "cat_..."
  }
}
```

## QA compartida

1. Admin puede editar stock de un producto y ver `catalog_version` nuevo.
2. CSV/XLSX con `sku` y `stock` permite `stock_only` sin crear productos nuevos.
3. CSV/XLSX completo permite `upsert` de precio, imagen, categoria y stock.
4. Widget no confirma compra si `stock_status` es `stock_unknown` o `out_of_stock`.
5. WhatsApp consulta stock desde backend y no desde cache frontend.
6. Gobierno y colegio no muestran texto comercial si backend publica labels verticales.
7. Demo muestra catalogo ilustrativo, pero no promete inventario real.
8. Cada import, patch y confirmacion conserva `request_id`.
