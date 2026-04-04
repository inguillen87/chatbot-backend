# Frontend Task Plan — Encuestas públicas (UX/UI premium)

> Objetivo: eliminar fricción en votación pública, evitar pantallas de bloqueo ambiguas, y llevar la experiencia a nivel “producto premium” con métricas accionables.

## 1) Contexto técnico (importante para FE)

Backend ya quedó preparado para resolver encuestas públicas por tenant cuando hay slugs compartidos:

- `GET /api/public/encuestas/:slug`
- `GET /public/encuestas/:slug`
- `/e/:slug` (share page)
- endpoints de `comentarios`, `reportar`, `qr`

Ahora se prioriza `tenant` derivado por dominio/headers para evitar conflictos cross-tenant que antes podían terminar en `403` confusos.

---

## 2) Tareas FE prioritarias (P0)

## P0.1 — Manejo de errores por `reason_code` (no más mensaje genérico)

Cuando API responda 403, mapear:

- `reason_code = survey_not_published`
  - **Título:** “Esta encuesta todavía no está publicada”
  - **CTA primario:** “Ver encuestas activas”
  - **CTA secundario:** “Volver al inicio”
- `reason_code = survey_outside_active_window`
  - **Título:** “Esta encuesta no está disponible en este momento”
  - **Subtexto:** mostrar rango de fechas si el payload lo trae
  - **CTA:** “Ver otras encuestas”
- Sin `reason_code`
  - **Título fallback:** “No pudimos cargar esta encuesta”
  - **Subtexto:** “Probá nuevamente en unos segundos.”
  - **CTA:** “Reintentar”

**Resultado esperado:** menos abandono y menos tickets de soporte por errores ambiguos.

## P0.2 — Retry UX controlado

Implementar retry con backoff para GET públicos:

- 1er retry a `+700ms`
- 2do retry a `+1500ms`
- luego mostrar estado final

Solo para errores transitorios (`5xx`, network fail, timeout). **No** reintentar 403/404 infinitamente.

## P0.3 — Skeletons + transición visual

En vez de “flash” de error o pantalla vacía:

- `Hero skeleton` (título + descripción)
- `Preguntas skeleton` (3–4 bloques)
- `Resultados en vivo skeleton` (si aplica)

Tiempo mínimo de skeleton recomendado: `350–500ms` para evitar parpadeo.

## P0.4 — Contrato de dominio/tenant consistente

Para requests públicas desde dominio custom:

- enviar `Host`/`Origin` correctos (browser lo hace)
- no sobreescribir base URL con dominio incorrecto
- evitar hardcode de `api.chatboc.ar` si el tenant usa dominio branded

Agregar alerta en Sentry si `window.location.host` no coincide con el host esperado de configuración.

---

## 3) Tareas UX/UI de alto impacto (P1)

## P1.1 — Empty states elegantes

Diseñar 4 estados distintos:

1. encuesta no encontrada (404)
2. no publicada (403 + reason)
3. fuera de ventana (403 + reason)
4. error técnico temporal (5xx)

Cada estado con:

- icono distinto
- titular corto
- microcopy claro
- CTA principal + secundario

## P1.2 — Barra de progreso en encuesta

Mostrar:

- “Pregunta X de N”
- progreso lineal
- guardado local temporal (draft en `localStorage`, TTL 30 min)

## P1.3 — Confirmación post-voto premium

Pantalla de éxito con:

- check visual + mensaje de impacto (“Tu voto ya cuenta en tiempo real”)
- botón “Ver resultados en vivo”
- botón “Compartir encuesta”

## P1.4 — Comentarios: moderación UX

En `POST /comentarios`:

- validación previa de longitud
- contador visible
- confirmación optimista
- si falla, rollback visual + toast con motivo

---

## 4) Tareas de accesibilidad (P1)

- contraste WCAG AA en dark mode y light mode
- foco visible en todos los botones/inputs
- navegación completa con teclado
- `aria-live="polite"` para mensajes de estado (error/success)
- labels explícitos en opciones de respuesta

Checklist QA accesibilidad por release.

---

## 5) Telemetría mínima requerida (P0/P1)

Eventos recomendados:

- `survey_page_view`
- `survey_load_error` (con `status_code`, `reason_code`, `slug`, `host`)
- `survey_retry_triggered`
- `survey_answer_selected`
- `survey_submitted`
- `survey_submit_error`
- `survey_comment_submitted`

Dashboard mínimo:

- tasa de carga exitosa
- tasa de error por `reason_code`
- abandono por pregunta
- conversión vista -> voto

---

## 6) Performance budget (P1)

- LCP objetivo < 2.5s (4G)
- CLS < 0.1
- JS inicial encuesta < 170KB gzip ideal
- lazy load para módulo de resultados/heatmap
- cache de GET encuesta (`stale-while-revalidate`)

---

## 7) Plan de implementación sugerido

## Sprint 1 (rápido, impacto alto)

- P0.1 reason_code mapping
- P0.2 retry controlado
- P0.3 skeletons
- telemetría base de errores

## Sprint 2

- empty states premium
- progreso de encuesta
- éxito post-voto
- mejoras comentarios

## Sprint 3

- accesibilidad completa
- optimización de performance
- dashboard de producto

---

## 8) Definition of Done (DoD)

Una entrega FE se considera “lista” cuando:

- No hay pantallas ambiguas para 403/404/5xx.
- Cada error tiene copy + CTA útil.
- Existe tracking de errores por `reason_code`.
- Lighthouse mobile >= 90 en Performance/Best Practices/Accessibility (página encuesta).
- QA manual validó flujo completo: abrir encuesta -> votar -> confirmar -> ver resultados.

---

## 9) Mensaje sugerido para equipo frontend (copiar/pegar)

> Equipo, backend ya resuelve correctamente encuestas públicas por tenant incluso con slugs compartidos. Necesitamos cerrar UX/UI del lado FE con prioridad en: manejo de `403` por `reason_code`, estados vacíos diferenciados, skeleton + retry transitorio, y telemetría de errores por host/slug. Este trabajo es crítico para que la experiencia de encuestas públicas sea sólida y sin fricción.

