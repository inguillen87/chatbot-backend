# CRM: tareas independientes y registro transaccional

Base: `9d978fe48bd9f62dd2263dbd7c98861110b6a0e7`, rama de release existente. Se recuperó el trabajo local previo en una copia separada, con manifiesto SHA-256. Los worktrees originales de encuestas y tareas permanecen intactos.

## Contrato implementado
`crm.tasks.v1`: varias tareas por contacto, con título, descripción, responsable elegible de la organización, vencimiento con zona horaria, prioridad y estado. Estados: pendiente, en curso, completada y cancelada. Reabrir es una transición explícita; una tarea cerrada no permite editar sus campos hasta reabrirla.
Las tareas son registros propios, no un reemplazo de `owner_notes` o `next_action_at`. No alteran el contacto, consentimiento, pedido o reclamo. El vencimiento no envía mensajes ni crea eventos externos.

Rutas autenticadas bajo `/api/admin/tenants/<slug>`:
- `GET /crm/tasks/capabilities` y `GET /crm/tasks/assignees`.
- `GET|POST /contacts/<contact_id>/tasks`.
- `GET|PATCH /contacts/<contact_id>/tasks/<task_id>`.
El listado pagina 50 tareas y el historial 20 eventos. Los conteos por estado se obtienen en una sola consulta agrupada y describen el contacto completo, no sólo la página cargada.

## Autorización y transacciones
Se conservan los decoradores originales de sesión y acceso CRM. El servicio valida nuevamente organización/contacto. Administradores y supervisores habilitados administran tareas; un empleado sólo puede avanzar las tareas asignadas a él y no editar sus campos administrativos. La API no permite asignar usuarios ajenos, inactivos o fuera de los roles admitidos.
Cada comando exige `Idempotency-Key`. La combinación organización/actor/clave es única; un reintento idéntico devuelve el evento original y un contenido distinto con la misma clave devuelve 409. El recibo incluye el hash de la clave y la versión del resultado. Una recuperación histórica no sustituye el estado actual de una tarea: el cliente vuelve a cargar el listado.
Las actualizaciones requieren `expected_revision`, que debe coincidir con el estado leído y con la cláusula atómica del UPDATE. Las escrituras concurrentes no pueden pisarse silenciosamente. Tarea y evento se confirman en una misma transacción; si falla la auditoría, se revierte el cambio.
`crm_task_event` tiene unicidad por revisión y triggers que rechazan UPDATE/DELETE ordinarios. No es almacenamiento WORM: administradores de base con privilegios suficientes podrían cambiar las protecciones.

## Esquema y activación
La migración está en `migrations/pending/20260924_crm_tasks_v1.py`, fuera del grafo activo aprobado. Alembic registra los modelos para no proponer su borrado por falta de metadatos. No se ejecutó DDL sobre bases de clientes.
`CRM_TASKS_ENABLED` permanece desactivado por defecto. Tener tablas con esos nombres no basta: se verifican columnas, unicidades y triggers antes de publicar disponibilidad. Si falta una protección o falla la inspección, el módulo permanece no disponible y no habilita escrituras. No existe reparación automática del esquema.

## Validación reproducible
`python -m tests.crm_tasks_http_acceptance --frontend <checkout-frontend> --evidence <directorio>` ejecuta la aplicación, autenticación, migración y rutas originales contra SQLite desechable, con red externa bloqueada. En el cierre local aprobaron **22 pruebas**, incluidos tres recorridos Chromium de interfaz real (1440/390/320), persistencia, historial, asignación, permisos y ausencia de errores graves de accesibilidad en los controles evaluados. Los datos y cuentas son sintéticos.
Se reprodujo una carrera donde una revisión futura podía validar una transición contra el estado anterior y luego ganar el UPDATE. La prueba falló antes y pasó después de exigir coincidencia con la revisión cargada, además de la condición atómica.
`tests/crm_tasks_postgres_acceptance.py` se ejecuta únicamente contra `chatboc_tasks_qa` en loopback. Usa servicio/modelos/migración reales con tablas mínimas de cuentas y contactos: valida transacciones y triggers, no login ni autenticación HTTP. El workflow adjunto proporciona PostgreSQL 16 desechable. Su resultado debe verificarse por SHA en el PR; no se hereda la evidencia SQLite como prueba de PostgreSQL.

## Checklist previo a activación
1. Revisar la pareja de PR, la versión backend realmente desplegada y el corte de migraciones. Mantener el flag desactivado durante el proceso.
2. Validar la propuesta en staging PostgreSQL y promoverla al grafo Alembic con el padre aprobado para ese corte; no ejecutar directamente el archivo pendiente en producción.
3. Revisar retención/archivo: las referencias y el historial protegido pueden impedir la purga de contactos, usuarios u organizaciones con tareas. El downgrade automático se bloquea para evitar pérdida de auditoría; rollback de aplicación no equivale a borrar el historial.
4. Publicar backend compatible, verificar esquema/permisos, publicar frontend coordinado y activar expresamente el flag. Comprobar alta, reasignación, cierre y replay con cuentas de prueba aisladas.
5. Ante problemas, desactivar el módulo preservando tablas e historial y volver a la versión compatible anterior.

No se modifica el despliegue productivo ni se fusionan otras ramas de encuestas/Tierra del Fuego en este lote. El mecanismo de idempotencia sólo protege comandos que conservan la misma clave; cerrar el navegador y recrear una operación con otra clave no constituye un reintento deduplicable. El frontend conserva la clave durante la sesión abierta y advierte antes de descartarla.
