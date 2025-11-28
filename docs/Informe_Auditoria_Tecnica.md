# Informe de Auditoría Técnica – Plataforma Chatboc

El presente documento consolida los hallazgos de la auditoría técnica realizada sobre el frontend y backend de la plataforma Chatboc, destacando causas raíz, impacto, y acciones recomendadas. El análisis cubre errores observados, arquitectura multi-tenant, aspectos de seguridad y mejoras sugeridas.

## 1. Errores detectados en el Frontend

### 1.1 Errores de React (Error #300 minificado y manejo de ErrorBoundary)
- **Causa raíz:** El error “Minified React error #300” aparece cuando la cantidad de hooks ejecutados difiere entre renderizados, generalmente por retornos condicionales que saltan llamadas de hooks. Se observaron pantallas en blanco al acceder directamente a rutas como `/chat`, lo que sugiere flujos de renderizado inconsistentes. Además, en algunos módulos faltan *Error Boundaries* locales, causando bloqueos completos al fallar un componente.
- **Impacto:** Renderizado abortado y pantallas en blanco sin retroalimentación visible; fallos en módulos como Tickets o Encuestas afectan la usabilidad y dificultan el diagnóstico.
- **Solución recomendada:** Garantizar orden estable de hooks (evitar retornos prematuros), refactorizar componentes con lógica condicional y añadir *Error Boundaries* específicos en módulos críticos para mostrar mensajes de fallback en lugar de fallos silenciosos.

### 1.2 Problemas de CORS en peticiones `fetch` al backend
- **Causa raíz:** Peticiones desde el widget omitían el *tenant* en la URL o los encabezados, y el backend no devolvía cabeceras CORS completas. Rutas como `/productos` sin contexto producían bloqueos del navegador por política de mismo origen.
- **Impacto:** Catálogo y carrito no funcionaban en el widget; el navegador abortaba solicitudes y la interfaz quedaba sin respuesta.
- **Solución recomendada:** Incluir siempre el identificador del *tenant* en las rutas públicas y configurar CORS en el backend con `Access-Control-Allow-Origin`, encabezados personalizados (p. ej., `X-Tenant`, `X-Widget-Token`) y métodos permitidos.

### 1.3 Malas prácticas en el manejo de login y sesión en el widget
- **Causa raíz:** El widget dependía de `localStorage` y parámetros de URL para tokens, sin capa adicional de seguridad ni flujo unificado con el panel principal; se mezclaban tokens anónimos y JWT sin un patrón consistente.
- **Impacto:** Riesgos de exposición de tokens, doble inicio de sesión entre panel y widget, pérdida de sesión al refrescar y acciones inconsistentes (p. ej., canjes que fallan sin explicación).
- **Solución recomendada:** Unificar la autenticación del widget (idealmente con cookies seguras o `postMessage` para SSO), usar `useUser`/`refreshUser` para propagar estado, sincronizar login/logout y documentar la distinción entre `authToken` y `chatAuthToken`.

### 1.4 Fallos de UX en el flujo del carrito y navegación con usuarios logueados
- **Causa raíz:** El checkout permitía operaciones anónimas incompletas (donaciones $0, canjes sin login) y la UI no actualizaba estado tras autenticación.
- **Impacto:** Canjes fallidos sin feedback, pedidos sin datos de contacto y confusión al no reflejar sesión activa.
- **Solución recomendada:** Exigir autenticación para acciones sensibles, recolectar datos mínimos en checkout anónimo, rehidratar el carrito post-login y actualizar la UI inmediatamente al autenticarse.

### 1.5 Problemas de validación de usuario y persistencia de sesión
- **Causa raíz:** Registro sin verificación de email/teléfono, ausencia de *refresh tokens* y expiraciones silenciosas; `safeLocalStorage.user` podía quedar desincronizado.
- **Impacto:** Riesgo de cuentas falsas/spam, cierres de sesión inesperados y estado de usuario inconsistente.
- **Solución recomendada:** Implementar verificación de correo/SMS, tokens de refresco o renovaciones silenciosas, mensajes claros al expirar sesión y asegurar actualización de `user` en `localStorage` tras cambios relevantes.

## 2. Errores detectados en el Backend

### 2.1 API devolviendo 400 (Bad Request) por falta de `tenantId`
- **Causa raíz:** Endpoints que requieren *tenant* fallaban si no recibían `slug`/`id`; abortaban con 400 genérico.
- **Impacto:** Peticiones legítimas (ej. catálogo) devolvían 400/404, bloqueando funcionalidades.
- **Solución recomendada:** Hacer obligatorio el contexto *tenant* en todas las llamadas, mejorar mensajes de error y centralizar la resolución de *tenant* (headers, query, dominio) via middleware.

### 2.2 Rutas `/productos`, `/carrito`, `/assign`/`/asignar` respondiendo con 400, 405 o 500
- **Causa raíz:** Rutas sin contexto de *tenant*, duplicidad de endpoints (idioma/método) y control de errores insuficiente.
- **Impacto:** Catálogo/carrito inaccesibles, asignación de tickets fallida y posibles errores 500 en estados no manejados.
- **Solución recomendada:** Normalizar URLs (públicas bajo `/api/pwa/public/<slug>/...`, admin bajo `/api/admin/...`), elegir una ruta/método único para asignación de tickets y manejar errores con respuestas descriptivas.

### 2.3 Inconsistencia en definición y asignación de usuarios vs. agentes
- **Causa raíz:** Un único modelo `User` con roles mezclados, sin procesos claros para promover/degradar ni filtros estrictos por *tenant*.
- **Impacto:** Riesgos de acceso indebido, tickets huérfanos al eliminar agentes y ausencia de permisos granulares.
- **Solución recomendada:** Definir roles y permisos formales, vincular empleados a su *tenant* y categorías, implementar cambio de rol desde el panel y reasignación de tickets al eliminar agentes.

### 2.4 Uso inadecuado del método HTTP
- **Causa raíz:** Desalineación entre frontend y backend respecto al método esperado (p. ej., POST vs. PUT), provocando 405.
- **Impacto:** Acciones que no se ejecutan y tiempo de depuración elevado.
- **Solución recomendada:** Alinear contratos de API, documentar métodos permitidos, y añadir respuestas 405 con mensajes claros o alias temporales solo si es necesario.

## 3. Arquitectura multi-tenant

### 3.1 Falta de aislamiento por *tenant*
- **Causa raíz:** Migración incompleta al modelo multi-tenant; consultas sin filtrar `tenant_id`.
- **Impacto:** Riesgo de fuga de datos entre clientes y respuestas mezcladas.
- **Solución recomendada:** Etiquetar todas las entidades con `tenant_id`, aplicar filtros automáticos usando el contexto `g.tenant_profile` y verificar resoluciones de *tenant* en endpoints públicos y admin.

### 3.2 Empleados sin gestión de permisos ni scope limitado
- **Causa raíz:** Todos los empleados tienen el mismo alcance dentro del *tenant*; no hay subroles ni asignación por categorías.
- **Impacto:** Sobrecarga operativa y riesgos internos al no poder restringir módulos o categorías.
- **Solución recomendada:** Introducir subroles (coordinador/agente), asociar empleados a categorías/equipos, y condicionar accesos en frontend/backend según permisos definidos.

### 3.3 Mezcla de lógica de usuarios finales y de agentes/admins
- **Causa raíz:** Endpoints y vistas compartidas para roles dispares; mismo flujo de login para ciudadanos y operadores.
- **Impacto:** Complejidad y errores lógicos; riesgo de acceso cruzado o UI incoherente.
- **Solución recomendada:** Separar rutas y servicios por contexto (público vs. administración), definir políticas de sesión diferenciadas, y asegurar que el frontend mantenga interfaces aisladas por rol.

## 4. Seguridad y errores estructurales

### 4.1 Faltan cabeceras de CORS configuradas
- **Causa raíz:** Configuración CORS incompleta (orígenes, headers personalizados, credenciales) en el backend.
- **Impacto:** Bloqueo de peticiones cross-domain (widget → API) y fallos de autenticación.
- **Solución recomendada:** Configurar `Access-Control-Allow-Origin` para dominios permitidos, habilitar credenciales, declarar encabezados/métodos usados y registrar Flask-CORS antes de los blueprints.

### 4.2 No se valida adecuadamente el `auth_token` en todas las rutas
- **Causa raíz:** Endpoints sensibles sin decoradores de autenticación o con validaciones incompletas; flujos anónimos sin controles adicionales.
- **Impacto:** Accesos no autorizados, pedidos sin dueño y riesgo de fuga/modificación de datos.
- **Solución recomendada:** Auditar y proteger todas las rutas que requieren autenticación, usar decoradores unificados (`token_requerido`, `anon_o_token_requerido`), y retornar 401/403 claros cuando falte autorización.

### 4.3 El login del widget falla sin feedback detallado
- **Causa raíz:** Manejo incompleto de errores en el formulario de login del widget; mensajes genéricos o inexistentes.
- **Impacto:** Usuarios sin claridad al fallar login, abandono del flujo y mayor carga de soporte.
- **Solución recomendada:** Capturar códigos de error, mostrar mensajes específicos (“Credenciales inválidas”, “Servicio no disponible”) y mantener coherencia con el login del sitio principal.

## 5. Sugerencias de mejora

- **Sistema de roles y acceso:** Formalizar roles (admin, empleados con subroles, usuarios finales), permisos granulares y gestión desde el panel; reforzar checks en backend.
- **Rutas y métodos normalizados:** Auditar endpoints para exigir *tenant*, definir método HTTP correcto, eliminar duplicados y documentar contratos.
- **Modularización del frontend:** Dividir componentes complejos, añadir *Error Boundaries* locales y usar reglas de hooks/linters para prevenir errores de renderizado.
- **Persistencia y UX de autenticación:** Implementar *refresh tokens* o renovaciones silenciosas, mensajes de expiración, rehidratación post-login y sincronización de estado entre panel y widget.
- **Flujo de widget (carrito/encuestas):** Guiar al usuario a login cuando se requiera, conservar contexto de acciones tras autenticarse, solicitar datos mínimos en checkout anónimo y fusionar carritos anónimo↔usuario.

---

Este informe sintetiza los hallazgos y las acciones recomendadas para estabilizar, asegurar y mejorar la plataforma Chatboc en su evolución hacia un SaaS multi-tenant robusto.
