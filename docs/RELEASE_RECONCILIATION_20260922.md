# Coordinated release reconciliation ? 2026-09-22

## Source continuity

The release branch in both repositories is `release/chatboc-production-20260922`.
Backend PR #2796 continues the existing candidate; frontend PR #1762 is its paired review.
No previous Work/Codex worktree was overwritten. New detached worktrees were used.
Desktop Commander and the authenticated Vercel CLI were verified operational again.
An old connector-scope failure does not describe the current CLI access.

## Reconciled changes

Backend main `efbf809db12dbdb123a6eeb3d40ae0a2f1af5e55` is merged, not copied over the release.
Only the two conflicting JWKS functions were selected from main; AST comparison
verified preservation of unrelated authentication functions and route decorators.
Keep September's expanded HMAC/bootstrap matrix and expect the stronger no-store
cache contract. Preserve main's public-key allowlist and scoped payment invalidations.
The temporary write-enabled release-security-object workflow was removed after use.

Frontend `344528986bf280d2de8b4bbd5b22f0accba6e539` includes both the accepted SaaS
candidate and production profile hotfix `9d85b481b026484dec0042b0b4193aac8c734e31`.
Tenant-owned branding/sections and canSave remain, alongside production safe-area,
touch-size, accessible icons and horizontal navigation fixes.

## Executed local validation

- 3,313 frontend tests in 423 files; application/scope TypeScript checks passed.
- 41 navigation function cases and 16 HTTP transport scenarios passed.
- Four profile and four survey-card browser scenarios passed with synthetic data.
- 33 backend focal cases passed (16 security and 17 deletion-policy).
- 12 complete-app survey HTTP tests and 9 widget/bootstrap tests passed in isolation.
- Four full SPA/Flask browser scenarios passed against the paired frontend, with
  original login, SQLite and disposable identities. Persistent results were checked.

The first local full-SPA subprocess stopped without an actionable exit diagnostic.
Its partial evidence was retained; the runner now reports subprocess exit codes.
The subsequent complete run passed. No client survey or external provider was used.
These are not live institutional, PostgreSQL concurrency or physical-device claims.
CI results and deployment IDs must be recorded separately against the pushed SHA.

## Deployment boundary

Vercel Sensitive values pull as redacted placeholders. Do not use such files as
valid local production credentials or publish an artifact built from placeholders.
Use the configured remote environment; stage before assigning domains.
Production frontend baseline inspected: dpl_2byACxumf1vYJHqeQZYD7P6A7HDY.
The public API reported version 912446bf96f8330664a9dec009ae57dbf935c73c, but that
version endpoint alone does not prove provider deployment identity or DB parity.
Do not promote the Render-to-Neon writer cutover without its separate runbook gates.
No aliases, client databases, provider callbacks, numbers or real plans were changed
while preparing this merge. Current deployment outcomes belong in the release PRs.
