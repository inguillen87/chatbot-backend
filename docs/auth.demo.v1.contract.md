# auth.demo.v1

Contrato de disponibilidad de endpoints demo de autenticación.

## Endpoints

- `GET /auth/demo/catalog`
- `POST /auth/demo`

## Response 404 (demo mode desactivado)

```json
{
  "contract_version": "auth.demo.v1",
  "error": {
    "code": 404,
    "message": "Demo mode disabled"
  }
}
```

## Regla operativa

- Si `ENABLE_DEMO_MODE=false`, ambos endpoints deben responder 404 con este contrato.
