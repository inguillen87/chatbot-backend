# Production territorial contract hotfix QA — 2026-09-08

## Scope and baseline

- Branch: `codex/territory-production-hotfix-20260908`.
- Parent baseline: `b68021923b99f0fb0971ed7657a476c2439b4695`.
- Previous Render deployment retained for rollback: `dep-daed1n2d0e5s7381tesg`.
- No migrations, dependency changes, startup changes, geocoding, production data writes, merge, or deployment performed for this patch.
- Production frontend consumer and live official source were independently checked by the coordinating agent: one JUNIN/codigo_dep 09 feature, matching global ID and exact geometry; frontend containment/source/hash and GeoJSON provenance contracts agree.

The patch adds official geometry and a read-only resolver, scopes points before final truncation and aggregation, and publishes point evidence and public boundary metadata. Rejected coordinates are never moved. Rounded cell centers falling outside the polygon are omitted. Employee category scope and k=5 remain; exact points, review details, and incidental custom metadata are not published to employees.

## Official snapshot

Source provenance is recorded in `data/municipios/junin/geo.json`, referencing the IDE Mendoza Hosted/Departamentos_Mendoza layer 0 query for department 09.

- Snapshot: `data/municipios/junin/official_department_boundary.geojson`.
- SHA-256: `3dbfc3bb3c98601d6bf1897d737c1e739f39173f1c10c440aef35e516661a731`.
- Git attribute pins `text eol=lf`; the geometry is unchanged from the reviewed source. Normalizing the Windows CRLF snapshot to LF prevents a different hash on Linux checkout.
- This generalized official geometry supports departmental containment, not cadastral surveying.

`DATA_DIR` has priority. A valid custom official boundary is retained. A declared missing/corrupt boundary or malformed override fails closed. A bounds-only legacy override can borrow the bundled boundary only for the same config path and matching explicit city, state, and country. No other tenant receives Junin as a default.

## Executed offline tests

Runtime: `C:\cbs34-platform-integration-backend\.codex-venv\Scripts\python.exe`.
Environment: `TESTING=1`, `FLASK_SKIP_GLOBAL_APP=1`, `PYTHONDONTWRITEBYTECODE=1`, `EVENTLET_NO_GREENDNS=YES`, `DATABASE_URL=sqlite:///:memory:`, `SQLALCHEMY_DATABASE_URI=sqlite:///:memory:`, `CHATBOC_ALLOW_EXTERNAL_NETWORK_TESTS=0`.

```text
python -m pytest tests/test_operational_jurisdiction.py tests/test_v2_operational_analytics.py tests/test_geo_polygon_truth_boundary.py tests/test_estadisticas_heatmap.py tests/test_encuestas_heatmap_fallback.py -q --disable-warnings --tb=short
61 passed, 272 warnings, 21 subtests passed in 31.58s
```

Coverage: snapshot hash/identity, Polygon/MultiPolygon/hole/edge containment, invalid/missing polygons, persistent custom precedence, safe legacy fallback, mismatched-place rejection, independent tenant isolation, all heatmap sources, pre-aggregation filtering, limit behavior, point evidence, employee privacy, outside rounded centers, unchanged persisted test coordinates, and retired legacy polygon behavior. Coordinates inserted or adjusted by these tests are synthetic fixtures in SQLite memory only.

`git diff --check` passed. Protected migration/model/dependency/startup/config paths were compared with the baseline and are unchanged. Migration tree: `a917d4862b0baa1c78798876d63c318c86f81b76`.

| Protected file | Unchanged Git blob |
| --- | --- |
| requirements.txt | 6896e49b3c1252992a0849a146a4cf0d2e7cfa92 |
| requirements_fixed.txt | 72d9a2b2ff8e5ca83db652f8217b06338a8fa08d |
| requirements-ai-oss.txt | 20851c732d12e3a444bebc9b7ea8d929087b9b24 |
| build.sh | b0b49d7c862b5775134ff8fec079f37156dcdce6 |
| render.yaml | 74a1b4c5cd8367be6f314d462ca7a3b09e24461a |
| gunicorn.conf.py | 48718cdff5ee652c2fc3c1edceb7d95c2092c75f |
| app.py | 2e455d1ce9ad16b9f4a9f0b19dbb5400e0e1bdbe |

The repository `render.yaml` retains `flask db upgrade`; it is not the effective production Settings command. Actual Settings and successful deployment logs show `test -n "$DATABASE_URL" && FLASK_MIGRATIONS_ONLY=1 MIGRATIONS_DATABASE_URL="$DATABASE_URL" python scripts/apply_migrations.py`. No migration files were added or altered. Build/start/worker/cron commands in `render.yaml` are identical to baseline.

## Production verification update

An unchanged retry became Live as Render deployment `dep-dag6uv740ujc738c0l00` at 2026-09-08 20:32:24 UTC, commit `3bc0d397e18435b8fc7fb7bed9f668c68c568e06`; `/api/version` independently confirmed this SHA. The first attempt failed after build without a diagnostic application log, so its precise failure cause is unconfirmed.

The authenticated Junín heatmap returned HTTP 200 and the official containment contract. All 19 existing candidate coordinate pairs were outside the department polygon; no points were fabricated or relocated. There were 31 pending geocoding records and 3 without location among 53 tickets. Existing-coordinate coverage must not be presented as validated map coverage. No database cutover or WhatsApp configuration changes were performed.

## Remaining release boundary

The coordinating agent must inspect the actual Production `DATA_DIR` override before any manual deployment and verify the authenticated frontend/API canary afterward. Local passing tests do not establish Production readiness. Some of the 19 previously hidden coordinates may correctly remain outside the official boundary. Keep Render and its current database/workers intact; this patch does not authorize or implement a backend database cutover.
