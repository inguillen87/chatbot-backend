# Control administrativo de la guía privada de evaluación

Base: `ee92bb2af540d841ec2466238cf68b852be5c8c0` (#2799). Este cambio NO activa ninguna organización ni despliega el backend.

## Operación soportada

`GET /api/admin/tenants/<slug>/conversation-guide-control` devuelve `tenant.conversation_guide_control.v1`: identidad exacta, revisión, estado, referencia del artefacto instalado, capacidades, textos de revisión y cabecera requerida. Requiere administración verificada de la organización. Sólo el SuperAdmin autorizado por la política existente puede modificar el acceso; un administrador institucional puede consultar el estado, no autoaprobar una activación.

`PUT` en esa misma ruta recibe JSON y `X-Chatboc-Guide-Control: 1`. El comando es `tenant.conversation_guide_control_command.v1`, con `tenant: {id, slug}`, `expected_revision`, `enabled` booleano, `guide_id: accessible-support-evaluation` y `acknowledge_evaluation_only: true`. Al habilitar exige además `expected_guide_sha256` y `expected_source_sha256` obtenidos de la lectura actual. No acepta rutas/URLs de documentos ni otros identificadores de guía, campos adicionales, claves JSON duplicadas, ecos de ámbito contradictorios o revisiones omitidas.

La respuesta `tenant.conversation_guide_control_save.v1` contiene `saved`, identidad y nuevo contrato de control; no expone secretos, documentos ni datos de contacto. Un estado ya coincidente leído con revisión vigente devuelve `saved: false`, sin nueva auditoría. Repetir una revisión anterior tras un cambio devuelve 412: el operador debe releer, no reenviar automáticamente.

## Consistencia y revocación

La configuración se relee con bloqueo de fila y se vuelve a verificar actor/organización. Se conserva el resto de `TenantProfile.configuracion`. El registro existente `{enabled, guide_id}` no cambia de forma; una clave hermana `private_conversation_guide_version` conserva el contador para evitar que habilitar/deshabilitar vuelva a aceptar una revisión antigua. No requiere migración de esquema.

La auditoría `conversation_guide.enabled` / `conversation_guide.disabled` en AuditEvent y la configuración se confirman en la misma transacción. Una excepción de base de datos revierte ambos cambios. El bloqueo de escrituras de cutover se respeta antes de modificar y antes de confirmar; GET permanece consultable durante mantenimiento. Los errores devuelven códigos sin cuerpos internos.

Habilitar comprueba los bytes del artefacto mediante el cargador existente y coteja guía/fuente solicitadas antes del commit. Deshabilitar no necesita que el archivo de guía siga disponible: permite revocar acceso aun si se dañó o retiró. La lectura privada existente vuelve a comprobar la configuración, por lo que una sesión ya abierta recibe denegación en su próxima petición tras la revocación.

El estado de evaluación y la aprobación institucional siguen separados. Este control no aprueba documentos, no convierte la guía en RAG productivo, no crea trámites ni casos, no consulta registros, no llama a proveedores y no envía WhatsApp. No se modifican cuentas, contraseñas o permisos. No habilita TDF por nombre, plan, correo ni URL.

## Validación y límites

El nuevo ensayo HTTP usa Flask, autenticación, decoradores, modelos y SQLite descartable reales del proyecto, con redes externas bloqueadas por `profile_acceptance_runtime`. Cubre permisos, entradas inválidas, habilitación/revocación en la misma sesión, 29 nodos instalados, preservación de otra organización, revisión antigua, ausencia de doble auditoría y rollback. El ensayo PostgreSQL utiliza tablas mínimas del servicio en una base efímera exclusiva de CI: fuerza dos transacciones concurrentes y comprueba el bloqueo efectivo con `pg_blocking_pids`, una sola auditoría y 412 para el segundo editor. Esa prueba de concurrencia no reemplaza la aceptación HTTP con los modelos de la aplicación.

Se mantiene el workflow existente `Survey and organization evidence` y todas sus regresiones. Se agrega PostgreSQL 16 efímero, los dos ensayos y artifact. No se conectan bases del usuario, no se leen archivos .env ni se prueba con la cuenta de Analía. Las dependencias del proyecto no cambian.

Incidentes locales: el worktree compartido anterior tenía un árbol Git ausente; se utilizó un clone aislado de la misma revisión sin modificar sus archivos. El Python global no tenía las dependencias. El intento de instalar el requirements fijo en un entorno descartable terminó por falta de espacio en C:. Se eliminó únicamente ese entorno recién creado, no caches ni archivos del usuario. La compilación sintáctica de los archivos pasa; la certificación de ejecución corresponde al CI del SHA final y debe consultarse en el PR. No se presentan como aprobadas pruebas locales que no pudieron ejecutarse.

Referencias técnicas consultadas: SQLAlchemy 2.0 `populate_existing` / `with_for_update` (docs.sqlalchemy.org/en/20/orm/queryguide/api.html, docs.sqlalchemy.org/en/20/orm/session_api.html), y OWASP REST Security Cheat Sheet para autorización por endpoint y auditoría. El control coordina escritores que usan esta ruta; no garantiza aislamiento frente a código externo que reemplace toda la configuración sin usar su revisión.

## Cierre operativo pendiente

El frontend #1797 continúa siendo el lector; este endpoint aporta el control soportado que faltaba, no una activación remota. Para entregar TDF faltan su exposición en la administración, publicación coordinada, activación explícita de la guía instalada y prueba nominal de Analía. No se ha repetido el paso de credenciales bloqueado, creado otra organización, escrito directamente la base ni modificado MuniControl. Los resultados finales, commits y estado de despliegue se registran en el PR.


## Control del origen web

La inspecci?n del autenticador compartido encontr? una pol?tica CORS que puede reflejar or?genes cuando la instalaci?n usa comod?n. Esta ruta administrativa no conf?a en ese comod?n: con Origin exige el origen de la petici?n o una coincidencia expl?cita en CORS_ALLOWED_ORIGINS. Conserva Vary: Cookie, Authorization, Origin despu?s de la envoltura de autenticaci?n. Dos pruebas ejercitan origen desconocido, null y origen expl?cito autorizado; la cabecera de comando y JSON siguen siendo obligatorios. No se modific? la pol?tica global de CORS.

Ante un error de comunicaci?n durante commit, el resultado es no confirmado: debe consultarse de nuevo el estado antes de otra edici?n. La transacci?n y auditor?a permanecen juntas, pero no se afirma que un rollback pueda deshacer un commit que el servidor ya complet? y cuya respuesta se perdi?.

El descriptor de administraci?n se publica como conversation_guide_control en el bundle de configuraci?n y el contrato de activaci?n, s?lo para el SuperAdmin autorizado. No lee el archivo de gu?a al descubrirlo. El panel debe usar esta publicaci?n; no construir una URL ni habilitar acciones por correo o plan.


## Evidencia de CI antes del cierre

La inspecci?n de los archivos del primer artifact detect? que el indicador verde no era una certificaci?n v?lida: el pipe a tee ocultaba el c?digo de fallo de unittest. El ensayo HTTP adem?s intentaba autenticar al SuperAdmin con contrase?a local, una v?a que la aplicaci?n correctamente rechaza porque requiere Clerk. Se corrigi? el pipeline con shell bash y pipefail. La identidad externa del SuperAdmin se simula mediante el mismo formato de sesi?n firmada que usa la bater?a existente, s?lo dentro de la aplicaci?n descartable y con su clave de prueba; validaci?n de tokens, versi?n de sesi?n y autorizaci?n permanecen reales. Se agreg? una regresi?n que exige que la contrase?a local siga siendo rechazada para ese rol. Los administradores institucionales siguen pasando por el login HTTP real de prueba. No se valida aqu? el proveedor Clerk externo ni una cuenta real.

El archivo inicial mostr? 18 casos HTTP con 17 fallos y dos casos PostgreSQL aprobados. Se conserva como diagn?stico, no como aceptaci?n. La revisi?n corregida debe certificar todos los pasos con el c?digo de salida propagado y los archivos de resultados inspeccionados.
