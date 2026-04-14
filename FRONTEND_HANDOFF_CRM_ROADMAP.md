# Frontend handoff — ejecución CRM omnicanal (alineado a backend)

> Documento de trabajo para equipo frontend (admin + portal + widget), diseñado para evitar drift con backend.

## 1) Contexto y objetivo

Backend quedó preparado para una estrategia más segura en runtime:

- El bootstrap automático de esquema (`db.create_all`) y tenant init ahora está **desactivado por defecto en producción**.
- Se habilita solo por flags (`ENABLE_RUNTIME_SCHEMA_SYNC`, `ENABLE_RUNTIME_TENANT_INIT`) para entornos de dev/diagnóstico.
- La expectativa operativa es: migraciones explícitas + contratos API estables.

**Objetivo frontend:** avanzar en UX omnicanal sin depender de comportamientos “mágicos” del backend.

---

## 2) Entregables frontend (prioridad alta)

## FE-01 · Canonical routing por tenant

### Qué implementar
- Definir un único path canónico para tenant-facing flows (recomendado: `/t/:tenantSlug/*`).
- Mantener prefijos legacy (`/market`, `/tenant`, `/municipio`, `/pyme`, etc.) solo como redirects client-side hacia canónico.

### Criterios de aceptación
- Deep links compartidos por WhatsApp siempre aterrizan en la ruta canónica.
- Analytics de pantalla (`screen_name`) usa nombres únicos y estables.
- Documentar tabla de redirecciones (legacy -> canónico).

### Riesgo que evita
- Fragmentación de navegación y métricas duplicadas por múltiples rutas equivalentes.

---

## FE-02 · Hardening de TypeScript (modo gradual)

### Qué implementar
- Activar `strict` progresivo por carpetas/módulos críticos:
  1. `src/api/*`
  2. `src/context/*`
  3. `src/pages/market/*`
  4. `src/pages/portal/*`

### Criterios de aceptación
- CI falla ante errores de tipos en módulos bajo alcance.
- No usar `any` salvo excepciones justificadas (con comentario y ticket).
- Reducir warnings por sprint con “type budget”.

### Riesgo que evita
- Bugs runtime por payloads ambiguos y contratos implícitos.

---

## FE-03 · Contrato de identidad omnicanal en requests

### Qué implementar
- En cliente web/widget agregar propagación de headers:
  - `X-Contact-Key`
  - `X-Conversation-Id` (si existe contexto WhatsApp/handoff)
- Centralizar en wrapper de fetch/axios para no duplicar lógica.

### Criterios de aceptación
- 100% requests críticas (market, claims, surveys, portal dashboard) envían `X-Contact-Key`.
- Cuando hay handoff desde WhatsApp, requests relevantes incluyen `X-Conversation-Id`.

### Riesgo que evita
- Pérdida de continuidad entre canales y funnels incompletos.

---

## FE-04 · Checkout E2E resiliente

### Qué implementar
- Orquestador de checkout con estados explícitos:
  - `idle`, `validating`, `creating_order`, `awaiting_payment`, `success`, `error`.
- Pantallas de reintento y recuperación de sesión/carro.

### Criterios de aceptación
- Usuario puede retomar checkout tras refresh/reingreso.
- Errores de API muestran CTA accionable (reintentar, volver al carrito, soporte).
- Se emiten eventos analytics por cada transición de estado.

### Riesgo que evita
- Abandono por estados inconsistentes y pérdida de sesión.

---

## FE-05 · Portal app separada (build aislado)

### Qué implementar
- Separar `portal-app` de `admin-app` a nivel build (sin forzar split de repositorio ahora).
- Mantener widget con entrypoint independiente.
- Definir manifest/scope PWA específico para portal.

### Criterios de aceptación
- Portal se despliega con bundle propio y sin cargar rutas/admin code innecesario.
- TTI y tamaño de bundle del portal mejoran frente al baseline.
- Installability PWA verificada para URL canónica del portal.

### Riesgo que evita
- UX pesada y percepción de “app mezclada”.

---

## FE-06 · Encuestas: contrato único sin fallback ambiguo

### Qué implementar
- Consumir un endpoint canónico de encuestas públicas (v1) y remover fallback en cascada cuando backend v1 esté activo.
- Normalizar shape de datos (tipos TS compartidos con capa API).

### Criterios de aceptación
- El listado público de encuestas funciona con un solo contrato estable.
- Alertar explícitamente cuando el backend devuelve shape inválido (telemetría + mensaje controlado).

### Riesgo que evita
- Comportamientos distintos según path/respuesta legacy.

---

## FE-07 · RBAC/capabilities consistente con backend

### Qué implementar
- Mantener guards por `requiredCapabilities`, pero agregar fallback UX cuando backend rechaza por permisos:
  - pantalla 403 usable,
  - CTA para solicitar acceso,
  - registro de intento denegado (analytics).

### Criterios de aceptación
- No hay pantallas “rotas” por denegación de permisos.
- Cada denegación queda trazada por capability + pantalla + tenant.

### Riesgo que evita
- Fricción operativa y tickets internos de acceso sin contexto.

---

## 3) Contratos frontend/backend a congelar (interfaz mínima)

## Headers estándar
- `X-Tenant-Slug` (cuando aplique)
- `X-Contact-Key` (obligatorio en flujos de usuario)
- `X-Conversation-Id` (opcional, recomendado en handoffs)

## Convenciones de respuesta de error
```json
{
  "error": {
    "code": 400,
    "message": "detalle"
  }
}
```

## Convenciones de eventos analytics (mínimo)
- `event_name`
- `screen_name`
- `tenant_id`
- `channel`
- `contact_key`
- `metadata` (objeto libre pero acotado)

---

## 4) Plan de entrega sugerido (4 sprints)

### Sprint 1
- FE-01 (routing canónico base)
- FE-03 (headers de identidad)
- FE-07 (fallback de permisos)

### Sprint 2
- FE-02 (strict en `src/api` y `src/context`)
- FE-06 (contrato canónico encuestas)

### Sprint 3
- FE-04 (checkout state machine + reintentos)

### Sprint 4
- FE-05 (portal build aislado + PWA portal)

---

## 5) Definition of Done transversal

- Todo flujo crítico con telemetría (`screen_view`, `api_error`, `checkout_step`, `permission_denied`).
- Sin `any` nuevo en módulos bajo hardening.
- Documentación actualizada (README frontend + changelog técnico).
- Pruebas mínimas:
  - unitarias de hooks/utilidades,
  - integración de rutas,
  - smoke E2E de portal/market.

---

## 6) Checklist de QA antes de merge

- [ ] Deep links de WhatsApp abren pantalla correcta en ruta canónica.
- [ ] Carrito persiste entre refresh y reingreso.
- [ ] Checkout recupera estado tras error temporal de API.
- [ ] Portal instala como PWA con `start_url` y scope correctos.
- [ ] Errores 4xx/5xx muestran mensajes controlados y medibles.
- [ ] Denegaciones RBAC no rompen navegación.

---

## 7) Notas de coordinación con backend

- Backend prioriza migraciones explícitas; no asumir “auto-create tables” en runtime.
- Si aparece `5xx` en ambientes nuevos, validar primero estado de migraciones (`flask db upgrade`) antes de debug UI.
- Cualquier endpoint nuevo debe salir con contrato versionado y ejemplo de payload para tipado inmediato en frontend.
