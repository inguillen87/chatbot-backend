# Registro metodologico por estudio

## Alcance implementado

La ficha se encuentra dentro de la analitica administrativa de cada encuesta.
Recoge 16 declaraciones agrupadas en estudio/responsables, marco/reclutamiento,
trabajo de campo y procesamiento/limitaciones. Se puede guardar informacion
parcial: lo no aportado queda pendiente, nunca generado o inferido por defecto.

Cada version conserva campos, motivo, fecha UTC, identificador del administrador,
revision del cuestionario y digest de la version anterior. Se conserva el historial;
consultar una version anterior NO restaura ni altera cuestionarios o respuestas.
La cobertura de campos documentados no es una puntuacion de representatividad.
Registrar un diseno probabilistico es una declaracion, no una verificacion de ese
diseno. Ningun campo activa ponderaciones ni autoriza precision inferencial.

## Datos y transacciones

Almacenamiento propio: survey_methodology_revision. No se introduce informacion
metodologica en recursos publicos de una encuesta, para evitar divulgar nombres,
procedimientos o fuentes desde endpoints abiertos. Lectura/escritura limitada a
administradores autorizados de la misma organizacion, con Cache-Control no-store.

La escritura exige expected_revision y expected_instrument_revision. El lock del
instrumento se toma antes de volver a comprobar estado, rol, organizacion y version.
Un editor desactualizado recibe409; el front conserva su edicion y exige recarga
explicita. Una repeticion identica inmediata recupera la version confirmada sin
duplicar filas ni auditorias. Los reintentos mas antiguos no se reutilizan como
permiso para sobrescribir: requieren leer la version actual.

La version y AuditEvent se confirman juntos; fallos de auditoria o construccion de
respuesta revierten la transaccion. El evento de auditoria no duplica textos de
campos ni direcciones IP. La cadena de digests detecta discrepancias locales, pero
NO es una firma digital ni almacenamiento inmutable frente a un administrador DB.
La inmutabilidad ofrecida por este modulo es de API: no hay rutas de update/delete
sobre versiones previas. La FK compuesta restringe referencias entre organizaciones
y la eliminacion del instrumento con historial, siempre que la DB aplique sus FK.

## API

Los siete alias administrativos existentes exponen:
- GET /<survey_id>/methodology: ficha actual, esquema de formulario e historial reciente.
- GET /<survey_id>/methodology?revision=N: lectura de una version previa.
- PUT /<survey_id>/methodology: crear una version con contrato surveys.methodology.write.v1.

El contrato de respuesta es surveys.methodology.v1. El PUT tiene limite32KiB, campos
cerrados, validacion de fechas y texto acotado. La UI usa una sola ruta canonica, no
repite escrituras por aliases ni considera un200 sin contexto/contenido coherente
como confirmacion valida. El historial visible esta acotado a10 entradas; versiones
anteriores siguen almacenadas y pueden consultarse por numero autorizado.

## Activacion: no ejecutar implicitamente en produccion

La propuesta de migracion es 20260922_methodology_v1, descendiente de
20260906_flask_sessions_v1. Permanece en migrations/pending, FUERA del grafo activo
de Alembic, hasta la revision explicita del siguiente rollout. Es aditiva y no
modifica respuestas de clientes. No se ejecuta mediante el upgrade habitual.
La prueba de migracion ejecuta upgrade/downgrade solo sobre SQLite en memoria.
No se ejecuto una migracion sobre Render/Neon por desarrollar este modulo.

SURVEY_METHODOLOGY_ENABLED permanece false por defecto. Primero verificar/ejecutar
la migracion en el entorno elegido, comprobar esquema y autorizacion, despues
habilitar esa variable en un rollout controlado. No habilitar DDL de inicio.
Con el modulo deshabilitado, la lectura autorizada devuelve available=false sin
consultar su tabla y no se permite guardar. Si se habilita sin la tabla valida,
se devuelve503 en lugar de crearla o simular persistencia. El writer fence y la
autoridad global de escritura siguen impidiendo cambios durante el cutover.

Para revertir UI/servicio, deshabilitar el modulo conservando el historial. La
migracion rechaza downgrade destructivo cuando hay versiones. No se debe eliminar
historial para hacer pasar una reversion. El codigo de release previo y su routing
productivo permanecen sujetos a los gates de compatibilidad y migracion originales.

## Aceptacion reproducible

- python -m unittest tests.test_survey_methodology_contract -v
- python -m unittest tests.test_survey_methodology_migration -v
- python -m tests.survey_methodology_http_acceptance
- python -m tests.run_methodology_browser --frontend <CHECKOUT_FRONTEND> --frontend-revision <SHA_EXACTO>

Las pruebas de HTTP usan la aplicacion, login, cookies, modelos, permisos y
transacciones originales sobre cuentas/SQLite desechables. El navegador completo
abre el formulario de ingreso, la pagina de analitica, guarda una ficha, recarga,
provoca un conflicto con otra solicitud, conserva el borrador y lo descarta solo
tras confirmacion. Verifica persistencia y auditorias, sin respuestas sinteticas
de API. Los anchos probados son1440/820/390oscuro/320; no dispositivos fisicos.
La cobertura aqui no certifica concurrencia PostgreSQL ni instalacion PWA.

Las pruebas de contrato/transporte/UI cubren campos ausentes, datos invalidos,
versiones cruzadas, respuesta tardia entre organizaciones, doble clic y perdida de
permisos. Los fixtures frontend provienen de la API real del entorno desechable,
no de informacion institucional de Junin/TDF u otras organizaciones.

## Relacion con referencias externas y siguiente fase

AAPOR Disclosure Standards sirve de referencia para registrar patrocinio/equipo,
poblacion, reclutamiento, modalidades/idiomas, fechas, procesamiento y limitaciones.
Esta implementacion es un soporte documental parcial; no implica membresia,
certificacion ni cumplimiento integral de esos estandares.
https://aapor.org/standards-and-ethics/disclosure-standards/

Qualtrics separa historial/versiones de la publicacion efectiva de un cuestionario.
Aqui se conserva esa distincion de control: guardar metodologia no publica,
restaura ni abre una encuesta. No se afirma paridad con sus productos completos.
https://www.qualtrics.com/support/survey-platform/survey-module/survey-publishing-versions/

Siguiente fase pendiente: disposicion de casos y denominadores de trabajo de campo,
revision de calidad y elegibilidad, despues analisis inferencial documentado y
exportaciones auditadas. La ficha actual no reemplaza esas capacidades.


## Continuidad y endurecimiento del 23/09/2026

Se recuperaron los archivos locales no publicados de la misma linea de release,
sin sobrescribir los worktrees de Work/Codex. Esta continuacion agrega cuatro
regresiones de revocacion de lectura en frontend:401/403 por boton de actualizar
como por revalidacion de cache. La denegacion retira todas las versiones privadas
cacheadas de ese estudio e impide refetch mientras esa vista siga revocada.
Antes del arreglo fallaban las dos variantes de revalidacion de cache; despues
pasaron los42 casos focales de contrato, transporte e interfaz.

La aceptacion HTTP a?ade recibos ligados al actor, permiso comprobado despues
del lock e historial alterado rechazado para lectura y nuevas versiones. El
cambio de rol se inyecta en un entorno desechable; no certifica una carrera real
entre transacciones PostgreSQL. El recorrido de navegador pulsa Enter realmente,
comprueba cancelacion sin perder borrador, dos intentos PUT de la pagina (uno
confirmado y uno409), botones de44px y ausencia de desbordamiento de viewport.

La evidencia final, SHA de la pareja y estado de publicacion se registran en los
PR existentes. No presentar estas pruebas locales/CI como habilitacion productiva
ni una certificacion metodologica externa.

## Evidencia local del cierre

Frontend exacto: 5307ed78eec15fc76a5432d54757389faf39a751.
Suite completa:3406 pruebas aprobadas en429 archivos; cero fallidas/pendientes.
Incluye42 pruebas de metodologia (22 contrato,7 transporte,13 interfaz); no
sumarlas de nuevo al total. TypeScript general/scope y build aprobados.
Backend:22 pruebas de contrato/migracion y23 HTTP completos aprobados. SQLite
desechable, login/modelos/autorizacion originales, sin credenciales de clientes.

Los cuatro recorridos Chromium del modulo pasaron en1440/820/390 oscuro/320.
Comprueban el SPA completo y Flask completo, sin respuestas de API simuladas:
guardado, recarga, conflicto409, borrador conservado, cancelacion y descarte por
teclado, sesion conservada, controles44px y cero desbordamiento horizontal.
La pagina emite dos PUT por caso: uno200 y uno409. La solicitud de edicion
competidora pertenece al contexto de pruebas. Persistencia final independiente:
dos versiones y dos auditorias por estudio; cuestionario borrador y cero
respuestas permanecen iguales. Se revisaron capturas desktop1440 y mobile390.

Estos resultados son ejecuciones locales verificadas, no resultados CI nuevos.
La ampliacion del workflow backend para metodologia no se aplico en este cierre;
la reproduccion esta disponible en los comandos documentados. Los workflows
existentes y la suite frontend conservan su estado independiente, por SHA.
La activacion productiva y la migracion remota NO se realizaron.

## Correccion del limite de migracion despues de la primera publicacion

El head f58be794 fallo en Migration runtime gate35816943322 con
local_migration_head_not_allowlisted: la nueva revision habia entrado en el
grafo activo de un cutover previamente aprobado. El control no se debilito.
La propuesta se traslado a migrations/pending/20260922_add_survey_methodology_v1.py.
El contenido de upgrade/downgrade es el mismo y sus pruebas lo ejecutan en SQLite.
Dos regresiones verifican que el grafo activo conserva el head revisado y que
promover la propuesta sin revisar el cutover sigue siendo rechazado.

Para activar: revisar la migracion y sus requisitos en PostgreSQL aislado;
promover el archivo a migrations/versions en una release coordinada; actualizar
el plan revisado, fingerprints y comprobaciones de esquema de forma conjunta;
obtener la aprobacion operativa y aplicar en el destino verificado antes de
habilitar SURVEY_METHODOLOGY_ENABLED. No existe un script alternativo de aplicacion
ni un auto-upgrade para saltar esos pasos. Mantener false hasta cerrar ese rollout.
Esta separacion conserva el historial y evita cambiar implicitamente el plan
de Render/Neon por desarrollar un formulario administrativo.

Validacion posterior a la separacion:24 pruebas de contrato/migracion (incluidas
las dos nuevas de frontera),12 del compilador territorial,48 regresiones pytest
de preflight/aplicacion de migraciones y23 HTTP de metodologia aprobadas. Las
pruebas de preflight usan dobles de DB y el aislamiento de red; no ejecutan la
migracion remota. Los48 casos son pytest; un intento con unittest descubrio cero
casos y NO se contabiliza como validacion. El intento local anterior con salida
cp1252 fallo antes de ejecutar pruebas; se repitio correctamente en UTF-8.
