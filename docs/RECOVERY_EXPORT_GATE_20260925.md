# Render → Neon: verificación del archivo antes de restaurar

## Estado operativo observado
El destino sigue siendo Vercel + Neon. Render conserva temporalmente la API y la base de origen; este lote no lo amplía ni cambia el escritor productivo.
Se comprobó la base `chatboc_recovery_20260925` en la rama de ensayo `br-wandering-term-acqwmdro`, proyecto Neon `nameless-rain-94060889`, usando un compute nuevo de sólo lectura. PostgreSQL respondió 18.6, transacción read-only, `pg_is_in_recovery=true`, cero tablas públicas, cero relaciones de usuario, cero large objects y cero extensiones adicionales.
El compute temporal `ep-shy-rain-act0arru` fue retirado al terminar. El endpoint de escritura preexistente `ep-falling-shape-act4vwsi` permanece deshabilitado. No se alteraron bases anteriores ni se borraron ramas o respaldos. La cuenta rechazó un ajuste de suspensión; se usó su configuración permitida sin cambiar de plan.
La exportación de Render solicitada en el tramo anterior sólo tiene evidencia HTTP 202 de aceptación. El archivo local asociado a esa respuesta está vacío: no es una copia de la base. No se descargó ni restauró la exportación productiva y no se verificó su finalización en este lote.

## Implementación nueva
`scripts/inspect_recovery_export.py` recibe un archivo local explícito, su SHA-256 esperado, el nombre de la base de origen, el ejecutable PostgreSQL 18 y un directorio nuevo. No descarga archivos, no obtiene credenciales, no usa URLs firmadas, no consulta proveedores y no se conecta a bases.
Antes de preparar el archivo:
- Rechaza archivos vacíos, checksum distinto y destinos existentes.
- Rechaza rutas absolutas, traversal, streams alternativos de Windows, enlaces, dispositivos, colisiones de mayúsculas y archivos inesperados.
- Limita cantidad, tamaño expandido y tamaño lógico, incluida la compresión anidada. Verifica que gzip termine correctamente y no acepte un archivo truncado como copia completa.
- Exige un único directorio PostgreSQL con `toc.dat` válido, datos presentes, base de origen y versión mayor esperadas. Comprueba el índice con `pg_restore --list`, sin conexión de base.
- Genera huellas por archivo y un informe acotado. El informe distingue validación de estructura, restauración ejecutada y paridad de filas: las dos últimas siguen siendo falsas en esta etapa.
El comando de restauración propuesto no incluye `--clean`, `--create`, desactivación de triggers ni borrado de objetos. Utiliza una transacción única y detención ante error. Sólo admite un nombre de base de recuperación; no ejecuta el comando ni considera verificada la conexión de destino.

## Límites de la verificación
Un SHA-256 vincula bytes a la huella que entrega el operador; no autentica por sí solo al proveedor ni certifica que el archivo contenga el último estado productivo. El índice y la integridad de compresión no demuestran que cada fila sea válida para su tipo: eso se prueba al restaurar.
Una copia PostgreSQL contiene SQL ejecutable. Debe proceder del origen confiable y mantenerse restringida; esta herramienta no convierte SQL arbitrario en seguro. Se admiten payloads sin compresión y gzip de formato directory; otras compresiones se rechazan explícitamente.
La preparación no implementa una ventana de mantenimiento, bloqueo global de escritores, continuidad WhatsApp, paridad definitiva ni autorización para cambiar `api.chatboc.ar`. No reemplaza el runbook de corte existente.
