# Chatboc: continuidad entre Chat, Work y Codex

Registro del 22 de septiembre de 2026, solicitado por Marcelo. Retomar el mismo
producto y sus revisiones comprobadas, sin crear otra aplicación ni depender de
una supuesta sincronización de historiales. Consultar estados actuales antes de escribir.

## Dirección conservada

Chatboc es el SaaS compartido para municipios, gobiernos, colegios, empresas y
pymes. Tierra del Fuego / Agente Conversa se trata como white-label; Junín conserva
su organización e integración. Continuar perfil institucional/comercial, rubro,
módulos, permisos e integraciones por organización, con plantillas WhatsApp
trazables y estados reales del proveedor. Es dirección de producto, no una
certificación de que todos esos módulos e integraciones estén terminados.

No rehacer Flask/SQLAlchemy ni React/Vite/TypeScript. Conservar legacy, aislamiento
de tenant y autorización del servidor. No extender a NexID, ObraSaaS u otras verticales.

## Implementaciones aceptadas

| Repositorio | PR y rama | Implementación aceptada |
| --- | --- | --- |
| `inguillen87/chatbot-backend` | #2794, `fix/survey-deletion-guard-20260922` | `a6b7dd44de3862e4936008e0f0c146333fcb460b` |
| `inguillen87/chatboc-frontend` | #1761, `fix/survey-confirmation-integrity-20260921` | `1e8b8104c79bc8a1391d0076c25167329f8f62d2` |

Ambos PR estaban abiertos, listos para revisión y no fusionados al retomar.
Backend parte de `feat/module-planning-verified-20260920` / `f13cd73f...`;
frontend de `fix/whatsapp-draft-authority-20260921` / `b0c3ba03...`.
Son PR apilados: revisar antecesores antes de integrar/publicar; fusionarlos no
equivale a publicar `main`.

El corte de continuidad modifica sólo dos documentos backend; no el frontend.
Obtener el head actual antes de continuar: los SHA de la tabla identifican
implementaciones aceptadas, no autorizan sobrescribir commits posteriores.
El registro de evidencia es `SURVEY_DELETION_POLICY_ACCEPTANCE.md`: 3.313 pruebas
frontend, 17 focales, 12 HTTP y cuatro recorridos integrados aprobados para su
pareja exacta. Al retomar se releyeron los reportes y se verificaron los digests,
no se ejecutaron manualmente las suites. El push documental activó CI automática
(run `35781755684`); comprobar por SHA cualquier ejecución posterior y no confundirla
con la evidencia histórica.

## Coordinación entre agentes

GitHub —commits, PR, documentos, comentarios y CI— conserva el estado compartido.
Este archivo no transfiere sesiones activas de Work/Codex, importa historiales o
recupera cambios locales sin commit. No se comprobó ni detuvo una ejecución local de Work.
Antes de escribir, leer instrucciones del repositorio, head/base, diff y revisiones.
Un responsable de escritura por rama. Si otro agente movió el head, reconciliar:
no force-push, no reemplazar su árbol por un snapshot antiguo. Agrupar por objetivo.

Codex está configurado para revisión en ambos PR. Los dos P2 antiguos del frontend
figuran resueltos, pero la revisión automática visible fue sobre `ac6e642`, no
sobre `1e8b8104`. Se solicitó revisar el head final en el comentario `5783756299`,
sin escribir, crear ramas, fusionar ni desplegar. El bot respondió en
`5783758332` que se alcanzó el **límite de uso para code reviews**. Por tanto,
la solicitud está bloqueada por cuota, no corriendo ni aprobada. No se compraron
créditos, no se ampliaron planes y no se reintenta automáticamente. Este resultado
no demuestra que todas las modalidades de Codex estén agotadas; sólo la revisión solicitada.

El P2 backend `discussion_r4067871705` señalaba frontend viejo y navegador pendiente
en el documento. Se corrigió en `a6d5bcd77...` con pareja, runs, artefactos y alcance
exactos, y se respondió/cerró el hilo tras releer el archivo. La posterior precisión
sobre CI automática no cambia runtime ni el resultado histórico de aceptación.

## Acceso y publicación

Vercel devolvió cero equipos en `list_teams` y 403 Forbidden al consultar
`marcelos-projects-c26aa499`, solicitando reautenticación al scope. Identificó
`team_BV1xuY6BnEzGanfok8GAyjZv`. No es evidencia de un problema de facturación.
No cambiar planes, reutilizar credenciales de otra vertical ni eludir la denegación.

El comentario Vercel del frontend #1761 marca `A6bEA5ggSYKZrcGtQRn871K6ZCNd`
como `Ignored / Skipped Deployment`, no `READY`. El éxito de Actions no prueba
publicación en `chatboc.ar`. Hace falta una conexión autorizada al equipo para
inspeccionar/publicar/verificar el candidato de forma controlada.

## Próximos cierres pendientes

1. Completar revisión del frontend final. La solicitud Codex está bloqueada por
   cuota: no confundir el bloqueo con una revisión favorable ni repetir P2 ya
   resueltos sin regresión demostrable. Revisar directamente el código mientras
   no esté disponible una nueva revisión externa.
2. Auditar antecesores y preparar un candidato coordinado; resolver acceso Vercel,
   comprobar revisiones servidas y entorno de API antes de mover aliases. La
   publicación incluye prueba autenticada de QA con instrumentos desechables y
   comprobación posterior, no sólo build.
3. Continuar autoservicio por organización sobre lo existente: perfil, rubro,
   módulos e integraciones. Cerrar plantillas con sesión autorizada y proveedor
   correcto antes de afirmar aprobación Meta, número conectado o entrega real.

No usar encuestas de clientes como prueba destructiva. No modificar bases,
números, callbacks, planes o contratos institucionales como efecto de este registro.
La demo de Agente Conversa no se convierte por documentación en CRM gubernamental
completo ni en WhatsApp aprobado.

## Referencias

- Backend: https://github.com/inguillen87/chatbot-backend/pull/2794
- Frontend: https://github.com/inguillen87/chatboc-frontend/pull/1761
- Solicitud de revisión: https://github.com/inguillen87/chatboc-frontend/pull/1761#issuecomment-5783756299
- Respuesta de cuota: https://github.com/inguillen87/chatboc-frontend/pull/1761#issuecomment-5783758332
- Aceptación integrada: https://github.com/inguillen87/chatbot-backend/actions/runs/35676839086
- CI frontend: https://github.com/inguillen87/chatboc-frontend/actions/runs/35676754880
