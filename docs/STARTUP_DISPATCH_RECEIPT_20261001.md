# Recibo explícito de arranque sin despacho

La validación real del operador completó su sesión y directorio en Vercel/Neon, pero exigió Reintentar ante 503 de arranque. El error SQL anterior está corregido; este corte describe exclusivamente el comienzo de una instancia nueva.

El límite WSGI añade `request_dispatched: false` al contrato `chatboc.bootstrap.v1`. Se emite únicamente en la rama que todavía no invoca la aplicación. Se mantienen `ok:false`, `status_code:503`, `reason_code`, `retryable`, `action_hint`, Retry-After y no-store. Una inicialización fallida sigue sin ser reintentable.

No se espera indefinidamente por una escritura ni se ejecuta su manejador después de haber contestado 503. Tampoco se cambia el presupuesto de arranque, schema, autoridad de sesión, CORS, permisos o contenido. La señal no convierte en idempotentes pedidos, pagos o mensajes.

Dos regresiones comprueban que el recibo precede al despacho para GET/POST/PUT/PATCH/DELETE y que la carga fallida no permite recuperación. El arranque terminado sólo ejecuta el manejador cuando llega una nueva solicitud. Junto con la batería existente aprobaron 13 pruebas locales.

El frontend acompañante usa esta evidencia explícita para una continuación acotada de lecturas y del intercambio Clerk en la misma URL e identidad; no reenvía escrituras de negocio ni solicitudes singleAttempt. Timeout de red, error genérico, falta de prueba de no ejecución, denegación o sesión retirada no autorizan continuidad automática.

El comportamiento coincide con el criterio de RFC 9110 sección 9.2.2: no repetir solicitudes no idempotentes sin conocer su semántica o que no se aplicaron. La implementación no transforma un 5xx genérico en esa prueba.

La publicación, los resultados del navegador y las limitaciones se registran por SHA en el PR. Los casos de usuario real no se infieren de los tests. No se cambia producción con este documento ni se toca MuniControl.
