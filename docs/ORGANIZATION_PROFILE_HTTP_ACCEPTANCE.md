# SS-PROFILE-ACCEPTANCE: aplicación completa y sesiones independientes

Base: backend b829c0b2 / frontend 852a506. Este corte no promueve QA ni producción.

## Qué se verifica ahora

La suite anterior ejercía servicios, cuerpos de handlers y PostgreSQL con una
adaptación sintética de autorización. Este corte agrega el create_app completo,
modelos reales, verificación de contraseña, sesiones, middleware de tenant,
decoradores originales y handlers publicados, mediante HTTP sobre loopback.
No sustituye el autenticador ni genera manualmente tokens para saltar el login.

Diez escenarios: contraseña correcta/incorrecta y anónimo; guardar y releer desde
otra sesión; conflicto entre administradores; lectura/escritura entre tenants;
empleado de consulta; grant tenant_admin y revocación con la misma sesión;
coincidencia entre /api/me y config; campos protegidos; tenant inactivo; bloqueo
de escritores. Las respuestas positivas se contrastan con una lectura posterior.

Las cuentas son inventadas y su clave se genera en cada ejecución. Sólo se crea
una SQLite temporal nueva. Se descarta al terminar. No hay acceso a Neon, Render,
Meta, Twilio, correo ni identidades reales. El proceso elimina variables heredadas,
no lee .env y bloquea red externa. Esto no reemplaza aislamiento a nivel de sistema
operativo y no debe emplearse como sandbox para ejecutar código no confiable.

## Corrección de la experiencia

El contrato opcional editability distingue permiso faltante y mantenimiento,
sin alterar el hash de contenido. can_edit sigue siendo la capacidad efectiva.
Los handlers pasan permiso y writer fence separados; el fence sigue bloqueando
mutaciones aun con una sesión previamente autenticada.

## Ejecución reproducible

Desde el backend, con requirements.txt instalado en un entorno virtual aislado:

```sh
python -m tests.profile_http_acceptance
python -m tests.run_profile_browser --frontend /ruta/al/frontend-coordinado
```

La segunda orden requiere node_modules y Chromium del frontend. Inicia la SPA
real con proxy al backend temporal. Cinco contextos de navegador pasan por el
formulario normal de ingreso; no hay respuestas de API sustituidas. El recorrido
verifica conflicto, elección explícita, guardado posterior, recarga, administrador
delegado, otro tenant y empleado sin permiso. Se capturan 1440/820/390 oscuro/320.
El proceso del navegador recibe únicamente cuentas sintéticas efímeras; no se
versionan cookies, claves, archivos de sesión ni capturas de clientes.

## Límites de evidencia

CI del backend ejecuta los diez escenarios HTTP y, en un trabajo separado, los
62 casos focales con PostgreSQL desechable, incluidos seis concurrentes. Los
resultados de cada commit se registran en su PR: esta descripción no presupone
que una ejecución futura esté aprobada. El recorrido SPA coordinado se ejecuta
localmente; el workflow frontend mantiene sus pruebas de componentes y fixtures.

Sigue pendiente la aceptación contra el QA desplegado, su configuración de
cookies/proxy y una sesión institucional autorizada. Este corte no certifica
Clerk, MFA, entrega de correo, WhatsApp real, dispositivos físicos o PWA instalada.
No soluciona el 503 de arranque en frío. No libera el bloqueo de escritores del
candidato Vercel, cambia alias estables ni conecta los horarios a agentes.
