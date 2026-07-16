# Meta WhatsApp Flow JSON 7.3 compiler contract

Status: implemented local compiler contract, pinned to Flow JSON `7.3` and
Data API `3.0`.

Last checked against Meta documentation: 2026-07-15.

Official references:

- https://developers.facebook.com/documentation/business-messaging/whatsapp/flows/guides/flowjson
- https://developers.facebook.com/documentation/business-messaging/whatsapp/flows/reference/error-codes#static-validation-errors
- https://developers.facebook.com/documentation/business-messaging/whatsapp/flows/guides/components

## Blueprint versus publishable artifact

`FlowBlueprint` is a conceptual Python source model. It contains a local name,
an endpoint mode, screens, and routing intent. It is not Flow JSON and must not
be uploaded to Meta.

`compile_flow_blueprint()` is the only blueprint-to-asset boundary. It injects
the pinned protocol versions, converts routing tuples to JSON arrays, validates
the full document, serializes canonical UTF-8 JSON, and returns a
`FlowJsonArtifact`.

`FlowJsonArtifact.canonical_json` and `FlowJsonArtifact.as_bytes()` are the
uploadable asset content. `content_sha256` is only a deterministic content
identity; it is not a signature and must not be used as an authentication or
integrity protocol.

The tenant-admin endpoint
`GET /api/admin/whatsapp/flows/<flow_id>/flow-json` serves these exact canonical
bytes for publishable flows. A successful local compile or download does not
imply that Meta accepted or published the artifact; publication readiness is
tracked separately against the exact content hash.

## Validation profile

Compilation fails closed with `FlowJsonValidationError`. Direct validation via
`validate_flow_document()` returns all errors as stable records:

```json
{
  "valid": false,
  "errors": [
    {
      "code": "INVALID_VERSION",
      "path": "$.version",
      "message": "Flow JSON version must be '7.3'."
    }
  ]
}
```

The local profile enforces:

- top-level `version: "7.3"` and at least one screen;
- `data_api_version: "3.0"` and `routing_model` for endpoint-driven flows;
- JSON-only values, no `null`, finite numbers, and canonical size at most 10 MiB;
- unique screen IDs containing ASCII letters and underscores, with `SUCCESS`
  reserved case-insensitively;
- `SingleColumnLayout`, known 7.3 component types, required baseline component
  properties, unique form field names, and at most one Footer per screen;
- at least one successful terminal screen, with a Footer whose action is
  `complete`;
- only `navigate`, `data_exchange`, `complete`, `open_url`, and `update_data`,
  including their action-specific object shapes and placement rules;
- complete payload leaves restricted to direct local or global user form
  references, or an empty payload;
- local and global `${form.*}`, `${data.*}`, and `${screen.*}` references must
  resolve to declared fields;
- endpoint routing covers every screen, has at most 10 outgoing branches per
  screen, contains no self/reverse/cyclic routes, remains connected, and ends
  only in terminal screens;
- deterministic canonical JSON, byte count, error order, and SHA-256 content
  digest.

Meta's upload validator remains authoritative. It can apply account rollout,
component-specific content limits, and platform checks that are not knowable
from a local JSON document. A locally valid artifact is ready to submit; it is
not proof that Meta published it.

## Example builders

`build_claim_tracking_flow()` creates an endpoint-driven lookup flow. The first
screen sends the user-entered claim number and PIN through `data_exchange`. The
terminal screen displays endpoint data and completes with these user-entered
fields:

- `ticket_number` from `${screen.CLAIM_LOOKUP.form.ticket_number}`;
- `follow_up_note` from `${form.follow_up_note}`.

`build_order_checkout_flow()` creates an endpoint-driven delivery and order
review flow. Delivery data is sent through `data_exchange`; the terminal
`complete` payload carries only user-entered form values:

- `full_name` from `${screen.ORDER_DETAILS.form.full_name}`;
- `phone` from `${screen.ORDER_DETAILS.form.phone}`;
- `delivery_address` from `${screen.ORDER_DETAILS.form.delivery_address}`;
- `delivery_notes` from `${screen.ORDER_DETAILS.form.delivery_notes}`;
- `confirm_order` from `${form.confirm_order}` on the terminal
  `ORDER_CONFIRM` screen.

The final payload never accepts order IDs, totals, prices, payment state,
fulfilment state, flow tokens, tenant identifiers, or any other server-owned
state from the client. Those values remain bound to the authenticated
interaction and authoritative backend records.

## Non-goals

This module does not implement or modify:

- Flow endpoint request/response handling;
- encryption, decryption, signing, or key management;
- Graph API upload or publication;
- models, migrations, persistence, readiness, or deployment;
- business validation of claims, stock, pricing, payments, or orders.
