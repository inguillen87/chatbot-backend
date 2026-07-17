# Meta WhatsApp Flow management runtime

Status: implemented backend lifecycle with dry-run, durable operation state,
remote publication verification, and Twilio wrapper handoff.

Last checked against Meta documentation: 2026-07-16.

Official references:

- https://developers.facebook.com/documentation/business-messaging/whatsapp/flows/guides/flowjson
- https://www.postman.com/meta/whatsapp-business-platform/request/slhc240/create-flow
- https://www.postman.com/meta/whatsapp-business-platform/request/ar2ufbg/update-flow-json
- https://www.postman.com/meta/whatsapp-business-platform/request/wcidrlg/publish-flow
- https://www.postman.com/meta/whatsapp-business-platform/request/9cwjfve/get-flow
- https://www.postman.com/meta/whatsapp-business-platform/request/m1exgnx/list-assets-get-flow-json-url

## Responsibility boundaries

1. `services/meta_flow_json.py` compiles and validates canonical Flow JSON.
2. `services/meta_flow_data_exchange.py` handles encrypted Data API requests.
3. `services/meta_flow_management.py` talks to Meta Graph API.
4. `routes/whatsapp_rules.py` enforces tenant, role, plan, confirmation,
   persistence, audit, and Twilio handoff.

The Twilio `whatsapp/flows` Content object is only a message wrapper that
references a published Meta Flow ID. It does not upload `flow.json` to Meta.

## Admin endpoints

### Inspect the exact artifact

`GET /api/admin/whatsapp/flows/<flow_id>/flow-json`

The response is canonical JSON with `private, no-store`, an attachment
filename, protocol-version headers, and the SHA-256 content identity.

### Publish or verify in Meta

`POST /api/admin/whatsapp/flows/meta/sync`

Dry-run is the default. It checks:

- authenticated tenant administrator;
- Full-plan integration access;
- a numeric WABA bound to the tenant sender;
- a scoped System User access token;
- a locally valid canonical artifact and content hash;
- Data Exchange readiness and endpoint URI for endpoint-driven Flows;
- conflicts with any persisted Meta Flow, WABA, or artifact identity;
- absence of an unresolved previous publication.

The dry-run returns a short-lived signed execution confirmation. The execute
request then:

1. claims a durable publication operation in `MessageTemplateRegistry`;
2. creates a new draft or reuses the exact persisted Meta Flow ID;
3. confirms WABA ownership before changing a draft;
4. blocks a mismatched Data Exchange URI before irreversible publication;
5. uploads the exact canonical `flow.json`;
6. checks Meta validation errors;
7. publishes the Flow;
8. reloads metadata, downloads the remote FLOW_JSON asset, and compares hashes;
9. persists the verified Meta ID, WABA, hash, status, timestamps, and audit;
10. returns a short-lived publication attestation for the Twilio wrapper.

Published Flows are immutable in Meta. A changed artifact requires a new Flow
revision and a newly approved wrapper.

### Create the Twilio wrapper

`POST /api/admin/whatsapp/flows/twilio-content/sync`

The endpoint accepts the Meta publication attestation, or reuses a previously
persisted verification only when Meta Flow ID, WABA, and artifact hash all
match. Missing proof remains fail-closed and is never presented as a verified
publication.

## Configuration

Global fallback:

```dotenv
META_GRAPH_ACCESS_TOKEN=
META_GRAPH_API_VERSION=v23.0
META_GRAPH_API_BASE_URL=https://graph.facebook.com
META_GRAPH_API_TIMEOUT_SECONDS=20
```

Preferred per-WABA token:

```dotenv
META_FLOW_WABA_<WABA_NORMALIZED>_ACCESS_TOKEN=
```

The token is server-only. API responses, logs, audit payloads, dataclass
representations, and frontend contracts expose readiness and token source but
never the token value.

## Failure and replay behavior

- Execution confirmations are bound to actor, tenant, WABA, Meta Flow ID,
  endpoint, artifact hash, and operation.
- A successful publication is persisted before browser refresh or wrapper
  creation.
- Replaying a pre-creation confirmation after Meta assigned an ID fails because
  the durable identity changed.
- Concurrent executions contend on one unique registry identity and one
  publication state.
- Known failures persist the candidate Meta Flow ID for safe verification.
- An uncertain create response is marked for reconciliation instead of blindly
  creating another Flow.
- Signed asset downloads validate every redirect hop against Meta-owned HTTPS
  hosts before sending a request.

## Production validation

For each real tenant and WABA:

1. verify the System User token scope and WABA ownership;
2. verify the Data Exchange public key and endpoint binding;
3. run dry-run and inspect all blockers;
4. publish one non-production revision and compare the remote hash;
5. create and approve its Twilio wrapper;
6. send a test Flow to an allowlisted number;
7. complete PING, INIT, BACK, data_exchange, completion, and error paths;
8. confirm durable audit, interaction, and completion records;
9. rotate any credential exposed outside the secret manager.
