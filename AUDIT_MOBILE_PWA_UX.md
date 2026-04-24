# AUDIT MOBILE/PWA UX (Backend support)

## Hallazgos
- En mobile hay síntomas de desaparición/mezcla de módulos por bootstrap/navegación no jerarquizada.

## Cambios backend de soporte
- `GET /auth/session/bootstrap` incorpora `mobile_priority_panels` para orientar navegación mobile-first.
- Bootstrap declara explícitamente endpoints siguientes para secuenciar carga.

## Recomendaciones frontend
- Consumir `mobile_priority_panels` para ordenar tabs/cards en móviles.
- No ocultar módulos por breakpoint sin fallback de navegación.
