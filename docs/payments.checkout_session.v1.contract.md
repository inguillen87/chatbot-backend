# payments.checkout_session.v1

## Endpoint

`POST /api/v2/payments/checkout-session`

Tambien disponible como:

- `POST /api/v2/payments/preference`
- `POST /api/v2/tenants/{tenant_slug}/payments/checkout-session`

Requiere autenticacion, rol `admin`, `empleado` o `super_admin`, y un tenant
que el usuario pueda operar.

## Request

Enviar `Idempotency-Key` en cada intento de checkout. Debe ser estable para la
misma confirmacion de carrito y tener hasta 128 caracteres de
`A-Z a-z 0-9 _ . : -`.

Por compatibilidad, `idempotency_key` o `external_reference` en el JSON se usan
como fallback. Los clientes nuevos deben usar el header.

```json
{
  "items": [
    {
      "catalogo_item_id": 42,
      "title": "Plan mensual",
      "quantity": 1,
      "unit_price": 1500,
      "currency_id": "ARS"
    }
  ],
  "total_monetary": 1500,
  "currency": "ARS",
  "contact": {"email": "buyer@example.com"}
}
```

Reglas:

- Debe existir al menos un item monetario.
- Las cantidades deben ser enteras positivas.
- Todos los items monetarios deben usar una sola moneda.
- Si se envia `total_monetary`, debe coincidir con la suma de las lineas.
- Un `catalogo_item_id` debe pertenecer al tenant autenticado.
- El `unit_price` y la moneda de un item de catalogo deben coincidir con el
  precio canonico persistido.
- Reutilizar una clave con otro carrito o monto devuelve `409 idempotency_key_conflict`.

## Persistencia e idempotencia

Antes de llamar a Mercado Pago, el backend confirma un
`PedidoConversacional`, un `MarketOrder` y su evento de reserva. La
`external_reference` enviada al proveedor es el `pedido_id` interno, no una
referencia libre del cliente.

Un replay con la misma clave y el mismo payload no crea otra orden ni otra
preferencia. Devuelve la preferencia ya guardada con `duplicate: true`.

Si una llamada al proveedor tiene resultado incierto, el backend no repite el
POST automaticamente. El primer intento devuelve el error del gateway con los
IDs de orden; un replay devuelve
`409 payment_preference_reconciliation_required`. El cliente debe consultar el
estado y no generar una clave nueva sin una decision explicita del operador.

## Response

```json
{
  "contract_version": "payments.checkout_session.v1",
  "pedido_id": 101,
  "market_order_id": 55,
  "external_reference": "101",
  "client_external_reference": "cart-confirmation-7",
  "preference_id": "123456-pref",
  "init_point": "https://www.mercadopago.com.ar/checkout/v1/redirect?...",
  "idempotency_key": "tenant-a:cart-confirmation-7",
  "duplicate": false,
  "status": "pending_payment"
}
```

El frontend debe persistir `idempotency_key`, `pedido_id`, `market_order_id`,
`external_reference` y `preference_id` hasta obtener estado final por webhook.
No debe interpretar el retorno del navegador como acreditacion.

Consultar estado con cualquiera de estos identificadores:

`GET /api/v2/payments/status?preference_id=...`

`GET /api/v2/payments/status?pedido_id=...`

`GET /api/v2/payments/status?idempotency_key=...`
