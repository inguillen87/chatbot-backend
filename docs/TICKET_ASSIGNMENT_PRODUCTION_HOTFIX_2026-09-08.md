# Ticket assignment: production-baseline candidate

Status: reviewed candidate saved on its dedicated review branch, not merged or
deployed. No provider call, production write, schema migration, infrastructure
change, or data repair was performed. Commit/push of these 21 files was separately
authorized after review; this is not release authorization.

- Base: `b68021923b99f0fb0971ed7657a476c2439b4695` (verified Production baseline).
- Worktree: `C:\cbs35-ticket-assignment-hotfix`.
- Branch: `codex/ticket-assignment-production-hotfix-20260908`.
- Integration reference only: `e4bd9407564ef5f3996963b03aaca440e652b4c3`.
- Preserve the Production authentication/security fixes. No broad integration merge.

## Cause and coherent dependency boundary

Production `/api/tickets` publishes the backing row ID but not `source_model`. The
current frontend therefore correctly refuses assignment for a record such as the
reported municipal source ID 409. Adding identity alone would expose a Production
writer with no self-claim action, no supervised compare-and-set, and alternate
assignment paths that could overwrite ownership.

The integration identity fix is `aa26d8c9c7c57230a6412f7830c0108d5530af99`.
The narrow backport uses assignment policy/atomic-claim ideas from `535e629c8`,
`58c70ec92`, `9a867f682`, and `1559c53f4`, but is implemented directly against the
Production baseline; those commits were not broadly cherry-picked.

| Boundary | Files and reason |
| --- | --- |
| Canonical identity and atomic policy | `routes/ticket.py`, `routes/v2/saas.py`, new `services/ticket_assignment_policy.py`: publish table + exact ID, reject contradictory aliases, lock/refresh current owner, employee self-claim, supervised CAS and replay without duplicate audit. Explicit GET source never falls back to another table. |
| Alternate owner writers | `services/ticket_service.py`, `services/v2/ticket_service.py`, `routes/v2/tickets.py`, `routes/admin_tenant.py`, `routes/education_routes.py`: legacy assignment, generic create/PATCH, both automatic routing surfaces and education enforce their authority plus expected owner. Education retains its existing domain permission gate. |
| Tenant/category scope | `services/employee_ticket_access.py`, `services/employee_routing.py` and legacy ticket service: operational destinations require `es_empleado`; category checks repeat after refresh where relevant. Explicit foreign tenant can never be admitted through the same business sector or stale owner link. |
| Grant and public input prerequisites | `routes/admin_tenant.py` restricts the five employee/role/category/scope grant writers to admins and verifies target tenant. `services/tenant_claim_receipts.py` rejects server-owned assignment/handoff/audit keys recursively in public extras. |
| Ownership-preserving JSON writers | `services/omnichannel_service.py`, `services/meta_flow_runtime.py`, `services/v2/sla_service.py`, ticket services and admin lead writes refresh under row lock before copying JSON. Ticket lists calculate SLA without ORM writes and keep due/status labels coherent. |

No model, migration, dependency, environment, `render.yaml`, `vercel.json`, or
`scripts/apply_migrations.py` change belongs to this candidate. It does not include
the separate territorial map patch or integration services/infrastructure.

## Client contract and deliberate fail-closed changes

- Inbox claim/assign require a canonical `source_model` plus exact positive row ID.
  Allowed legacy model aliases must agree; numeric booleans/floats and contradictory
  `id`/`ticket_id`/`legacy_id` values are rejected. Display numbers are not row IDs.
- `claim` derives the destination from the authenticated operational employee.
  Tenant/category restrictions apply; another owner yields 409. Same-owner retry
  leaves the local audit unchanged.
- `assign` requires admin/superadmin/supervisor or explicit `tickets.assign`
  (`tickets_assign` remains an alias), plus an operational destination and explicit
  `expected_assignee_id` (`null` means the client saw it unassigned). A changed owner
  yields 409; retry to the already-current target is safe.
- All legacy supervised assignment and applied auto-routing clients must send the
  expected owner. Bulk apply additionally needs explicit unique table + row targets.
  Dry-run recommendations stay read-only. Legacy nested assignee objects are no
  longer accepted: use an exact integer or ASCII decimal ID.
- Generic v2 create/PATCH cannot let an ordinary employee/customer assign or
  unassign another person. Use inbox claim for employee self-service.
- `accept_handoff` may claim an unassigned case or accept one already owned by the
  same employee. It cannot steal ownership. A-to-B transfer requires supervised CAS
  assignment before B accepts. Full owner-only lifecycle policy is not in this patch.
- Pyme tickets without explicit tenant identity fail closed; no guessed business
  sector repair is performed. Municipal legacy NULL-tenant employee fallback remains
  only where the owner resolves uniquely, never over an explicit foreign tenant.
- This change does not publish legacy `next_states` or implement the separate state
  transition selector. It does not certify all close/reply/priority/lead permissions.

## Local verification

Use the existing runtime from the integration checkout (no new install):

```powershell
$env:TESTING='1'
$env:FLASK_SKIP_GLOBAL_APP='1'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DATABASE_URL='sqlite:///:memory:'
$env:SQLALCHEMY_DATABASE_URI='sqlite:///:memory:'
$env:CHATBOC_ALLOW_EXTERNAL_NETWORK_TESTS='0'
& C:\cbs34-platform-integration-backend\.codex-venv\Scripts\python.exe -m pytest tests/test_ticket_assignment_hotfix.py tests/test_v2_tickets.py tests/test_employee_scope_and_assignment.py tests/test_tenant_claim_receipts.py tests/test_education_routes.py tests/test_education_rbac.py tests/test_v2_saas_contracts.py tests/test_ticket_endpoints.py -q --disable-warnings --tb=short -k 'not test_send_ticket_history_email and not test_send_ticket_history_handles_missing_dates and not whatsapp_experience_contract_connects_channel_content_tracking_and_admin_panel'
```

`tests/conftest.py` blocks external network by default; all DB fixtures use isolated
SQLite. The new regressions cover table-ID collisions, all three backing models,
self-claim/replay/conflict, supervisor and explicit capability, forbidden grants,
public recursive metadata injection, legacy/bulk/generic assignment, post-lock
category changes, stale JSON in an independent ORM identity map, and read-only SLA
labels. Final expanded run: **214 passed, 26 subtests passed, 3 deselected**
in 104.59 seconds. An additional offline run of `test_omnichannel_service.py`,
`test_meta_flow_runtime.py`, `test_meta_flow_claim_evidence.py`,
`test_meta_flow_completion.py`, and `test_ticket_public_chat_reply.py` passed
**59 tests** in 16.17 seconds. Total across these disjoint suites: **273 passed**.
AST syntax checks passed for all 20 changed/new Python files; `git diff --check`
passed. These are local tests, not Production or PostgreSQL concurrency evidence.

Excluded from the final command: two history-email success tests require SMTP
configuration their sender mocks do not supply (503 in this isolated environment),
and one WhatsApp-experience test expects a local Twilio manifest unavailable in this
worktree. The SMTP-missing failure-contract test remains included. No production
configuration was supplied to make these unrelated tests pass.

Independent review by `review_bootstrap` identified stale JSON, role-read guards,
tenant fallback and post-lock category/SLA consistency issues. These were corrected
and regressions added. The final static review reported no remaining blocking
finding inside this diff; reviewer also ran 18 pure policy checks and diff hygiene.

## Release gates (not completed by local tests)

- SQLite does not implement PostgreSQL `SELECT FOR UPDATE` semantics. The stale
  identity-map test demonstrates refresh preservation, not competing transaction
  scheduling. Real PostgreSQL two-session claim/CAS/comment interleaving remains a
  separate pre-release verification gate against a disposable local test database.
- Local runtime inventory on 2026-09-08 found no `postgres`, `pg_ctl`, `initdb`,
  `psql` or Docker on PATH, no matching service, no PostgreSQL installation/installed
  program registry entry, and no binaries in Program Files (64/32 bit) or the user
  Local Programs directory. Conventional portable/Scoop/Chocolatey locations did
  not supply a candidate. WSL 2.7.13.0 is installed, but its distribution inventory
  is empty. No service/distribution was started and no database was accessed.
  **The PostgreSQL concurrency gate remains pending: a disposable local instance
  must be explicitly provisioned before that evidence can be collected.**
- Parent verified Render still serves baseline `b68021923`: service
  `chatbot-backend` / `srv-d0rq2rp5pdvs738t3bhg`, branch `main`. Its existing predeploy
  runs `scripts/apply_migrations.py` against `DATABASE_URL`. Do not deploy the full
  integration tree or change that command as an implicit part of this patch.
- Review the exact candidate diff, retain the baseline migration graph, then obtain
  release direction. No database cutover, production traffic test, outbound
  notification test, or end-to-end Production assignment has been performed here.
- The parent is releasing territorial membership separately in
  `3bc0d397e18435b8fc7fb7bed9f668c68c568e06`. Any later integration of this assignment
  branch must preserve that territorial commit; never replace the deployed map
  work by releasing this older-base branch directly.
