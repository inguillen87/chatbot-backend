# analytics.identity_coverage.v1

- **Endpoint:** `GET /analytics/identity/coverage`
- **Contract Version:** `analytics.identity_coverage.v1`

## Query params
- `tenant_id` (string|number, required)
- `scope` (`municipio|pyme`, according existing analytics filters)
- `date_from` (ISO datetime, optional)
- `date_to` (ISO datetime, optional)
- `limit` (int 1..20000, default 5000)
- `target_pct` (float, default 90)
- `target_by_channel` (JSON object or `canal:valor,canal:valor`)
- `emit_alert_events` (`0|1|true|false`, default false)

## Response fields
- `tenant_id`
- `sample_size`
- `total_events`
- `events_with_identity`
- `coverage_pct`
- `target_pct`
- `target_by_channel`
- `slo_status` (`ok|below_target`)
- `channels` (`{ channel: { total, with_identity, coverage_pct } }`)
- `alerts` (`[{ type, channel, coverage_pct, target_pct, gap_pct, recommended_action }]`)
- `alert_count`
- `emit_alert_events`
- `alert_events_emitted`
- `contract_version` (`analytics.identity_coverage.v1`)

## Compatibility policy
- Additive-only for non-breaking evolution.
- Frontend should treat unknown fields as optional.
