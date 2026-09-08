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

## Publication completion — 2026-09-08 22:26 UTC

- Published backend: `912446bf96f8330664a9dec009ae57dbf935c73c`.
- Render: `dep-dag8ijpt0dsc73ecdfug`, service `srv-d0rq2rp5pdvs738t3bhg`; Live 22:23:00 UTC. Public version/health confirmed exact SHA and HTTP 200 at 22:23:26 and 22:26:13 UTC.
- Frontend: `3c430c2d6c7a470745a42f7e489b2e552726ef54`, Vercel `dpl_Go8eNRudNY1uT23kvWroRqgXuxKp`, promoted to both public domains. Final frontend regression: 377 files / 2781 tests passed; typecheck/build and three focused browser tests passed.
- Broad backend regression: 377 tests +47 subtests passed, three known environment-fixture cases deselected. Final partial-batch rollback delta: 181 tests +9 subtests passed, one known Twilio manifest fixture deselected. Local PostgreSQL locking gate: 18 tests passed on the combined implementation.
- During the handover, public health/version returned 502 at 22:22:04 and 22:22:54 UTC. Previous instance `shpc6` logged `RuntimeError: do not call blocking functions from the mainloop` at 22:20:40; replacement `srjrh` listened at 22:22:58 and passed health at 22:23:00. Full lifecycle root cause is not established. This release is not certified as zero-downtime.
- Unchanged migration preparation finished 22:20:33. The migration tree remains `a917d4862b0baa1c78798876d63c318c86f81b76`; no DB/schema/provider/worker/billing settings were changed.
- Authenticated Junin queue, details, timeline, message history, workflow metadata and employee-routing returned HTTP 200. Actual tree-claim category offered employee438, while luminarias correctly had no compatible employee because its configured scope contains only tree/calle categories. No claim, assignment, status, permission or outbound-message mutation was used for this production verification.
- Browser /api/accessibility/me remains HTTP404: server persistence of accessibility preferences is a known separate gap. The installed browser also required a document cache refresh to leave the older frontend; automatic updating of all already-open clients is not certified.
- Rollback versions above remain available. Render retirement and Neon cutover are explicitly NOT complete.
