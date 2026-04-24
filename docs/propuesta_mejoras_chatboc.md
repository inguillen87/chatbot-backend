# Plan Maestro de Mejoras para la Plataforma Chatboc

Este documento traduce el plan estratégico en una hoja de ruta ejecutable para producto, backend, frontend, datos y operaciones.

## Objetivo
Convertir Chatboc en una plataforma SaaS multi-tenant robusta para municipios y empresas, con:
- aislamiento de datos por cliente,
- módulos de catálogo/tickets/encuestas/pagos maduros,
- IA aplicada a operación y crecimiento,
- UX/UI moderna y demostrable comercialmente.

---

## 1) Arquitectura multi-tenant y seguridad de datos (prioridad crítica)

### 1.1 Aislamiento por tenant
- Tenant obligatorio en toda operación de lectura/escritura.
- Resolver tenant por `slug` y propagarlo al contexto de request.
- Aplicar filtros automáticos por `tenant_id` en consultas críticas (catálogo, pedidos, tickets, encuestas, usuarios, métricas).
- Responder **404** ante tenant inexistente/no resoluble para evitar enumeración o fuga.

### 1.2 Resolución de tenant por canal
- **WhatsApp**: mapear número oficial entrante → tenant slug.
- **Widget web**: aceptar `X-Tenant`, `data-tenant` o subdominio.
- Definir precedencia y validación uniforme para evitar inconsistencias entre canales.

### 1.3 Roles y permisos por tenant
- Modelo de roles recomendado: `superadmin`, `admin_tenant`, `agente`, `empleado`, `viewer`.
- Enforzar scope por tenant en JWT/sesión y en cada endpoint sensible.
- Bloquear cross-tenant incluso para usuarios con rol alto no global.

### 1.4 Público vs privado
- Separar rutas y políticas:
  - `/widget/*` (usuarios finales, acceso público/semipúblico controlado).
  - `/admin/*` (panel interno, auth fuerte y permisos).
- Evitar reutilizar handlers ambiguos entre ambos contextos.

### 1.5 Sesión y autenticación
- Mover sesión de panel a cookies `HttpOnly`, `Secure`, `SameSite`.
- Mantener refresh token/renovación silenciosa.
- Preservar tenant en toda navegación para evitar dobles logins o pérdida de contexto.

---

## 2) Catálogo conversacional y checkout multimodal

### 2.1 Modalidades de producto
Agregar `modalidad` en `CatalogoItem`:
- `venta`: precio monetario normal.
- `donacion`: total monetario 0, registra intención/orden sin pasarela.
- `puntos`: precio en puntos (`PTS`) con validación de saldo.

### 2.2 Checkout adaptativo
- Separar totales por tipo de cobro en un mismo pedido.
- Reglas:
  - Ítems monetarios → pasarela.
  - Ítems donación → cierre directo.
  - Ítems puntos → débito atómico de saldo.
- En pedidos mixtos, mostrar desglose claro al usuario.

### 2.3 Marketplace conversacional
- Búsqueda por categoría/filtros desde chat.
- Respuestas enriquecidas con imagen, descripción y precio/modalidad.
- Flujo guiado: seguir explorando vs ir a checkout.

### 2.4 Recomendaciones
- Fase 1: reglas (kits, complementos, estacionalidad, upsell).
- Fase 2: recomendaciones por co-ocurrencia e historial.

---

## 3) Encuestas y participación con recompensas

### 3.1 Motor de encuestas
- Tipos de pregunta: opción múltiple, escala, texto libre.
- Distribución por chatbot, web y email.

### 3.2 Integración conversacional
- Disparadores automáticos (p.ej. ticket resuelto → encuesta de satisfacción).
- Recolección de respuestas dentro del chat.

### 3.3 Sistema de puntos
- Servicio de recompensas con ledger por usuario/tenant.
- Eventos configurables de acreditación: encuestas, tickets, participación.
- Canje en checkout de productos en modalidad `puntos`.

### 3.4 Analítica
- Resultados en tiempo real por encuesta.
- Resumen de respuestas abiertas con IA para reporte ejecutivo.

---

## 4) Tickets omnicanal y operación asistida por IA

### 4.1 Flujo operativo estándar
Estados: `nuevo`, `asignado`, `en_progreso`, `resuelto`, `cerrado`.
- Asignación por agente/área.
- Notificaciones ante cambios de estado.

### 4.2 Visibilidad y permisos
- Estrictamente por tenant y rol.
- Subroles opcionales (coordinador/agente).

### 4.3 UX de gestión
- Vista Kanban/lista con filtros por estado, categoría, prioridad.
- Historial de conversación, adjuntos y mapa cuando aplique.

### 4.4 IA operacional
- Categorización y sugerencia de asignación automática.
- Resumen automático de tickets extensos.

### 4.5 Omnicanalidad
- Creación de tickets desde chat, web, email y redes.
- Respuesta por canal de origen cuando sea posible.

---

## 5) Pagos con MercadoPago por tenant

### 5.1 Checkout monetario
- Crear preferencia con ítems y total.
- Guardar pedido en estado `pendiente_pago` con `preference_id`.
- Entregar `init_point`/link/QR según canal.

### 5.2 Webhooks y conciliación
- Procesar eventos de pago y validar estado `approved`.
- Actualizar pedido: `pagado`, `rechazado`, `expirado`.
- Tolerancia a reintentos/eventos fuera de orden.

### 5.3 Credenciales por cliente
- Configuración de credenciales MP por tenant.
- Almacenamiento seguro (encriptado en DB).

### 5.4 Flujos especiales
- Donaciones (sin pasarela).
- Puntos (sin pasarela, con deducción de saldo).

---

## 6) Experiencia demo comercial

### 6.1 Acceso demo
- Botón “Ingresar como demo” en login.
- Tenant demo aislado con permisos controlados.

### 6.2 Datos precargados
- Catálogo, tickets, encuestas y métricas de ejemplo de alta calidad.

### 6.3 Variantes de demo
- Perfil Municipio y perfil E-commerce (opcional por tenant demo separado).

### 6.4 Restricciones demo
- Simular integraciones sensibles (pagos/notificaciones reales deshabilitados).
- Mostrar etiqueta clara de entorno de prueba.

### 6.5 Reinicio de datos
- Reset programado para mantener consistencia entre sesiones.

---

## 7) Landing y captación de leads

- Mensaje de valor orientado a municipios y empresas.
- Secciones de funcionalidades clave (catálogo, tickets, encuestas, IA, analíticas).
- Demo visible (video/GIF/chat embebido).
- Casos de uso/testimonios.
- CTAs claros: demo, contacto, prueba.
- SEO técnico + accesibilidad.

---

## 8) Analíticas, reportes e insights automáticos

- Dashboard con KPIs semanales/mensuales/anuales.
- Gráficos de tendencia y comparación por categorías/áreas.
- Heatmap temporal de actividad (día/hora).
- Exportables CSV/PDF.
- Alertas automáticas por desvíos (picos de tickets, baja de satisfacción).
- Métricas del bot: tasa de resolución, fallback, tiempo de respuesta.

---

## 9) IA avanzada y automatizaciones

- Resúmenes ejecutivos periódicos con IA.
- Asistente de consultas para admins sobre datos del tenant.
- OCR/document AI para convertir archivos (Excel/PDF/imagen) en pedidos.
- Recomendaciones inteligentes (reglas → personalización progresiva).
- Automatización de flujos conversacionales con validación backend estricta.

---

## 10) Personalización e integraciones self-service

- Branding por tenant: logo, colores, nombre/avatar del bot.
- Configuración de mensajes y tono del chatbot.
- Integraciones por panel (copiar/pegar credenciales):
  - MercadoPago,
  - WhatsApp/Twilio,
  - Google Analytics/Tag Manager,
  - Google Maps,
  - SMTP/SendGrid.
- Token de integración por tenant con opción de regeneración segura.

---

## 11) UX/UI y estabilidad de frontend

- Rediseño responsive del panel y widget.
- Consistencia visual y navegación clara.
- Error boundaries por módulo crítico.
- Feedback de acciones (loading, toasts, errores descriptivos).
- Flujo de auth robusto (login/logout/retorno a contexto).
- Optimización de flujos críticos (compra, ticket, encuesta).
- Modo oscuro, i18n y mejoras de accesibilidad.

---

## 12) Robustez, seguridad y escalabilidad operativa

- Cobertura de auth/roles en endpoints críticos.
- Configuración CORS segura y explícita por origen.
- Índices y paginación en listados grandes.
- Servicios modulares (catálogo, recompensas, OCR, checkout).
- Tareas asíncronas para procesos pesados (Celery/Redis).
- Escalado horizontal con cache/sesión compartida.
- Rate limiting para APIs públicas.
- Observabilidad: logs estructurados, errores, métricas y alertas.

---

## Fases sugeridas de implementación

### Fase 0 (Hardening base)
- Multi-tenant estricto + roles + auth + CORS.
- Separación público/privado y sesión robusta.

### Fase 1 (Quick wins de negocio)
- Catálogo multimodal + checkout adaptativo.
- MercadoPago completo + webhooks.
- Demo accesible con datos precargados.
- Landing enfocada en leads.

### Fase 2 (Operación y valor continuo)
- Encuestas + puntos + dashboards + exportables.
- Tickets con UX renovada y automatizaciones IA iniciales.

### Fase 3 (IA avanzada y escala)
- OCR de pedidos, recomendaciones avanzadas, asistente analítico.
- Asincronía extensa, monitoreo avanzado y optimización continua.

---

## Criterios de aceptación globales

- Cero accesos cross-tenant en pruebas de seguridad.
- Cobertura de permisos en endpoints críticos.
- Flujo de pago estable con reconciliación por webhook.
- Flujo de puntos atómico y auditable.
- Demo funcional de punta a punta para ventas.
- KPIs de operación visibles en dashboard.

Este plan prioriza primero seguridad y arquitectura, luego crecimiento comercial, y finalmente sofisticación de IA; así se maximiza impacto sin comprometer estabilidad.
