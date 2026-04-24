# AUDIT LOGIN PERFORMANCE (Backend)

## Causa raíz por endpoint/servicio

### `POST /auth/login` (`routes/auth.py`)
- El login tenía trabajo bloqueante mezclado con bootstrap inicial.
- Faltaba trazabilidad fina por etapa para saber si el cuello era DB, password, tenant o firma JWT.
- El frontend podía esperar cargas no críticas sin contrato shell-first suficientemente explícito.

### `GET /auth/session/bootstrap` (`routes/auth.py`)
- Existía necesidad de un contrato mínimo para entrar rápido al shell y diferir fetches pesados.
- Sin request-id/timing consistente en todos los pasos, era difícil correlacionar lentitud percibida.

## Mejoras aplicadas
1. Login shell-first con bloque `bootstrap` mínimo y endpoint recomendado `/auth/session/bootstrap`.
2. Tiempos por etapa en login (`db_lookup_ms`, `password_verify_ms`, `tenant_resolve_ms`, `token_sign_ms`, `total_ms`).
3. Trazabilidad estándar por request con `X-Request-Id` y `Server-Timing`.
4. Migraciones de datos anónimos en modo diferido (thread) para no bloquear respuesta inicial.

## Métricas mínimas disponibles
- `timing.*` en payload login.
- `Server-Timing: auth_total`.
- logs estructurados `[auth.login]` con `request_id` y `total_ms`.

## Próximo paso recomendado
- Exportar percentiles P50/P95 desde logs (o APM) por etapa de login para validar regresiones por release.
