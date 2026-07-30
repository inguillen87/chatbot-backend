# WhatsApp inbound content matrix (local contract v1)

Contract: `whatsapp.inbound_content.v1`

This matrix describes what the backend can prove locally. It does not certify
the Meta/Twilio channel, Twilio media delivery, or any OpenAI provider call.
Every provider form is signature- and sender/tenant-scoped before this contract
may affect a conversation.

| Inbound kind | Local behavior | Understanding claim | External proof still required |
| --- | --- | --- | --- |
| Text | Routed to the tenant's municipal, school or business orchestrator. | Only the orchestrator response may claim an interpretation. | Meta/Twilio inbound and outbound E2E. |
| Emoji-only | Kept distinct from an empty callback and routed as a real message. The post-claim window acknowledges it without creating another claim. | No semantic meaning is hard-coded. | Native-device behavior across emoji variants. |
| Emoji reply/reaction context | Emoji plus `OriginalRepliedMessageSid` is classified as `reaction` and durably stored as text. | The original message is not fetched or guessed. | Twilio/Meta native reaction and reply-context E2E. |
| Image | Tenant-authenticated bounded download, attachment storage and existing image/claim analysis. | Analysis errors are not presented as successful vision. | Real Meta media URL, storage and configured vision provider E2E. |
| Audio / voice note | Tenant-authenticated bounded download and STT. `audio/ogg`, parameterized Ogg/Opus, `application/ogg`, and other `audio/*` variants share the audio path. Failed STT asks for a resend, text description or human; it never sends the filename to the LLM as speech. | A transcript is used only when the STT service returns non-empty text. | Real WhatsApp codecs and configured OpenAI STT E2E. |
| Document | Tenant-authenticated bounded download, attachment storage, existing classifier and optional assisted intake. | A failed/empty extraction remains reviewable and is not called complete. | PDF/Office/vCard samples through the live provider and storage. |
| Video | Tenant-authenticated bounded download and attachment storage; outside an exact open ticket or live human chat, the user receives an actionable limitation message. | No frame/audio understanding is claimed. | A future reviewed video-understanding contract plus provider E2E. |
| Sticker (`image/webp`) | Recognized separately and acknowledged with guidance to send actionable context. | No image/claim meaning is inferred from the sticker. | Real static sticker E2E. |
| Location | Uses Twilio `Latitude`/`Longitude` plus optional address/label and existing tenant flow. | Reverse geocoding failure does not invent an address. | Live location webhook and geocoder E2E. |
| Contact / vCard | Bounded receipt and attachment; it does not overwrite sender identity or create a case. The user is asked what to do and must confirm. | No contact fields are parsed or trusted by this slice. Raw vCard data is not logged by the classifier. | Real vCard webhook/storage E2E and a future explicit consented import flow. |
| Button / list / interactive / Flow | Existing signed interactive path and Flow token/tenant validation. | Only validated payload fields are used. | Approved templates/Flows and live callbacks. |
| Empty unsupported provider message | Treated as a real but unsupported message only when it has a provider message SID; sends an honest resend/human option without invoking the LLM or creating a ticket. | None. | Samples such as unsupported disappearing messages. |
| Delivery status callback | Acknowledged only after signature and tenant sender resolution, before user/session/durable-turn/ticket creation. | Not a conversation. | Live callback endpoint configuration. |
| Voice/call control event | Acknowledged only after signature and tenant sender resolution, before user/session/durable-turn/ticket creation. | Not a WhatsApp text turn and not proof that a call connected. | The dedicated Twilio Voice/Media Streams security and live-call E2E. |
| Unknown control event without message identity/content | Tenant-scoped no-op; never becomes a conversation. | None. | Provider-specific fixture if it should become a supported message type. |

Twilio documents one media attachment per WhatsApp message, the supported
free-form media families (including Ogg/Opus and vCard), and `image/webp` as the
sticker MIME type: <https://help.twilio.com/articles/360017961894-Sending-and-Receiving-Media-with-WhatsApp-Messaging-on-Twilio-Beta->.
Its incoming webhook contract documents text, media, button/interactive/Flow
fields and reply context: <https://www.twilio.com/docs/messaging/guides/webhook-request>.

## Safety and idempotency invariants

- A control event never creates `User`, `ChatSessionContext`,
  `WhatsAppInboundTurn`, or any ticket.
- Event direction changes (`From` vs `To` for a status callback) may be used
  only to resolve an already-configured provider sender; the signature and
  account/tenant scope remain authoritative.
- Media uses the credentials of the tenant account that received it and a
  streaming size bound. A timeout, provider error, oversize body or storage
  failure cannot degrade into an empty LLM turn.
- The provider `MessageSid`/durable turn remains the idempotency boundary. A
  retry cannot produce a second fallback reply or duplicate ticket.
- vCard bodies, URLs and contact fields are not copied into classifier results
  or logs. Durable payload retention/scrubbing remains governed by the existing
  WhatsApp inbound-turn policy.
- Media on an exact open municipal ticket and media in an authorized live human
  chat preserve those routes; the generic fallback cannot create a second
  claim.

## Local regression evidence

The focused suite covers the classification matrix, audio MIME variants,
signature/cross-tenant rejection, status/call no-op behavior, bounded media
failure, duplicate provider IDs, video/sticker/vCard fallbacks, post-claim
follow-up, durable turns and assisted intake. No test performs a real Meta,
Twilio, storage, geocoding, or OpenAI request.
