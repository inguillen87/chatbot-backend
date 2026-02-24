# Master Contract — Endpoints, Campos y Columnas (Demo + Analytics + Encuestas)

Documento de sincronización backend/frontend para operación enterprise.

## A) Demo/Auth

### 1. GET `/api/auth/demo/catalog`
**Uso:** bootstrap previo al login demo.

**Campos principales de respuesta:**
- `frontend_contract_version: string`
- `demo_login_enabled: boolean`
- `demo_login_endpoint: string` (`/auth/demo`)
- `entry_points: Array<{key,label,enabled,login_payload}>`
- `quick_login_payload: { tenant_slug: string | null }`
- `tenant_demos: Array<{
  key,label,tipo_chat,sector,rubro_clave,tenant_slug,login_payload,enabled,login_endpoint
}>`
- `onboarding: {
  default_sector,
  requires_rubro_selection_for,
  steps,
  sector_options
}`
- `frontend: {
  demo_selector,
  preload_before_login
}`

### 2. POST `/api/auth/demo`
**Uso:** login demo (sector-first).

**Body recomendado:**
- Gobierno: `{ "sector": "gobierno" }`
- Empresas: `{ "sector": "empresas", "rubro": "<demo_key>" }`

**Respuesta crítica:**
- `token`
- `tenant_slug`
- `tipo_chat`
- `sector`
- `demo_mode`
- `marketplace`

---

## B) Analytics

### 1. GET `/api/admin/analytics/overview`
**Query:** `from`, `to`, `scope`, `tenant_slug|tenant|tenant_id`

**Respuesta (mínimo confiable):**
- `totals: { total_interactions, ... }`
- Backend garantiza `totals.total_interactions`.

### 2. GET `/api/admin/analytics/heatmap`
**Respuesta:** `{ geo, temporal, tz }`

---

## C) Encuestas públicas

### 1. GET `/api/public/encuestas/:slug`
**Uso:** detalle de encuesta pública.

### 2. GET `/api/public/encuestas/:slug/live-results`
**Uso:** resultados live con heatmap.

**Query útil:**
- `include_heatmap=1`
- `window_minutes`
- `max_points`
- `max_cells`

### 3. GET `/api/public/encuestas/:slug/comentarios`
**Query:** `limit`, `offset`

**Contrato de item comentario (render-safe):**
- `id: number`
- `texto: string`
- `nombre_autor: string`
- `fecha: string | null`
- `user_id: number | null`
- `anon_id: string | null`

> `texto` y `nombre_autor` salen normalizados por backend (nunca dict/list como child de React).

### 4. POST `/api/public/encuestas/:slug/comentarios`
**Body:**
- `texto` (required)
- `nombre` o `nombre_autor` (optional)
- `anon_id` (optional)

---

## D) Encuestas admin

### 1. GET `/api/admin/encuestas`
### 2. GET `/api/municipal/encuestas`
### 3. GET `/api/admin/surveys`

> Requieren auth/rol de admin. Si el usuario demo no tiene permisos sobre tenant solicitado, frontend debe mostrar fallback de autorización.

---

## E) reason_code para UX fallback

- `demo_catalog_unavailable`
- `demo_login_unavailable`
- `tenant_info_unavailable`
- `anon_id_unavailable`
- `auth_service_unavailable`
- `survey_not_published`
- `survey_outside_active_window`

---

## F) Columnas DB críticas

### Tabla `enc_comentario`
- `id`
- `encuesta_id`
- `user_id`
- `anon_id`
- `nombre_autor`
- `texto`
- `estado`
- `report_count` ✅ (migración requerida)
- `created_at`
- `updated_at`

### Migración obligatoria
Ejecutar:
```bash
flask db upgrade
```

Migración incluida:
- `20260224_add_report_count_to_enc_comentario`

---

## G) Checklist pro de release
1. Deploy backend.
2. Ejecutar migraciones.
3. Smoke test:
   - `/api/auth/demo/catalog`
   - `/api/auth/demo`
   - `/api/admin/analytics/overview`
   - `/api/public/encuestas/:slug/comentarios`
4. Deploy frontend.
5. Verificar demo municipio -> analytics y encuesta pública con comentarios.
