# Changelog

## Unreleased

### Added
- Tenant-scoped analytics event ingest endpoint:
  - `POST /analytics/event`
- Tenant-scoped admin analytics endpoints:
  - `GET /admin/analytics/overview`
  - `GET /admin/analytics/heatmap`
  - `GET /admin/analytics/export.csv`
  - `GET /admin/analytics/export.pdf`
- Tenant-scoped admin AI endpoints:
  - `POST /admin/ai/executive-summary`
  - `POST /admin/tickets/{id}/ai-summary`
  - `POST /admin/ai/product-recommendations`
  - `POST /admin/ai/order-draft-from-document`

### Security / Isolation
- Enforced tenant and role checks (`require_access`) for admin analytics and admin AI APIs.
- Added cross-tenant rejection tests for analytics and AI routes.

### Tests
- Added and extended tests:
  - `tests/test_analytics_module.py`
  - `tests/test_admin_ai_endpoints.py`

### Documentation
- Documented new tenant-scoped Admin AI endpoints and payloads in `README.md`.
