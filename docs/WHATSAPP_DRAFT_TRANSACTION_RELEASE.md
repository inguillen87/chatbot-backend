# Borradores WhatsApp: transaccion y recuperacion

Base backend: 8b54e0114856e1ff21815a60f247bad8041d4ad4.
Frontend coordinado: PR #1744, 74e58fb173b9e1b7089e5aa09f7e012c61a6504f.

La recuperacion buscaba solamente los ultimos 20 recibos de cada plantilla.
Ahora consulta primero los eventos de auditoria existentes y mantiene compatibilidad
con los recibos anteriores. Los registros nuevos almacenan el hash de la clave de
operacion. La retencion de auditoria no cambia; la garantia depende de conservarla.

La fila de organizacion se bloquea durante cada operacion, se verifica que continue
activa y se vuelve a comprobar el permiso. Las filas y su auditoria se confirman en
una transaccion. Los fallos de escritura o construccion de respuesta revierten los
cambios. El cliente mantiene la identidad ante un resultado no confirmado.

No se agregan migraciones ni llamadas a proveedores. No se modifica autenticacion,
planes, numeros, callbacks, datos de clientes o configuracion de otros proyectos.

## Pruebas y alcance

El arnes ejecuta los cuerpos reales del handler y las clases reales de borrador y
auditoria extraidos del codigo fuente. SQLAlchemy, Flask, consultas, restricciones,
flush, commit y rollback son reales. Tenant/usuario y el adaptador de autorizacion
son fixtures: no certifica login, segundo factor o middleware de la aplicacion.

Local: 15 escenarios sobre SQLite; cuatro escenarios de concurrencia no se ejecutan
alli. CI usa PostgreSQL 18 desechable para los 19 escenarios, incluyendo dos peticiones
simultaneas: misma clave, distintas claves, distinto paquete y organizaciones distintas.
El arnes no acepta una URL arbitraria de base de datos: el destino PostgreSQL es
127.0.0.1, base chatboc_draft_regression, del servicio temporal de CI.

Tambien cubre fallos de flush/commit/serializacion, recibos anteriores, borradores
eliminados o alterados, claves de otro paquete y reintentos tras 24 operaciones.
Los resultados remotos se registran en el PR despues de finalizar su ejecucion.

## Publicacion

Sin publicacion de backend ni promocion productiva en este corte. La aceptacion
integrada con una sesion autorizada de QA sigue siendo un gate independiente.
La conexion del proveedor, sus aprobaciones, el envio y la entrega se validan aparte.
El frontend recuperado se publica en QA con el procedimiento de rutas existente,
no en chatboc.ar ni en el alias del piloto Conversa.

Referencia de bloqueo: https://www.postgresql.org/docs/current/explicit-locking.html
Referencia de aislamiento: https://www.postgresql.org/docs/current/transaction-iso.html
