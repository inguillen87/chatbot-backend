# Cutover WhatsApp ingress contract

This contract applies only to the independent, buffer-only ingress used during
the Render to Vercel/Neon cutover. It does not change the normal Chatboc
WhatsApp intake or its provider callbacks.

## Security and idempotency boundary

- Twilio `MessageSid` remains the sole deduplication key and the primary key of
  `cutover_whatsapp_ingress`.
- `I-Twilio-Idempotency-Token` is optional audit evidence. It never decides
  whether a message is created, duplicated, replayed or rejected.
- The header is read only after `X-Twilio-Signature` validates and duplicate
  form keys are rejected. An invalid signature cannot create either a buffered
  row or an evidence row.
- The raw token must never enter the encrypted form payload, database, replay
  claim, response body or application log.

## Versioned evidence

Schema revision `cutover_whatsapp_ingress_v2` adds
`cutover_twilio_idempotency_evidence`. Each row contains only:

- `message_sid`, correlating the observation to the canonical provider event;
- `hmac_version = hmac-sha256.v1`;
- a 64-character, domain-separated HMAC-SHA256 digest;
- first/last observation timestamps and a bounded positive observation count.

The composite primary key is `(message_sid, hmac_version, token_hmac)`. The same
token and secret therefore produce the same digest and increment one evidence
row; a different token produces a different evidence row for the same
`MessageSid`. Multiple retry tokens do not create multiple buffered messages.

## Runtime configuration

`CUTOVER_INGRESS_IDEMPOTENCY_TOKEN_HMAC_KEY_B64` is an independent base64 secret
whose decoded value must be at least 32 bytes. It must not equal the Twilio auth
token, stream hash secret, envelope HMAC key or any encryption key.

- Header absent: the setting may be absent and signed ingress remains
  compatible.
- Header present and key absent: ingress returns `503`, emits no ACK and writes
  neither table.
- Header present and invalid: ingress returns `422` and writes neither table.

Provision this secret before directing a real Twilio webhook at the cutover
ingress if that provider path emits the idempotency header. Never place the raw
header value in operational evidence; use `MessageSid`, `hmac_version` and
`token_hmac` only.

## Migration and replay

The migration runner accepts only an exact v1 schema, creates the evidence table
and advances the revision to v2 in one transaction. It does not alter
`cutover_whatsapp_ingress`, its `MessageSid` primary key or FIFO indexes.

Replay decrypts and forwards only the original signed form payload. Token HMAC
evidence remains in the isolated ingress database for reconciliation and is not
sent to the Chatboc intake. Cutover certification must prove:

1. one buffered row and one downstream effect per `MessageSid`;
2. versioned HMAC evidence for each observed provider retry token, with no raw
   token in the artifact;
3. zero buffered/replaying/dead rows after the controlled drain.
