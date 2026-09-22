# Chatboc: continuidad entre Chat, Work y Codex

Registro de continuidad del 22 de septiembre de 2026, solicitado por Marcelo.
Su propósito es retomar el mismo producto y las revisiones comprobadas, no crear
otra aplicación ni depender de una supuesta sincronización de historiales.
Actualizar los estados mediante herramientas antes de la próxima escritura.

## Dirección de producto conservada

Chatboc es el SaaS compartido para municipios, gobiernos, colegios, empresas y
pymes. Tierra del Fuego / Agente Conversa se trata como white-label, mientras
Junín conserva su propia organización e integración. La continuidad del plan es
perfil institucional/comercial, rubro, módulos, permisos e integraciones por
organización, con plantillas de WhatsApp trazables y estados reales del proveedor.
Estas son instrucciones de producto, no una afirmación de que todos esos
módulos o las integraciones institucionales ya estén terminados.

No rehacer el stack: backend Flask/SQLAlchemy y frontend React/Vite/TypeScript.
Conservar rutas legacy, aislamiento de tenant y autorización del servidor.
No extender esta tarea a NexID, ObraSaaS u otros proyectos del holding.

## Punto de continuación comprobado

| Repositorio | PR y rama | Implementación aceptada |
| --- | --- | --- |
| `inguillen87/chatbot-backend` | #2794, `fix/survey-deletion-guard-20260922` | `a6b7dd44de3862e4936008e0f0c146333fcb460b` |
| `inguillen87/chatboc-frontend` | #1761, `fix/survey-confirmation-integrity-20260921` | `1e8b8104c79bc8a1391d0076c25167329f8f62d2` |

Ambos PR estaban abiertos, no eran borradores y no estaban fusionados al retomar.
El backend parte de `feat/module-planning-verified-20260920` / `f13cd73f...`;
el frontend parte de `fix/whatsapp-draft-authority-20260921` / `b0c3ba03...`.
Son PR apilados: revisar sus antecesores antes de integrarlos o publicarlos.
No asumir que fusionar uno de estos PR equivale a publicar en `main`.

Este registro y la corrección del documento de aceptación son cambios sólo de
documentación sobre la implementación backend aceptada. Obtener el head actual
de la rama antes de continuar; no usar la tabla para sobrescribir commits
posteriores. El código frontend no se modifica en este corte de continuidad.

La evidencia exacta y sus límites están en `SURVEY_DELETION_POLICY_ACCEPTANCE.md`:
3.313 pruebas frontend, 17 focales backend, 12 HTTP y cuatro recorridos integrados
aprobados. Los ZIP conservados en el contexto se volvieron a contrastar con los
digests de CI y se leyeron sus reportes. No se volvieron a ejecutar las suites.

## Coordinación sin escrituras superpuestas

La fuente compartida de continuidad es GitHub: ramas, commits, documentación,
comentarios, revisiones y resultados de CI. Este archivo no transfiere una
sesión activa de Work/Codex ni importa automáticamente su historial o cambios
locales sin commit. No se ha comprobado ni detenido una ejecución local de Work.

Antes de escribir, leer instrucciones del repositorio, head/base, cambios y
revisiones recientes. Utilizar un único responsable de escritura por rama.
Si otro agente movió el head, reconciliar primero; no usar force-push ni
reemplazar su árbol con un snapshot antiguo. Agrupar cambios por objetivo.
No inventar una sesión iniciada o un trabajo terminado por la mera aceptación
de una solicitud de herramienta.

Codex en GitHub figura configurado para revisión en ambos PR. Los dos P2 antiguos
del frontend aparecen resueltos. La revisión automática visible del frontend se
había realizado sobre `ac6e642`, no sobre la implementación final. Desde este
chat se solicitó una nueva revisión del head `1e8b8104...` en el comentario
`5783756299`, sin escritura de código, merge ni despliegue. Consultar la respuesta
antes de atribuir un resultado a esa revisión.

El P2 backend `discussion_r4067871705` señaló que el documento todavía nombraba
un frontend anterior y dejaba la aceptación del navegador como pendiente. Se
corrige aquí con la pareja exacta, runs, artefactos y alcance realmente ejecutado.

## Bloqueo de publicación observado

La conexión Vercel devolvió cero equipos en `list_teams`. Al consultar los
proyectos del scope `marcelos-projects-c26aa499`, devolvió 403 Forbidden y pidió
reautenticarse con acceso a ese scope; identificó el equipo
`team_BV1xuY6BnEzGanfok8GAyjZv`. No es evidencia de un problema de facturación.
No cambiar planes, no reutilizar credenciales de otra vertical y no intentar
eludir la denegación mediante otra ruta de despliegue.

El comentario Vercel del frontend #1761 marca el candidato `A6bEA5ggSYKZrcGtQRn871K6ZCNd`
como `Ignored / Skipped Deployment`, no `READY`. Ningún éxito de GitHub Actions
se presenta como publicación en `chatboc.ar`. Se requiere una conexión autorizada
al equipo para inspeccionar el candidato y publicar/verificar de forma controlada.

## Próximos cierres, todavía pendientes

1. Revisar la respuesta de Codex del frontend final y cualquier hallazgo nuevo;
   comprobar la corrección documental backend. No repetir los P2 ya resueltos
   sin una regresión demostrable.
2. Auditar la cadena de PR apilados y construir un único candidato coordinado,
   después de resolver el acceso Vercel. Comprobar revisiones servidas y entorno
   de API antes de mover aliases. La publicación incluye prueba autenticada de
   QA con instrumentos desechables y comprobación posterior, no sólo un build.
3. Continuar el plan de autoservicio por organización: perfil, rubro, módulos e
   integraciones, partiendo de lo existente. Cerrar la operación de plantillas
   con sesión autorizada y proveedor correcto antes de afirmar aprobación Meta,
   número conectado o entrega real de WhatsApp.

No cerrar ni borrar encuestas de clientes para probar. No modificar bases de
clientes, números, callbacks, planes o contratos institucionales como efecto de
este registro. No presentar la demo de Agente Conversa como CRM gubernamental
productivo completo o integración WhatsApp aprobada.

## Referencias operativas

- Backend: https://github.com/inguillen87/chatbot-backend/pull/2794
- Frontend: https://github.com/inguillen87/chatboc-frontend/pull/1761
- Revisión solicitada: https://github.com/inguillen87/chatboc-frontend/pull/1761#issuecomment-5783756299
- Aceptación integrada: https://github.com/inguillen87/chatbot-backend/actions/runs/35676839086
- CI frontend: https://github.com/inguillen87/chatboc-frontend/actions/runs/35676754880
