# Neon territorial migration rehearsal — 2026-09-18

## Scope and live execution

Executed the first three pending territorial migrations on a new isolated child
branch, then rolled back every schema/version change inside the same transaction.
This is not a production migration, final data-parity certificate, or approval to
retire Render. The production migration runner and its approval gates are unchanged.

- Project: `nameless-rain-94060889` (`chatboc-postgres`), PostgreSQL 18.
- Parent: `br-floral-unit-acgqawl6`.
- Child: `br-wandering-term-acqwmdro`, `rehearsal-territorial-20260918`.
- Branch snapshot LSN: `0/919AB38`.
- Database: `render_rehearsal_20260829`.
- Compute: fixed 0.25 CU, inherited suspend setting; no plan upgrade.
- Baseline: `20260829_global_writer_authority_v1`, 173 public tables.

## Results actually observed

| Temporary revision | Public tables inside transaction |
| --- | ---: |
| `20260830_territorial_geocoding_v1` | 175 |
| `20260830_geo_review_v1` | 176 |
| `20260830_geo_sync_v1` | 177 |

The remote transaction verified column sets, required constraint names, exact
index names, and index readiness for all four territorial tables. It executed
the actual Alembic-generated PostgreSQL DDL, including foreign keys. Application
permissions, concurrent writes and geocoding-provider behavior were not exercised.

After `ROLLBACK TO SAVEPOINT`, the transaction compared the sorted SHA-256 hashes
of every row in every original public table, entirely inside PostgreSQL:

- **173 tables / 52,877 rows verified unchanged.**
- Aggregate content fingerprint:
  `8ce18f4b725261408dc4571268b5512af137f6591daeefeab814d4a3320e7f3d`.
- Alembic restored to `20260829_global_writer_authority_v1`.
- `schema_committed=false`, `rollback_verified=true`, `cutover_authorized=false`.

Independent reads after transaction completion confirmed both parent and child
still have 173 public tables at that revision and zero territorial tables.
The child is retained as a baseline, not deleted or promoted.

An initial SQL invocation failed before migration DDL because adjacent literals
lost newline-based concatenation during transport. The transaction aborted. The
compiler now emits explicit `||` concatenation, has a regression for this case,
and the second remote invocation passed. No failed attempt is counted as success.

## Reproducibility

`scripts/compile_neon_territorial_rehearsal.py` generates a rollback-only SQL bundle
for one transaction. It accepts explicit project/child/parent/database identity,
rejects the source as destination and non-rehearsal database names, and checks
migration source fingerprints through the existing exact migration plan loader.
It has no network connection, credentials, apply mode or production commit path.
Execution in this rehearsal used the authorized Neon SQL transaction connector.
The normal production runner still requires its separate direct TLS identity,
source snapshot, writer fence, final parity and per-revision contract gates.

The reviewed source hashes were:

- Queue: `06a9cbc03fe602e13a7a51644cb755c4d9d21faab20ecc3ac475a227996fa866`.
- Review: `2eb446115a7c0045a65927862348d48fe6a6f31b83805574e808c7e0f8a499e3`.
- Sync: `36cf3a43b025986bc2e30d16e7d5f6055c42c5699e53fd5b41087a84c593389c`.
- Graph: `da68613ec6b38f757b2d9524c5a4e5377340e4c1689770a1ee46b92225d16b49`.

Twelve local compiler regression tests passed. CI now includes execution of the
same SQL on disposable PostgreSQL 18 with synthetic rows and synthetic Neon GUCs;
that test is not presented as a cloud database or production-identity test.
Remote CI results for the new head are recorded in PR #2777 after execution.

## Vercel candidate and remaining gates

This turn successfully submitted one Preview candidate:
`dpl_DZQtKa2pXc42PQa8zK4aTZ3DebjT`, source
`51a96b299bbc81a80163849ad28e1cab58897906`.
Unlike the previously blocked attempt, Vercel accepted it. At this documentation
checkpoint the API reports **QUEUED**, no build error and no aliases. Runtime
warmup improvement is not yet measured on this candidate. No production domain,
WhatsApp callback, cron ownership or writer authority has been changed.

The live public API still reports backend
`912446bf96f8330664a9dec009ae57dbf935c73c` on Render.
Do not replace it with this migration branch before reconciling all release deltas.

The historical runbook lists three territorial revisions, but the checked code's
reviewed head is `20260906_flask_sessions_v1`: eight further revisions follow the
three tested here. Those eight, the direct-runner rehearsal, Render standby,
application canaries, exact final parity, ingress replay, and committed migration
remain open. The old three-step checklist is not full readiness for today's code.
