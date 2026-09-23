# Render to Vercel cutover release manifest gate

`scripts/audit_cutover_release_manifest.py` is the final offline aggregator for
the Render-to-Vercel cutover. It performs no network, database, provider,
deployment, DNS, or filesystem mutation. A successful result means that the
required evidence is internally consistent and ready for operator review; it
does not authorize the cutover.

## Why this gate exists

A Vercel `READY` deployment proves only that one artifact built. Repository
definitions also do not prove that workers, queues, crons, or Twilio callbacks
have transferred safely. The manifest therefore requires every certification
to be fresh, archived, redacted, and bound to the same:

- release ID and maintenance-window ID;
- full Git revision;
- Vercel project, deployment ID, and immutable `.vercel.app` host.

The gate returns nonzero when any certification is missing, pending, stale,
bound to another deployment, or contains an incomplete claim inventory.

## Required certifications

| Gate | Required producer contract |
| --- | --- |
| Writer fence | `chatboc.cutover_writer_fence_certification.v1` |
| Strict content parity | `chatboc.render_neon_final_parity.v1` |
| Exact Neon migrations | `chatboc.neon_cutover_migrations.v1` |
| Worker and durable queue replacement | `chatboc.worker_queue_replacement_certification.v1` |
| Vercel cron replacement | `chatboc.vercel_cron_replacement_certification.v1` |
| WhatsApp and Twilio webhook replacement | `chatboc.whatsapp_twilio_webhook_replacement_certification.v1` |
| Authenticated application canary | `chatboc.cutover_application_canary.v1` |
| Live provider canary | `chatboc.cutover_live_provider_canary.v1` |
| Compute rollback rehearsal | `chatboc.compute_rollback_rehearsal.v1` |
| Vercel cold start | `chatboc.vercel_cold_start_certification.v1` |

The worker certification must cover the WhatsApp, domain-effect, and survey
effect queues. The cron certification must cover exactly the four paths in
`vercel.json`, prove the registry and runtime fence, prove global no-overlap
with source schedulers, and include the rollback retarget plan. The webhook
certification must cover both WhatsApp inbound and the Twilio status callback,
including signature verification, durable persistence/replay, unique provider
ownership, and preserved rollback URLs.

The existing `chatboc.vercel_cron_ownership_audit.v3` artifact is an input to
the combined cron replacement certification, not a substitute for it. That
auditor deliberately cannot prove global Render-versus-Vercel scheduler
ownership by itself.

## Manifest shape

The manifest uses contract
`chatboc.cutover_release_manifest.v1`. Its `candidate.target` must be
`production`; this refers to the immutable Vercel production deployment, not
to the `api.chatboc.ar` DNS switch.

```json
{
  "contract_version": "chatboc.cutover_release_manifest.v1",
  "release_id": "release-example-0001",
  "cutover_window_id": "window-example-0001",
  "candidate": {
    "project_name": "chatboc-backend",
    "deployment_id": "dpl_REPLACE_WITH_APPROVED_ID",
    "deployment_host": "approved-host.vercel.app",
    "revision": "0000000000000000000000000000000000000000",
    "target": "production"
  },
  "certifications": {
    "writer_fence": {
      "contract_version": "chatboc.cutover_gate_certification.v1",
      "gate": "writer_fence",
      "status": "certified",
      "release_id": "release-example-0001",
      "cutover_window_id": "window-example-0001",
      "project_name": "chatboc-backend",
      "deployment_id": "dpl_REPLACE_WITH_APPROVED_ID",
      "deployment_host": "approved-host.vercel.app",
      "revision": "0000000000000000000000000000000000000000",
      "producer_contract_version": "chatboc.cutover_writer_fence_certification.v1",
      "evidence_sha256": "replace-with-the-archived-evidence-digest",
      "observed_at": "2026-09-05T23:00:00Z",
      "expires_at": "2026-09-06T01:00:00Z",
      "evidence_archived": true,
      "secret_values_redacted": true,
      "production_mutations_performed": false,
      "claims": {
        "source_fenced": true,
        "destination_fenced": true,
        "single_writer_certified": true,
        "source_stable": true
      }
    }
  }
}
```

Use the complete claim keys enforced by `GATE_SPECS` for every receipt. Do not
commit the populated manifest or its external evidence to this repository.
Archive them in the private cutover evidence location.

## Run

```powershell
py -3 scripts/audit_cutover_release_manifest.py `
  --manifest C:\private-cutover-evidence\release-manifest.json `
  --audit-only
```

Exit codes:

- `0`: closure evidence is complete and ready for operator review;
- `1`: well-formed evidence is incomplete or blocked;
- `2`: the manifest or command is malformed.

`pre_switch_ready: true` with `ready: false` means every pre-switch gate is
certified but the live provider canary is still pending. Only the later live
canary can produce the full closure state. Neither state instructs the tool to
deploy, promote, change an alias, change a webhook, transfer writer authority,
or stop Render.
