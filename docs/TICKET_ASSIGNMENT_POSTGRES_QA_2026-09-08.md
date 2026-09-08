# Assignment: disposable PostgreSQL concurrency evidence

Validated the assignment candidate `a5046ca3902ca403e4fae6448750eed0e22990c9`
on 2026-09-08 using **real PostgreSQL 17.11, READ COMMITTED**, not SQLite or a
mock lock. No production application code changed during this verification.
This closes the candidate's local PostgreSQL contention gate; it is not a
Production release, migrated-schema validation, provider test, or end-to-end
Production assignment certification.

## Result and scope

`tests/test_ticket_assignment_postgres.py`: **18 passed** in 5.62 seconds on the
final run, repeating an earlier expanded **18 passed** run in 5.20 seconds.
Default execution without the explicit local PostgreSQL URL: **18 skipped**.
The 88 final warnings were existing `Query.get()` legacy-API warnings (87) and
`datetime.utcnow()` deprecation (1), not failed concurrency assertions.

| Real competing operations | Cases | Verified outcome |
| --- | ---: | --- |
| Different/same employee claims, all three backing models | 6 | Winner 200; competing employee 409; same-employee replay 200; exactly one local assignment audit |
| Different/same supervisor assignment targets, all three models | 6 | Stale expected owner cannot overwrite; same-target replay does not duplicate audit; subsequent transfer with current expected owner succeeds |
| Stale JSON comment vs assignment/category writer | 2 | Comment retains committed owner and assignment audit; changed category denies the stale comment without adding history |
| Category change while inbox claim waits, all three models | 3 | Refreshed category denies actor with 404 and no assignment/audit |
| Category change while legacy assignment waits | 1 | Actor category is rechecked after lock, even when destination can handle the new category |

Each test holds the first transaction immediately after its **actual SELECT FOR
UPDATE**. A second independent request/ORM session attempts the same row. Before
the first transaction is allowed to continue, a third read-only observer verifies
that `pg_blocking_pids(second_pid)` contains `first_pid`, that the PIDs differ, and
that the second operation has not completed. This proves actual server lock
contention, not merely sequential requests. Assertions then inspect committed
ownership, category and local audit history.

Inbox and legacy assignment checks use the real Flask application, authenticated
synthetic users and real HTTP handlers. Stale-JSON tests call the production
`add_comment` service from a separate request context retaining an independently
loaded stale ORM object. Lock/policy functions, assignment writes and local audit
writes are not mocked. Artificial category edits are synthetic competing writers.

## Isolated runtime and artifact provenance

- Official discovery chain: [PostgreSQL Windows downloads](https://www.postgresql.org/download/windows/)
  links its binary ZIP option to [EDB binary archives](https://www.enterprisedb.com/download-postgresql-binaries).
- EDB's Windows x86-64 17.11 link, `https://sbp.enterprisedb.com/getfile.jsp?fileid=1260491`,
  returned HTTP 302 to
  `https://get.enterprisedb.com/postgresql/postgresql-17.11-3-windows-x64-binaries.zip`;
  that URL returned HTTP 200 and Content-Length **341325378**.
- The existing complete local archive was reused; no duplicate download.
  ZIP integrity/CRC validation (`python -m zipfile -t`) passed.
- Local archive SHA-256:
  `4b8db0930c38f6ef845db919551dedda3b6b845aeb0927b3d79a6e8e9e4537cf`.
  This is a locally computed identity/integrity record, **not** a comparison to a
  separately published vendor digest or signature.
- Extracted only `pgsql/bin`, `pgsql/lib`, `pgsql/share` (~141 MB uncompressed),
  excluding pgAdmin, StackBuilder, docs and installer execution.
- Cluster: `C:\cbs35-postgres-assignment-qa\data-20260908`.
- Database/user: `assignment_qa_20260908` / `assignment_qa` (synthetic local QA).
- Listener: **127.0.0.1:55439 only**, verified by PostgreSQL server address/port
  and Windows listener inventory. No Windows service or system configuration.
- Runtime options: `max_connections=12`, `shared_buffers=32MB`.
  Test connections bound lock/statement timeouts to 10/15 seconds.
- Tests create and remove only their own randomly named `assignment_qa_<uuid>`
  schema. They do not run the migration graph or use a production schema/data.
- External-network pytest guard remains enabled. The launcher explicitly disables
  `dotenv.load_dotenv` before application import; no environment files or remote
  database credentials were read. Default application DB variables point to
  in-memory SQLite; only the explicit QA fixture opens local PostgreSQL.

Local machine-readable evidence:
`C:\cbs35-postgres-assignment-qa\assignment-postgres-results-20260908.xml`.
Server log: `C:\cbs35-postgres-assignment-qa\postgres-20260908.log`.

After validation, PostgreSQL reported zero remaining QA schemas and zero public
tables. The owned cluster was gracefully stopped at **2026-09-08 18:40:26 -03**;
the server log confirms shutdown, and no owned PostgreSQL process or listener on
port 55439 remained. The archive, portable binaries, stopped empty QA cluster and
local evidence files are retained in the bounded QA artifact directory.

## Reproduction after explicitly starting this disposable cluster

```powershell
$env:TESTING='1'
$env:FLASK_SKIP_GLOBAL_APP='1'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DATABASE_URL='sqlite:///:memory:'
$env:SQLALCHEMY_DATABASE_URI='sqlite:///:memory:'
$env:CHATBOC_ALLOW_EXTERNAL_NETWORK_TESTS='0'
$env:CHATBOC_ASSIGNMENT_POSTGRES_URL='postgresql+psycopg2://assignment_qa@127.0.0.1:55439/assignment_qa_20260908'
& C:\cbs34-platform-integration-backend\.codex-venv\Scripts\python.exe -c "import dotenv; dotenv.load_dotenv=lambda *args, **kwargs: False; import pytest; raise SystemExit(pytest.main(['tests/test_ticket_assignment_postgres.py','-q','--tb=short']))"
```

The fixture rejects non-loopback hosts, port 5432, non-PostgreSQL drivers,
non-QA database names and DSN query/service overrides. Without the explicit URL it
skips, so normal offline runs do not provision or connect to PostgreSQL.

## Remaining release boundary

The assignment branch is still based on `b68021923`, not the separately released
territorial patch `3bc0d397`. Preserve that territorial work when integrating this
candidate. This test suite does not certify every alternate ownership/JSON writer,
all role mutations, production migrations, outbound notifications, frontend
assignment, or sustained load. Deployment and controlled post-deploy verification
remain separately authorized gates.
