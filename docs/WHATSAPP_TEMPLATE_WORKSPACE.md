# Biblioteca de plantillas para operadores

Continúa la línea de módulos/configuración #2790; no duplica su implementación
ni modifica la rama productiva de recuperación #2791. Frontend coordinado #1759.

Cambio runtime: se añade frontend_contract.workspace_ui al catálogo existente con
textos de búsqueda, filtro, contador, estado vacío y confirmación. El registro se
copia por respuesta. No contiene permisos, credenciales o nombres de clientes.
El contenido de las15plantillas existentes y sus estados predeterminados se
protege con un hash de regresión. No hay nuevas llamadas a proveedores, rutas,
acciones del agente, variables de entorno o migraciones.

El frontend consume ese contrato para buscar, filtrar y confirmar creación local.
La acción sigue usando el endpoint original y su clave de idempotencia; afecta
el conjunto completo, no sólo los resultados visibles. La advertencia se publica
desde backend. Una creación no equivale a aprobación Meta o entrega de mensajes.

Diez pruebas focales nuevas protegen compatibilidad, copia independiente,
placeholders, textos y ausencia de cambios en la definición de los conjuntos.
La CI existente mantiene las transacciones PostgreSQL y las21pruebas HTTP de
configuración. Se añade un recorrido de la biblioteca real contra create_app,
login y cookies originales en SQLite/cuentas desechables: buscar, cancelar,
confirmar, persistir5borradores, recargar y releer desde otra sesión. Cuatro anchos
verifican filtros estables y geometría. No hay mocks de respuestas de API/auth.

La red externa se bloquea en el helper existente y el navegador de aceptación.
No son clientes, hardware real, PWA instalada o aprobación del proveedor. Capturas
generadas en CI no implican revisión manual hasta poder abrirlas. Los resultados
finales se registran en los PR; las pruebas escritas no se cuentan como aprobadas.

Publicación pendiente de acceso autorizado a Vercel. No se cambia WhatsApp de
Junín/TDF, callbacks, planes, cuentas o bases de Render/Neon. La rama nueva es el
siguiente sprint sobre #2790, no una variante incompatible del selector anterior.
