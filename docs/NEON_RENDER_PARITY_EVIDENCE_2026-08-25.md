# Render SQLite to Neon parity evidence - pre-cutover

Date: 2026-08-25 (America/Argentina/Buenos_Aires)

Status: **pre-cutover evidence only**. Render writes were not fenced for this
snapshot, so the checks must be repeated during the maintenance window against
the final signed inventory before Render can be retired.

## Scope and safety

- Source: Render service `srv-d0rq2rp5pdvs738t3bhg`, persistent `/data/database.db`.
- Destination: Neon project `nameless-rain-94060889`, database `neondb`.
- SQLite was opened with `mode=ro&immutable=1` and `PRAGMA query_only=ON`.
- Neon checks were read-only `SELECT` statements.
- No raw rows, paths, credentials, session identifiers, or PII are recorded here.

## Table coverage

All 29 shared legacy tables exist in Neon. For every table, the Neon row count
is greater than or equal to the Render SQLite row count. Exact-count tables
include `llm_interaction_log` (261), `qa` (257), `sugerencia` (130), and every
legacy-empty table. Tables with more rows in Neon reflect post-migration
activity and newer product surfaces.

## Primary-key inclusion

- Twelve non-empty numeric primary-key sets were compared after filtering Neon
  to each signed legacy range: 2,076/2,076 legacy keys matched, with identical
  salted set fingerprints.
- `chat_session_context`: 306/306 legacy session keys were found inside the
  1,033-row Neon table; missing count was zero and the aggregate fingerprint
  matched.
- Empty legacy tables were checked separately and do not create a migration
  deficit.

## Critical content inclusion

Salted cell fingerprints were compared for 17,406 legacy cells across stable,
common columns in these tables:

- `analisis_archivo`: 53 rows / 371 cells - exact.
- `archivo_adjunto`: 133 / 1,463 - exact.
- `chat_session_context`: 306 / 918 - exact.
- `conversacion`: 406 / 3,248 - exact.
- `llm_interaction_log`: 261 / 1,044 - exact.
- `qa`: 257 / 1,799 - exact.
- `sessions`: 402 / 804 - exact.
- `ticket_comentario`: 155 / 1,550 - exact.
- `whatsapp_numero`: 2 / 8 - exact.
- `municipio_ticket`: every checked cell matched except `estado` on 2/194
  records; all 194 records remain present. This is consistent with mutable
  post-migration workflow state and is not a missing-row signal.
- `user`: all 31 legacy records remain present. Immutable/profile cells match;
  14 mutable authentication, role, plan, ownership, or usage columns have
  post-migration changes on a subset of rows. No legacy primary key is missing,
  so these values must not be overwritten from the stale SQLite snapshot.

The legacy-only `municipio_ticket.archivo_url` column is absent in Neon, but it
contains zero non-null values in all 194 source records; no payload was lost by
that schema difference.

## Method limitation

Salted MD5 was used only as a deterministic accidental-change fingerprint
available in both stock SQLite/Python and Neon without modifying the database.
It is not used as an authentication primitive. Artifact authenticity and path
integrity remain governed by the separately HMAC-SHA256-signed storage
manifest. Final cutover requires a fresh signed snapshot, the same inclusion
checks, a writer fence, application E2E, and a tested rollback.

