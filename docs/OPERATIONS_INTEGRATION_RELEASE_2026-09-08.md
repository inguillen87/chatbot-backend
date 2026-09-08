# Production operations integration — 2026-09-08

## Exact integration boundary

This worktree integrates the already deployed territorial commit
`3bc0d397e18435b8fc7fb7bed9f668c68c568e06` with the assignment patch
`6310e0f90` and PostgreSQL regression tests `0494aa672`. It does not replace the
territorial release with the older assignment-only base.

No migration/model/dependency/application bootstrap/Render configuration changes
are included relative to the deployed territorial baseline. The same database,
workers and WhatsApp configuration remain in use. No citizen message, production
claim/reassignment or database cutover is part of the verification below.

## Combined checks completed before release

- 18 focused backend suites: **334 passed, 47 subtests passed, 3 deselected**.
  The three exclusions are two SMTP-success tests lacking the local SMTP fixture
  and a WhatsApp-experience test depending on an absent local Twilio manifest.
  Missing-SMTP error handling remains covered. No production secrets were used.
- Combined commit `0494aa67281c40a43601ac8c88b16def029f0a08`: **18 real PostgreSQL
  tests passed** against the disposable loopback-only instance. Separate backend
  PIDs and `pg_blocking_pids()` establish actual contention, not sequential mocks.
- PostgreSQL test instance stopped again at 18:48 -03. Zero owned processes,
  listeners, test schemas or public tables remained. Evidence:
  `C:\cbs35-postgres-assignment-qa\assignment-postgres-combined-0494aa672-results-20260908.xml`.
- Frontend principal ticket assignment/claim contract: 53 focused mocked tests
  passed. These do not certify external delivery or real Production mutations.

## Compatibility review follow-ups

The review found two active secondary consumers requiring adaptation before
release: the employee matrix apply action and the superadmin lead auto-assignment
action. Both must submit explicit canonical identity and observed ownership.
Absent ownership evidence is not interpreted as `null`.

The matrix now submits `expected_suggested_assignee_id`. The server compares the
entire reviewed selection and suggested employees before invoking an assignment
writer; a changed suggestion, missing case or truncated set returns 409. Legacy
clients without the optional field retain their existing contract; the new UI
always supplies it when applying a reviewed preview.

The additional preview-contract tests plus assignment hotfix tests passed **100
tests**; three historic employee-routing tests also passed. Invalid limits return
400 rather than 500. This is separate from the previously recorded PostgreSQL
contention gate (the new guard does not alter those locking primitives).

## Known limitations not claimed complete

- Employees delegated `tickets.assign` still receive a self-only routing candidate
  list. This is a conservative UI limitation; widening it requires scoped minimal
  candidate references without disclosing other categories or employee workloads.
- Pyme assignment is not certified end to end by the current frontend controls.
- Production Junín geography has zero eligible ticket points: 19 stored coordinate
  pairs outside the official department, 31 pending geocoding and 3 missing a
  location among 53 records. Do not move or invent coordinates to fill the map.
- Render/Neon cutover, workers/crons parity, provider webhooks, canary and rollback
  drills remain distinct migration gates. Render is not shut down.

## Rollback and publish evidence

Backend predecessor: `3bc0d397e18435b8fc7fb7bed9f668c68c568e06`, Render deployment
`dep-dag6uv740ujc738c0l00` (service `srv-d0rq2rp5pdvs738t3bhg`).
Frontend predecessor: `82fbc153eddec3158fe3c799c96838842acc6b12`, Vercel deployment
`dpl_CqDZhUQEkn1n1uoE5L9gkD1C2mYJ`.

At creation of this report the combined candidate is **not yet deployed**.
