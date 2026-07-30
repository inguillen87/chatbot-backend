# Survey governance release v1

## Scope

`surveys.governance_release.v1` is an opt-in integrity contract for surveys,
consultations and non-certified votes. It records the exact questions, options,
collection rules, privacy configuration, minimized eligibility policy,
versioned consent policy and declarative quorum/tie/challenge rules that were
published.

It does **not** certify a regulated election, determine whether a person is
eligible, adjudicate a challenge, compute a binding winner or prove an external
anchor. Those decisions remain with authorized human reviewers and applicable
legal/organizational processes.

## Lifecycle and API

All mutation endpoints require tenant authorization,
`survey.governance.manage` for scoped employees, and an `Idempotency-Key`.
Tenant admins and authorized superadmins retain the existing capability-policy
compatibility.

- `POST /api/v2/surveys/{survey_id}/releases` creates the immutable draft
  snapshot over a pristine survey draft.
- `POST /api/v2/surveys/{survey_id}/releases/{release_id}/publish` publishes
  that exact snapshot. The legacy publish endpoint fails closed for governed
  surveys.
- `POST /api/v2/surveys/{survey_id}/releases/{release_id}/close` requires an
  opaque human-review reference and creates a deterministic closure manifest.
- `GET /api/v2/surveys/{survey_id}/releases` exposes tenant-scoped release
  receipts to authorized operators. Its response is the UI authority contract:
  it exposes `capabilities.read/manage/create_release`, active/latest release
  identifiers, and per-release `can_publish/can_close`. Missing booleans are
  interpreted fail-closed; each POST remains the final authority.

Public survey contracts expose the active release and the explicit
non-certification flags. A governed response must acknowledge the release hash,
eligibility policy version and consent policy version. The response row and its
idempotency receipt expose the same release binding. A retry with the same key
and exact payload replays the committed response; a changed acknowledgement or
answer conflicts.

Legacy surveys with no release keep `mode: legacy` and remain compatible with
the existing publish/close/response paths.

The public form resets both acknowledgements whenever the active release/hash
changes and never preselects them. It sends the exact release id, snapshot hash
and eligibility/consent policy versions, and treats an ACK whose durable receipt
does not echo that binding as ambiguous. Every newly created governed release
must include the approved `consent_policy.public_text` and the matching
`text_sha256`. The exact normalized text lives inside the immutable snapshot and
is published at `active_release.governance.consent.public_text` before the
unchecked consent control. The browser recomputes SHA-256 with Web Crypto and
keeps participation disabled until the displayed text matches the release hash.
This proves local text/hash consistency; it still does not certify legal
sufficiency for a jurisdiction.

Public consent normalization is `unicode_nfc_lf_trim_v1`, applied in this exact
order: CRLF/CR become LF, Unicode is normalized to NFC, ASCII spaces and LF are
trimmed only at the document edges, C0/DEL controls other than LF and lone
UTF-16 surrogates are rejected, and the result must contain 1..4000 Unicode code
points. `text_sha256` is lowercase SHA-256 over the UTF-8 bytes of that result.
The snapshot states `content_format: plain_text`, `stores_public_text: true` and
`records_participant_input: false`. React renders the value as a text node with
preserved line breaks: no HTML interpretation or automatic linkification.

Persisted legacy releases are not silently rewritten. A release without the
canonical public text is returned with
`completeness.public_consent.complete: false`; draft publication and governed
response intake fail closed. An already-published incomplete release remains
visible for audit and can be closed by an authorized human, but
`accepting_responses` is false and no consent is inferred.

This P0 intentionally supports one release (`version_number: 1`) per survey.
Any questionnaire/policy revision is created by duplicating the survey into a
new draft, preserving the prior survey and its responses. Multi-release
supersession/rollback within the same survey is a follow-up and is not promised
by this contract.

## Integrity and privacy properties

- Canonical JSON and SHA-256 bind instrument and policy content.
- Published/closed releases are immutable in SQLAlchemy and database triggers.
- A partial unique index allows at most one `published` release for a
  tenant/survey.
- Composite tenant/release foreign keys prevent cross-tenant response pinning.
- Close fails if any survey response is legacy or pinned elsewhere.
- Eligibility stores only policy/declaration codes and acknowledgement
  timestamps; it rejects padrones, identity lists and common PII fields.
- Public consent stores only the institution-approved, non-personalized policy
  text. Operator guidance prohibits participant names, DNI, email and other PII.
- Quorum, tie and challenge settings are declarative and always marked for
  human review; no outcome or eligibility decision is calculated.
- Create, publish and close state changes commit atomically with `AuditEvent`.

## Evidence boundary

Locally covered: SQLite migration upgrade/downgrade, DB triggers, one-active
release constraint, composite tenant FK, HTTP lifecycle, idempotent replay and
conflict, tenant isolation, response pinning through public aliases, unpinned
close rejection, immutable ORM updates, audit rollback, public-text
normalization/bounds/hash mismatch, legacy incomplete blocking, and browser
Web-Crypto verification before acknowledgement.

Still pending before a production claim: apply/verify the migration on the
staging PostgreSQL database; perform authenticated staging HTTP smoke tests;
design and test explicit multi-release supersession/rollback if the product
later needs it instead of the current duplicate-as-new-draft rule;
review jurisdiction-specific election/consultation requirements; connect and
independently verify any external anchoring/signature service; define the human
review/challenge operating procedure and retention policy. Until those pass,
the UI/API must continue to report `regulated_election_certified: false`,
`result_certified: false`, and external verification as not performed.
