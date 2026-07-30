# Survey privacy and retention runbook

Updated: 2026-07-30

## Contract

`surveys.privacy.v1` separates two explicit storage modes:

- `legacy`: preserves the historical survey behavior and fields. It must never
  be advertised as source-anonymous.
- `source_anonymous`: direct identity and request-source fields are discarded
  before the response is flushed. The database also rejects a row in this mode
  if it contains `user_id`, DNI, phone, IP, user agent, precise coordinates,
  UTM identifiers, exact age/year of birth or raw metadata.

Source-anonymous responses may retain answers, channel, age range and coarse
geography such as neighborhood/city for aggregate analysis. Free-text answers
can themselves contain personal data; tenants must design the instrument and
moderation policy accordingly. This mode is privacy-preserving participation,
not a legal certification for secret or regulated elections.

## Publication requirements

A source-anonymous survey requires all of the following before publication:

1. An immutable `privacy_policy_version` for that survey generation.
2. An absolute HTTPS `privacy_policy_url` visible in the public contract.
3. Explicit consent. Submissions send `privacy_consent=true` and the exact
   `privacy_policy_version`; a stale version receives a conflict response.
4. `response_retention_days` between 1 and 3650 (365 by default).
5. Zero personal reward points. A reward ledger would link identity back to the
   response and is therefore rejected in this privacy mode.
6. For any uniqueness policy other than `libre`, a dedicated
   `SURVEY_IDENTITY_HMAC_SECRET_V1` containing at least 32 random bytes.

Privacy settings lock permanently after the first response. Duplicate the
survey into a new draft to publish a new policy or key generation.

## Public small-cell protection

Public live results for `source_anonymous` surveys use
`surveys.public_small_cell.v1`. The default minimum cell size is five and can
be raised with `SURVEY_PUBLIC_MIN_CELL_SIZE` (bounded from 3 to 50).

- If the whole filtered cohort is below the threshold, its exact size,
  version, questions, timeline and map are redacted.
- If any option is below the threshold, the complete question distribution is
  hidden so the missing value cannot be reconstructed by subtraction.
- If any minute bucket or heatmap cell is below the threshold, the complete
  timeline or map is hidden for the same reason.
- AI summaries, map layers and public export actions are removed whenever a
  public surface is suppressed. The small-cell decision runs before the AI
  builders, so exact protected aggregates are not sent to an AI provider and
  then merely hidden from the response. The response exposes the reason and
  threshold, never a fabricated zero.

This protects published aggregates; it does not turn informal participation
into a regulated secret ballot. Administrative raw access and exports remain
separate, capability-gated and audited operations.

## HMAC key handling

The uniqueness token is `hmac-sha256-v1:<digest>` and is bound to tenant,
survey, uniqueness policy and normalized transient identifier. Raw identifiers
are not stored in the response or analytics event.

Do not reuse `SECRET_KEY`, expose this secret to the frontend, or rotate V1 in
place. An in-place rotation changes every future digest and weakens duplicate
prevention. Introduce a new key/version and a new survey generation instead.

## Retention job

Render declares the daily `chatboc-survey-retention` cron:

```text
python -m services.survey_privacy --batch-size 200 --max-batches 20
```

The job deletes an expired response only after all its durable effects are in a
terminal state. It removes details, exactly-once receipt, effect rows and the
individual analytics event in the same transaction. Each tenant receives an
aggregate `survey_privacy_retention_purge` audit record with counts and survey
ids; response ids, answers and fingerprints are excluded.

Use `--dry-run` before enabling the cron in staging. Exit code 2 means the batch
budget was exhausted and more expired rows remain; treat it as an operational
alert, then increase capacity or rerun deliberately.

## Staging proof

Before calling this live:

1. Apply the current Alembic head (which includes
   `20260730_survey_privacy_v1`) and verify the checks/index.
2. Configure the V1 HMAC secret only on the backend web service.
3. Publish a controlled source-anonymous survey and submit DNI/phone/IP/GPS and
   nested metadata; inspect PostgreSQL to prove those fields are absent.
4. Verify a duplicate normalized identifier is rejected without logging it.
5. Advance one test response past retention, run the cron and verify response,
   receipt, outbox and individual analytics evidence are removed.
6. Verify backups, replicas and exported files follow the tenant's documented
   deletion schedule. The application job cannot erase an independent backup.

Current evidence is local tests and migration execution only. No PostgreSQL,
Render cron or production privacy smoke has been performed yet.
