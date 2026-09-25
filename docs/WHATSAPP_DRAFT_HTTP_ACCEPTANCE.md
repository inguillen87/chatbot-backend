# Borradores de WhatsApp: aceptación HTTP y panel real

Base backend f13cd73f (#2790); frontend fijado a b0c3ba03 (#1760), cambio focal
sobre la línea canónica de módulos. No se reemplaza el trabajo de selección
asistida ni la recuperación de conexión. Este corte agrega pruebas/CI/documentación,
no un runtime paralelo, tablas, permisos o integraciones productivas.

Trece casos levantan create_app completo con el helper de aceptación existente:
credenciales, cookies, roles, aislamiento y handlers son originales. SQLite y las
cuentas se crean exclusivamente en un directorio temporal. Se limpia el entorno,
no se leen .env y se impiden conexiones Python externas. La limpieza de cada test
sólo afecta registros de las organizaciones creadas por ese mismo proceso.

Se comprueban creación y auditoría, relectura independiente, mismo intento sin
duplicación, nueva operación que reutiliza borradores, conflicto de clave entre
verticales, aislamiento de clave por tenant, autorización, versión/campos inválidos,
organización inactiva, writer fence y recuperación del recibo desde auditoría
tras superar la ventana de 20 recibos en fila. Un borrador eliminado invalida
el recibo anterior sin recrearlo silenciosamente.

El runner de navegador inicia sesión por la UI de la SPA, navega a la ruta real
/perfil/plantillas-respuesta y verifica doble clic, un único POST, recibo, relectura
tras recarga y cuatro tamaños de pantalla. No simula respuestas API, autenticación
ni el panel. Se verifican por ORM los borradores locales y una sola auditoría.
Las capturas generadas no equivalen a revisión visual manual ni dispositivos reales.

Todos los registros son local_draft, sin referencias remotas ni permiso de envío.
No se contacta Meta/Twilio ni se certifica su aprobación/entrega. La prueba SQLite
no sustituye las regresiones concurrentes PostgreSQL de la suite ya existente.
Resultados definitivos se registran en el PR después de ejecutar la CI.

La revisión P2 corrigió un filtro de CI incompleto: el gate ahora incluye código
raíz de la app, rutas, servicios, utilidades, middleware, modelos, configuración,
migraciones y dependencias del runtime, además de los archivos de aceptación.
Los cambios exclusivamente documentales no disparan esta instalación completa.
Se fija el frontend final b0c3ba03, cuyo cambio frente a f62bdbda sólo ajusta el
control determinista de dos tests; el runtime de producción no cambia entre ambos.

No se ha publicado este corte en producción. Vercel continúa sin equipo accesible;
no se cambia de cuenta, permisos, capacidad ni callbacks para sortearlo. Las ramas
apiladas conservan el código existente y permiten revisar el delta del sprint.
