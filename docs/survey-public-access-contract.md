# Public participation and administrative availability

`surveys.public_access.v1` in the administrative survey list describes current
public participation through the existing `get_public_encuesta` guard. It checks
effective public visibility, published state, the active participation window,
and an available public slug. It does not introduce a new authorization policy.

In `observe` mode, a legacy published government survey can remain publicly
available while its institutional evidence is unverified. The contract therefore
reports `allowed: true` when that existing guard allows access. The separate
`government_survey_evidence_gate` still controls new publication. Under effective
`enforce_visibility`, an unverified instrument remains unavailable and public
resolution returns 404. No rollout modes, allowlists, roles, or grants are changed.

Administrative `can_share` and `accepts_responses` also retain the existing veto
for a persisted jurisdiction conflict. They describe permitted administrative
operations and can therefore be false even when the legacy public guard allows
access in observe mode. Clients must respect both contracts.

`public_access` concerns active participation. Closed instruments can still expose
their exact public slug or durable public link through explicitly read-only routes
using `allow_closed_for_read`. Published final results remain readable; hidden
results remain unavailable on public routes even to an authenticated owner.
Closed participation does not authorize responses, comments, or reports.

Comment mode `anon` discards session and supplied identity. Social comments require
a valid server-signed profile token. Providers appear in the public contract only
when a configured HTTPS flow and message origin are usable; local signed-token and
URL fixtures do not certify an external OAuth integration.

Regression evidence uses disposable local SQLite fixtures and the application's
durable session issuer. It does not certify production publication or provider
authentication.
