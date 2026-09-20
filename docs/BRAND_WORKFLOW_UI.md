# Presentation contract for the workspace brand studio

Companion to frontend PR #1754. Base: backend PR #2787 / 8386769a81cd797af83b48e30ec90ea3969aaff5.

The authenticated organization.branding.v1 response now includes workflow_ui,
with contract_version organization.branding_workflow_ui.v1 and 70 plain-text keys.
Publish receipts include the same field. Palette storage, revision, permissions,
Full entitlement, audit transactions and provider behavior are unchanged.

services/organization_branding_ui.py owns the vocabulary. Optional organization
configuration under organization_branding_workflow_copy overrides known keys only.
Values must be nonblank, bounded to 600 characters, free of markup/control characters
and preserve the original template's placeholder set. Invalid values retain server
defaults. Unknown fields cannot grant rights. Results are detached dictionaries.
This change adds no API or screen for users to edit that configuration.

The private copy section is recursively excluded from unauthenticated public config.
No customer setting was changed, no migration or provider call is required.
The frontend uses escaped text and requires a complete validated UI contract;
old clients may ignore this additive field, but the corrected frontend must be paired
with this backend revision. Missing or invalid vocabulary does not enable publication.

Local checks: 22 unittest cases, 21 passed and one existing PostgreSQL row-lock race
skipped on SQLite. Eight of those cases are new UI-contract regressions. Existing
PostgreSQL CI now includes them along with the original transaction suite; its actual
result and the remote candidate are recorded in the PR, not inferred from local tests.
The corrected frontend passed 3183 tests / 409 files and nine real SPA flows using
this backend on disposable SQLite, with original login/sessions/middleware and no API mocks.

No promotion of production or stable QA aliases. Cold-start 503s and institutional
QA acceptance remain independent release gates.

Corrección P2 Unicode: se rechaza la categoría Unicode Cc completa, incluidos C1
U+0085 y U+009B incrustados en texto. Las regresiones se ejecutan en ambos lados;
la API usa unicodedata.category y el cliente una propiedad Unicode de RegExp.
La representación sigue siendo texto escapado; el contrato no cambia.
