# Vercel Production predeploy guard

Status: mandatory in the documented Chatboc release process before creating or
redeploying a fenced backend Production candidate. It is not a control imposed
by the Vercel platform: `vercel deploy --prod` executed directly can bypass it.
Reviewers must reject any Production deployment without the archived redacted
guard result.

`scripts/audit_vercel_production_predeploy.py` closes a release gap that the
runtime and database preflights cannot close by themselves: an environment
variable may exist in Vercel but contain an empty, stale or contradictory
value. A deployment can therefore become `READY` without being reproducible or
eligible for the cutover.

The guard is offline and read-only:

- it does not call Vercel, Neon, Redis or any application endpoint;
- it does not connect to a database;
- it does not mutate environment variables, deployments, DNS or data;
- it never emits environment values, DSNs, credentials or their hashes;
- it exits `2` with stable reason codes when any check fails;
- a successful result still says `cutover_authorized=false`.

## Inputs

Run it from the exact clean revision to be deployed. The process must already
contain the Production environment. `vercel env run` is one safe injection
mechanism because it passes values to the child process without writing an
`.env` file or printing them.

The redacted identity evidence is the JSON output of
`scripts/preflight_neon_cutover.py` from the approved target. Its SHA-256 must
come from the independently archived release record. **Do not compute a new
digest from an arbitrary local file and treat that digest as approval.**
Supplying both a file and its freshly calculated digest from the same local
operator is not a technical trust anchor. Until a signed manifest is available,
the approval must come from protected CI or a separately controlled release
record and remains an organizational control.

The supported release entrypoint runs the audit and stops before
`vercel deploy --prod` on any non-zero result:

```powershell
$identityEvidence = 'C:\cutover-evidence\approved-neon-preflight.json'

# Supplied by the approved release record, not derived ad hoc here.
$approvedIdentityDigest = $env:APPROVED_NEON_IDENTITY_EVIDENCE_SHA256

.\scripts\deploy_vercel_production_guarded.ps1 `
  -IdentityEvidence $identityEvidence `
  -ApprovedEvidenceSha256 $approvedIdentityDigest
```

Use `-GuardOnly` to produce the same release check without deploying. The raw
Vercel CLI remains technically available and can evade this process guard; it
is not an approved Chatboc release path.

CI may inject the same variables using its secret manager and invoke the
Python command directly. The JSON output is safe to archive as release
evidence; the environment itself is not.

## Mandatory environment contract

The following variables must be present and non-blank:

- pooled Neon runtime aliases: `DATABASE_URL`,
  `SQLALCHEMY_DATABASE_URI`;
- direct Neon migration aliases: `MIGRATIONS_DATABASE_URL`, `ALEMBIC_DB_URL`;
- declarative Neon identity: `EXPECTED_NEON_PROJECT_ID`,
  `EXPECTED_NEON_BRANCH_ID`;
- immutable source identity: `CHATBOC_DEPLOYMENT_REVISION`;
- release-critical secrets: `SECRET_KEY`, `CRON_SECRET`,
  `TENANT_CLAIM_RECEIPT_SECRET_V1`;
- shared state transports: `RATELIMIT_STORAGE_URI`,
  `SOCKETIO_MESSAGE_QUEUE_URL`.

The guard additionally proves, without opening a connection, that:

- runtime DSNs are pooled Neon TLS endpoints;
- migration DSNs are direct Neon TLS endpoints;
- all four aliases identify the same endpoint family, database, role and
  credential;
- the approved redacted evidence digest matches exactly;
- the evidence carries a timezone-aware UTC capture time no older than 15
  minutes (with at most 60 seconds of future clock skew); a fresh read-only
  preflight is therefore required for every release attempt;
- the evidence is read-only, `ready`, at migration head and reports the same
  project, branch, logical database and direct-host fingerprint;
- `migration.expected_heads` and `migration.current_revisions` in that
  evidence exactly match the single Alembic head read from the current
  checkout, so an old `ready` artifact cannot certify a newer graph;
- the evidence was captured from the same exact Git source revision, reports
  a clean worktree, and contains the same deterministic SHA-256 fingerprint
  of the committed Git tree under `migrations/versions` as the deploy
  checkout. This detects migration content changed under an unchanged Alembic
  revision ID;
- `.vercel/project.json` identifies the canonical `chatboc-backend` project
  and team;
- the worktree is clean and all four source references match: CLI expected
  revision, checkout HEAD, `CHATBOC_DEPLOYMENT_REVISION` and evidence source;
- the candidate remains fenced and all Vercel writer cron flags remain off;
- both shared Redis transports use authenticated `rediss://` endpoints.

Preflight artifacts created before the `source.source_revision`,
`source.worktree_clean` and
`source.migration_versions_fingerprint_sha256` fields existed are deliberately
not compatible: the guard fails closed and a fresh read-only preflight is
required.

## Scope boundary

This is a `fenced-candidate` gate only. It does not replace final Render/Neon
content parity, the writer freeze, global authority transfer, WhatsApp/Twilio
attestation, cron ownership, canaries, rollback or soak. Render must remain
available until those separate gates pass.

The wrapper requests Production confirmation before the audit and rechecks
HEAD plus the complete Git worktree immediately after it, reducing the local
guard-to-upload race. It cannot freeze a Vercel project environment against an
administrator changing it concurrently; protected release permissions and
post-deployment revision/environment attestation remain mandatory before any
alias promotion or writer transfer.
