# Survey rates in analytics summaries

`get_survey_summary` describes the latest published survey for the authorized
tenant. `total_votes` remains the count of real response records. Synthetic demo
and legacy unverified records remain excluded and reported in response provenance.
It is not a verified count of unique eligible people.

`participation_rate` is `null`. Recent analytics users do not establish the
survey's eligible population, and response records do not establish a matching
unique-person numerator. `participation_rate_metadata` uses
`analytics.survey_participation.v1` to explain the intended basis, unavailable
numerator and denominator, observed real response records, and survey scope.
When no survey is published, the reason is `no_active_survey` and the observed
response count is unavailable. A published survey with no real responses retains
the measured count `0` while its rate stays unavailable.

The v2 overview also returns `survey_completion_rate: null`. Completion requires
compatible counts of completed and started survey sessions. Participation is a
different metric and cannot be reused as completion. The accompanying
`analytics.survey_completion.v1` metadata describes this missing evidence.

Clients should show an unavailable rate as “No disponible”, keeping it separate
from a measured zero. No rate is divided by a fallback denominator or capped at
100 percent. This change does not activate survey methodology, certify a voting
result, change eligibility grants, or modify database records.

Local regression coverage uses disposable SQLite fixtures, including an empty
survey, real responses without an eligible population, recent activity that is
not a survey population, and simulated responses excluded from real counts.
