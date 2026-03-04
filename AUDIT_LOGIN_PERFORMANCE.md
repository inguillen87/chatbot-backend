# AUDIT LOGIN PERFORMANCE (Backend)

## Hallazgos
- El login entregaba token/perfil pero sin contrato explícito para bootstrap incremental.
- El frontend podía quedar acoplado a cargas posteriores bloqueantes (`/auth/me`, `/auth/me/dashboard`).
- No había una guía backend clara para shell-first + carga async.

## Cambios aplicados
1. `POST /auth/login` ahora devuelve bloque `bootstrap` con `mode=lite` y endpoint recomendado `/auth/session/bootstrap`.
2. Se expone `GET /auth/session/bootstrap` para entregar contrato mínimo (usuario, panels, prioridades mobile, endpoints siguientes).
3. Se centralizó la lógica de panels con `_dashboard_panels_for_user` para evitar inconsistencias entre bootstrap y dashboard.

## Impacto esperado
- Menor tiempo percibido al entrar: frontend puede renderizar shell sin esperar datasets pesados.
- Menor riesgo de duplicar lógica de navegación por rol.

## Próximos pasos sugeridos
- Instrumentar tiempos por etapa (`login_db_ms`, `tenant_attach_ms`, `jwt_ms`).
- Exponer `X-Request-Id` en login/bootstrap y medir percentiles P50/P95.
