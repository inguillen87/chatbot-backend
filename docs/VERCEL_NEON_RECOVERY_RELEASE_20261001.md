# Cierre operativo: backend Vercel y base Neon

Base de código: `e3bee31c60e1e3318a853ac445b3ee4b0c657540`.
Se integran las correcciones institucionales del backend `cbef904e111e99703fcb99e5e0e00f6e9e3545d3`: botones normalizados, propietario heredado inequívoco, CORS de consultas públicas/confirmación administrativa y capacidad del selector comprobada al importar. No se agregan datos del cliente al repositorio.

## Ensayo que estaba pendiente

Se restauró el export identificado del 30/09 02:26 UTC en un PostgreSQL 18.6 temporal de loopback. Se aplicaron las 16 revisiones exactas verificadas por el plan local hasta `20260906_flask_sessions_v1`; todos los postchecks aprobaron y la transacción terminó correctamente.

Los registros de usuarios, organizaciones, configuración, encuestas, respuestas y auditoría se cotejaron antes/después sin cambios. La autoridad de escritura final quedó sin propietario, epoch 0 y ambos runtimes bloqueados. El cluster local se detuvo al finalizar. No se modificaron Render, Neon main o la base de recuperación con este ensayo.

Se creó separadamente `chatboc_candidate_20261001` en la rama de ensayo autorizada, sin sustituir `chatboc_recovery_20260930`. Su restauración y aplicación remota de la misma cadena tienen evidencia propia; la ejecución y sus resultados efectivos se registran en el PR, no se presumen a partir del ensayo local.

## Controles de despliegue

El candidato Vercel se publica sin dominios de clientes y con bloqueo de escrituras, efectos externos y sincronización automática de esquema. Revisión exacta antes/después del arranque, PostgreSQL/Redis comprobados y rutas administrativas protegidas. La base original recuperada queda intacta.

El objetivo de este corte es probar la versión completa contra el esquema y datos restaurados correctos, no promover la copia antigua de agosto. Se mantiene explícita la ventana posterior al export que no se pudo contrastar con Render. El contenido institucional conservado se debe reimportar por separado, con referencia y revisión exactas.

## Validación local del código

42 pruebas unitarias de contenido, empaquetado, arranque, staging y migraciones, y 22 HTTP de la aplicación y responder originales aprobaron. No son pruebas del proveedor de IA, WhatsApp nativo ni primer login de los usuarios nominales. El PR registra la identidad exacta del candidato, evidencia cloud y comprobaciones adicionales efectivas.

No se cambia el API público, se borra Render, se paga facturación, se cambian contraseñas o se actúa sobre MuniControl como parte de este corte. Antes de la conmutación definitiva deben pasar ingreso y aislamiento por cliente, archivos, mensajería, tareas programadas y un único escritor; un health 200 no es la aceptación completa.
