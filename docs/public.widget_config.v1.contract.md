# public.widget_config.v1

Contrato de configuración pública de widget para builder/preview.

## Endpoint canónico

- `GET /api/public/widget-config?tenant=<slug>`

## Response 200

```json
{
  "contract_version": "public.widget_config.v1",
  "tenant": {
    "slug": "colegio-san-martin",
    "tipo": "pyme"
  },
  "widget": {
    "builder_config": {}
  },
  "builder_config": {},
  "suppress_global_widget": true,
  "integration_preview": false
}
```

## Response 404

```json
{
  "contract_version": "public.widget_config.v1",
  "error": {
    "code": 404,
    "message": "Tenant no encontrado"
  }
}
```

## Notas

- Se incluye `contract_version` también en errores para parsing consistente.
- `quick_menu` puede venir dentro de `widget` y variar por rubro (por ejemplo, educación).
