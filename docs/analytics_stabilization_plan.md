# Plan integral de estabilización (Analytics + Auth + Demo Catalog)

## Resumen ejecutivo
Este documento aterriza los problemas visibles en producción (502 intermitentes en login/catalog/tenant, spam de warnings de charts y bloques de analytics con datos mal renderizados) y define tareas concretas para **backend + frontend**.

Objetivo: que el flujo `Probar demo -> login -> analytics` sea robusto, observable y visualmente profesional incluso ante degradación parcial.

---

## 1) Diagnóstico de lo observado (con base en logs e imágenes)

## 1.1 Errores de backend críticos
- `GET /api/auth/demo/catalog` devolviendo `502`.
- `GET /api/app/me/tenants` devolviendo `502`.
- `POST /api/auth/admin/login` devolviendo `502`.

### Hipótesis validada en código
1. Había un `NameError` en login (`owner_tenant` no definido) que rompe `/auth/login` bajo ciertos caminos.
2. El middleware de tenant resolvía contra DB en cada request sin fallback defensivo; ante error DB/transitorio podía reventar la request completa.
3. `demo_catalog` dependía de consultas DB sin degradación segura, pudiendo escalar a 5xx.

## 1.2 Problemas UI/UX en analytics (imágenes)
1. **Spam de consola:** `width(-1) and height(-1) of chart should be greater than 0`.
2. **Cards mal formateadas:** se ve texto como `:100`, `:media`, o JSON crudo (`{"participantes_unicos":...}`), señal de render directo de objetos sin normalización de schema.
3. **Inconsistencia semántica:** sección “Últimas respuestas” mostrando “Todavía no recibimos respuestas” cuando arriba hay 100 respuestas (posible fuente distinta o filtro no sincronizado).
4. **Mapa de participación vacío** en algunos estados mientras hay heatmap/tablas con puntos (falta fallback UX “sin geometría compatible”).
5. **Experiencia general**: hay datos valiosos, pero faltan micro-estados de carga, vacíos, errores y fallback visual para nivel “producto premium”.

---

## 2) Cambios backend aplicados en esta iteración

1. **Fix hard para login**
   - Reemplazo de referencia inválida `owner_tenant` por `tenant_obj` en `/auth/login`.
   - Evita crash y 5xx en login normal.

2. **Resiliencia de middleware tenant-context**
   - `before_request` ahora encapsula resolución tenant en `try/except`.
   - Ante excepción (por ejemplo OperationalError/transitorio DB), no derriba toda la request por defecto.

3. **Resiliencia en demo catalog**
   - `load_demo_rubros()` envuelto en fallback seguro.
   - `_resolve_default_demo_slug()` protegido para no elevar excepción a 5xx.

4. **Pruebas de regresión/estabilidad**
   - Ajuste de test de token para no chocar con seed por `rubro.clave` duplicado.
   - Nuevo test para validar login estándar con tenant resuelto y municipio correcto.

---

## 3) Backlog frontend (accionable, prioridad alta)

## P0 (hacer ya)
1. **Chart container guard**
   - No renderizar charts hasta tener dimensiones > 0 (`ResizeObserver` + `minHeight`).
   - Si width/height inválido: skeleton o placeholder, no chart.

2. **Normalizador de payload analytics**
   - Crear capa `normalizeDashboardPayload(raw)`.
   - Nunca renderizar objetos crudos en cards.
   - Establecer defaults por campo (`number | null`, `string`, `[]`).

3. **Estados unificados por bloque**
   - `loading`, `empty`, `error`, `ready` por widget.
   - Mensajes consistentes: evitar contradicción “sin respuestas” cuando hay total > 0.

4. **Manejo de 502 en onboarding demo**
   - Retry exponencial corto (ej. 250ms, 700ms, 1500ms) para `/auth/demo/catalog`.
   - Si falla: fallback UI con “Demo Municipio / Demo PyME” hardcoded y CTA “Reintentar”.

## P1
1. **Mapa de participación degradable**
   - Si no hay geometría válida, mostrar tabla/lista con top coordenadas y mensaje claro.
2. **Consistencia filtros bbox**
   - Todos los endpoints analytics deben reflejar el mismo filtro activo y mostrar chip de filtro.
3. **Telemetría de frontend**
   - Evento `analytics_widget_render_failed` con `widgetId`, `reason`, `tenantSlug`, `encuestaId`.

## P2
1. **Performance visual**
   - virtualización de tablas largas;
   - memoización de transformaciones;
   - limitar repaints de charts.
2. **Polish UX**
   - tooltips explicativos, definiciones métricas, accesibilidad de contrastes y labels.

---

## 4) Contratos sugeridos backend->frontend para robustez

## 4.1 Envelope estándar
```json
{
  "ok": true,
  "data": {},
  "meta": {
    "tenant_slug": "junin-1",
    "encuesta_id": 84,
    "filters": {"bbox": "..."},
    "generated_at": "2026-02-24T15:31:40Z"
  },
  "errors": []
}
```

## 4.2 Convenciones
- Nunca enviar objetos arbitrarios para card-value; usar `value`, `unit`, `delta`, `trend`.
- Si una sección no aplica: `data.section = null` + `meta.section_state = "not_applicable"`.
- Si no hay datos: arrays vacíos + `meta.section_state = "empty"`.

---

## 5) Checklist QA recomendado

1. Smoke demo:
   - `/api/auth/demo/catalog` responde 200 y estructura esperada.
   - login demo y admin sin 5xx.
2. Analytics:
   - sin warnings de width/height;
   - cards sin `:` huérfanos ni JSON crudo;
   - consistencia entre “total respuestas” y “últimas respuestas”.
3. Tenant-context:
   - ante fallo forzado DB temporal, endpoints públicos no deben caer en cascada con 5xx sin control.

---

## 6) Siguiente iteración sugerida
- Unificar todos los endpoints analytics en un `dashboard_bundle` con versionado (`schema_version`).
- Agregar pruebas E2E que validen render sin warnings de chart y sin blocks corruptos.
- Implementar alertas operativas para ratio de 502 por endpoint y por tenant.

