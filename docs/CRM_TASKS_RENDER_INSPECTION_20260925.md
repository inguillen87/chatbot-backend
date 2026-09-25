# Tareas CRM: inspección del runtime Render y PostgreSQL

## Identidad verificada
Se usó el workspace autorizado `tea-csuhvjdumphs739ipi40` (Marcelo's workspace).
El servicio productivo es `srv-d0rq2rp5pdvs738t3bhg`, repositorio `inguillen87/chatbot-backend`, con auto-deploy desactivado. Su deployment vivo `dep-dag8ijpt0dsc73ecdfug` publica `912446bf96f8330664a9dec009ae57dbf935c73c`.
La rama configurada es `main`, pero su revisión actual no coincide con la revisión viva. No debe usarse un deploy genérico de latest: la publicación coordinada debe fijar el SHA exacto aprobado.

Se confirmó mediante dos vías la conexión de producción:
1. Consulta del conector Render dentro de una transacción de sólo lectura.
2. Job `job-daqrm06gekts739hrd7g` ejecutado desde el propio servicio con su conexión runtime, sin importar la aplicación ni escribir en la base.
Ambas devolvieron la base `chatboc_postgres_lw1n`, rol runtime `chatboc_postgres_user`, PostgreSQL **18.3** y revisión Alembic **20260820_survey_content_jurisdiction_v1**. El job confirmó además el SHA vivo y que el flag de tareas no está configurado.
El recurso de base correspondiente es `dpg-d4lusfali9vc73egvnq0-a`. No existen todavía `crm_task` ni `crm_task_event`.

## Entorno de ensayo
La base Render de staging `dpg-da69dugn74is739jvpl0-a` está suspendida por billing después de su expiración declarada del 23/09. No se reactivó, no se cambió de plan y no se consideró apta para validar la migración.
La validación PostgreSQL pasa a ejecutar dos versiones desechables independientes: **16 y 18**. El informe registra la versión real del servidor de cada ejecución. Los servicios/modelos/migración de tareas son reales; las tablas padre son fixtures y no contienen datos de clientes.
La aceptación SQLite/Chromium previa permanece separada de esta comprobación, y no se suma de nuevo como evidencia de una ejecución que no ocurrió.

## Estado de la entrega
No se aplicó la migración, no se cambiaron flags, no se modificó la rama configurada en Render y no se publicó un nuevo deployment. El archivo de migración sigue en `migrations/pending`.
La preparación de la promoción de ese archivo fue bloqueada por el control de seguridad de la herramienta. No se volvió a ejecutar mediante otro mecanismo. Se requiere confirmar expresamente el cambio de esquema y el alcance de la activación antes de continuar esa operación.
La autorización de la CLI oficial se renovó por el flujo de login estándar, sin copiar credenciales a código ni exponerlas en reportes. La CLI fue obtenida del repositorio oficial `render-oss/cli`, versión 2.28.0; el archivo descargado se verificó contra su digest SHA-256 publicado.

La primera matriz flotante aprobó en PostgreSQL 16.15 y 18.6. Para no confundir ese ensayo con la versión productiva 18.3, se fijaron los jobs a **16.15 y 18.3** y se exige que el servidor del test reporte exactamente la versión configurada. La evidencia final del PR corresponde a esta matriz fijada; las ejecuciones previas no se suman como casos nuevos.
