# Frontend Contract — Encuestas Pro (Public + Admin + Live)

Última actualización backend: 2026-02-27

Este documento es el contrato operativo para que el frontend quede sincronizado con backend en demo, analytics y encuestas sin pantallas blancas.

## 1) Demo Login (sector-first)

### Catalog
`GET /api/auth/demo/catalog`

Campos críticos:
- `frontend_contract_version`
- `onboarding.default_sector`
- `onboarding.sector_options[]`
- `frontend.demo_selector`
- `frontend.preload_before_login[]`

### Demo login
`POST /api/auth/demo`

Payloads sugeridos:
- Gobierno: `{ "sector": "gobierno" }`
- Empresas: `{ "sector": "empresas", "rubro": "<demo_key>" }`

Respuesta usar:
- `token`
- `tenant_slug`
- `tipo_chat`
- `sector`
- `marketplace`

---

## 2) Analytics Dashboards

### Overview
`GET /api/admin/analytics/overview?from=...&to=...&scope=municipio&tenant_slug=...`

Garantía backend:
- `payload.totals.total_interactions` siempre presente (fallback backend).

Guardrail frontend:
```ts
const totals = payload?.totals ?? {};
const totalInteractions = Number(totals.total_interactions ?? 0);
```

### Bundle (encuestas analytics)
`GET /api/encuestas/<encuesta_id>/analytics/dashboard`

Estructura esperada (resumida):
- `executive_summary`
- `visual_blueprint`
- `kpis`
- `cards`
- `modules`
- `ui_state`

No asumir campos obligatorios fuera de este set; usar defaults locales.

---

## 3) Encuestas públicas (nivel producción)

### Obtener encuesta pública
`GET /api/public/encuestas/<slug>`

### Resultados en vivo
`GET /api/public/encuestas/<slug>/live-results?include_heatmap=1&window_minutes=60&max_points=800&max_cells=120`

### Comentarios
`GET /api/public/encuestas/<slug>/comentarios?limit=50&offset=0`

Contrato de comentarios (render-safe):
- `id: number`
- `texto: string` (backend ya normaliza dict/list a string)
- `nombre_autor: string` (fallback "Anónimo")
- `fecha: string | null`
- `user_id: number | null`
- `anon_id: string | null`

### Publicar comentario
`POST /api/public/encuestas/<slug>/comentarios`
Body:
- `texto` (required)
- `nombre` o `nombre_autor` (optional)
- `anon_id` (optional)

---

## 4) Estados de error/reason_code relevantes

Demo/Aliases pueden devolver:
- `demo_catalog_unavailable`
- `demo_login_unavailable`
- `tenant_info_unavailable`
- `anon_id_unavailable`
- `auth_service_unavailable`

Encuestas públicas pueden devolver payload con:
- `reason_code: survey_not_published`
- `reason_code: survey_outside_active_window`

Frontend: mostrar fallback UX (mensaje + retry) y no crashear la vista.

---

## 5) Requisitos DB/backend para cero downtime

### Migración obligatoria
`enc_comentario.report_count` debe existir.

Comando:
```bash
flask db upgrade
```

Nota: backend tiene degradación temporal si falta columna, pero la corrección profesional definitiva es migración aplicada.

---

## 6) Checklist de release sincronizado (backend + frontend)

1. Deploy backend.
2. Ejecutar migraciones (`flask db upgrade`).
3. Smoke endpoints:
   - `/api/auth/demo/catalog`
   - `/api/auth/demo`
   - `/api/admin/analytics/overview`
   - `/api/public/encuestas/<slug>/comentarios`
4. Deploy frontend.
5. Validar UX:
   - Login demo municipio -> analytics sin white-screen.
   - Encuesta pública -> comentarios renderizan sin React #31.


---

## 7) Comunicado formal para Frontend — Analytics Ejecutivo (obligatorio)

> **Objetivo:** evitar pérdida funcional en UI y asegurar que todo lo entregado por backend se renderice de forma consistente en modo white-label (gobierno/empresa).

### 7.1 Endpoints nuevos y/o ampliados

1. **Dashboard bundle ejecutivo**
   - `GET /api/encuestas/<encuesta_id>/analytics/dashboard`
   - Campos nuevos relevantes:
     - `admin_template`
     - `kpis_executive`

2. **Sugerencias dinámicas de segmentos**
   - `GET /api/encuestas/<encuesta_id>/analytics/segments/suggestions?limit=5`
   - Devuelve `dimensions` por `canal`, `genero`, `rango_etario`, `barrio`, `ciudad`, `provincia`, `pais` con:
     - `label`
     - `filters`
     - `count`
     - `coverage`

3. **Comparación A/B flexible**
   - `GET /api/encuestas/<encuesta_id>/analytics/segments/compare?...`
   - Acepta `a_*` y `b_*` en formato simple o múltiple (CSV):
     - Ejemplo: `a_canal=web,whatsapp&b_ciudad=Junin`
   - Campos nuevos:
     - `segment_a.meta`, `segment_b.meta`
     - `comparison_meta`

4. **Anomalías accionables**
   - `GET /api/encuestas/<encuesta_id>/analytics/anomalies`
   - Campos nuevos:
     - `severity` (top-level: `low|medium|high|critical`)
     - `top_anomalies[]` priorizadas por score

### 7.2 Contrato mínimo que Frontend debe consumir

#### A) `admin_template`
- `layout_version`
- `tabs[]`
- `chart_stack.recommended[]`
- `datasets`:
  - `geo_rankings`
  - `category_rankings`
  - `age_distribution`
  - `activity_timeseries`
  - `heatmap_points`
  - `by_barrio`, `by_distrito`, `by_ciudad`
- `decision_cards[]`
- `visual_modules[]` (backend-driven UI spec):
  - `title`, `description`, `empty_state`, `units`, `decimals`, `sort`, `thresholds`, `palette`

#### B) `kpis_executive`
Cada KPI incluye:
- `value`
- `trend`
- `status`
- `explanation`

KPIs disponibles:
- `participacion_total`
- `representatividad_territorial`
- `brecha_segmento_max`
- `indice_confianza_datos`
- `tiempo_respuesta_medio`
- `tendencia_7d`
- `tendencia_30d`

#### C) `top_anomalies[]`
Cada entrada incluye:
- `type`, `detail`, `score`
- `why_it_matters`
- `recommended_action`
- `affected_segment`
- `confidence`
- `severity`
- `timestamp`

### 7.3 Reglas de implementación frontend (para no perder información)

1. **No hardcodear tabs ni labels**: renderizar tabs desde `admin_template.tabs`.
2. **No hardcodear umbrales ni colores**: usar `visual_modules.thresholds` y `visual_modules.palette`.
3. **No hardcodear segmentos A/B**: construir selector desde `segments/suggestions`.
4. **No descartar fields desconocidos**: mantener estrategia forward-compatible.
5. **Fail-safe visual**: si falta un módulo, mostrar `empty_state` y no romper pantalla.

### 7.4 Checklist de aceptación frontend

- [ ] Dashboard renderiza `admin_template.tabs` sin white-screen.
- [ ] Mapa territorial usa `by_barrio/by_distrito/by_ciudad` y `normalized_density`.
- [ ] Vista de anomalías consume `top_anomalies` completo (incluyendo recomendación).
- [ ] Selector A/B usa `segments/suggestions` (sin valores hardcodeados).
- [ ] KPIs ejecutivos se muestran desde `kpis_executive` con `status` y `explanation`.

### 7.5 Mensaje corto sugerido para Product/Frontend

"A partir de esta versión, el dashboard de encuestas expone contrato ejecutivo backend-driven (`admin_template`, `kpis_executive`, segmentos dinámicos y anomalías accionables). Para preservar la experiencia premium y evitar pérdidas funcionales, frontend debe mapear visualización y decisiones desde payload, sin hardcodes de tabs, segmentos, thresholds ni paletas."

### 7.6 Guardrails UX/UI para evitar errores de gráficos y widget

Consumir también desde `admin_template.ux_guardrails`:
- `chart_container.default_min_width` (usar como `min-width` del contenedor de charts)
- `chart_container.default_min_height` (usar como `min-height` del contenedor de charts)
- `chart_container.render_when_visible` (evitar mount de chart en tabs/paneles ocultos)
- `telemetry.event_endpoint_preferred` (`/api/analytics/event`)
- `telemetry.fallback_event_name` (`frontend_analytics_event`)
- `widget.config_endpoint_preferred` (`/api/public/widget-config`)

Checklist técnico frontend adicional:
- [ ] No renderizar Recharts/ECharts si el contenedor mide 0x0.
- [ ] Aplicar `min-width >= 280` y `min-height >= 220` por card de gráfico.
- [ ] Reintentar bootstrap de widget con backoff corto (2-3 intentos).
- [ ] En fallback de telemetría, enviar al menos tenant + evento por defecto.
