# Tareas CRM: candidato sobre la API que realmente está publicada

## Evidencia de partida
La lectura pública de `https://api.chatboc.ar/api/version` devolvió `912446bf96f8330664a9dec009ae57dbf935c73c`. DNS mantiene `api.chatboc.ar` en el servicio Render existente. El frontend productivo permanece en `a7e2b3eb64917b2702d6bfd935d0581d5b8fafa0`.
La comparación Git entre esa API y el candidato previo `b6a476cd17916d5ed98242e021d912b5d946242e` mostró 9 commits exclusivos del runtime y 235 exclusivos de la rama candidata. Una promoción completa de esa rama no sería un despliegue limitado a tareas.
Este candidato parte exactamente de `912446bf`, conserva sus cambios y agrega sólo el módulo de tareas y su validación. No incorpora la rama de release de encuestas ni el traslado de proveedor Render→Vercel/Neon. El frontend compatible sigue siendo el PR #1776, SHA `24cb22be7f9cd714eab8a18bdc8f1913a180b91c`.

## Alcance del porte
Se conserva el contrato `crm.tasks.v1` del PR #2800: múltiples tareas por contacto, responsable elegible, prioridad, vencimiento, estados, revisión atómica, idempotencia y evento en la misma transacción. Los cambios en archivos productivos existentes se limitan a dos líneas para registrar la ruta y una para registrar metadatos de migración. No se reemplazan autenticación, CRM legacy, tickets, pagos, WhatsApp, dependencias ni configuración del servidor.
La aceptación ejecuta la aplicación y las rutas de esta base productiva, no las de la release adelantada. La utilidad de fixture importada sirve sólo al test y crea una base desechable; no es runtime de producción.

## Activación por organización
La activación ahora requiere ambas condiciones:
- `CRM_TASKS_ENABLED=true`: interruptor general.
- `CRM_TASKS_TENANT_IDS`: lista explícita de IDs internos de organizaciones, separados por comas.
Una lista vacía no activa ninguna organización. Comodines, listas malformadas, IDs inválidos o un valor truthy distinto del flag admitido no habilitan tareas. No se elige una organización por nombre, slug adivinado, querystring ni headers del cliente.
La API devuelve únicamente el resultado para la organización autorizada actual; no expone la lista global. Las organizaciones no seleccionadas ni siquiera realizan la inspección del esquema de tareas. Retirar un ID o desactivar el módulo bloquea nuevas peticiones sin borrar tareas ni historial.
No se configuraron IDs de Junín, Tierra del Fuego u otros clientes en este lote. La lista real exige una selección operativa explícita y verificada.

## Verificación del esquema
La comprobación anterior por nombre de trigger no bastaba: un trigger vacío con el mismo nombre podría aparentar disponibilidad. Ahora se contrastan definición, tabla, eventos, condición, habilitación y función de protección. En PostgreSQL también se rechazan funciones privilegiadas no esperadas, filtros por columnas, condiciones parciales y una inspección realizada en una conexión en modo réplica que omita las protecciones ordinarias.
La inspección es de sólo lectura: no repara tablas, no crea triggers y no cambia datos. No constituye una protección frente a un administrador de base con privilegios para modificar el esquema después de inspeccionarlo.

## Migración correcta para esta línea de código
El grafo de migraciones de la versión productiva tiene como único head `20260820_survey_content_jurisdiction_v1`. La migración de tareas previa apuntaba a `20260906_flask_sessions_v1`, que no existe en esta línea. Se corrigió su padre para este candidato, sin importar migraciones de otras releases.
El archivo continúa en `migrations/pending/20260924_crm_tasks_v1.py`: no se incorpora a ejecución automática ni se asume que el estado de la base sea idéntico al del repositorio. El `alembic_version` real aún debe contrastarse antes de promover la migración al corte aprobado.

## Preflight de sólo lectura
`python -m scripts.preflight_crm_tasks --output <reporte.json>` inspecciona el grafo de código sin abrir una conexión a base.
Para inspeccionar una base identificada por el operador, se requiere la variable explícita `CRM_TASKS_PREFLIGHT_DATABASE_URL`, más `--inspect-database --expected-database-host <host-verificado> --expected-database-revision <revision> --tenant-ids <ids>`.
La herramienta nunca lee automáticamente `DATABASE_URL` ni archivos .env. Comprueba el host esperado y ejecuta una transacción PostgreSQL de sólo lectura con timeout. Consulta revisión instalada, estructura de tareas y existencia de los IDs seleccionados, sin devolver contactos, credenciales ni direcciones. No ejecuta migraciones, no modifica flags y nunca emite una autorización de activación.
No se ejecutó esta herramienta contra la base de clientes durante la preparación. Un preflight offline correcto no certifica la base real.

## Validación de esta base
- 16 pruebas locales de política de activación y preflight, aprobadas.
- 29 pruebas de aplicación completa sobre SQLite desechable, cero fallidas/saltadas. Incluyen aislamiento entre organizaciones, revocación sin pérdida, comandos concurrentes, replay, rol de empleado, detección de un trigger falso y tres recorridos Chromium contra las rutas originales.
- El frontend probado es el SHA inalterado `24cb22be`. No se atribuyen nuevamente sus 3310 pruebas históricas como ejecución nueva de este sprint.
- El workflow PostgreSQL valida servicio, modelos, migración y guardas reales con tablas padre mínimas de prueba. La nueva ejecución y su resultado deben verificarse por el SHA del candidato; no se hereda el éxito del PR #2800.
Referencias técnicas consultadas: catálogos oficiales PostgreSQL 16 `pg_trigger` y `pg_proc`, y `src/include/catalog/pg_trigger.h` de `REL_16_STABLE` para las máscaras de eventos.

## Activación operativa pendiente
1. Verificar la configuración y el despliegue autorizado de Render, y la identidad exacta de su base. Conservar respaldo/recuperación y registrar el `alembic_version` real mediante preflight de lectura.
2. Revisar y promover sólo la migración de tareas sobre la revisión aprobada. Mantener el módulo apagado mientras se publica el backend.
3. Verificar el backend y frontend compatibles; seleccionar por ID una organización piloto autorizada, habilitar esa selección y comprobar alta/cierre/replay. No habilitar a todas las organizaciones por un flag global.
4. Ante problemas, retirar la selección o apagar el módulo sin borrar el historial. Su retención condiciona operaciones de purga y no admite downgrade destructivo automático.
No se modificaron cuentas, roles, números, canales, callbacks, dominios, credenciales o bases de clientes. No se enviaron mensajes ni se cambió el proveedor de la API.
