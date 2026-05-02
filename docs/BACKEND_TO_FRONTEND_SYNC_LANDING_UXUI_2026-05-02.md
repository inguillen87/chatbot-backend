# Backend to Frontend Sync - Landing UX/UI 2026-05-02

Fecha: 2026-05-02

Objetivo: mejorar landing y paginas aledanas sin hardcodear copy, colores, logos ni animaciones en React. Backend expone un contrato publico para que frontend renderice una experiencia comercial premium, white-label y coherente con demo/widget/WhatsApp.

## Endpoint nuevo

`GET /api/public/landing-experience`

Query params opcionales:

- `page`: `home`, `demo`, `pymes`, `municipios`, `colegios`, `encuestas`, `widget`.
- `tenant` o `slug`: activa modo white-label para un tenant.
- `widget_token`: tambien puede resolver tenant.

Contrato:

`public.landing_experience.v1`

## Shape principal

```json
{
  "contract_version": "public.landing_experience.v1",
  "experience_kind": "platform",
  "selected_page": "home",
  "tenant": {
    "slug": null,
    "tipo": null,
    "vertical": null,
    "white_label": false
  },
  "brand": {},
  "design_tokens": {},
  "motion": {},
  "navigation": {},
  "hero": {},
  "sections": [],
  "adjacent_pages": [],
  "conversion": {},
  "proof_bar": [],
  "pricing_teaser": {},
  "faq": [],
  "content_rules": []
}
```

## UX que frontend debe implementar

### Landing principal

- H1 desde `hero.h1`; para plataforma viene `Chatboc`.
- Supporting copy desde `hero.subtitle`.
- CTAs desde `hero.primary_cta`, `hero.secondary_cta`, `hero.tertiary_cta`.
- Media desde `hero.media.assets`; usar visuales de producto, dashboard o chat real. No usar hero abstracto de gradiente solo.
- Dejar visible un indicio de la siguiente seccion en mobile y desktop.

### Colores y tokens

Usar `design_tokens.color`:

- `primary`
- `accent`
- `warm`
- `info`
- `success`
- `danger`
- `surface`
- `surface_alt`
- `ink`
- `muted`
- `line`

Regla: evitar landing de un solo color. Combinar neutros + primary + accent + warm en pequenos acentos.

### Logo / marca

Usar `brand.logo`:

- Si `brand.logo.source === "tenant"` y hay `url`, renderizar logo tenant.
- Si no hay logo, usar `brand.wordmark` + mark compacto `brand.logo.fallback_mark`.
- No inventar logos municipales, escolares o de pymes en frontend.

### Animaciones

Usar `motion`:

- `motion.contract_version: landing.motion.v1`
- respetar `respect_reduced_motion`.
- aplicar `components.hero_media`, `components.chat_preview`, `components.cards`, `components.cta`.
- en mobile respetar `mobile_rules.max_parallel_animations`.

### Paginas aledanas

Renderizar o alinear estas paginas desde `adjacent_pages`:

- `/demo`: selector de sector/rubro + chat real usando `/api/v2/demo/catalog` y `/api/v2/demo/session`.
- `/pymes`: catalogo, pedidos, pagos, WhatsApp, soporte.
- `/municipios`: reclamos, tramites, mapas, encuestas.
- `/colegios`: asistencia, comunicados, secretaria, casos escolares.
- `/encuestas`: participacion, votaciones, resultados.
- `/widget`: preview, theme controls, quick menu, embed code.

## Tareas frontend recomendadas

1. Crear cliente `getLandingExperience({ page, tenant })`.
2. Refactorizar landing para leer `brand`, `hero`, `sections`, `proof_bar`, `faq`, `design_tokens` y `motion`.
3. Hacer que `/demo`, `/pymes`, `/municipios`, `/colegios`, `/encuestas`, `/widget` compartan estructura desde `adjacent_pages`.
4. Usar `content_rules` como guardrails de implementacion.
5. Conectar CTAs con:
   - `/api/v2/demo/catalog`
   - `/api/v2/demo/session`
   - `/api/public/lead-capture`
   - `/api/public/widget-config`
6. Aplicar estados empty/loading/error sin inventar copy local.

## Verificacion backend

Ejecutado con `venv\\Scripts\\pythonw.exe`:

- `py_compile` sobre `services/landing_experience_contract.py`, `routes/public_resolver.py` y tests.
- `tests.test_landing_experience_contract`
- `tests.test_public_resolver_widget_config_contract`
