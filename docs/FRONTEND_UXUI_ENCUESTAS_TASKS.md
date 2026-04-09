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

---

## 10) Estado backend implementado (para que FE avance sin bloqueo)

Esto ya está disponible desde backend y FE puede usarlo hoy:

- Resolución tenant-aware en lectura/escritura de encuestas públicas.
- Errores estructurados en endpoints públicos con:
  - `status_code`
  - `reason_code` (cuando aplica)
  - `retryable`
  - `action_hint`
  - `request_id`

---

## 11) Handoff técnico adicional para FE (comentarios sociales)

### 11.1 Contrato de configuración para UI

En el payload de encuesta pública, cuando `permitir_comentarios = true`, ahora FE puede esperar:

```json
{
  "commentConfig": {
    "requiresSocialToken": false,
    "acceptedModes": ["anon", "social"]
  },
  "socialProviders": [
    {"id": "facebook", "label": "Facebook"},
    {"id": "google", "label": "Google"},
    {"id": "instagram", "label": "Instagram"}
  ]
}
```

Regla FE:

- Si `requiresSocialToken = true`, **no permitir submit en modo social** sin `social_token` válido.
- Si es `false`, se puede mantener fallback legacy.

### 11.2 Submit de comentarios (POST `/comentarios`)

Campos recomendados para modo social:

```json
{
  "texto": "Excelente propuesta",
  "mode": "social",
  "social_token": "<token-firmado>"
}
```

Notas:

- El backend valida firma/TTL del token.
- Si FE manda `auth_provider`/`auth_user_id` y no coinciden con el token => error de seguridad.
- Si el token es válido, backend completa automáticamente identidad social.

### 11.3 Reason codes que FE debe mapear (comentarios sociales)

- `invalid_social_token`
  - Copy sugerido: “Tu sesión social venció. Volvé a iniciar sesión para comentar.”
  - CTA: “Reiniciar sesión”
- `social_token_required`
  - Copy sugerido: “Para comentar con tu cuenta social primero necesitás validarte.”
  - CTA: “Conectar cuenta”
- `social_identity_mismatch`
  - Copy sugerido: “No pudimos validar tu identidad social. Reintentá desde cero.”
  - CTA: “Reintentar”

### 11.4 Contrato de respuesta de comentario creado

Para permitir render optimista sin pedir refetch inmediato:

```json
{
  "ok": true,
  "comentario": {
    "id": 123,
    "texto": "Excelente propuesta",
    "nombre_autor": "Nombre Apellido",
    "fecha": "2026-04-09T03:10:00+00:00",
    "comment_mode": "social",
    "auth_provider": "facebook",
    "auth_user_id": "fb_5566"
  }
}
```

### 11.5 Checklist FE de implementación (para no trabarse)

- [ ] Leer `commentConfig` y `socialProviders` al cargar encuesta.
- [ ] Deshabilitar submit social sin token cuando `requiresSocialToken=true`.
- [ ] Mapear `reason_code` nuevos a toasts/modales con CTA.
- [ ] Hacer rollback UI ante error de submit (sacar comentario optimista).
- [ ] Guardar `request_id` en logs FE para soporte cruzado con backend.

### Ejemplo error 403 no publicada

```json
{
  "error": "La encuesta no está activa",
  "status_code": 403,
  "reason_code": "survey_not_published",
  "retryable": false,
  "action_hint": "view_other_surveys",
  "request_id": "req-123"
}
```

### Ejemplo error 500 inesperado en live-results

```json
{
  "error": "Error interno",
  "status_code": 500,
  "reason_code": "internal_error",
  "retryable": true,
  "action_hint": "retry",
  "request_id": "4f8f7c9f..."
}
```

---

## 11) Tareas FE concretas para ejecutar ahora (copiar a Jira/Trello)

## FE-ENC-001 — Error mapper unificado

Implementar `mapSurveyError(apiError)`:

- Entrada: payload backend (`status_code`, `reason_code`, `retryable`, `action_hint`).
- Salida: `title`, `description`, `primaryCta`, `secondaryCta`, `trackCode`.

**Aceptación**

- 403 `survey_not_published` muestra CTA “Ver encuestas activas”.
- 403 `survey_outside_active_window` muestra CTA “Ver otras encuestas”.
- 500 `internal_error` muestra CTA “Reintentar”.
- Se registra `request_id` en logs FE/Sentry.

## FE-ENC-002 — Componente `<SurveyErrorState />`

Crear componente único reutilizable para:

- encuesta
- live-results
- comentarios
- QR panel

Props mínimas:

- `reasonCode`
- `retryable`
- `actionHint`
- `requestId`
- callbacks `onRetry`, `onGoHome`, `onViewOtherSurveys`

## FE-ENC-003 — Retry policy inteligente

- Si `retryable=true`: retry automático con backoff (700ms, 1500ms).
- Si `retryable=false`: no retry automático; mostrar CTA contextual.
- Siempre mostrar botón manual “Reintentar”.

## FE-ENC-004 — Telemetría obligatoria

Eventos:

- `survey_error_rendered`
- `survey_retry_clicked`
- `survey_cta_clicked`

Payload obligatorio del evento:

- `slug`
- `host`
- `status_code`
- `reason_code`
- `action_hint`
- `request_id`

## FE-ENC-005 — QA matrix de errores

Probar en staging:

1. `403 + survey_not_published`
2. `403 + survey_outside_active_window`
3. `404`
4. `500 + internal_error`

En cada caso validar:

- copy correcto
- CTA correcto
- evento analytics disparado
- `request_id` visible en consola debug

---

## 12) Contrato rápido de CTA por `action_hint`

Mapeo recomendado:

- `view_other_surveys` → navegar a listado de encuestas públicas.
- `retry_later` → mostrar “Intentá de nuevo en unos minutos”.
- `retry` → botón “Reintentar” + retry automático si corresponde.
- `go_home` → volver a home/landing tenant.

---

## 13) Checklist de release FE (obligatorio)

- [ ] Todos los errores públicos usan `SurveyErrorState`.
- [ ] Se lee y propaga `request_id` en logging FE.
- [ ] Los eventos de error llegan al dashboard.
- [ ] No hay mensaje genérico sin CTA.
- [ ] Lighthouse A11y >= 90 en vista de encuesta.
- [ ] Se validó mobile + desktop + dark mode.

---

## 14) Extensión cross-product (portal + marketplace + noticias + eventos)

Para que “todo se vea hermoso y en armonía” más allá de encuestas:

## FE-XP-001 — Diseño unificado de tarjetas de estado

Aplicar un mismo patrón visual para:

- reclamos/tickets
- sugerencias
- pedidos marketplace
- encuestas/votaciones
- noticias y eventos

Regla: cada tarjeta debe tener `estado`, `última actualización`, `CTA principal`.

## FE-XP-002 — Render semántico de datos estructurados

Nunca renderizar JSON crudo en chat/widget/portal.

Ejemplos a transformar:

- horarios de atención (lista día/hora)
- links de seguimiento (`PIN` + URL ticket)
- contacto especializado
- bloques “Más info”

## FE-XP-003 — Componente único de seguimiento

Crear `TrackingCard` reutilizable con:

- `ticket_id`
- `pin`
- `tracking_url`
- `contact_phone`
- `contact_schedule`

Uso en:

- widget web
- portal usuario
- mensajes enriquecidos del chat

## FE-XP-004 — QA journeys ejecutivos (demo para gobierno/empresa)

Flujos obligatorios para demo:

1. iniciar reclamo → confirmación → tracking bonito
2. votar encuesta → ver resultados
3. abrir portal → historial + puntos + reclamos + pedidos
4. abrir catálogo → agregar al carrito → pedido
5. abrir noticias/eventos → navegación limpia

Medir tiempo de tarea + errores + percepción visual.

---

## 15) Troubleshooting FE (errores reales detectados en consola)

### A) `POST /api/tickets/chat/:id/responder_ciudadano` devuelve 415

**Contrato recomendado FE**

- Enviar `Content-Type: application/json`
- Body:

```json
{ "comentario": "texto del ciudadano" }
```

Backend ahora tolera también `application/x-www-form-urlencoded`, pero FE debe priorizar JSON para trazabilidad uniforme.

### B) `presence` y `socket.io` con reconexiones infinitas / 502

Implementar estrategia defensiva FE:

1. Si falla `websocket`, permitir fallback a `polling` (engine.io).
2. Backoff exponencial en reconexión (1s, 2s, 5s, 10s, max 30s).
3. Circuit breaker: pausar reconnect por 60s después de N fallos consecutivos.
4. No spamear endpoint de presence en background tab.
5. En `document.hidden === true`, bajar frecuencia de heartbeat.

### C) Errores de extensiones de navegador

Mensajes como:

- `SES Removing unpermitted intrinsics`
- `No matching tab found`
- `Disconnected from polkadot...`

No son necesariamente bugs de Chatboc. Etiquetar como `browser_extension_noise` en telemetry para no contaminar métricas de producto.
