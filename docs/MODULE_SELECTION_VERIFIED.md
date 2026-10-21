# Selección verificada de pasos por organización

Estado: implementación en una rama aislada para revisión; no habilitación productiva.
Base backend 03b21c46, frontend coordinado 4f8d0eba. Se conserva la línea de marca
existente; no se crea otra aplicación por cliente.

## Alcance funcional

El administrador de una organización Full elige qué preparar: WhatsApp/plantillas,
catálogo, cobros/pedidos, encuestas y, para gobiernos, territorio. Cobros requiere
catálogo. Los IDs, dependencias, textos y mensajes los publica el backend.
Una selección no activa ni desactiva permisos, canales, pasarelas o servicios
que ya funcionan. Personaliza las verificaciones opcionales del asistente v2;
identidad, atención, equipo, acceso y protección permanecen en la ruta.
Antes del primer guardado se conserva la ruta anterior por vertical.

GET del control existente entrega selección, catálogo, versión, revisión y motivo
de sólo lectura. PUT usa una operación exclusiva organization_modules sobre el
endpoint de configuración existente. Recomprueba pertenencia y plan Full en el
servidor, bloquea la fila de organización y confirma configuración/auditoría juntas.
Revisión antigua devuelve 412; payloads mixtos, IDs desconocidos y dependencias
incompletas no producen escrituras parciales. La personalización se guarda bajo
organization_setup_modules, excluida de la configuración pública.

No hay cambios de esquema, nuevas dependencias o llamadas a proveedores. El
mantenimiento conserva su bloqueo de escritura, y un downgrade no borra la selección.
Referencia de diseño: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html

## Revisión y coordinación

Se detectó edición simultánea en los worktrees backend-module-selection y
frontend-module-selection. Se preservó un checkpoint de código (sin .env ni
credenciales) en C:/Temp/chatboc-module-concurrent-checkpoint-20260920 y se continuó
sobre worktrees separados chatboc-modules-verified-backend/frontend. No se debe
mezclar automáticamente otra implementación de organization_setup_modules: hay
que revisar la integración antes de unir ramas. El registro persistido se alineó
con la forma catalog_version, version, selected del trabajo concurrente para no
introducir formatos incompatibles bajo la misma clave. El DTO público de edición
y sus recibos siguen requiriendo una única versión coordinada frontend/backend.
Ninguna de las dos se aplicó desde este sprint a datos de clientes.

## Evidencia y pendientes

15 escenarios focales nuevos cubren persistencia, no-op, revisión, dependencia,
plan/rol, rollback y una carrera PostgreSQL. Tres escenarios HTTP nuevos usan la
aplicación completa: guardar/releer y cambiar la ruta, rechazo Free/ajeno/empleado,
conflicto/dependencia y mantenimiento. La suite HTTP completa usa SQLite y cuentas
sintéticas; PostgreSQL se utiliza aparte para transacciones y concurrencia.
Los resultados finales se registran en el PR, no se infieren de esta descripción.

La prueba SPA usa login, backend y base desechables sin mocks de API. Comprueba
selección, dependencia, confirmación, persistencia tras recargar, repercusión en
el asistente y pantallas 1440/820/390 oscuro/320. No es usuario institucional
real, PWA física ni aceptación remota de una escritura. No completa WhatsApp,
la migración Render/Neon, dominios propios ni el problema de arranque 503.
