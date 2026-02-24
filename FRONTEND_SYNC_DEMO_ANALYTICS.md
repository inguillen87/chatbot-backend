# Frontend Sync — Demo Login + Analytics + Encuestas (2026-02)

Este documento resume los contratos backend que el frontend debe consumir para evitar pantallas blancas en demo/analytics/encuestas.

## 1) Flujo demo recomendado (sector-first)

### Endpoint
`GET /auth/demo/catalog`

### Campos clave para UI
- `frontend_contract_version`
- `onboarding.default_sector`
- `onboarding.sector_options[]`
- `frontend.demo_selector`
- `frontend.preload_before_login[]`

### Implementación recomendada
1. Renderizar selector de sector (`gobierno` / `empresas`).
2. Si sector=`gobierno`: no pedir rubro, enviar `{"sector":"gobierno"}` a login demo.
3. Si sector=`empresas`: pedir rubro y usar `login_payload` del rubro seleccionado.
4. Ejecutar preloads antes de navegar:
   - `/auth/demo/catalog`
   - `/api/pwa/tenant-info`
   - `/api/pwa/anon-id`

### Login demo
`POST /api/auth/demo`

Payload mínimo:
- Gobierno: `{"sector":"gobierno"}`
- Empresas: `{"sector":"empresas", "rubro":"<key>"}`

Respuesta: usar `tenant_slug`, `tipo_chat`, `sector`, `token`.

---

## 2) Analytics overview

### Endpoint
`GET /api/admin/analytics/overview`

### Compatibilidad
Backend garantiza `totals.total_interactions` aunque el agregador no lo traiga explícito.

### Frontend guardrails
- Leer seguro: `const totalInteractions = payload?.totals?.total_interactions ?? 0`.
- No asumir que toda métrica existe.

---

## 3) Encuestas públicas: comentarios

### Endpoint
`GET /api/public/encuestas/:slug/comentarios`

### Importante
Backend degradará en forma segura si hay drift de DB (ej. columna `report_count` ausente durante despliegue), devolviendo comentarios en lugar de 500.

### Frontend guardrails
- Si endpoint falla, mostrar lista vacía + retry suave.
- No renderizar objetos completos como React children (`React error #31`): serializar siempre strings.

---

## 4) Checklist de release coordinado

1. Deploy backend.
2. Ejecutar migraciones:
   - `flask db upgrade`
3. Verificar endpoint de salud + smoke:
   - `/api/auth/demo/catalog`
   - `/api/admin/analytics/overview?...`
   - `/api/public/encuestas/:slug/comentarios`
4. Deploy frontend.

---

## 5) Nota de operación

Se agregó migración para `enc_comentario.report_count`. Igual, backend mantiene fallback para evitar downtime durante ventanas de migración.
