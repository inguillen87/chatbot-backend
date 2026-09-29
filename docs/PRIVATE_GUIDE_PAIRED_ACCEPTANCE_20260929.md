# Guía privada: aceptación integrada de panel y servidor

Base backend: `7acdd7a06e0b2d48805ebf016aa1e494974db66d` (#2803).
Frontend fijado: `60fdccd825b4279698d85d54519ce8790aba15bf` (#1798).
El manifest `tests/guide_pair_frontend.json` y el checkout de CI fijan el mismo SHA; el runner comprueba la coincidencia antes de arrancar.

## Brecha que cubre

Los ensayos anteriores probaban el navegador con HTTP sintético y el backend por separado. Esta batería usa los componentes originales ChannelActivationChecklist, PrivateGuideControl, PrivateConversationGuide y apiFetch contra la aplicación Flask completa, con su parser, autorización, servicio, modelos y commits reales. Ninguna respuesta de guía o configuración se fabrica en Playwright.

La sesión externa es un límite de pruebas explícito: tres proxies locales separados representan al SuperAdmin, administrador institucional y administrador de otra organización. Cada uno envía una sesión firmada para usuarios nuevos de una base SQLite temporal, generada con la clave efímera del runtime. La validación y autorización del servidor no se sustituyen. El escenario no acredita una autenticación externa de Clerk, el login productivo de Analía ni la SPA completa; la envoltura del componente es un fixture que obtiene su identidad de /auth/me real.

Se usan puertos exclusivos en loopback y orígenes permitidos explícitos. El backend descarta entornos del usuario, no carga .env ni alcanza redes externas. El navegador bloquea solicitudes fuera de los tres orígenes locales. No se leen credenciales, cuentas o bases de producción. Las sesiones temporales no se escriben en los informes ni en Git.

## Recorrido y comprobaciones

A 1440, 390 oscuro y 320 px: estado inicialmente deshabilitado, revisión y acuse obligatorios, cancelación sin escritura, doble clic con un único PUT, habilitación confirmada por lectura del servidor y acceso del administrador institucional. La versión de escritorio recorre los 29 nodos originales usando únicamente sus opciones publicadas; los móviles prueban inicio, menú y documentación. En cada respuesta se comprueban identidad, hash de la guía y referencia de fuente.

Se intenta una repetición manual de la revisión antigua (412), lectura desde otra organización (403), escritura desde administración institucional sin rol de plataforma (403) y origen web no permitido (403). Luego se revoca desde el panel; la sesión institucional que ya tenía el lector abierto pierde la siguiente lectura y el contenido desaparece. La recarga confirma que la entrada ya no está disponible.

El proceso Python comprueba al final seis eventos de auditoría (habilitar y revocar por tamaño), versiones 1 a 6, estado final deshabilitado, cuenta/actor/tenant correctos y conservación de las demás configuraciones, identidades, contraseñas, casos, respuestas y bytes de la fuente. No se afirma que eso valide la vigencia institucional del documento.

## Ejecución y alcance de cierre

Se añade un job al workflow existente de evidencias, conservando sin cambios las pruebas de PostgreSQL, guía, acceso y encuestas. No se crea un workflow de despliegue ni se aumentan sus permisos. Sólo se publican JSON de resultados/persistencia y capturas; no se sube el entorno temporal, bases, cabeceras o sesiones.

Comando: `python -m tests.run_private_guide_pair --frontend <checkout-del-SHA-fijado>` después de instalar las dependencias bloqueadas y Chromium. Las dependencias no se reinstalan en el equipo Windows con poco espacio; la ejecución completa se certifica en CI y la sintaxis se comprueba localmente. Los resultados efectivos, incidentes y revisiones se documentan en el PR.

No se publica código ni se habilita Tierra del Fuego. Continúan pendientes el acceso autorizado de despliegue, la publicación emparejada, la activación explícita productiva y el primer ingreso nominal. No se repite la operación de credenciales bloqueada ni se toca MuniControl.
