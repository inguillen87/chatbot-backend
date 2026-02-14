# Widget UX Top-Tier Handoff (Backend → Frontend)

Este documento resume los nuevos tokens UX expuestos por backend para lograr una experiencia visual premium mundial en el widget.

## Endpoint fuente
- `GET /api/public/widget-config?tenant=<slug>`

## Nuevos atributos HTML (`widget.attributes`)
- `data-typing-animation`
- `data-bubble-animation`
- `data-launcher-animation`
- `data-message-enter-animation`
- `data-logo-badge-style`
- `data-cursor-trail`
- `data-ambient-particles`

## Objeto UX canónico (`builder_config.ux`)
```json
{
  "preset": "premium",
  "motion_level": "balanced",
  "glassmorphism": true,
  "logo_ring": true,
  "gradient_start": "#0f172a",
  "gradient_end": "#007aff",
  "typing_animation": "wave-dots",
  "bubble_animation": "soft-rise",
  "launcher_animation": "pulse-glow",
  "message_enter_animation": "fade-up",
  "logo_badge_style": "ring",
  "cursor_trail": false,
  "ambient_particles": false
}
```

## Recomendación de implementación frontend
1. Leer siempre `builder_config.ux` como fuente principal.
2. Permitir override con `widget.attributes` (útil para script-embed directo).
3. Degradar automáticamente animaciones si `motion_level = low` o dispositivo de baja potencia.
4. Mantener `cursor_trail` y `ambient_particles` desactivados por defecto en mobile.

## Compatibilidad
Si el tenant no configuró nada, backend entrega defaults seguros para evitar UI rota.
