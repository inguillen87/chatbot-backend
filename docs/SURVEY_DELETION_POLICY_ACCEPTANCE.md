# Protección del borrado administrativo de encuestas

## Continuidad y alcance

Este corte aplica el parche preparado sobre f13cd73f6161fdd003edbc86bc6fc2506235e484.
Frontend coordinado consultado: PR #1761, ec63a2e1e1d5d1c2b2856b54d48dfe4e5804e196.
La ruta base se verificó contra el blob a5f014bf63019b27c615777232030ca4600bbb1d.
No se cambia la línea de selección de módulos ni se introducen clientes nuevos.

El servicio de borrado existente verifica tenant, gobernanza, materialización y
recibos institucionales. Este corte añade una precondición HTTP explícita de
borrador sin respuestas. Ocultar el botón no basta frente a solicitudes directas
o una pantalla desactualizada.

El guard en la fábrica compartida de siete rutas administrativas:
1. Verifica acceso antes de bloquear o consultar respuestas.
2. Usa el lock existente y vuelve a leer estado y organización.
3. Rechaza con 409 cualquier estado distinto de borrador.
4. Rechaza con 409 cualquier respuesta registrada, incluso sintética o inconsistente.
5. Mantiene las comprobaciones previas del servicio de eliminación.

El guard no borra ni confirma la transacción. La consulta de existencia de respuestas
lee un ID con límite, no carga contenido ni cuenta toda la tabla. Los mensajes y
códigos del rechazo proceden del backend, con instrucciones para releer/revisar.

Alcance exacto: entradas HTTP administrativas compartidas. No se reescribe el
servicio interno delete_encuesta ni se afirma que un futuro caller directo esté
cubierto. No existe excepción silenciosa para borrar respuestas de prueba.

## Evidencia y estado

Se repitieron localmente las 17 pruebas focales sobre el guard, SQLite/SQLAlchemy
reales y adaptadores sintéticos de permisos/lock. La integración del handler se
verifica también por AST. Son pruebas distintas de create_app, no una certificación
de concurrencia PostgreSQL ni de cuentas institucionales.

Las 12 pruebas HTTP usan create_app, login/cookies/modelos/middleware originales,
SQLite y cuentas desechables, sin .env ni red externa. Cubren lectura por tenant,
cierre/conservación, borrado permitido, siete alias, denegaciones y fallo de lectura
posterior a un borrado confirmado. Los controles de fallos sólo existen en el runner.
El workflow ejecuta ambas suites en procesos separados con dependencias existentes.
El resultado final de la CI se registrará en el PR; no se infiere de este documento.

No se ha desplegado ni promovido producción al abrir esta revisión. No hay cambios
de esquema/dependencias, escrituras sobre encuestas de clientes, números de WhatsApp,
planes o callbacks. La aceptación del navegador contra el panel completo es una
fase posterior y no debe darse por aprobada por estos tests HTTP.
