# Auditoría full-stack y roadmap para chatboc.ar

## 1) Diagnóstico ejecutivo

La plataforma ya tiene capacidades de producto avanzadas para un SaaS CRM omnicanal:

- Multi-tenant operativo.
- Widget embebible + PWA + portal de usuario.
- Marketplace con carrito/checkout.
- Encuestas y votaciones con segmentación geo.
- Analytics hub con heatmaps, funnel de WhatsApp y panel en tiempo real.

El principal límite actual no es la falta de features, sino la **deuda estructural** que impacta velocidad, UX y confiabilidad.

### Bloqueantes críticos (prioridad alta)

1. **Acoplamiento backend + capas legacy/compatibilidad**
   - Multiplicidad de rutas y aliases con riesgo de drift contractual.
   - Aumenta costo de pruebas, regresiones y onboarding técnico.
2. **Estrategia de esquema insegura en runtime**
   - `db.create_all()` en startup y bootstrap implícito en ejecución normal.
   - Riesgo de esquemas parciales y auditoría débil de cambios.
3. **Frontend sobre-flexibilizado y tipado laxo**
   - Ruteo con múltiples prefijos canónicos para un mismo recurso.
   - `TypeScript strict` no plenamente exigido en build efectivo.

---

## 2) Visión objetivo (12 meses)

Convertir chatboc.ar en un CRM SaaS integral con estos pilares:

- **Identidad omnicanal única** por contacto/conversación.
- **Contratos API estables** con versionado explícito.
- **RBAC/ABAC tenant-scoped** con auditoría completa.
- **Superficies desacopladas**: Admin, Portal Usuario, Widget/Chat.
- **Métricas trazables E2E** por funnel, canal, categoría y zona.

---

## 3) Principios de arquitectura

1. **Una sola fuente de verdad por dominio**
   - Un endpoint canónico por caso de uso.
   - Legacy solo con deprecación, fecha de retiro y redirecciones controladas.

2. **Compatibilidad explícita, no implícita**
   - Evitar duplicación de blueprints o aliases sin observabilidad.

3. **Contrato primero, resiliencia después**
   - El frontend deja de “adivinar payloads” y consume contratos versionados.

4. **Seguridad multi-tenant como invariante**
   - Todo query y cache debe ser tenant-scoped por diseño.

5. **Observabilidad por evento canónico**
   - Misma taxonomía de eventos para WhatsApp, widget, portal, market y encuestas.

---

## 4) Plan de ejecución por fases

## Fase 0 (0-30 días): estabilización mínima

### Objetivos
- Reducir riesgo operacional inmediato.
- Congelar entropía de rutas/contratos.

### Entregables
- Eliminar `db.create_all()` de startup de producción.
- Mover bootstrap demo/seed a comandos CLI explícitos.
- Definir API canónica inicial: `/api/v1/*`.
- Inventario de endpoints legacy + matriz de deprecación.
- Activar checks de tipos más estrictos en CI para frontend (modo progresivo).

### KPIs
- 0 ejecuciones de migración implícita en runtime.
- 100% endpoints nuevos bajo `/api/v1/*`.
- Error rate de rutas legacy monitoreado.

## Fase 1 (31-90 días): contratos e identidad omnicanal

### Objetivos
- Unificar continuidad de usuario entre canales.
- Consolidar capa de eventos y analytics.

### Entregables
- Modelo canónico de identidad:
  - `contact_key`.
  - `conversation_id` (cuando aplique).
  - `phone_e164` normalizado.
  - fallback `anon_id` web.
- Headers estándar en web/widget: `X-Conversation-Id`, `X-Contact-Key`.
- `AnalyticsEventV2` con campos obligatorios:
  - `tenant_id`, `contact_key`, `channel`, `category`, `geo`, `screen_name`, `event_name`.
- Correlación WhatsApp ↔ portal/market con IDs de botones/listas persistidos.

### KPIs
- >90% de eventos críticos con `contact_key`.
- Funnel WhatsApp con trazabilidad completa por tenant.

## Fase 2 (91-180 días): operación CRM (roles, asignación, SLA)

### Objetivos
- Habilitar operación real de equipos por tenant.
- Mejorar tiempo de respuesta y distribución de carga.

### Entregables
- RBAC por capabilities atómicas (enforced backend).
- ABAC liviano para tickets por:
  - categoría,
  - zona,
  - estado.
- Motor de asignación determinístico:
  - `(categoria, zona, carga, SLA) -> agente`.
- Cola “sin asignar” + escalamiento + override manual.
- Auditoría de asignaciones y reasignaciones.

### KPIs
- Disminución del tiempo de primera respuesta.
- Reducción de backlog sin asignar.
- 100% cambios de owner con trazabilidad.

## Fase 3 (181-270 días): experiencia de producto integrada

### Objetivos
- Lograr percepción de “sistema armonioso” y no “módulos sueltos”.

### Entregables
- Separación de superficies frontend en builds/apps:
  - `admin-app`.
  - `portal-app`.
  - widget/iframe como entrypoint específico.
- URL canónica de tenant para portal (sin prefijos ambiguos).
- Checkout orquestado E2E con estados/reintentos robustos.
- Contratos de encuestas y market unificados (sin fallbacks ambiguos en cliente).

### KPIs
- Mejora en conversión de checkout.
- Menor tasa de errores de navegación/deep links.
- Mejor retención del portal instalado como PWA.

## Fase 4 (271-365 días): optimización y escala

### Objetivos
- Productividad operativa + predictibilidad de crecimiento.

### Entregables
- Catálogo admin avanzado (variantes, promociones, importación, preview omnicanal).
- Playbooks de automatización por eventos (journeys CRM).
- Forecast de demanda por categoría/zona y recomendaciones de staffing.
- SLOs por dominio (tickets, orders, survey ingest, realtime).

### KPIs
- Aumento de conversión omnicanal.
- Mejora de NPS/CSAT.
- Cumplimiento de SLOs por tenant.

---

## 5) Iniciativas transversales

### 5.1 Gobernanza de contratos
- OpenAPI por dominio con versionado semántico.
- Pruebas de contrato backend/frontend en CI.
- Política de deprecación (aviso, ventana, retiro).

### 5.2 Calidad y testing
- Pirámide de pruebas: unitarias, integración, E2E.
- Smoke tests por tenant y por canal (WhatsApp, widget, portal).
- Mocks consistentes para proveedores externos.

### 5.3 Seguridad
- Validación fuerte de payloads en backend.
- Revisión de aislamiento tenant-scoped (queries/caches/storage).
- Auditoría de accesos administrativos y cambios críticos.

### 5.4 Datos y analítica
- Taxonomía única de eventos.
- Datasets por dominio con ownership explícito.
- Tableros operativos con alertas accionables (no solo visuales).

---

## 6) Riesgos y mitigaciones

- **Riesgo:** romper integraciones existentes al unificar rutas.
  - **Mitigación:** capa de compatibilidad temporal con métricas de uso y cutover por fases.

- **Riesgo:** fricción interna por migrar a TS strict.
  - **Mitigación:** adopción gradual por carpetas y “type budget” semanal.

- **Riesgo:** baja calidad de datos para identidad omnicanal.
  - **Mitigación:** normalización centralizada + observabilidad de campos faltantes.

- **Riesgo:** sobrecarga operativa al introducir RBAC/ABAC.
  - **Mitigación:** plantillas de roles por vertical + asistentes de configuración.

---

## 7) Backlog priorizado (top 12)

1. Quitar `db.create_all()` de producción.
2. Crear comando CLI de bootstrap demo/tenant seed.
3. Congelar convención de rutas `/api/v1/*`.
4. Implementar mapa de deprecación de endpoints legacy.
5. Definir esquema `ContactIdentity` y persistencia.
6. Estandarizar headers `X-Conversation-Id` y `X-Contact-Key`.
7. Publicar contrato `AnalyticsEventV2`.
8. Forzar capabilities backend en endpoints críticos.
9. Implementar ABAC de tickets (categoría/zona/estado).
10. Motor de auto-asignación + cola + override.
11. Separar builds frontend en admin/portal.
12. Orquestador de checkout E2E con observabilidad.

---

## 8) Resultado esperado

Si se ejecuta este roadmap, chatboc.ar puede evolucionar de una plataforma con muchas piezas potentes a un **CRM omnicanal integral, consistente y escalable**, con continuidad real de identidad, operación multi-tenant robusta y experiencia de producto de nivel enterprise.
